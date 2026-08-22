#!/usr/bin/env python3
"""
Autonomous Solana DEX Trading Agent -- Complete Rewrite v4
Strategy: Momentum Pullback on Trending Tokens

What changed from v3
---------------------
COMPLETE STRATEGY REWRITE.

The previous versions had compounding filter failures:
  1. Tokens selected AFTER a 900-1800% pump = buying tops
  2. BEARISH hard block + MACD hard block = 0 entries
  3. BB filter in RANGING regime blocked mid-range entries (pct_b 0.3-0.7)
  4. RSI <35 + not rising = blocked (knife-catching prevention but too aggressive)
  5. 60-candle warmup = 30 minutes before first trade possible
  6. 8% stop on 30% ATR tokens = constant stop-outs

New approach:
  - Select tokens with MOMENTUM but NOT yet at peak (prefer 30-150% 24h change)
  - Enter on PULLBACK within confirmed uptrend (EMA20 slope > 0)
  - Only 2 hard entry gates: (1) min 5 buy points, (2) RSI < 70
  - Stop: 10%, Take Profit: 25%, Trail from +12%
  - Position size: flat 10% per trade (simple and reliable)
  - Cooldown: 180s (3 min) not 300s
"""

import time
import math
import argparse
from datetime import datetime
from typing import Dict, Optional, List

from solana_trading_agent import (
    SolanaTradingAgent, TradingMode, TOKENS, TradingAI
)
from trending_tokens import EnhancedTrendingTokenDetector, AutoTokenSelector


