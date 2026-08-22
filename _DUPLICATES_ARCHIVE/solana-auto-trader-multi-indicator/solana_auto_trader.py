#!/usr/bin/env python3
"""
Autonomous Solana DEX Trading Agent
Continuously monitors and trades Solana tokens automatically via Jupiter
"""

import time
import argparse
from datetime import datetime
from typing import Dict, Optional
from solana_trading_agent import (
    SolanaTradingAgent, TradingMode, TOKENS, TradingAI
)
from trending_tokens import TrendingTokenDetector


class SolanaAutoTrader:
    """Autonomous trading engine for Solana DEXs"""
    
    def __init__(self, agent: SolanaTradingAgent):
        self.agent = agent
        self.running = False
        self.last_trades = {}
        self.trade_cooldown = 300  # 5 minutes
        self.max_trades_per_day = 15
        self.trades_today = 0
        self.day_start = datetime.now().date()
        self.min_confidence = 0.70
        self.max_position_pct = 0.15  # 15% max per token
        self.stop_loss_pct = 0.03    # 3% SL — tight exit
        self.take_profit_pct = 0.10  # 10% TP ceiling
        self.trailing_stop_pct = 0.03
        self.position_peaks = {}
        
    def should_trade(self, token: str, signal: dict) -> bool:
        """Determine if we should execute a trade"""
        # Check confidence
        if signal['confidence'] < self.min_confidence:
            print(f"  âŠ— Confidence too low: {signal['confidence']:.1%} < {self.min_confidence:.1%}")
            return False
        
        # Check cooldown
        if token in self.last_trades:
            time_since_last = (datetime.now() - self.last_trades[token]).seconds
            if time_since_last < self.trade_cooldown:
                remaining = self.trade_cooldown - time_since_last
                print(f"  âŠ— Cooldown active: {remaining}s remaining")
                return False
        
        # Check daily limit
        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start = datetime.now().date()
        
        if self.trades_today >= self.max_trades_per_day:
            print(f"  âŠ— Daily trade limit reached: {self.trades_today}/{self.max_trades_per_day}")
            return False
        
        # Check position limits
        if signal['signal'] == 'BUY':
            if token in self.agent.portfolio.positions:
                print(f"  âŠ— Already holding {token}")
                return False
            
            # Check if we have enough USDC for position
            total_value = self.agent.portfolio.get_total_value()
            max_position_value = total_value * self.max_position_pct
            if self.agent.portfolio.usdc_balance < max_position_value:
                print(f"  âŠ— Insufficient USDC for new position")
                return False
        
        if signal['signal'] == 'SELL' and token not in self.agent.portfolio.positions:
            print(f"  âŠ— No {token} position to sell")
            return False
        
        return True
    
    def execute_auto_trade(self, token: str, signal: dict):
        """Execute an automated trade"""
        current_price = signal.get('current_price', 0)
        side = signal['signal'].lower()
        
        if current_price == 0:
            print(f"  âœ— Invalid price for {token}")
            return
        
        # Calculate trade amount
        if side == 'buy':
            # Use up to max_position_pct of portfolio value
            total_value = self.agent.portfolio.get_total_value()
            usdc_amount = min(
                total_value * self.max_position_pct,
                self.agent.portfolio.usdc_balance * 0.95  # Keep 5% buffer
            )
            
            if usdc_amount < 10:  # Minimum $10 trade
                print(f"  âœ— Trade amount too small: ${usdc_amount:.2f}")
                return
            
            print(f"\n  ðŸ¤– AUTO-BUY TRIGGERED")
            print(f"     Token: {token}")
            print(f"     USDC Amount: ${usdc_amount:.2f}")
            print(f"     Expected Price: ${current_price:.6f}")
            print(f"     Confidence: {signal['confidence']:.1%}")
            
            trade = self.agent.execute_swap("USDC", token, usdc_amount)
            
        else:  # sell
            # Sell entire position
            position = self.agent.portfolio.positions[token]
            amount = position.amount
            
            print(f"\n  ðŸ¤– AUTO-SELL TRIGGERED")
            print(f"     Token: {token}")
            print(f"     Amount: {amount:.6f}")
            print(f"     Expected Price: ${current_price:.6f}")
            print(f"     Confidence: {signal['confidence']:.1%}")
            
            trade = self.agent.execute_swap(token, "USDC", amount)
        
        if trade:
            self.last_trades[token] = datetime.now()
            self.trades_today += 1
            print(f"  âœ“ Trade executed successfully!")
        else:
            print(f"  âœ— Trade execution failed")
    
    def analyze_and_trade(self, token: str):
        """Analyze a token and execute trade if conditions are met"""
        if token not in self.agent.price_history:
            return
        
        prices = self.agent.price_history[token]["prices"]
        if len(prices) < 50:
            return
        
        # Get analysis
        analysis = self.agent.analyze_token(token)
        if not analysis:
            return
        
        # Print analysis
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"\n[{timestamp}] {token}")
        print(f"  Price: ${analysis['current_price']:.6f}  |  Trend: {analysis['trend']}  |  Momentum: {analysis['momentum']}")
        print(f"  Signal: {analysis['signal']} (Confidence: {analysis['confidence']:.1%})  |  BUY={analysis['buy_score']:.1f} SELL={analysis['sell_score']:.1f}")
        print(f"  RSI: {analysis['rsi']:.1f}  |  ", end="")
        sk = analysis.get('stoch_k')
        if sk is not None:
            print(f"Stoch %K: {sk:.1f}  |  ", end="")
        wr = analysis.get('williams_r')
        if wr is not None:
            print(f"W%R: {wr:.1f}  |  ", end="")
        cci_val = analysis.get('cci')
        if cci_val is not None:
            print(f"CCI: {cci_val:.1f}", end="")
        print()
        print(f"  MACD Hist: {analysis['macd_histogram']:+.6f}  |  VWAP: ${analysis['vwap']:.4f}  |  OBV: {['flat','↑ accum','↓ distrib'][analysis['obv_slope']]}")
        bb_pos = ("⚡LOWER" if analysis['current_price'] <= analysis['bb_lower'] else
                  "⚡UPPER" if analysis['current_price'] >= analysis['bb_upper'] else "inside")
        print(f"  BB: [{bb_pos}] upper=${analysis['bb_upper']:.4f} lower=${analysis['bb_lower']:.4f}  |  ATR%: {analysis['atr_pct']*100:.2f}%")
        rsi_d  = analysis.get('rsi_divergence', 'NONE')
        macd_d = analysis.get('macd_divergence', 'NONE')
        if rsi_d != 'NONE' or macd_d != 'NONE':
            print(f"  🔀 Divergence — RSI: {rsi_d}  MACD: {macd_d}")
        cloud = ("☀️ above cloud" if analysis['ichi_above_cloud'] else
                 "☁️ below cloud" if analysis['ichi_below_cloud'] else "in cloud")
        print(f"  Ichimoku: {cloud}  Tenkan=${analysis['ichi_tenkan']:.4f} Kijun=${analysis['ichi_kijun']:.4f}")
        
        # Check if we should trade
        if analysis['signal'] in ['BUY', 'SELL']:
            if self.should_trade(token, analysis):
                self.execute_auto_trade(token, analysis)
    
    def monitor_positions(self):
        """Monitor positions: trailing stop, hard stop, take profit."""
        TRAIL_TRIGGER = 1.5
        TRAIL_DIST    = 3.0

        for token, position in list(self.agent.portfolio.positions.items()):
            pnl_pct = position.pnl_percentage

            if token not in self.position_peaks:
                self.position_peaks[token] = pnl_pct
            elif pnl_pct > self.position_peaks[token]:
                self.position_peaks[token] = pnl_pct
            peak = self.position_peaks[token]

            if pnl_pct <= -self.stop_loss_pct * 100:
                print(f"\n  ⚠️  STOP LOSS: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.position_peaks.pop(token, None)
                continue

            if pnl_pct >= self.take_profit_pct * 100:
                print(f"\n  🎯 TAKE PROFIT: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.position_peaks.pop(token, None)
                continue

            if peak >= TRAIL_TRIGGER:
                trail_floor = peak - TRAIL_DIST
                if pnl_pct <= trail_floor:
                    print(f"\n  🔒 TRAIL STOP: {token}  Peak:{peak:+.2f}%  Now:{pnl_pct:+.2f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.position_peaks.pop(token, None)
                else:
                    print(f"  📌 {token}  Peak:{peak:+.2f}%  Now:{pnl_pct:+.2f}%  "
                          f"Floor:{trail_floor:+.2f}% (trailing)")
    def run(self, update_interval: int = 30):
        """Run autonomous trading loop"""
        self.running = True
        print("\n" + "="*60)
        print("  ðŸ¤– SOLANA AUTONOMOUS TRADING ACTIVATED")
        print("="*60)
        print(f"\nConfiguration:")
        print(f"  Mode: {self.agent.mode.value.upper()}")
        print(f"  DEX: Jupiter Aggregator")
        print(f"  Update Interval: {update_interval}s")
        print(f"  Min Confidence: {self.min_confidence:.0%}")
        print(f"  Trade Cooldown: {self.trade_cooldown}s")
        print(f"  Max Trades/Day: {self.max_trades_per_day}")
        print(f"  Max Position: {self.max_position_pct:.0%}")
        print(f"  Stop Loss: {self.stop_loss_pct:.0%}")
        print(f"  Take Profit: {self.take_profit_pct:.0%}")
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
                
                # Update market data
                print("\nðŸ“Š Updating market data...")
                self.agent.update_market_data()
                
                # Monitor existing positions
                if self.agent.portfolio.positions:
                    print("\nðŸ‘€ Monitoring positions...")
                    self.monitor_positions()
                
                # Analyze each token
                print("\nðŸ” Analyzing tokens...")
                for token in self.agent.watched_tokens:
                    if token != "USDC":  # Don't trade USDC
                        self.analyze_and_trade(token)
                
                # Show portfolio summary
                print("\nðŸ’¼ Portfolio Summary:")
                perf = self.agent.portfolio.get_performance()
                print(f"  USDC: ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total Value: ${perf['current_value']:.2f}")
                print(f"  Return: ${perf['total_return']:.2f} ({perf['total_return_pct']:.2f}%)")
                print(f"  Swaps Today: {self.trades_today}/{self.max_trades_per_day}")
                
                if self.agent.portfolio.positions:
                    print(f"\n  Open Positions:")
                    for token, pos in self.agent.portfolio.positions.items():
                        print(f"    {token}: {pos.amount:.6f} @ ${pos.avg_price_usdc:.4f} "
                              f"(P&L: {pos.pnl_percentage:+.2f}%)")
                
                # Wait before next cycle
                print(f"\nâ³ Next update in {update_interval}s...")
                time.sleep(update_interval)
                
        except KeyboardInterrupt:
            print("\n\nðŸ›‘ Stopping autonomous trading...")
            self.running = False
            
            # Final summary
            print("\n" + "="*60)
            print("  TRADING SESSION SUMMARY")
            print("="*60)
            self.agent.show_portfolio()
            print("Goodbye! ðŸ‘‹\n")


def main():
    """Main entry point for autonomous Solana trading"""
    parser = argparse.ArgumentParser(description="Autonomous Solana DEX Trading AI")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                       help="Trading mode (default: paper)")
    parser.add_argument("--wallet", default="", help="Solana wallet address")
    parser.add_argument("--balance", type=float, default=1000.0,
                       help="Initial USDC balance for paper trading")
    parser.add_argument("--interval", type=int, default=30,
                       help="Update interval in seconds (default: 30)")
    parser.add_argument("--min-confidence", type=float, default=0.70,
                       help="Minimum confidence for auto-trading (default: 0.70)")
    parser.add_argument("--tokens", nargs='+',
                       default=["SOL", "JUP", "RAY"],
                       help="Tokens to trade (default: SOL JUP RAY)")
    
    args = parser.parse_args()
    
    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER
    
    # Create agent
    agent = SolanaTradingAgent(mode, args.wallet, args.balance)
    
    # Add tokens to watchlist
    for token in args.tokens:
        agent.add_token(token.upper())
    
    # Always add USDC for reference
    if "USDC" not in agent.watched_tokens:
        agent.add_token("USDC")
    
    # Pre-fill 3600 minutes of history (GeckoTerminal â†’ synthetic fallback)
    print("\nðŸ“ˆ Fetching up to 1000 historical candles from GeckoTerminal...")
    detector = TrendingTokenDetector()
    import requests as _req
    for token in args.tokens:
        sym  = token.upper()
        mint = TOKENS.get(sym, '')
        if not mint:
            print(f"  âš   No mint address for {sym}")
            continue

        # Fetch pair data from DexScreener to get pair_address + current price/change
        pair_addr  = ''
        token_info = {}
        try:
            resp = _req.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=10
            )
            if resp.status_code == 200:
                pairs        = resp.json().get('pairs', [])
                solana_pairs = [p for p in pairs if p.get('chainId') == 'solana']
                if solana_pairs:
                    best = solana_pairs[0]
                    pc   = best.get('priceChange') or {}
                    pair_addr  = best.get('pairAddress', '')
                    token_info = {
                        'price_usd':        float(best.get('priceUsd', 1.0) or 1.0),
                        'price_change_h1':  float(pc.get('h1',  0)),
                        'price_change_h6':  float(pc.get('h6',  0)),
                        'price_change_24h': float(pc.get('h24', 0)),
                    }
        except Exception as e:
            print(f"  âš   DexScreener lookup failed for {sym}: {e}")

        print(f"  â¬‡  Fetching history for {sym}...")
        hist = detector.fetch_historical_prices(pair_addr, n_points=1000, token_info=token_info)
        if hist:
            agent.prefill_price_history(sym, hist)
        else:
            print(f"  âš   No history available for {sym}")

    print("\nâœ“ History pre-fill complete!")

    print("\nðŸ“ˆ Collecting initial market data...")
    print("This will take about 30 seconds...\n")
    for i in range(30):
        agent.update_market_data()
        if (i + 1) % 5 == 0:
            print(f"  Progress: {i + 1}/30 data points")
        time.sleep(1)
    
    print("\nâœ“ Initial data collection complete!")
    
    # Create and run auto trader
    auto_trader = SolanaAutoTrader(agent)
    auto_trader.min_confidence = args.min_confidence
    auto_trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()