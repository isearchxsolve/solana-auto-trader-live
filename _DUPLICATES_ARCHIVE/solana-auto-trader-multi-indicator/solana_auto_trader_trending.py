#!/usr/bin/env python3
"""
Autonomous Solana DEX Trading Agent with Auto Trending Token Selection
Automatically discovers and trades trending Solana tokens
"""

import time
import argparse
from datetime import datetime
from typing import Dict, Optional, List
from solana_trading_agent import (
    SolanaTradingAgent, TradingMode, TOKENS, TradingAI
)
from trending_tokens import TrendingTokenDetector, AutoTokenSelector


class TrendingSolanaAutoTrader:
    """Autonomous trading engine with automatic trending token selection"""
    
    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent = agent
        self.auto_discover = auto_discover
        self.running = False
        self.last_trades = {}
        self.trade_cooldown = 300  # 5 minutes
        self.max_trades_per_day = 20
        self.trades_today = 0
        self.day_start = datetime.now().date()
        self.min_confidence = 0.60
        self.max_position_pct = 0.15
        self.stop_loss_pct = 0.03    # 3% — tight exit, break-even WR drops to ~46%
        self.take_profit_pct = 0.10  # 10% TP ceiling
        self.trailing_stop_pct = 0.03  # trail 3% below peak (once up 1.5%)
        self.position_peaks = {}
        self.sl_cooldowns = {}      # token → extended cooldown expiry after stop-loss
        self.sl_strikes   = {}      # token → number of stop-losses in this session
        self.sl_blacklist = set()   # tokens banned after 2 SLs — no more trading them
        self.last_trade_prices = {} # token → price at last trade exit (for crash detection)

        # Trending token detection
        if auto_discover:
            self.trend_detector = TrendingTokenDetector()
            self.token_selector = AutoTokenSelector(self.trend_detector)
            self.refresh_interval = 3600  # Refresh trending tokens every hour
            self.last_refresh = 0
    
    def refresh_trending_tokens(self):
        """Refresh the list of trending tokens"""
        if not self.auto_discover:
            return
        
        print("\n🔍 Discovering trending tokens...")
        
        try:
            # Get trending tokens
            trending = self.trend_detector.get_trending_tokens(
                min_liquidity=100000,
                min_volume_24h=200000,
                max_tokens=15
            )
            
            # Display trending
            self.trend_detector.display_trending(trending[:10])
            
            # Select top tokens for trading
            selected_tokens = self.token_selector.get_token_symbols()
            
            print("\n✅ AUTO-SELECTED TOKENS FOR TRADING:")
            print("-" * 60)
            
            # Build a lookup from symbol → full token_info (includes price, change, pair_addr)
            # selected_tokens only has symbol/address/score; we need trending list for full info
            trending_lookup = {t['symbol']: t for t in trending}

            # Update agent's watchlist
            new_tokens = []
            for idx, token in enumerate(selected_tokens, 1):
                symbol = token['symbol']
                score  = token['score']

                # Add to TOKENS dict if not already there
                if symbol not in TOKENS:
                    TOKENS[symbol] = token['address']

                # Add to watchlist with address so unknown tokens get registered
                STABLES = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}
                if symbol in STABLES:
                    continue  # Never add stablecoins to watchlist

                is_new = symbol not in self.agent.watched_tokens
                if is_new:
                    self.agent.add_token(symbol, token.get('address', ''))
                    new_tokens.append(symbol)

                    # ── Instant history pre-fill (GeckoTerminal → synthetic fallback) ──
                    full_info  = trending_lookup.get(symbol, {})
                    pair_addr  = full_info.get('pair_address', '') or token.get('pair_address', '')
                    print(f"  ⬇  Fetching history for {symbol}...")
                    hist = self.trend_detector.fetch_historical_prices(
                        pair_addr, n_points=1000, token_info=full_info
                    )
                    if hist:
                        self.agent.prefill_price_history(symbol, hist)
                    else:
                        print(f"  ⚠  History unavailable for {symbol} — will warm up live")
                    # ─────────────────────────────────────────────────────────────────

                print(f"  {idx}. {symbol:<12} (Score: {score:.1f}) {'🆕 NEW' if symbol in new_tokens else ''}")
            
            print("-" * 60)
            
            # Remove tokens no longer trending (keep for a while to avoid churn)
            current_symbols = {t['symbol'] for t in selected_tokens}
            for token in list(self.agent.watched_tokens):
                if token not in current_symbols and token not in ['SOL', 'USDC']:
                    # Only remove if it's been more than 2 hours
                    # This prevents constant churn
                    pass  # Keep old tokens for now
            
            self.last_refresh = time.time()
            
        except Exception as e:
            print(f"⚠️  Error refreshing trending tokens: {e}")
            print("   Continuing with existing watchlist...")
            # Update last_refresh even on error — otherwise last_refresh stays 0
            # and every subsequent cycle immediately retries, hammering DexScreener.
            # Use a 2-minute backoff before the next attempt.
            self.last_refresh = time.time() - self.refresh_interval + 120
    
    def should_trade(self, token: str, signal: dict) -> bool:
        """Determine if we should execute a trade"""
        # ── Bearish-trend buy block ───────────────────────────────────────────
        # Never buy when the trend is BEARISH (price below SMA20).
        # Lagged indicators (SMA crossover, MACD) can still show bullish signals
        # while price has already rolled over, producing contradictory BUY signals
        # in a downtrend. Every session loss traces back to exactly this.
        if signal['signal'] == 'BUY' and signal.get('trend') == 'BEARISH':
            print(f"  ⊗ Trend-block: BUY refused in BEARISH trend")
            return False

        # Check confidence
        if signal['confidence'] < self.min_confidence:
            print(f"  ⊗ Confidence too low: {signal['confidence']:.1%} < {self.min_confidence:.1%}")
            return False

        # ── Trend-confirmation veto ───────────────────────────────────────────
        # In a BULLISH trend, only allow a signal-SELL if momentum is actually
        # fading — i.e. MACD histogram is negative (falling below signal line).
        # Using the raw MACD line (EMA12-EMA26) as "bullish" was wrong:
        # a positive MACD line just means EMA12>EMA26 (trend direction), but
        # the histogram can be negative while the line is positive (momentum fading).
        # Selling on a fading histogram in a bullish trend IS the right time to exit.
        if signal['signal'] == 'SELL' and signal.get('trend') == 'BULLISH':
            rsi           = signal.get('rsi', 50)
            macd_hist     = signal.get('macd_histogram', 0)
            macd_momentum_bullish = macd_hist >= 0  # histogram ≥ 0 means MACD above signal line
            if rsi < 70 and macd_momentum_bullish:
                print(f"  ⊗ Trend-veto: SELL blocked in BULLISH trend "
                      f"(RSI:{rsi:.1f} < 70, MACD histogram:{macd_hist:+.5f} ≥ 0)")
                return False

        # ── Minimum-move guard ────────────────────────────────────────────────
        # When holding a position, only allow a signal-SELL if the P&L is
        # positive (> +0.5%). Negative P&L signal-SELLs are noise — let the
        # hard stop-loss handle those instead of signal-firing into a loss.
        if signal['signal'] == 'SELL' and token in self.agent.portfolio.positions:
            pos = self.agent.portfolio.positions[token]
            pnl_pct = pos.pnl_percentage
            if pnl_pct < 0.5:
                print(f"  ⊗ Signal-SELL blocked: P&L {pnl_pct:+.2f}% "
                      f"(let SL handle it)")
                return False

        # Check cooldown
        if token in self.last_trades:
            time_since_last = (datetime.now() - self.last_trades[token]).total_seconds()
            cooldown = self.sl_cooldowns.get(token, self.trade_cooldown)
            if time_since_last < cooldown:
                remaining = int(cooldown - time_since_last)
                print(f"  ⊗ Cooldown active: {remaining}s remaining")
                return False
            elif token in self.sl_cooldowns:
                # SL cooldown has expired — revert to normal cooldown
                del self.sl_cooldowns[token]
        
        # Check daily limit
        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start = datetime.now().date()
        
        if self.trades_today >= self.max_trades_per_day:
            print(f"  ⊗ Daily trade limit reached: {self.trades_today}/{self.max_trades_per_day}")
            return False
        
        # Check position limits
        if signal['signal'] == 'BUY':
            if token in self.agent.portfolio.positions:
                print(f"  ⊗ Already holding {token}")
                return False

            # ── Double-SL blacklist ───────────────────────────────────────────
            # After 2 stop-losses on the same token in a session, it has shown
            # it can't hold any entry point — permanently skip it this session.
            if token in self.sl_blacklist:
                print(f"  ⊗ Blacklisted: {token} has 2 SLs this session — skipping")
                return False

            # ── Post-crash re-entry block ─────────────────────────────────────
            # If the token has crashed >25% since our last exit, it's likely a
            # rug or catastrophic dump. RSI near 0-15 after a -80% move is NOT
            # a dip-buy opportunity — the coin is dead. Block re-entry until
            # price recovers at least 30% from the crash low.
            if token in self.last_trade_prices:
                last_price = self.last_trade_prices[token]
                current_price = signal.get('current_price', 0)
                if current_price > 0 and last_price > 0:
                    crash_pct = (current_price - last_price) / last_price * 100
                    if crash_pct < -25:
                        print(f"  ⊗ Crash-guard: {token} is {crash_pct:.1f}% below last exit "
                              f"(${last_price:.6f} → ${current_price:.6f}) — blocking re-entry")
                        return False

            total_value = self.agent.portfolio.get_total_value()
            max_position_value = total_value * self.max_position_pct
            if self.agent.portfolio.usdc_balance < max_position_value:
                print(f"  ⊗ Insufficient USDC for new position")
                return False
        
        if signal['signal'] == 'SELL' and token not in self.agent.portfolio.positions:
            print(f"  ⊗ No {token} position to sell")
            return False
        
        return True
    
    def execute_auto_trade(self, token: str, signal: dict):
        """Execute an automated trade"""
        current_price = signal.get('current_price', 0)
        side = signal['signal'].lower()
        
        if current_price == 0:
            print(f"  ✗ Invalid price for {token}")
            return
        
        # Calculate trade amount
        if side == 'buy':
            total_value = self.agent.portfolio.get_total_value()
            usdc_amount = min(
                total_value * self.max_position_pct,
                self.agent.portfolio.usdc_balance * 0.95
            )
            
            if usdc_amount < 10:
                print(f"  ✗ Trade amount too small: ${usdc_amount:.2f}")
                return
            
            print(f"\n  🤖 AUTO-BUY TRIGGERED")
            print(f"     Token: {token}")
            print(f"     USDC Amount: ${usdc_amount:.2f}")
            print(f"     Expected Price: ${current_price:.6f}")
            print(f"     Confidence: {signal['confidence']:.1%}")
            print(f"     Trend: {signal['trend']}")
            
            trade = self.agent.execute_swap("USDC", token, usdc_amount)
            
        else:  # sell
            position = self.agent.portfolio.positions[token]
            amount = position.amount
            
            print(f"\n  🤖 AUTO-SELL TRIGGERED")
            print(f"     Token: {token}")
            print(f"     Amount: {amount:.6f}")
            print(f"     Expected Price: ${current_price:.6f}")
            print(f"     Confidence: {signal['confidence']:.1%}")
            
            trade = self.agent.execute_swap(token, "USDC", amount)
        
        if trade:
            self.last_trades[token] = datetime.now()
            self.trades_today += 1
            if side == 'sell':
                self.last_trade_prices[token] = current_price
                self.position_peaks.pop(token, None)  # new entry must start fresh
            print(f"  ✓ Trade executed successfully!")
        else:
            print(f"  ✗ Trade execution failed")
    
    def analyze_and_trade(self, token: str):
        """Analyze a token and execute trade if conditions are met"""
        if token not in self.agent.price_history:
            return

        prices = self.agent.price_history[token]["prices"]

        # Visual feedback during warm-up phase
        if len(prices) < 50:
            print(f"  [⏳] {token:<12}: Warming up {len(prices)}/50")
            return

        # Get analysis
        analysis = self.agent.analyze_token(token)
        if not analysis:
            return

        # Print analysis for every token
        timestamp = datetime.now().strftime("%H:%M:%S")
        signal_icon = "🟢" if analysis['signal'] == 'BUY' else ("🔴" if analysis['signal'] == 'SELL' else "🟡")
        ob_flag = " ⚠️OB" if analysis.get('overbought') else (" ⚠️OS" if analysis.get('oversold') else "")
        print(f"  [{timestamp}] {token:<12} ${analysis['current_price']:<12.6f} "
              f"{signal_icon} {analysis['signal']:<5} ({analysis['confidence']:.0%}){ob_flag}  "
              f"Trend:{analysis['trend']}  BUY={analysis['buy_score']:.1f} SELL={analysis['sell_score']:.1f}")

        # Compact indicator line
        sk = analysis.get('stoch_k')
        wr = analysis.get('williams_r')
        cci_val = analysis.get('cci')
        stoch_str = f"Stoch:{sk:.0f}" if sk is not None else ""
        wr_str    = f"W%R:{wr:.0f}"   if wr is not None else ""
        cci_str   = f"CCI:{cci_val:.0f}" if cci_val is not None else ""
        obv_str   = {1: "OBV↑", -1: "OBV↓", 0: "OBV~"}.get(analysis['obv_slope'], "")
        bb_pos    = ("BB⚡LOW" if analysis['current_price'] <= analysis['bb_lower'] else
                     "BB⚡HIGH" if analysis['current_price'] >= analysis['bb_upper'] else "BB:mid")
        cloud_str = ("☀️cloud" if analysis['ichi_above_cloud'] else
                     "☁️cloud" if analysis['ichi_below_cloud'] else "⛅cloud")
        parts = [f"RSI:{analysis['rsi']:.1f}", stoch_str, wr_str, cci_str,
                 f"MACD:{analysis['macd_histogram']:+.5f}", obv_str, bb_pos, cloud_str]
        print(f"           " + "  ".join(p for p in parts if p))

        # Divergence — only print when detected (high signal)
        rsi_d  = analysis.get('rsi_divergence', 'NONE')
        macd_d = analysis.get('macd_divergence', 'NONE')
        if rsi_d != 'NONE' or macd_d != 'NONE':
            print(f"           🔀 RSI-DIV:{rsi_d}  MACD-DIV:{macd_d}")

        # Execute trade if signal is actionable
        if analysis['signal'] in ['BUY', 'SELL']:
            if self.should_trade(token, analysis):
                self.execute_auto_trade(token, analysis)

    def monitor_positions(self):
        """Monitor positions: trailing stop, hard stop, take profit."""
        TRAIL_TRIGGER = 1.5   # start trailing once up 1.5%
        TRAIL_DIST    = 3.0   # exit if it falls 3% below peak

        for token, position in list(self.agent.portfolio.positions.items()):
            pnl_pct = position.pnl_percentage

            # Track peak P&L for each position
            if token not in self.position_peaks:
                self.position_peaks[token] = pnl_pct
            elif pnl_pct > self.position_peaks[token]:
                self.position_peaks[token] = pnl_pct
            peak = self.position_peaks[token]

            # Hard stop loss
            if pnl_pct <= -self.stop_loss_pct * 100:
                print(f"\n  ⚠️  STOP LOSS: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.last_trade_prices[token] = position.current_price
                    self.position_peaks.pop(token, None)
                    # ── Post-SL extended cooldown ─────────────────────────────
                    self.sl_cooldowns[token] = 600   # 10 minutes
                    print(f"  🚫 Extended cooldown: {token} locked 10min after SL")
                    # ── Double-SL strike counter ──────────────────────────────
                    self.sl_strikes[token] = self.sl_strikes.get(token, 0) + 1
                    if self.sl_strikes[token] >= 2:
                        self.sl_blacklist.add(token)
                        print(f"  🛑 {token} BLACKLISTED — 2 stop-losses this session, no more trades")
                continue

            # Take profit ceiling
            if pnl_pct >= self.take_profit_pct * 100:
                print(f"\n  🎯 TAKE PROFIT: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.last_trade_prices[token] = position.current_price
                    self.position_peaks.pop(token, None)
                continue

            # Trailing stop — engage once up TRAIL_TRIGGER %
            if peak >= TRAIL_TRIGGER:
                trail_floor = peak - TRAIL_DIST
                if pnl_pct <= trail_floor:
                    print(f"\n  🔒 TRAIL STOP: {token}  "
                          f"Peak:{peak:+.2f}%  Now:{pnl_pct:+.2f}%  Floor:{trail_floor:+.2f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.last_trade_prices[token] = position.current_price
                        self.position_peaks.pop(token, None)
                else:
                    print(f"  📌 {token}  Peak:{peak:+.2f}%  Now:{pnl_pct:+.2f}%  "
                          f"Floor:{trail_floor:+.2f}% (trailing)")
    
    def run(self, update_interval: int = 30):
        """Run autonomous trading loop"""
        self.running = True
        print("\n" + "="*60)
        print("  🤖 SOLANA AUTO-TRENDING TRADER ACTIVATED")
        print("="*60)
        print(f"\nConfiguration:")
        print(f"  Mode: {self.agent.mode.value.upper()}")
        print(f"  DEX: Jupiter Aggregator")
        print(f"  Auto-Discovery: {'✅ ENABLED' if self.auto_discover else '❌ DISABLED'}")
        print(f"  Update Interval: {update_interval}s")
        print(f"  Min Confidence: {self.min_confidence:.0%}")
        print(f"  Trade Cooldown: {self.trade_cooldown}s")
        print(f"  Max Trades/Day: {self.max_trades_per_day}")
        print(f"  Max Position: {self.max_position_pct:.0%}")
        print(f"  Stop Loss: {self.stop_loss_pct:.0%}")
        print(f"  Take Profit: {self.take_profit_pct:.0%}")
        
        # Initial trending token discovery
        if self.auto_discover:
            self.refresh_trending_tokens()
        else:
            print(f"  Watched Tokens: {', '.join(self.agent.watched_tokens)}")
        
        print("\nPress Ctrl+C to stop\n")
        print("="*60)
        
        iteration = 0
        
        try:
            while self.running:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Cycle #{iteration} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Refresh trending tokens periodically
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_trending_tokens()
                
                # Update market data
                print("\n📊 Updating market data...")
                self.agent.update_market_data()
                
                # Monitor existing positions
                if self.agent.portfolio.positions:
                    print("\n👀 Monitoring positions...")
                    self.monitor_positions()
                
                # Analyze each token — skip stablecoins
                SKIP_TOKENS = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}
                print("\n🔍 Analyzing tokens...")
                for token in self.agent.watched_tokens:
                    if token not in SKIP_TOKENS:
                        self.analyze_and_trade(token)
                
                # Show portfolio summary
                print("\n💼 Portfolio Summary:")
                perf = self.agent.portfolio.get_performance()
                print(f"  USDC: ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total Value: ${perf['current_value']:.2f}")
                print(f"  Return: ${perf['total_return']:.2f} ({perf['total_return_pct']:.2f}%)")
                print(f"  Swaps Today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Active Tokens: {len(self.agent.watched_tokens)}")
                
                if self.agent.portfolio.positions:
                    print(f"\n  Open Positions:")
                    for token, pos in self.agent.portfolio.positions.items():
                        print(f"    {token}: {pos.amount:.6f} @ ${pos.avg_price_usdc:.4f} "
                              f"(P&L: {pos.pnl_percentage:+.2f}%)")
                
                # Wait before next cycle
                print(f"\n⏳ Next update in {update_interval}s...")
                if self.auto_discover:
                    next_refresh = int(self.refresh_interval - (time.time() - self.last_refresh))
                    print(f"   Next trending refresh in {next_refresh//60}min {next_refresh%60}s")
                
                time.sleep(update_interval)
                
        except KeyboardInterrupt:
            print("\n\n🛑 Stopping autonomous trading...")
            self.running = False
            
            # Final summary
            print("\n" + "="*60)
            print("  TRADING SESSION SUMMARY")
            print("="*60)
            self.agent.show_portfolio()
            print("Goodbye! 👋\n")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Autonomous Solana Trading with Auto-Trending Token Discovery"
    )
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                       help="Trading mode (default: paper)")
    parser.add_argument("--wallet", default="", help="Solana wallet address")
    parser.add_argument("--balance", type=float, default=1000.0,
                       help="Initial USDC balance for paper trading")
    parser.add_argument("--interval", type=int, default=30,
                       help="Update interval in seconds (default: 30)")
    parser.add_argument("--min-confidence", type=float, default=0.60,
                       help="Minimum confidence for trading (default: 0.60)")
    parser.add_argument("--no-auto-discover", action="store_true",
                       help="Disable automatic trending token discovery")
    parser.add_argument("--manual-tokens", nargs='+',
                       help="Manually specify tokens (if auto-discover disabled)")
    parser.add_argument("--refresh-interval", type=int, default=3600,
                       help="Trending token refresh interval in seconds (default: 3600)")
    
    args = parser.parse_args()
    
    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER
    auto_discover = not args.no_auto_discover
    
    # Create agent
    agent = SolanaTradingAgent(mode, args.wallet, args.balance)
    
    # Add manual tokens if specified
    if args.manual_tokens and not auto_discover:
        for token in args.manual_tokens:
            agent.add_token(token.upper())
    elif not auto_discover and not args.manual_tokens:
        # Default tokens if not auto-discovering
        for token in ["SOL", "JUP", "RAY"]:
            agent.add_token(token)
    
    # Always add USDC
    if "USDC" not in agent.watched_tokens:
        agent.add_token("USDC")
    
    # Collect initial data only if not auto-discovering
    # (auto-discover will add tokens and collect data automatically)
    if not auto_discover:
        print("\n📈 Collecting initial market data...")
        print("This will take about 30 seconds...\n")
        for i in range(30):
            agent.update_market_data()
            if (i + 1) % 5 == 0:
                print(f"  Progress: {i + 1}/30 data points")
            time.sleep(1)
        print("\n✓ Initial data collection complete!")
    
    # Create and run auto trader
    auto_trader = TrendingSolanaAutoTrader(agent, auto_discover)
    auto_trader.min_confidence = args.min_confidence
    auto_trader.refresh_interval = args.refresh_interval
    auto_trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()