class TrendingSolanaAutoTrader:

    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent         = agent
        self.auto_discover = auto_discover
        self.running       = False

        # Trade management
        self.last_trades:   Dict[str, datetime] = {}
        self.trade_cooldown = 180  # 3 min (was 5 min - too restrictive)

        self.max_trades_per_day = 240
        self.trades_today       = 0
        self.day_start          = datetime.now().date()

        # Entry thresholds (SIMPLIFIED)
        self.min_buy_pts       = 5    # minimum signal points to buy
        self.max_entry_rsi     = 70   # won't buy if RSI > 70
        self.min_entry_rsi     = 25   # won't buy if RSI < 25 (catching knife)
        self.max_concurrent    = 3    # max open positions at once
        self.position_pct      = 0.12 # 12% of portfolio per trade

        # Exit parameters (WIDENED for meme token volatility)
        self.stop_loss_pct   = 0.10  # 10% stop (was 8%, too tight for meme tokens)
        self.take_profit_pct = 0.25  # 25% take profit (was 15%, leaving profit on table)
        self.trail_trigger   = 12.0  # trailing stop kicks in after +12%
        self.trail_dist      = 6.0   # trail 6% below peak

        # Position tracking
        self.position_peaks: Dict[str, float] = {}
        self.tp_lockout:     Dict[str, float] = {}
        self.tp_lockout_secs = 300  # 5 min lockout after TP

        # Blacklist (3 stop losses = blacklisted for session)
        self.sl_strikes:   Dict[str, int]   = {}
        self.sl_blacklist: set              = set()

        # Discovery
        self.refresh_interval = 600
        self.last_refresh     = 0
        self.detector         = EnhancedTrendingTokenDetector()
        self.selector         = AutoTokenSelector(self.detector)
        self.token_metadata:  Dict[str, Dict] = {}

        # Warmup: only 15 live candles needed (7.5 min)
        self.min_live_candles = 15

        # Minimum relative volume to enter
        self.min_rel_vol = 0.40

    # ──────────────────────────────────────────────────────────────────────
    # Token discovery
    # ──────────────────────────────────────────────────────────────────────

    def refresh_trending_tokens(self):
        try:
            print("\n[discover] Fetching trending tokens...")

            # Select tokens preferring 30-200% 24h change (not 500%+ tops)
            self.selector.preferences.update({
                "min_liquidity": 25_000,
                "min_volume":    25_000,
                "max_tokens":    12,
                "prefer_rising": False,
                "max_price_change_24h": 400,  # filter extreme pumps
                "prioritize_boosted": True,
                "min_score": 50.0,
            })

            selected = self.selector.get_token_symbols()
            if not selected:
                print("[discover] No tokens found")
                self.last_refresh = time.time()
                return

            # Display what we found
            trending = self.detector.get_trending_tokens(
                min_liquidity=25_000, min_volume_24h=25_000,
                max_tokens=20, prioritize_boosted=True)
            if trending:
                self.detector.display_trending(trending)

            print("\n[discover] AUTO-SELECTED TOKENS:")
            print("-" * 60)
            added = 0
            skipped_rsi = 0

            for token in selected:
                sym        = token["symbol"]
                addr       = token["address"]
                pair_addr  = token.get("pair_address", "")
                is_boosted = token.get("is_boosted", False)
                is_new     = token.get("is_new_listing", False)
                quality    = token.get("quality_indicator", "[~]")
                score      = token.get("score", 0)
                ch24       = token.get("price_change_24h", 0)

                # Skip already-watched tokens (don't re-add)
                if sym in self.agent.watched_tokens:
                    continue

                if sym not in TOKENS:
                    TOKENS[sym] = addr
                self.agent.add_token(sym, addr)

                self.token_metadata[sym] = {
                    "is_boosted": is_boosted,
                    "is_new_listing": is_new,
                    "quality_indicator": quality,
                    "score": score,
                    "ch24": ch24,
                }

                # Fetch history
                boost_tag = "[BOOST]" if is_boosted else ""
                print(f"  Fetching history for {sym} {boost_tag}...")
                hist, is_real = self.detector.fetch_historical_prices(
                    pair_addr, n_points=1000, token_info=token)

                if not hist:
                    print(f"  [skip] No history for {sym}")
                    self.agent.watched_tokens.remove(sym)
                    self.token_metadata.pop(sym, None)
                    continue

                # RSI gate: skip if already extremely overbought (RSI > 80)
                rsi_check = self.agent.ai.calculate_rsi(hist)
                if rsi_check and rsi_check > 80:
                    print(f"  [skip] RSI {rsi_check:.0f} > 80 -- extremely overbought, skip")
                    self.agent.watched_tokens.remove(sym)
                    self.token_metadata.pop(sym, None)
                    skipped_rsi += 1
                    continue

                self.agent.prefill_price_history(sym, hist)
                buf = self.agent.price_history[sym]
                if is_real:
                    buf["live_points"] = len(hist)
                    print(f"  [OK] Real history: {len(hist)} candles")
                else:
                    buf["live_points"] = 0
                    print(f"  [~] Synthetic history -- gated until {self.min_live_candles} live")

                rsi_str = f"{rsi_check:.0f}" if rsi_check else "N/A"
                print(f"  {added+1}. {sym:<12} {quality} score={score:.0f}  "
                      f"24h={ch24:+.0f}%  RSI={rsi_str}")
                added += 1

            print("-" * 60)
            print(f"\n[discover] Added: {added}  Skipped (overbought RSI): {skipped_rsi}")
            print(f"  Total watching: {len(self.agent.watched_tokens)}")
            print()
            self.last_refresh = time.time()

        except Exception as exc:
            print(f"[discover] Error: {exc}")
            self.last_refresh = time.time() - self.refresh_interval + 120

    # ──────────────────────────────────────────────────────────────────────
    # Trade gate
    # ──────────────────────────────────────────────────────────────────────

    def should_trade(self, token: str, signal: dict) -> bool:
        """Clean, minimal gate. Fail fast with clear reason."""

        # 1. Blacklist
        if token in self.sl_blacklist:
            print(f"  [skip] Blacklisted: {token}")
            return False

        # 2. TP lockout (re-entry ban after take profit)
        if signal["signal"] == "BUY" and token in self.tp_lockout:
            if time.time() < self.tp_lockout[token]:
                rem = int(self.tp_lockout[token] - time.time())
                print(f"  [skip] TP lockout {rem}s")
                return False
            else:
                del self.tp_lockout[token]

        # 3. Cooldown
        if token in self.last_trades:
            elapsed = (datetime.now() - self.last_trades[token]).total_seconds()
            if elapsed < self.trade_cooldown:
                rem = int(self.trade_cooldown - elapsed)
                print(f"  [skip] Cooldown {rem}s")
                return False

        # 4. Daily limit
        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start    = datetime.now().date()
        if self.trades_today >= self.max_trades_per_day:
            print(f"  [skip] Daily limit {self.trades_today}/{self.max_trades_per_day}")
            return False

        # 5. BUY-specific gates
        if signal["signal"] == "BUY":
            # Already holding
            if token in self.agent.portfolio.positions:
                print(f"  [skip] Already holding {token}")
                return False
            # Concurrent position cap
            n_open = len(self.agent.portfolio.positions)
            if n_open >= self.max_concurrent:
                print(f"  [skip] Max positions {n_open}/{self.max_concurrent}")
                return False
            # Cash check
            total = self.agent.portfolio.get_total_value()
            if self.agent.portfolio.usdc_balance < total * 0.05:
                print(f"  [skip] Low USDC")
                return False
            # RSI gates
            rsi = signal.get("rsi", 50)
            if rsi > self.max_entry_rsi:
                print(f"  [skip] RSI {rsi:.0f} > {self.max_entry_rsi} (overbought)")
                return False
            if rsi < self.min_entry_rsi:
                print(f"  [skip] RSI {rsi:.0f} < {self.min_entry_rsi} (knife)")
                return False
            # Signal strength gate
            buy_pts = signal.get("buy_pts", 0)
            if buy_pts < self.min_buy_pts:
                print(f"  [skip] Weak signal buy_pts={buy_pts} < {self.min_buy_pts}")
                return False
            # Volume gate
            rel_vol = signal.get("relative_volume", 1.0)
            if rel_vol < self.min_rel_vol:
                print(f"  [skip] Thin volume {rel_vol:.2f}x")
                return False

        # 6. SELL: must have a position
        if signal["signal"] == "SELL" and token not in self.agent.portfolio.positions:
            print(f"  [skip] No {token} position")
            return False

        return True

    # ──────────────────────────────────────────────────────────────────────
    # Position monitoring
    # ──────────────────────────────────────────────────────────────────────

    def monitor_positions(self):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl = position.pnl_percentage

            # Track peak
            if token not in self.position_peaks:
                self.position_peaks[token] = pnl
            elif pnl > self.position_peaks[token]:
                self.position_peaks[token] = pnl
            peak = self.position_peaks[token]

            # Stop loss
            if pnl <= -self.stop_loss_pct * 100:
                print(f"\n  [STOP LOSS] {token}  P&L: {pnl:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.position_peaks.pop(token, None)
                    self.sl_strikes[token] = self.sl_strikes.get(token, 0) + 1
                    if self.sl_strikes[token] >= 3:
                        self.sl_blacklist.add(token)
                        print(f"  [blacklist] {token} banned after 3 stops")
                continue

            # Take profit
            if pnl >= self.take_profit_pct * 100:
                print(f"\n  [TAKE PROFIT] {token}  P&L: {pnl:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.position_peaks.pop(token, None)
                    self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                continue

            # Trailing stop
            if peak >= self.trail_trigger:
                floor = peak - self.trail_dist
                if pnl <= floor:
                    print(f"\n  [TRAIL STOP] {token}  Peak:{peak:+.2f}%  Now:{pnl:+.2f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.position_peaks.pop(token, None)
                else:
                    print(f"  [trail] {token}  Peak:{peak:+.2f}%  Now:{pnl:+.2f}%  "
                          f"Floor:{floor:+.2f}%")

    # ──────────────────────────────────────────────────────────────────────
    # Execute trade
    # ──────────────────────────────────────────────────────────────────────

    def execute_auto_trade(self, token: str, signal: dict):
        cur   = signal.get("current_price", 0)
        side  = signal["signal"].lower()
        if cur == 0:
            return

        is_boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
        tag        = " [BOOSTED]" if is_boosted else ""

        if side == "buy":
            total      = self.agent.portfolio.get_total_value()
            usdc_amt   = min(total * self.position_pct,
                             self.agent.portfolio.usdc_balance * 0.95)
            if usdc_amt < 10:
                print(f"  [skip] Trade too small ${usdc_amt:.2f}")
                return
            print(f"\n  --- BUY{tag} ---")
            print(f"     Token:  {token}")
            print(f"     USDC:   ${usdc_amt:.2f}")
            print(f"     Price:  ${cur:.8f}")
            print(f"     RSI:    {signal.get('rsi',0):.0f}")
            print(f"     Trend:  {signal.get('trend','?')}")
            print(f"     BuyPts: {signal.get('buy_pts',0)}")
            print(f"     Reason: {signal.get('reason','')}")
            trade = self.agent.execute_swap("USDC", token, usdc_amt)
        else:
            pos = self.agent.portfolio.positions.get(token)
            if not pos:
                return
            print(f"\n  --- SELL{tag} ---")
            print(f"     Token: {token}  P&L: {pos.pnl_percentage:+.2f}%")
            trade = self.agent.execute_swap(token, "USDC", pos.amount)

        if trade:
            self.last_trades[token] = datetime.now()
            self.trades_today += 1
            if side == "buy":
                self.position_peaks[token] = 0.0
            else:
                self.position_peaks.pop(token, None)
            print(f"  [OK] Trade executed")
        else:
            print(f"  [FAIL] Trade failed")

    # ──────────────────────────────────────────────────────────────────────
    # Analyse and trade
    # ──────────────────────────────────────────────────────────────────────

    def analyze_and_trade(self, token: str):
        data = self.agent.price_history.get(token)
        if not data:
            return

        prices   = data.get("prices", [])
        live_pts = data.get("live_points", 0)

        # Warmup: need 15 live candles (7.5 min) OR 30+ historical prices
        if live_pts < self.min_live_candles and len(prices) < 30:
            print(f"  [warm] {token:<12}: live={live_pts}/{self.min_live_candles}  "
                  f"total={len(prices)}")
            return

        # Don't trade on synthetic history alone (live_points=0)
        if live_pts == 0:
            print(f"  [warm] {token:<12}: waiting for live candles (0/{self.min_live_candles})")
            return

        analysis = self.agent.analyze_token(token)
        if not analysis:
            return

        is_boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
        boost_tag  = "[B]" if is_boosted else "   "
        ts         = datetime.now().strftime("%H:%M:%S")
        bb         = analysis.get("bb")
        bb_str     = f"  BB:{bb['pct_b']:.2f}" if bb else ""
        squeeze    = " SQZ" if analysis.get("bb_squeeze") else ""

        sig_disp = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(
            analysis["signal"], "HOLD")

        print(f"  [{ts}] {boost_tag}{token:<12}  "
              f"${analysis['current_price']:<12.6f}  "
              f"RSI:{analysis['rsi']:.0f}  "
              f"{sig_disp}  "
              f"Trend:{analysis['trend']}  "
              f"Buy:{analysis['buy_pts']} Sell:{analysis['sell_pts']}"
              f"{bb_str}{squeeze}  "
              f"L:{live_pts}")

        if analysis["signal"] in ["BUY", "SELL"]:
            if self.should_trade(token, analysis):
                self.execute_auto_trade(token, analysis)

    # ──────────────────────────────────────────────────────────────────────
    # Main loop
    # ──────────────────────────────────────────────────────────────────────

    def run(self, update_interval: int = 30):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 60)
        print("  SOLANA AUTO-TRENDING TRADER  --  v4 MOMENTUM PULLBACK")
        print("=" * 60)
        print(f"\nMode:            {self.agent.mode.value.upper()}")
        print(f"Auto-Discovery:  {'YES' if self.auto_discover else 'NO'}")
        print(f"Interval:        {update_interval}s")
        print(f"Trending Refresh:{self.refresh_interval}s")
        print(f"\nEntry Rules:")
        print(f"  Min buy points: {self.min_buy_pts}  (RSI pullback in uptrend)")
        print(f"  RSI range:      {self.min_entry_rsi}–{self.max_entry_rsi}")
        print(f"  Min live candles:{self.min_live_candles}")
        print(f"  Min rel volume: {self.min_rel_vol:.1f}x")
        print(f"  Max concurrent: {self.max_concurrent} positions")
        print(f"  Position size:  {self.position_pct:.0%} of portfolio")
        print(f"\nExit Rules:")
        print(f"  Stop loss:      {self.stop_loss_pct:.0%}")
        print(f"  Take profit:    {self.take_profit_pct:.0%}")
        print(f"  Trail trigger:  +{self.trail_trigger:.0f}%")
        print(f"  Trail distance: {self.trail_dist:.0f}%")
        print(f"\nPress Ctrl+C to stop")
        print("=" * 60)

        iteration = 0
        try:
            while self.running:
                iteration += 1
                print(f"\n{'='*60}")
                print(f"Cycle #{iteration} -- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*60}")

                # Discovery refresh
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_trending_tokens()

                # Update prices
                print("\n[market] Updating prices...")
                self.agent.update_market_data()

                # Monitor open positions first
                if self.agent.portfolio.positions:
                    print("\n[positions] Monitoring...")
                    self.monitor_positions()

                # Scan for entries
                print("\n[analysis] Scanning tokens...")
                for token in list(self.agent.watched_tokens):
                    if token not in SKIP:
                        self.analyze_and_trade(token)

                # Portfolio summary
                perf = self.agent.portfolio.get_performance()
                print(f"\n[portfolio]")
                print(f"  USDC:         ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total Value:  ${perf['current_value']:.2f}")
                print(f"  Return:       ${perf['total_return']:.2f} "
                      f"({perf['total_return_pct']:.2f}%)")
                print(f"  Trades Today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Watching:     {len(self.agent.watched_tokens)} tokens")

                if self.agent.portfolio.positions:
                    print(f"\n  Open Positions:")
                    for tok, pos in self.agent.portfolio.positions.items():
                        def _fp(p):
                            if p == 0: return "$0.00"
                            if p >= 0.01: return f"${p:.6f}"
                            d = max(8, -int(math.floor(math.log10(abs(p))))+3)
                            return f"${p:.{d}f}"
                        peak = self.position_peaks.get(tok, pos.pnl_percentage)
                        tag  = "[B]" if self.token_metadata.get(tok,{}).get("is_boosted") else "   "
                        print(f"    {tag}{tok}: {_fp(pos.current_price)}"
                              f"  P&L: {pos.pnl_percentage:+.2f}%"
                              f"  Peak: {peak:+.2f}%")

                if self.sl_blacklist:
                    print(f"\n  Blacklisted: {', '.join(self.sl_blacklist)}")

                nxt = max(0, int(self.refresh_interval - (time.time() - self.last_refresh)))
                print(f"\n[wait] Next update in {update_interval}s  |  "
                      f"Next discovery in {nxt//60}m{nxt%60}s")
                time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\n[stop] Stopping...")
            self.running = False
            print("\n" + "="*60)
            print("  SESSION SUMMARY")
            print("="*60)
            self.agent.show_portfolio()
            print("Goodbye!\n")


def main():
    parser = argparse.ArgumentParser(description="Solana Auto Trader v4")
    parser.add_argument("--mode", choices=["paper","live"], default="paper")
    parser.add_argument("--wallet", default="")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--no-auto-discover", action="store_true")
    parser.add_argument("--manual-tokens", nargs="+")
    parser.add_argument("--refresh-interval", type=int, default=600)
    parser.add_argument("--stop-loss", type=float, default=0.10)
    parser.add_argument("--take-profit", type=float, default=0.25)
    parser.add_argument("--position-pct", type=float, default=0.12)
    args = parser.parse_args()

    mode          = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER
    auto_discover = not args.no_auto_discover

    wallet_instance = None
    if mode == TradingMode.LIVE:
        try:
            from wallet_integration import load_wallet_config, SolanaWallet
            cfg = load_wallet_config()
            if not cfg:
                print("[ERROR] No wallet. Run: python setup_wallet.py --setup")
                return
            wallet_instance = SolanaWallet(cfg)
            sol_bal = wallet_instance.get_balance()
            if sol_bal < 0.01:
                r = input(f"Low SOL ({sol_bal:.4f}). Continue? (yes/no): ")
                if r.lower() not in ("yes","y"):
                    return
            if input("\nType 'START' to begin live trading: ") != "START":
                print("Cancelled.")
                return
        except ImportError:
            print("[ERROR] pip install solana solders base58")
            return
        except Exception as e:
            print(f"[ERROR] {e}")
            return

    agent = SolanaTradingAgent(mode, args.wallet, args.balance,
                               wallet=wallet_instance)

    if not auto_discover and args.manual_tokens:
        for tok in args.manual_tokens:
            agent.add_token(tok.upper())
        if "USDC" not in agent.watched_tokens:
            agent.add_token("USDC")

    auto_trader = TrendingSolanaAutoTrader(agent, auto_discover=auto_discover)
    auto_trader.refresh_interval  = args.refresh_interval
    auto_trader.stop_loss_pct     = args.stop_loss
    auto_trader.take_profit_pct   = args.take_profit
    auto_trader.position_pct      = args.position_pct

    if auto_discover:
        print("\n[discover] Fetching initial trending tokens...")
        auto_trader.refresh_trending_tokens()

    print("\n[market] Collecting initial market data (20 ticks)...")
    for i in range(20):
        agent.update_market_data()
        if (i+1) % 5 == 0:
            print(f"  Progress: {i+1}/20")
        time.sleep(1)
    print("\n[OK] Ready to trade!")

    auto_trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()
