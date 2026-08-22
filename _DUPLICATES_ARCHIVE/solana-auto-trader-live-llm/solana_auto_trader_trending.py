#!/usr/bin/env python3
"""
Autonomous Solana DEX Trading Agent -- v5.1 (FIXED)
Strategy: Momentum Pullback on Trending Tokens (Meme Coin Volatility Tuned)

FIXES in v5.1 vs v5:
  1. [CRITICAL] Cache invalidation: analyzer.invalidate_cache() called at the
     START of each cycle so adjust_entry_rules() and should_trade_with_trending()
     share the SAME computed score rather than each advancing the EMA separately.
     Root cause of score oscillation: 66->65->56->72 every 30s in the logs.

  2. [CRITICAL] adjust_entry_rules() no longer calls analyze() itself -- it
     now receives the score already computed this cycle as a parameter.
     Previously it read history[-1] which was the PREVIOUS cycle's score.

  3. [BUG] _regime_was_strong flip detection moved AFTER current score is
     computed (was reading stale history), and now respects forced_refresh_cooldown.

  4. [BUG] buy_pts scoring: rel_vol threshold reduced 1.5x->1.2x in
     solana_trading_agent.py and ema_slope threshold halved to 0.0005.
     (The 1.5x threshold was double-gating with the trade-gate's own vol check.)

  5. [MINOR] Portfolio/position display shown every cycle regardless of pause state.
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

        # Entry
        self.min_buy_pts    = 5
        self.min_entry_rsi  = 40
        self.max_entry_rsi  = 60
        self.min_rel_vol    = 1.2
        self.position_pct   = 0.12
        self.max_concurrent = 2

        # Exit
        self.stop_loss_pct   = 0.12
        self.tp_tier1_pct    = 0.25
        self.tp_tier1_amount = 0.50
        self.trail_trigger   = 15.0
        self.trail_dist      = 10.0

        # Risk
        self.max_trades_per_day = 120

        # Trade management
        self.last_trades:   Dict[str, datetime] = {}
        self.trade_cooldown = 180

        self.trades_today = 0
        self.day_start    = datetime.now().date()

        # Position tracking
        self.position_peaks: Dict[str, float] = {}
        self.tp1_hit:        Dict[str, bool]  = {}
        self.tp_lockout:     Dict[str, float] = {}
        self.tp_lockout_secs = 300

        # Blacklist
        self.sl_strikes:   Dict[str, int] = {}
        self.sl_blacklist: set            = set()

        # Discovery
        self.refresh_interval = 600
        self.last_refresh     = 0
        self.detector         = EnhancedTrendingTokenDetector()
        self.selector         = AutoTokenSelector(self.detector)
        self.token_metadata:  Dict[str, Dict] = {}

        self.min_live_candles        = 15
        self.last_forced_refresh     = 0
        self.forced_refresh_cooldown = 120

        # FIX #2: regime flip uses current-cycle score, not stale history
        self._regime_was_strong = False

    # -------------------------------------------------------------------------
    # Regime-adaptive entry rules  (FIX #1 + #2)
    # -------------------------------------------------------------------------

    def adjust_entry_rules(self, current_score: float):
        """
        Receive the score already computed this cycle -- avoids calling
        analyze() a second time and drifting the EMA smoother.
        """
        # FIX #3: flip detection uses current_score (not stale history[-1])
        is_strong = current_score > 70
        if is_strong and not self._regime_was_strong:
            if time.time() - self.last_forced_refresh > self.forced_refresh_cooldown:
                print("🔄 Regime flipped strong — forcing fresh discovery")
                self.selector.detector.get_trending_tokens(force_refresh=True)
                self.last_forced_refresh = time.time()
        self._regime_was_strong = is_strong

        self.selector.set_regime(current_score)

        if current_score >= 80:
            self.min_buy_pts    = 4
            self.min_rel_vol    = 1.1
            self.max_entry_rsi  = 62
            self.max_concurrent = 2
            regime_label = f"🟢 STRONG  (score {current_score:.0f})"
        elif current_score >= 65:
            self.min_buy_pts    = 5
            self.min_rel_vol    = 1.2
            self.max_entry_rsi  = 60
            self.max_concurrent = 2
            regime_label = f"🟡 MODERATE (score {current_score:.0f})"
        else:
            self.min_buy_pts    = 6
            self.min_rel_vol    = 1.5
            self.max_entry_rsi  = 58
            self.max_concurrent = 1
            regime_label = f"🔴 WEAK    (score {current_score:.0f})"

        print(f"  [regime] {regime_label}  ->  "
              f"buy_pts>={self.min_buy_pts}  "
              f"RSI<={self.max_entry_rsi}  "
              f"vol>={self.min_rel_vol:.1f}x  "
              f"max_pos={self.max_concurrent}")

    # -------------------------------------------------------------------------
    # Token discovery
    # -------------------------------------------------------------------------

    def refresh_trending_tokens(self):
        try:
            print("\n[discover] Fetching trending tokens...")

            self.selector.preferences.update({
                "min_liquidity":        25_000,
                "min_volume":           25_000,
                "max_tokens":           12,
                "prefer_rising":        True,
                "max_price_change_24h": 150,
                "prioritize_boosted":   True,
                "min_score":            60.0,
            })

            selected = self.selector.get_token_symbols()
            if not selected:
                print("[discover] No tokens found")
                self.last_refresh = time.time()
                return

            trending = self.detector.get_trending_tokens(
                min_liquidity=25_000, min_volume_24h=25_000,
                max_tokens=20, prioritize_boosted=True)
            if trending:
                self.detector.display_trending(trending)

            print("\n[discover] AUTO-SELECTED TOKENS:")
            print("-" * 60)
            added       = 0
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

                if sym in self.agent.watched_tokens:
                    continue

                if sym not in TOKENS:
                    TOKENS[sym] = addr
                self.agent.add_token(sym, addr)

                self.token_metadata[sym] = {
                    "is_boosted":        is_boosted,
                    "is_new_listing":    is_new,
                    "quality_indicator": quality,
                    "score":             score,
                    "ch24":              ch24,
                }

                boost_tag = "[BOOST]" if is_boosted else ""
                print(f"  Fetching history for {sym} {boost_tag}...")
                hist, is_real = self.detector.fetch_historical_prices(
                    pair_addr, n_points=1000, token_info=token)

                if not hist:
                    print(f"  [skip] No history for {sym}")
                    self.agent.watched_tokens.remove(sym)
                    self.token_metadata.pop(sym, None)
                    continue

                rsi_check = self.agent.ai.calculate_rsi(hist)
                if rsi_check and rsi_check > 70:
                    print(f"  [skip] RSI {rsi_check:.0f} > 70 -- overbought at discovery")
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

    # -------------------------------------------------------------------------
    # Trade gate
    # -------------------------------------------------------------------------

    def should_trade(self, token: str, signal: dict) -> bool:
        if token in self.sl_blacklist:
            print(f"  [skip] Blacklisted: {token}")
            return False

        if signal["signal"] == "BUY" and token in self.tp_lockout:
            if time.time() < self.tp_lockout[token]:
                rem = int(self.tp_lockout[token] - time.time())
                print(f"  [skip] TP lockout {rem}s")
                return False
            else:
                del self.tp_lockout[token]

        if token in self.last_trades:
            elapsed = (datetime.now() - self.last_trades[token]).total_seconds()
            if elapsed < self.trade_cooldown:
                rem = int(self.trade_cooldown - elapsed)
                print(f"  [skip] Cooldown {rem}s")
                return False

        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start    = datetime.now().date()
        if self.trades_today >= self.max_trades_per_day:
            print(f"  [skip] Daily limit {self.trades_today}/{self.max_trades_per_day}")
            return False

        if signal["signal"] == "BUY":
            if token in self.agent.portfolio.positions:
                print(f"  [skip] Already holding {token}")
                return False

            n_open = len(self.agent.portfolio.positions)
            if n_open >= self.max_concurrent:
                print(f"  [skip] Max positions {n_open}/{self.max_concurrent}")
                return False

            total = self.agent.portfolio.get_total_value()
            if self.agent.portfolio.usdc_balance < total * 0.05:
                print(f"  [skip] Low USDC")
                return False

            rsi = signal.get("rsi", 50)
            if rsi > self.max_entry_rsi:
                print(f"  [skip] RSI {rsi:.0f} > {self.max_entry_rsi} (overbought)")
                return False
            if rsi < self.min_entry_rsi:
                print(f"  [skip] RSI {rsi:.0f} < {self.min_entry_rsi} (knife)")
                return False

            buy_pts = signal.get("buy_pts", 0)
            if buy_pts < self.min_buy_pts:
                print(f"  [skip] Weak signal buy_pts={buy_pts} < {self.min_buy_pts}")
                return False

            rel_vol = signal.get("relative_volume", 1.0)
            if rel_vol < self.min_rel_vol:
                print(f"  [skip] Thin volume {rel_vol:.2f}x < {self.min_rel_vol}x")
                return False

        if signal["signal"] == "SELL" and token not in self.agent.portfolio.positions:
            print(f"  [skip] No {token} position")
            return False

        return True

    # -------------------------------------------------------------------------
    # Position monitoring
    # -------------------------------------------------------------------------

    def monitor_positions(self):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl = position.pnl_percentage

            if token not in self.position_peaks:
                self.position_peaks[token] = pnl
            elif pnl > self.position_peaks[token]:
                self.position_peaks[token] = pnl
            peak = self.position_peaks[token]

            # 1. Hard Stop Loss
            if pnl <= -self.stop_loss_pct * 100:
                print(f"\n  [STOP LOSS] {token}  P&L: {pnl:+.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.position_peaks.pop(token, None)
                    self.tp1_hit.pop(token, None)
                    self.sl_strikes[token] = self.sl_strikes.get(token, 0) + 1
                    if self.sl_strikes[token] >= 3:
                        self.sl_blacklist.add(token)
                        print(f"  [blacklist] {token} banned after 3 stops")
                continue

            # 2. Tier 1 Take Profit
            if pnl >= self.tp_tier1_pct * 100 and not self.tp1_hit.get(token, False):
                sell_amount = position.amount * self.tp_tier1_amount
                print(f"\n  [TAKE PROFIT T1] {token}  P&L: {pnl:+.2f}% "
                      f"- Selling {self.tp_tier1_amount*100:.0f}%")
                trade = self.agent.execute_swap(token, "USDC", sell_amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp1_hit[token] = True
                continue

            # 3. Trailing Stop
            if peak >= self.trail_trigger:
                floor = peak - self.trail_dist
                if pnl <= floor:
                    print(f"\n  [TRAIL STOP] {token}  Peak:{peak:+.2f}%  Now:{pnl:+.2f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.position_peaks.pop(token, None)
                        self.tp1_hit.pop(token, None)
                        self.tp_lockout[token] = time.time() + self.tp_lockout_secs
                else:
                    print(f"  [trail] {token}  Peak:{peak:+.2f}%  Now:{pnl:+.2f}%  "
                          f"Floor:{floor:+.2f}%")

    def _check_stop_losses_only(self):
        if not self.agent.portfolio.positions:
            return
        print("\n[positions] Stop-loss/TP scan (trading paused)...")
        self.monitor_positions()

    # -------------------------------------------------------------------------
    # Position sizing
    # -------------------------------------------------------------------------
    def update_position_sizing(self):
        total = self.agent.portfolio.get_total_value()
        if total < 1500:
            self.position_pct = 0.12
        elif total < 3000:
            self.position_pct = 0.10
        else:
            self.position_pct = 0.08

    # -------------------------------------------------------------------------
    # Execute trade
    # -------------------------------------------------------------------------
    def execute_auto_trade(self, token: str, signal: dict):
        cur  = signal.get("current_price", 0)
        side = signal["signal"].lower()
        if cur == 0:
            return

        is_boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
        tag        = " [BOOSTED]" if is_boosted else ""

        if side == "buy":
            self.update_position_sizing()
            total    = self.agent.portfolio.get_total_value()
            usdc_amt = min(total * self.position_pct,
                           self.agent.portfolio.usdc_balance * 0.95)
            if usdc_amt < 10:
                print(f"  [skip] Trade too small ${usdc_amt:.2f}")
                return
            print(f"\n  --- BUY{tag} ---")
            print(f"     Token:  {token}")
            print(f"     USDC:   ${usdc_amt:.2f}")
            print(f"     Price:  ${cur:.8f}")
            print(f"     RSI:    {signal.get('rsi', 0):.0f}")
            print(f"     Vol:    {signal.get('relative_volume', 0):.2f}x")
            print(f"     Trend:  {signal.get('trend', '?')}")
            print(f"     BuyPts: {signal.get('buy_pts', 0)}")
            trade = self.agent.execute_swap("USDC", token, usdc_amt)
        else:
            pos = self.agent.portfolio.positions.get(token)
            if not pos:
                return
            print(f"\n  --- FULL SELL{tag} ---")
            print(f"     Token: {token}  P&L: {pos.pnl_percentage:+.2f}%")
            trade = self.agent.execute_swap(token, "USDC", pos.amount)

        if trade:
            self.last_trades[token] = datetime.now()
            self.trades_today += 1
            if side == "buy":
                self.position_peaks[token] = 0.0
                self.tp1_hit[token]        = False
            else:
                if token not in self.agent.portfolio.positions:
                    self.position_peaks.pop(token, None)
                    self.tp1_hit.pop(token, None)
            print(f"  [OK] Trade executed")
        else:
            print(f"  [FAIL] Trade failed")

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self, update_interval: int = 30):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 60)
        print("  SOLANA AUTO-TRENDING TRADER  --  v5.1 (FIXED)")
        print("=" * 60)
        print(f"\nPersonality: Momentum Pullback")
        print(f"\nBase Entry Rules (regime-adjusted each cycle):")
        print(f"  Min buy points: {self.min_buy_pts}  (adapts: 4/5/6 by regime)")
        print(f"  RSI range:      {self.min_entry_rsi}-{self.max_entry_rsi}  (max adapts: 60/62)")
        print(f"  Min rel volume: {self.min_rel_vol:.1f}x  (adapts: 1.1/1.2/1.5x)")
        print(f"  Position size:  {self.position_pct:.0%} of portfolio")
        print(f"  Max concurrent: {self.max_concurrent}  (adapts: 1/2 by regime)")
        print(f"\nExit Rules (fixed - sized for meme-coin volatility):")
        print(f"  Hard stop loss: -{self.stop_loss_pct*100:.0f}%")
        print(f"  Tier 1 TP:      Sell {self.tp_tier1_amount*100:.0f}% at +{self.tp_tier1_pct*100:.0f}%")
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

                # FIX #1: Invalidate the analyzer cache at the TOP of each cycle.
                # Without this, adjust_entry_rules() and should_trade_with_trending()
                # each advanced the EMA, causing the score to oscillate every 30s.
                if hasattr(self.agent, "market_analyzer"):
                    self.agent.market_analyzer.invalidate_cache()

                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_trending_tokens()

                print("\n[market] Updating prices...")
                self.agent.update_market_data()

                # Build tokens_analysis for this cycle
                tokens_analysis: Dict[str, dict] = {}
                for token in list(self.agent.watched_tokens):
                    if token in SKIP:
                        continue
                    data     = self.agent.price_history.get(token, {})
                    live_pts = data.get("live_points", 0)
                    prices   = data.get("prices", [])
                    if live_pts < self.min_live_candles and len(prices) < 30:
                        continue
                    if live_pts == 0:
                        continue
                    analysis = self.agent.analyze_token(token)
                    if analysis:
                        tokens_analysis[token] = analysis

                # FIX #2: Call should_trade_with_trending() FIRST (it calls
                # analyze() once and caches the score). Then read the cached
                # score and pass it to adjust_entry_rules() -- no second EMA step.
                should_trade    = True
                trending_tokens: List[str] = list(tokens_analysis.keys())
                pause_reason    = ""
                current_score   = 50.0  # fallback if no analyzer

                if hasattr(self.agent, "market_analyzer") and tokens_analysis:
                    analyzer = self.agent.market_analyzer
                    should_trade, trending_tokens, pause_reason = \
                        analyzer.should_trade_with_trending(tokens_analysis)
                    # Score is now in history (cached), read it back
                    current_score = analyzer.history[-1]["score"] if analyzer.history else 50.0
                    print(f"\n{pause_reason}")
                else:
                    print(f"\nℹ️  No MarketAnalyzer or no token data — scanning all")

                # Adjust entry rules with this cycle's already-computed score
                self.adjust_entry_rules(current_score)

                if not should_trade:
                    print("⏸️  TRADING PAUSED - Protecting capital")
                    self._check_stop_losses_only()

                else:
                    if not trending_tokens:
                        if time.time() - self.last_forced_refresh > self.forced_refresh_cooldown:
                            print("🔄 Regime strong but no qualified tokens — refreshing discovery...")
                            self.refresh_trending_tokens()
                            self.last_forced_refresh = time.time()
                        else:
                            print("⏳ Waiting — recently refreshed.")
                    else:
                        print(f"🚀 TRADING ENABLED - Focusing on: {', '.join(trending_tokens)}")

                        if self.agent.portfolio.positions:
                            print("\n[positions] Monitoring...")
                            self.monitor_positions()

                        print("\n[analysis] Scanning trending tokens...")
                        for token in trending_tokens:
                            analysis = tokens_analysis.get(token)
                            if not analysis:
                                continue

                            is_boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
                            boost_tag  = "[B]" if is_boosted else "   "
                            ts         = datetime.now().strftime("%H:%M:%S")
                            bb         = analysis.get("bb")
                            bb_str     = f"  BB:{bb['pct_b']:.2f}" if bb else ""
                            live_pts   = self.agent.price_history.get(token, {}).get("live_points", 0)
                            squeeze    = " SQZ" if analysis.get("bb_squeeze") else ""
                            rel_v      = analysis.get("relative_volume", 1.0)

                            sig_disp = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(
                                analysis["signal"], "HOLD")

                            print(f"  [{ts}] {boost_tag}{token:<12}  "
                                  f"${analysis['current_price']:<12.6f}  "
                                  f"RSI:{analysis['rsi']:.0f}  "
                                  f"{sig_disp}  "
                                  f"Vol:{rel_v:.1f}x  "
                                  f"Trend:{analysis['trend']}  "
                                  f"Buy:{analysis['buy_pts']} Sell:{analysis['sell_pts']}"
                                  f"{bb_str}{squeeze}")

                            # Show buy_pts breakdown so it's clear what's blocking
                            detail = analysis.get("buy_pts_detail", {})
                            if detail:
                                flags = "".join([
                                    "T" if detail.get("trend")    else ".",
                                    "R" if detail.get("rsi_rise") else ".",
                                    "M" if detail.get("macd")     else ".",
                                    "Z" if detail.get("rsi_zone") else ".",
                                    "V" if detail.get("vol")      else ".",
                                    "S" if detail.get("slope")    else ".",
                                    "B" if detail.get("bb_low")   else ".",
                                ])
                                print(f"         pts [{flags}]  "
                                      f"T=trend R=rsiRise M=macd Z=rsiZone V=vol S=slope B=bbLow")

                            if analysis["signal"] in ("BUY", "SELL"):
                                if self.should_trade(token, analysis):
                                    self.execute_auto_trade(token, analysis)

                # Portfolio display (FIX #5: always shown, not only when trading)
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
                            if p == 0:    return "$0.00"
                            if p >= 0.01: return f"${p:.6f}"
                            d = max(8, -int(math.floor(math.log10(abs(p)))) + 3)
                            return f"${p:.{d}f}"
                        peak = self.position_peaks.get(tok, pos.pnl_percentage)
                        tag  = "[B]" if self.token_metadata.get(tok, {}).get("is_boosted") else "   "
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
            print("\n" + "=" * 60)
            print("  SESSION SUMMARY")
            print("=" * 60)
            self.agent.show_portfolio()
            print("Goodbye!\n")


def main():
    parser = argparse.ArgumentParser(description="Solana Auto Trader v5.1")
    parser.add_argument("--mode",             choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",           default="")
    parser.add_argument("--balance",          type=float, default=1000.0)
    parser.add_argument("--interval",         type=int,   default=30)
    parser.add_argument("--no-auto-discover", action="store_true")
    parser.add_argument("--manual-tokens",    nargs="+")
    parser.add_argument("--refresh-interval", type=int,   default=600)

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
                if r.lower() not in ("yes", "y"):
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

    from market_condition_analyzer import MarketConditionAnalyzer
    agent.market_analyzer = MarketConditionAnalyzer()
    print("[market] MarketConditionAnalyzer attached — trading will pause in poor conditions.")

    if not auto_discover and args.manual_tokens:
        for tok in args.manual_tokens:
            agent.add_token(tok.upper())
        if "USDC" not in agent.watched_tokens:
            agent.add_token("USDC")

    auto_trader = TrendingSolanaAutoTrader(agent, auto_discover=auto_discover)
    auto_trader.refresh_interval = args.refresh_interval

    if auto_discover:
        print("\n[discover] Fetching initial trending tokens...")
        auto_trader.refresh_trending_tokens()

    print("\n[market] Collecting initial market data (20 ticks)...")
    for i in range(20):
        agent.update_market_data()
        if (i + 1) % 5 == 0:
            print(f"  Progress: {i+1}/20")
        time.sleep(1)
    print("\n[OK] Ready to trade!")

    auto_trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()
