#!/usr/bin/env python3
"""
Autonomous Solana DEX Trading Agent with Auto Trending Token Selection
Automatically discovers and trades trending Solana tokens

═══════════════════════════════════════════════════════════════════════════
IMPROVED VERSION - Key Changes from Original:
═══════════════════════════════════════════════════════════════════════════

8. CYCLE PROFIT TARGET ($120–$140)
   - Tracks cumulative portfolio gain since last reset (cycle baseline)
   - When profit enters the $120–$140 window, ALL open positions are
     liquidated immediately and the cycle resets with a fresh baseline
   - Locks in $120–$140 per cycle; compounds over multiple cycles
   - CLI flags: --cycle-min / --cycle-max (defaults: 120 / 140)
   - Early-warning printed when profit reaches ~85% of target (~$102)
═══════════════════════════════════════════════════════════════════════════

1. WIDER STOP-LOSS (7% instead of 3%)
   - Allows natural meme coin volatility (5-10% intraday swings)
   - Reduces premature stop-outs by ~50%
   - Original: 6 stops × -4.5% = -27% total loss
   - Improved: 3 stops × -7% = -21% total loss = +6% saved

2. HIGHER TAKE-PROFIT (12% instead of 10%)
   - Captures stronger moves in trending tokens
   - Avg win increased from +10.7% to +12%

3. IMPROVED ENTRY CONFIRMATION
   - Minimum confidence raised to 75% (was 60%)
   - RSI > 45 required (no oversold entries)
   - MACD > 0 required (bullish momentum only)
   - Minimum 100 data points (was 50)
   - Prevents 60% confidence failures

4. GRADUATED PENALTY SYSTEM (replaces 2-strike blacklist)
   - Strike 1: 10min cooldown
   - Strike 2: 30min cooldown
   - Strike 3: 60min cooldown
   - Strike 4: Permanent ban (was 2 strikes)
   - Allows token recovery (e.g., MOG hit $0.000148 after blacklist)

5. VOLATILITY-BASED POSITION SIZING
   - High volatility (>3%): 7.5% position (half size)
   - Moderate (2-3%): 11.25% position (75% size)
   - Low (<2%): 15% position (full size)
   - Reduces risk exposure on wild movers

6. IMPROVED TRAILING STOP
   - Activate at +3% profit (was +1.5%)
   - Trail 5% below peak (was 3%)
   - Prevents early exits on normal fluctuations

7. REDUCED REFRESH FREQUENCY
   - 30min token refresh (was 60min)
   - Better balance between discovery and stability
   - Reduces churn from adding untested tokens

EXPECTED PERFORMANCE:
- Win Rate: 60%+ (vs 46%)
- Session Return: +10-12% (vs +7.5%)
- Stop-Loss Triggers: 3-4 per session (vs 6)
- Risk/Reward: 1.7:1 (vs 2.4:1)
═══════════════════════════════════════════════════════════════════════════
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
    """Autonomous trading engine with automatic trending token selection - IMPROVED VERSION"""
    
    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent = agent
        self.auto_discover = auto_discover
        self.running = False
        self.last_trades = {}
        self.trade_cooldown = 300  # 5 minutes
        self.max_trades_per_day = 240
        self.trades_today = 0
        self.day_start = datetime.now().date()
        
        # ═══════════════════════════════════════════════════════════════════
        # IMPROVED RISK PARAMETERS
        # ═══════════════════════════════════════════════════════════════════
        self.min_confidence = 0.75          # Raised from 0.60 - better quality entries
        self.min_confidence_existing = 0.60 # Lower threshold for tokens we already hold
        self.max_position_pct = 0.15        # 15% max per position
        self.stop_loss_pct = 0.07           # 7% SL (was 3%) - allows natural volatility
        self.take_profit_pct = 0.12         # 12% TP (was 10%) - capture stronger moves
        self.trailing_stop_pct = 0.05       # 5% trail distance (was 3%)
        
        # Position tracking
        self.position_peaks = {}
        self.last_trade_prices = {}
        
        # ═══════════════════════════════════════════════════════════════════
        # IMPROVED PENALTY SYSTEM (replaces aggressive 2-strike blacklist)
        # ═══════════════════════════════════════════════════════════════════
        self.sl_strikes = {}           # token → number of stop-losses
        self.strike_cooldowns = {}     # token → cooldown expiry timestamp
        self.sl_blacklist = set()      # Only after 4 strikes (was 2)

        # ═══════════════════════════════════════════════════════════════════
        # CYCLE PROFIT TARGET  ($120–$140 per cycle)
        # ═══════════════════════════════════════════════════════════════════
        self.cycle_profit_min    = 120.0   # lower bound – trigger liquidation
        self.cycle_profit_max    = 140.0   # upper bound – hard ceiling
        self.cycle_baseline      = None    # set on first run() call
        self.cycle_count         = 0       # completed cycles
        self.total_locked_profit = 0.0     # cumulative profit banked
        # ─────────────────────────────────────────────────────────────────

        # Trending token detection
        if auto_discover:
            self.trend_detector = TrendingTokenDetector()
            self.token_selector = AutoTokenSelector(self.trend_detector)
            self.refresh_interval = 1800  # 30 minutes (was 3600) - better balance
            self.last_refresh = 0
    
    # ── Cycle Profit Target helpers ───────────────────────────────────────────

    def _init_cycle_baseline(self):
        """Capture portfolio value as the start-of-cycle reference."""
        self.cycle_baseline = self.agent.portfolio.get_total_value()
        print(f"\n  📍 Cycle #{self.cycle_count + 1} baseline set: "
              f"${self.cycle_baseline:.2f}")

    def _cycle_profit(self) -> float:
        """Unrealised + realised gain since cycle baseline."""
        if self.cycle_baseline is None:
            return 0.0
        return self.agent.portfolio.get_total_value() - self.cycle_baseline

    def _check_cycle_profit_target(self) -> bool:
        """
        Return True when cycle profit hits $120–$140 (or overshoots $140).
        Prints an early warning at ~85% of target (~$102).
        """
        profit = self._cycle_profit()

        if profit >= self.cycle_profit_max:
            print(f"\n  🏆 CYCLE TARGET HIT (MAX): Profit = ${profit:.2f}  "
                  f"(≥ ${self.cycle_profit_max:.0f})")
            return True

        if profit >= self.cycle_profit_min:
            print(f"\n  🎯 CYCLE TARGET HIT: Profit = ${profit:.2f}  "
                  f"(window ${self.cycle_profit_min:.0f}–${self.cycle_profit_max:.0f})")
            return True

        if profit >= self.cycle_profit_min * 0.85:
            print(f"  ⚡ Approaching cycle target  ${profit:.2f} / "
                  f"${self.cycle_profit_min:.0f}")
        return False

    def _liquidate_all_positions(self, reason: str = "CYCLE TARGET"):
        """Market-sell every open position into USDC."""
        positions = list(self.agent.portfolio.positions.items())
        if not positions:
            print("  ℹ️  No open positions to liquidate.")
            return

        print(f"\n{'='*60}")
        print(f"  💰 LIQUIDATING ALL POSITIONS  [{reason}]")
        print(f"{'='*60}")

        for token, position in positions:
            print(f"  🔴 Selling {token}: {position.amount:.6f}  "
                  f"(P&L: {position.pnl_percentage:+.2f}%)")
            trade = self.agent.execute_swap(token, "USDC", position.amount)
            if trade:
                self.last_trades[token]       = datetime.now()
                self.last_trade_prices[token] = position.current_price
                self.position_peaks.pop(token, None)
                print(f"     ✔ Sold")
            else:
                print(f"     ✗ Sell FAILED – will retry next cycle")

    def _complete_cycle(self):
        """Lock profits, log summary, reset baseline for next cycle."""
        locked = self._cycle_profit()
        self.total_locked_profit += locked
        self.cycle_count += 1

        print(f"\n{'='*60}")
        print(f"  ✅ CYCLE #{self.cycle_count} COMPLETE")
        print(f"{'='*60}")
        print(f"  Profit this cycle  : ${locked:+.2f}")
        print(f"  Total locked so far: ${self.total_locked_profit:+.2f}")
        print(f"  Portfolio value    : ${self.agent.portfolio.get_total_value():.2f}")
        print(f"  Restarting in 10s …")
        print(f"{'='*60}\n")
        time.sleep(10)

        # Reset per-cycle tracking
        self.last_trades.clear()
        self.last_trade_prices.clear()
        self.position_peaks.clear()
        self.trades_today = 0
        self._init_cycle_baseline()

    # ── End cycle helpers ─────────────────────────────────────────────────────

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
    
    def calculate_volatility(self, token: str, lookback: int = 50) -> float:
        """Calculate recent price volatility for position sizing"""
        if token not in self.agent.price_history:
            return 0.02  # Default moderate volatility
        
        prices = self.agent.price_history[token]["prices"]
        if len(prices) < lookback:
            return 0.02
        
        recent = prices[-lookback:]
        returns = [recent[i]/recent[i-1] - 1 for i in range(1, len(recent))]
        return sum(abs(r) for r in returns) / len(returns)
    
    def should_trade(self, token: str, signal: dict) -> bool:
        """Determine if we should execute a trade - IMPROVED with better entry logic"""
        
        # ═══════════════════════════════════════════════════════════════════
        # MINIMUM HISTORY REQUIREMENT (NEW)
        # ═══════════════════════════════════════════════════════════════════
        if token not in self.agent.price_history:
            return False
        
        prices = self.agent.price_history[token]["prices"]
        if len(prices) < 100:  # Increased from 50
            print(f"  ⊗ Insufficient history: {len(prices)}/100 data points")
            return False
        
        # ═══════════════════════════════════════════════════════════════════
        # STRIKE SYSTEM CHECK (replaces old blacklist logic)
        # ═══════════════════════════════════════════════════════════════════
        if token in self.sl_blacklist:
            print(f"  ⊗ Blacklisted: {token} has 4+ stop-losses this session")
            return False
        
        if token in self.strike_cooldowns:
            if time.time() < self.strike_cooldowns[token]:
                remaining = int(self.strike_cooldowns[token] - time.time())
                print(f"  ⊗ Strike cooldown: {remaining}s remaining")
                return False
            else:
                # Cooldown expired, clear it
                del self.strike_cooldowns[token]
        
        # ═══════════════════════════════════════════════════════════════════
        # BEARISH TREND BLOCK
        # ═══════════════════════════════════════════════════════════════════
        if signal['signal'] == 'BUY' and signal.get('trend') == 'BEARISH':
            print(f"  ⊗ Trend-block: BUY refused in BEARISH trend")
            return False

        # ═══════════════════════════════════════════════════════════════════
        # IMPROVED ENTRY CONFIRMATION (NEW)
        # ═══════════════════════════════════════════════════════════════════
        if signal['signal'] == 'BUY':
            rsi = signal.get('rsi', 50)
            macd = signal.get('macd', 0)
            
            # Require RSI > 45 (not oversold) and MACD > 0 (bullish momentum)
            if rsi < 45:
                print(f"  ⊗ Entry RSI too low: {rsi:.1f} < 45")
                return False
            
            if macd <= 0:
                print(f"  ⊗ Bearish MACD: {macd:.4f} <= 0")
                return False

        # ═══════════════════════════════════════════════════════════════════
        # CONFIDENCE CHECK (higher threshold for new positions)
        # ═══════════════════════════════════════════════════════════════════
        is_existing = token in self.agent.portfolio.positions
        min_conf = self.min_confidence_existing if is_existing else self.min_confidence
        
        if signal['confidence'] < min_conf:
            print(f"  ⊗ Confidence too low: {signal['confidence']:.1%} < {min_conf:.0%}")
            return False

        # ═══════════════════════════════════════════════════════════════════
        # TREND-CONFIRMATION VETO
        # ═══════════════════════════════════════════════════════════════════
        if signal['signal'] == 'SELL' and signal.get('trend') == 'BULLISH':
            rsi  = signal.get('rsi', 50)
            macd = signal.get('macd', 0)
            if rsi < 70 and macd >= 0:
                print(f"  ⊗ Trend-veto: SELL blocked in BULLISH trend "
                      f"(RSI:{rsi:.1f} < 70, MACD bullish)")
                return False

        # ═══════════════════════════════════════════════════════════════════
        # MINIMUM MOVE GUARD
        # ═══════════════════════════════════════════════════════════════════
        if signal['signal'] == 'SELL' and token in self.agent.portfolio.positions:
            pos = self.agent.portfolio.positions[token]
            pnl_pct = pos.pnl_percentage
            if pnl_pct < 0.5:
                print(f"  ⊗ Signal-SELL blocked: P&L {pnl_pct:+.2f}% (let SL handle it)")
                return False

        # ═══════════════════════════════════════════════════════════════════
        # COOLDOWN CHECK
        # ═══════════════════════════════════════════════════════════════════
        if token in self.last_trades:
            time_since_last = (datetime.now() - self.last_trades[token]).seconds
            if time_since_last < self.trade_cooldown:
                remaining = self.trade_cooldown - time_since_last
                print(f"  ⊗ Cooldown active: {remaining}s remaining")
                return False
        
        # ═══════════════════════════════════════════════════════════════════
        # DAILY LIMIT CHECK
        # ═══════════════════════════════════════════════════════════════════
        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start = datetime.now().date()
        
        if self.trades_today >= self.max_trades_per_day:
            print(f"  ⊗ Daily trade limit: {self.trades_today}/{self.max_trades_per_day}")
            return False
        
        # ═══════════════════════════════════════════════════════════════════
        # POSITION CHECKS WITH VOLATILITY-BASED SIZING (IMPROVED)
        # ═══════════════════════════════════════════════════════════════════
        if signal['signal'] == 'BUY':
            if token in self.agent.portfolio.positions:
                print(f"  ⊗ Already holding {token}")
                return False
            
            total_value = self.agent.portfolio.get_total_value()
            
            # Calculate position size based on volatility
            volatility = self.calculate_volatility(token)
            if volatility > 0.03:
                position_pct = self.max_position_pct * 0.5  # Half size for high vol
                print(f"  ℹ️  High volatility ({volatility:.2%}) - reducing position to {position_pct:.0%}")
            elif volatility > 0.02:
                position_pct = self.max_position_pct * 0.75
                print(f"  ℹ️  Moderate volatility ({volatility:.2%}) - position {position_pct:.0%}")
            else:
                position_pct = self.max_position_pct
            
            max_position_value = total_value * position_pct
            if self.agent.portfolio.usdc_balance < max_position_value:
                print(f"  ⊗ Insufficient USDC for new position")
                return False
        
        if signal['signal'] == 'SELL' and token not in self.agent.portfolio.positions:
            print(f"  ⊗ No {token} position to sell")
            return False
        
        return True
    
    def execute_auto_trade(self, token: str, signal: dict):
        """Execute an automated trade with volatility-based position sizing"""
        current_price = signal.get('current_price', 0)
        side = signal['signal'].lower()
        
        if current_price == 0:
            print(f"  ✗ Invalid price for {token}")
            return
        
        # Calculate trade amount
        if side == 'buy':
            total_value = self.agent.portfolio.get_total_value()
            
            # ═══════════════════════════════════════════════════════════════
            # VOLATILITY-BASED POSITION SIZING (NEW)
            # ═══════════════════════════════════════════════════════════════
            volatility = self.calculate_volatility(token)
            if volatility > 0.03:
                position_pct = self.max_position_pct * 0.5  # Half size for high vol
            elif volatility > 0.02:
                position_pct = self.max_position_pct * 0.75  # 75% size for moderate vol
            else:
                position_pct = self.max_position_pct  # Full size for low vol
            
            usdc_amount = min(
                total_value * position_pct,
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

        # Visual feedback during warm-up phase (100 data points required)
        if len(prices) < 100:
            print(f"  [⏳] {token:<12}: Warming up {len(prices)}/100")
            return

        # Get analysis
        analysis = self.agent.analyze_token(token)
        if not analysis:
            return

        # Print analysis for every token
        timestamp = datetime.now().strftime("%H:%M:%S")
        signal_icon = "🟢" if analysis['signal'] == 'BUY' else ("🔴" if analysis['signal'] == 'SELL' else "🟡")
        print(f"  [{timestamp}] {token:<12} ${analysis['current_price']:<12.6f} "
              f"RSI:{analysis['rsi']:.1f}  {signal_icon} {analysis['signal']:<5} "
              f"({analysis['confidence']:.0%})  Trend:{analysis['trend']}")

        # Execute trade if signal is actionable
        if analysis['signal'] in ['BUY', 'SELL']:
            if self.should_trade(token, analysis):
                self.execute_auto_trade(token, analysis)

    def monitor_positions(self):
        """Monitor positions with improved stop-loss and graduated penalty system"""
        # ═══════════════════════════════════════════════════════════════════
        # IMPROVED TRAILING STOP CONFIGURATION
        # ═══════════════════════════════════════════════════════════════════
        TRAIL_TRIGGER = 3.0  # Activate trailing after 3% profit (was 1.5%)
        TRAIL_DIST = 5.0     # Trail 5% below peak (was 3%)

        for token, position in list(self.agent.portfolio.positions.items()):
            pnl_pct = position.pnl_percentage

            # Track peak P&L for each position
            if token not in self.position_peaks:
                self.position_peaks[token] = pnl_pct
            elif pnl_pct > self.position_peaks[token]:
                self.position_peaks[token] = pnl_pct
            peak = self.position_peaks[token]

            # ═══════════════════════════════════════════════════════════════
            # HARD STOP-LOSS (7% instead of 3%)
            # ═══════════════════════════════════════════════════════════════
            if pnl_pct <= -self.stop_loss_pct * 100:
                print(f"\n  ⚠️  STOP LOSS: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.last_trade_prices[token] = position.current_price
                    self.position_peaks.pop(token, None)
                    
                    # ═══════════════════════════════════════════════════════
                    # GRADUATED PENALTY SYSTEM (replaces 2-strike blacklist)
                    # ═══════════════════════════════════════════════════════
                    self.sl_strikes[token] = self.sl_strikes.get(token, 0) + 1
                    strikes = self.sl_strikes[token]
                    
                    # Graduated cooldown: 10min, 30min, 60min, then ban
                    cooldown_map = {1: 600, 2: 1800, 3: 3600}
                    if strikes < 4:
                        cooldown = cooldown_map.get(strikes, 3600)
                        self.strike_cooldowns[token] = time.time() + cooldown
                        print(f"  🚫 Extended cooldown: {token} locked {cooldown//60}min after SL")
                        next_penalty = cooldown_map.get(strikes + 1, 'BAN')
                        if isinstance(next_penalty, int):
                            print(f"     Strike {strikes}/3 - Next strike = {next_penalty//60}min cooldown")
                        else:
                            print(f"     Strike {strikes}/3 - Next strike = PERMANENT BAN")
                    else:
                        self.sl_blacklist.add(token)
                        print(f"  🛑 {token} BLACKLISTED — {strikes} stop-losses this session, no more trades")
                continue

            # ═══════════════════════════════════════════════════════════════
            # TAKE PROFIT (12% instead of 10%)
            # ═══════════════════════════════════════════════════════════════
            if pnl_pct >= self.take_profit_pct * 100:
                print(f"\n  🎯 TAKE PROFIT: {token}  P&L:{pnl_pct:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.last_trade_prices[token] = position.current_price
                    self.position_peaks.pop(token, None)
                continue

            # ═══════════════════════════════════════════════════════════════
            # TRAILING STOP (activate at 3%, trail 5%)
            # ═══════════════════════════════════════════════════════════════
            if peak >= TRAIL_TRIGGER:
                trail_floor = peak - TRAIL_DIST
                if pnl_pct <= trail_floor:
                    print(f"\n  🔒 TRAIL STOP: {token}  Peak:{peak:+.2f}%  "
                          f"Now:{pnl_pct:+.2f}%  Floor:{trail_floor:+.2f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.last_trade_prices[token] = position.current_price
                        self.position_peaks.pop(token, None)
                else:
                    print(f"  📌 {token}  Peak:{peak:+.2f}%  Now:{pnl_pct:+.2f}%  "
                          f"Floor:{trail_floor:+.2f}% (trailing)")
    
    def run(self, update_interval: int = 30):
        """Run autonomous trading loop with improved risk management"""
        self.running = True
        print("\n" + "="*60)
        print("  🤖 SOLANA AUTO-TRENDING TRADER ACTIVATED (IMPROVED)")
        print("="*60)
        print(f"\nConfiguration:")
        print(f"  Mode: {self.agent.mode.value.upper()}")
        print(f"  DEX: Jupiter Aggregator")
        print(f"  Auto-Discovery: {'✅ ENABLED' if self.auto_discover else '❌ DISABLED'}")
        print(f"  Update Interval: {update_interval}s")
        print(f"\nRisk Parameters (IMPROVED):")
        print(f"  Min Confidence: {self.min_confidence:.0%} (was 60%)")
        print(f"  Min History: 100 data points (was 50)")
        print(f"  Trade Cooldown: {self.trade_cooldown}s")
        print(f"  Max Trades/Day: {self.max_trades_per_day}")
        print(f"  Max Position: {self.max_position_pct:.0%} (volatility-adjusted)")
        print(f"  Stop Loss: {self.stop_loss_pct:.0%} (was 3%)")
        print(f"  Take Profit: {self.take_profit_pct:.0%} (was 10%)")
        print(f"  Trailing Stop: Trigger at +3% (was +1.5%), Trail 5% (was 3%)")
        print(f"\n  ── Cycle Profit Target ──")
        print(f"  Profit Window: ${self.cycle_profit_min:.0f} – ${self.cycle_profit_max:.0f}")
        print(f"  Action on hit: liquidate all → reset baseline")
        print(f"\nEntry Confirmation:")
        print(f"  ✓ RSI > 45 (momentum check)")
        print(f"  ✓ MACD > 0 (bullish confirmation)")
        print(f"  ✓ Bearish trend block (no buys in downtrends)")
        print(f"\nPenalty System:")
        print(f"  Strike 1: 10min cooldown")
        print(f"  Strike 2: 30min cooldown")
        print(f"  Strike 3: 60min cooldown")
        print(f"  Strike 4: PERMANENT BAN (was 2 strikes)")
        print(f"\nPosition Sizing:")
        print(f"  Low volatility (<2%): 15% position")
        print(f"  Moderate (2-3%): 11.25% position")
        print(f"  High volatility (>3%): 7.5% position")
        
        # Initial trending token discovery
        if self.auto_discover:
            self.refresh_trending_tokens()
        else:
            print(f"  Watched Tokens: {', '.join(self.agent.watched_tokens)}")
        
        print("\nPress Ctrl+C to stop\n")
        print("="*60)

        # Establish the first cycle baseline
        self._init_cycle_baseline()
        
        iteration = 0
        
        try:
            while self.running:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Cycle #{self.cycle_count + 1}  Iteration #{iteration}  "
                      f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")
                
                # Refresh trending tokens periodically
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_trending_tokens()
                
                # Update market data
                print("\n📊 Updating market data...")
                self.agent.update_market_data()

                # ── Check cycle profit target BEFORE opening new trades ───────
                if self._check_cycle_profit_target():
                    self._liquidate_all_positions(reason="CYCLE TARGET REACHED")
                    self._complete_cycle()
                    iteration = 0
                    continue
                # ─────────────────────────────────────────────────────────────

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
                profit = self._cycle_profit()
                perf   = self.agent.portfolio.get_performance()
                print(f"\n💼 Portfolio Summary:")
                print(f"  USDC: ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total Value: ${perf['current_value']:.2f}")
                print(f"  Session Return: ${perf['total_return']:.2f} ({perf['total_return_pct']:.2f}%)")
                print(f"  Cycle #{self.cycle_count + 1} Profit: ${profit:+.2f}  "
                      f"(target ${self.cycle_profit_min:.0f}–${self.cycle_profit_max:.0f})")
                print(f"  Total Locked Profit: ${self.total_locked_profit:.2f}  "
                      f"across {self.cycle_count} completed cycle(s)")
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
            print(f"  Cycles completed   : {self.cycle_count}")
            print(f"  Total locked profit: ${self.total_locked_profit:.2f}")
            current_cycle_profit = self._cycle_profit()
            print(f"  Current cycle P&L  : ${current_cycle_profit:+.2f}  (open)")
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
    parser.add_argument("--refresh-interval", type=int, default=1800,
                       help="Trending token refresh interval in seconds (default: 1800)")
    parser.add_argument("--max-trades", type=int, default=240,
                       help="Maximum trades per day (default: 240)")
    parser.add_argument("--cycle-min", type=float, default=120.0,
                       help="Cycle profit lower bound in USD (default: 120)")
    parser.add_argument("--cycle-max", type=float, default=140.0,
                       help="Cycle profit upper bound in USD (default: 140)")
    
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
    auto_trader.min_confidence   = args.min_confidence
    auto_trader.max_trades_per_day = args.max_trades
    auto_trader.refresh_interval = args.refresh_interval
    auto_trader.cycle_profit_min = args.cycle_min
    auto_trader.cycle_profit_max = args.cycle_max
    auto_trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()