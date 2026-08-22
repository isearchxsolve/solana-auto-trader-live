#!/usr/bin/env python3
"""
Solana High Win Rate + Rapid Growth Trader — v2.0
==================================================
Strategy: You cannot have high win rate + rapid growth + low risk.
We pick two: HIGH WIN RATE + RAPID GROWTH.

That requires:
  1. Ultra-selective entries (hard filters eliminate 90%+ of signals)
  2. Probability-weighted position sizing — not fixed %
       prob >= 85 -> 30% of portfolio
       prob 75-84 -> 20% of portfolio
       prob 65-74 -> 10% of portfolio
       prob < 65  -> no trade
  3. Strict regime gate — score < 70 = cash only, no exceptions
  4. Asymmetric exits — small stops, let runners run
       Stop: 9% | TP1: 18% (sell 50%) | Trail: 8% from +12%
  5. Compounding engine
       New equity high -> position size cap +2%
       Drawdown >10%   -> position size cap -5%

What this trades away: trade frequency and low stress.
You WILL sit in cash for long periods. That is correct behavior.
"""

import time
import argparse
from datetime import datetime
from typing import Dict, Optional, List

from solana_trading_agent import SolanaTradingAgent, TradingMode, TOKENS
from trending_tokens import EnhancedTrendingTokenDetector, AutoTokenSelector
from hwr_signal_engine import HWRSignalEngine


# ─────────────────────────────────────────────────────────────────────────────
# PROBABILITY-WEIGHTED POSITION SIZING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class PositionSizer:
    _max_multiplier = 1.40   # class-level attribute for external reference
    _min_multiplier = 0.50   # class-level attribute for external reference
    """
    Scales position size to probability conviction.
    Base sizes are adjusted dynamically by the compounding engine.
    """

    # Base size tiers (% of total portfolio)
    TIER_HIGH   = 0.30   # prob >= 85
    TIER_MED    = 0.20   # prob 75-84
    TIER_LOW    = 0.10   # prob 65-74
    MIN_PROB    = 65.0   # below this = no trade

    def __init__(self):
        self._size_multiplier = 1.0    # adjusted by compounding engine
        self._max_multiplier  = 1.40   # cap: never go above 1.4x base sizes
        self._min_multiplier  = 0.50   # floor: never below 0.5x base sizes

    def get_size(self, prob_score: float, total_portfolio: float,
                 available_usdc: float) -> float:
        """
        Returns USDC amount to deploy based on probability score.
        Returns 0.0 if prob_score is below minimum threshold.
        """
        if prob_score < self.MIN_PROB:
            return 0.0

        if prob_score >= 85:
            base_pct = self.TIER_HIGH
            tier     = "HIGH"
        elif prob_score >= 75:
            base_pct = self.TIER_MED
            tier     = "MED"
        else:
            base_pct = self.TIER_LOW
            tier     = "LOW"

        adjusted_pct = base_pct * self._size_multiplier
        usdc_target  = total_portfolio * adjusted_pct

        # Never use more than 95% of available USDC in one trade
        usdc_amt = min(usdc_target, available_usdc * 0.95)
        return usdc_amt, tier, adjusted_pct

    def on_new_equity_high(self):
        """Called when portfolio value exceeds previous all-time high."""
        old = self._size_multiplier
        self._size_multiplier = min(self._size_multiplier + 0.02,
                                    self._max_multiplier)
        if self._size_multiplier != old:
            print(f"  [compound] New equity high → size multiplier "
                  f"{old:.2f}x → {self._size_multiplier:.2f}x")

    def on_drawdown(self, drawdown_pct: float):
        """Called when portfolio drops >10% from peak."""
        if drawdown_pct > 10.0:
            old = self._size_multiplier
            self._size_multiplier = max(self._size_multiplier - 0.05,
                                        self._min_multiplier)
            if self._size_multiplier != old:
                print(f"  [compound] Drawdown {drawdown_pct:.1f}% → size multiplier "
                      f"{old:.2f}x → {self._size_multiplier:.2f}x  (capital protection)")

    @property
    def multiplier(self) -> float:
        return self._size_multiplier


# ─────────────────────────────────────────────────────────────────────────────
# COMPOUNDING ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CompoundingEngine:
    """
    Tracks equity high-water mark and drawdown.
    Calls PositionSizer callbacks to adjust size dynamically.
    """

    def __init__(self, sizer: PositionSizer, starting_equity: float):
        self._hwm      = starting_equity   # high-water mark
        self._sizer    = sizer
        self._last_dd  = 0.0              # last reported drawdown

    def update(self, current_equity: float):
        if current_equity > self._hwm:
            self._hwm = current_equity
            self._sizer.on_new_equity_high()
            return

        if self._hwm > 0:
            dd = (self._hwm - current_equity) / self._hwm * 100
            if dd > 10.0 and abs(dd - self._last_dd) > 1.0:
                self._last_dd = dd
                self._sizer.on_drawdown(dd)

    @property
    def hwm(self) -> float:
        return self._hwm

    def drawdown_pct(self, current_equity: float) -> float:
        if self._hwm <= 0:
            return 0.0
        return max(0.0, (self._hwm - current_equity) / self._hwm * 100)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TRADER
# ─────────────────────────────────────────────────────────────────────────────

class HWRGrowthTrader:

    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent         = agent
        self.auto_discover = auto_discover
        self.running       = False
        self.engine        = HWRSignalEngine()

        # ── Position sizing + compounding ─────────────────────────────────────
        starting_equity    = agent.portfolio.get_total_value()
        self.sizer         = PositionSizer()
        self.compounder    = CompoundingEngine(self.sizer, starting_equity)

        # ── Entry parameters (regime-adjusted as side-effect of regime gate) ──
        self.min_soft_score = 3      # 4/7 base; drops to 3 in strong regimes
        self.max_concurrent = 2

        # ── Exit parameters ───────────────────────────────────────────────────
        self.stop_loss_pct   = HWRSignalEngine.STOP_LOSS_PCT   # 9%
        self.tp1_pct         = HWRSignalEngine.TP1_PCT          # 18%
        self.tp1_amount      = HWRSignalEngine.TP1_AMOUNT       # 50%
        self.trail_trigger   = HWRSignalEngine.TRAIL_TRIGGER    # 12%
        self.trail_dist      = HWRSignalEngine.TRAIL_DIST       # 8%

        # ── Risk ──────────────────────────────────────────────────────────────
        self.max_trades_per_day  = 10      # HWR+G: quality not quantity
        self.trade_cooldown_secs = 300     # 5 min per token
        self.min_live_candles    = 20

        # ── State tracking ────────────────────────────────────────────────────
        self.last_trades:    Dict[str, datetime] = {}
        self.position_peaks: Dict[str, float]    = {}
        self.tp1_hit:        Dict[str, bool]     = {}
        self.tp_lockout:     Dict[str, float]    = {}
        self.tp_lockout_secs = 600

        self.trades_today = 0
        self.day_start    = datetime.now().date()

        self.sl_strikes:   Dict[str, int] = {}
        self.sl_blacklist: set            = set()

        # ── Discovery ─────────────────────────────────────────────────────────
        self.refresh_interval        = 600
        self.last_refresh            = 0
        self.last_forced_refresh     = 0
        self.forced_refresh_cooldown = 120
        self.detector                = EnhancedTrendingTokenDetector()
        self.selector                = AutoTokenSelector(self.detector)
        self.token_metadata:          Dict[str, Dict] = {}

        # ── Regime state ──────────────────────────────────────────────────────
        self._regime_was_strong = False
        self._current_score     = 50.0

        # ── Session tracking ──────────────────────────────────────────────────
        self.session_trades: List[Dict] = []

    # ─────────────────────────────────────────────────────────────────────────
    # REGIME GATE  (strict: < 70 = cash)
    # ─────────────────────────────────────────────────────────────────────────

    REGIME_MIN = 70   # hard floor — spec says "raise to 70"

    def _check_regime(self, tokens_analysis: Dict) -> tuple:
        """
        Returns (allowed, score, label).
        Side-effects: adjusts min_soft_score, max_concurrent, sizer tier hints.
        Score < 70 = no new entries, period.
        """
        analyzer = getattr(self.agent, "market_analyzer", None)
        if not analyzer:
            return True, 50.0, "no analyzer"

        # When no tokens are watched, reuse the last cached score rather than
        # defaulting to 50 (which silently blocks the regime gate forever).
        if not tokens_analysis:
            cached_score = getattr(analyzer, "_smoothed_score", None)
            if cached_score is not None:
                self._current_score = cached_score
                self.selector.set_regime(cached_score)
                lbl = "🟢 STRONG" if cached_score >= 80 else ("🟡 MODERATE" if cached_score >= 70 else "🔴 WEAK")
                return cached_score >= 70, cached_score, f"  [regime] {lbl} ({cached_score:.0f})  no tokens watched"
            return True, 50.0, "  [regime] no data yet — allowing discovery"

        status, reason, details = analyzer.analyze(tokens_analysis)
        score = details["overall_score"]
        self._current_score = score

        self.selector.set_regime(score)

        # Regime flip -> force token refresh
        is_strong = score >= self.REGIME_MIN
        if is_strong and not self._regime_was_strong:
            since_last = time.time() - self.last_forced_refresh
            if since_last > self.forced_refresh_cooldown:
                print("  🔄 Regime crossed 70 — refreshing token universe")
                self.refresh_tokens()
                self.last_forced_refresh = time.time()
        self._regime_was_strong = is_strong

        if score < self.REGIME_MIN:
            return False, score, f"⏸️  score {score:.0f} < {self.REGIME_MIN} — cash only"

        # Tighten/relax soft score with regime strength
        if score >= 85:
            self.min_soft_score = 3
            self.max_concurrent = 2
            lbl = f"🟢 STRONG  ({score:.0f})"
        elif score >= 70:
            self.min_soft_score = 4
            self.max_concurrent = 2
            lbl = f"🟡 MODERATE ({score:.0f})"

        print(f"  [regime] {lbl}  "
              f"soft≥{self.min_soft_score}/7  "
              f"sizer×{self.sizer.multiplier:.2f}")
        return True, score, f"✅ regime {score:.0f}"

    # ─────────────────────────────────────────────────────────────────────────
    # DISCOVERY
    # ─────────────────────────────────────────────────────────────────────────

    def refresh_tokens(self):
        try:
            print("\n[discover] Scanning for HWR+G setups...")

            self.selector.preferences.update({
                "min_liquidity":        50_000,    # signal engine re-checks at 100k; this just pre-screens
                "min_volume":           30_000,    # low to let more tokens through for evaluation
                "max_tokens":           10,         # more candidates = more chances to find setups
                "prefer_rising":        True,
                "max_price_change_24h": 2000,       # discovery cap; signal engine enforces 500% hard filter
                "prioritize_boosted":   True,
                "min_score":            25.0,       # score gate is very late; let signal engine decide
            })

            selected = self.selector.get_token_symbols()

            # Always display full trending table
            trending = self.detector.get_trending_tokens(
                min_liquidity=30_000, min_volume_24h=30_000,
                max_tokens=20, prioritize_boosted=True)
            if trending:
                self.detector.display_trending(trending)

            if not selected:
                print("[discover] No tokens pass HWR+G criteria — holding cash")
                self.last_refresh = time.time()
                return

            print("\n[discover] QUALIFIED TOKENS:")
            print("-" * 65)
            added = skipped_rsi = skipped_liq = 0

            for token in selected:
                sym       = token["symbol"]
                addr      = token["address"]
                pair_addr = token.get("pair_address", "")
                liq       = token.get("liquidity_usd", 0)
                ch24      = token.get("price_change_24h", 0)
                ch1       = token.get("price_change_h1", 0)
                score_t   = token.get("score", 0)
                quality   = token.get("quality_indicator", "")
                boosted   = token.get("is_boosted", False)

                if sym in self.agent.watched_tokens:
                    continue

                # Discovery liq gate — lower than signal engine's hard filter
                # because the signal engine re-checks at MIN_LIQUIDITY_USD during analysis.
                DISCOVERY_MIN_LIQ = 40_000
                if liq < DISCOVERY_MIN_LIQ:
                    print(f"  [skip] {sym}: liq ${liq/1000:.0f}k below ${DISCOVERY_MIN_LIQ/1000:.0f}k")
                    skipped_liq += 1
                    continue

                if sym not in TOKENS:
                    TOKENS[sym] = addr
                self.agent.add_token(sym, addr)

                self.token_metadata[sym] = {
                    "is_boosted":           boosted,
                    "quality_indicator":    quality,
                    "score":                score_t,
                    "price_change_24h":     ch24,
                    "price_change_h1":      ch1,
                    "liquidity_usd":        liq,
                    "liquidity_change_pct": token.get("liquidity_change_pct", 0.0),
                }

                print(f"  Fetching {sym} {'[BOOST]' if boosted else ''}...")
                hist, is_real = self.detector.fetch_historical_prices(
                    pair_addr, n_points=1000, token_info=token)

                if not hist:
                    print(f"  [skip] No price history")
                    if sym in self.agent.watched_tokens: self.agent.watched_tokens.remove(sym)
                    self.token_metadata.pop(sym, None)
                    continue

                rsi_val = _quick_rsi(hist)
                if rsi_val and rsi_val > 70:
                    print(f"  [skip] RSI {rsi_val:.0f} > 70 — overbought at discovery")
                    if sym in self.agent.watched_tokens: self.agent.watched_tokens.remove(sym)
                    self.token_metadata.pop(sym, None)
                    skipped_rsi += 1
                    continue

                self.agent.prefill_price_history(sym, hist)
                buf = self.agent.price_history[sym]
                if is_real:
                    buf["live_points"] = len(hist)
                    print(f"  [OK] {len(hist)} real candles")
                else:
                    buf["live_points"] = 0
                    print(f"  [~] Synthetic — gated until {self.min_live_candles} live")

                rsi_s = f"{rsi_val:.0f}" if rsi_val else "N/A"
                print(f"  → {added+1}. {sym:<10} {quality}  "
                      f"score={score_t:.0f}  24h={ch24:+.0f}%  "
                      f"1h={ch1:+.1f}%  liq=${liq/1000:.0f}k  RSI={rsi_s}")
                added += 1

            print("-" * 65)
            print(f"[discover] Added:{added}  SkippedRSI:{skipped_rsi}  "
                  f"SkippedLiq:{skipped_liq}  "
                  f"Watching:{len(self.agent.watched_tokens)}\n")
            self.last_refresh = time.time()

        except Exception as e:
            import traceback
            print(f"[discover] Error: {e}")
            traceback.print_exc()
            self.last_refresh = time.time() - self.refresh_interval + 120

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY GATE
    # ─────────────────────────────────────────────────────────────────────────

    def _can_enter(self, token: str, analysis: Dict) -> bool:
        """All non-signal checks for a new BUY entry."""
        if token in self.sl_blacklist:
            return False

        if analysis.get("hard_fail"):
            return False

        prob = analysis.get("prob_score", 0.0)
        if prob < PositionSizer.MIN_PROB:
            print(f"  [skip] prob {prob:.0f} < {PositionSizer.MIN_PROB:.0f}")
            return False

        if token in self.tp_lockout:
            if time.time() < self.tp_lockout[token]:
                rem = int(self.tp_lockout[token] - time.time())
                print(f"  [skip] TP lockout {rem}s remaining")
                return False
            del self.tp_lockout[token]

        if token in self.last_trades:
            elapsed = (datetime.now() - self.last_trades[token]).total_seconds()
            if elapsed < self.trade_cooldown_secs:
                print(f"  [skip] cooldown {int(self.trade_cooldown_secs - elapsed)}s")
                return False

        # Day reset
        if datetime.now().date() != self.day_start:
            self.trades_today = 0
            self.day_start    = datetime.now().date()

        if self.trades_today >= self.max_trades_per_day:
            print(f"  [skip] daily limit {self.trades_today}/{self.max_trades_per_day}")
            return False

        if token in self.agent.portfolio.positions:
            print(f"  [skip] already holding")
            return False

        if len(self.agent.portfolio.positions) >= self.max_concurrent:
            print(f"  [skip] max {self.max_concurrent} concurrent positions")
            return False

        total = self.agent.portfolio.get_total_value()
        if self.agent.portfolio.usdc_balance < total * 0.05:
            print(f"  [skip] insufficient USDC")
            return False

        if analysis.get("buy_pts", 0) < self.min_soft_score:
            print(f"  [skip] soft score {analysis.get('buy_pts',0)} < {self.min_soft_score}")
            return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # POSITION MONITORING  (asymmetric exits)
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_exits(self):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl = position.pnl_percentage

            # Track peak
            if token not in self.position_peaks or pnl > self.position_peaks[token]:
                self.position_peaks[token] = pnl
            peak = self.position_peaks[token]

            sl = self.stop_loss_pct * 100

            # ── Hard stop ────────────────────────────────────────────────────
            if pnl <= -sl:
                print(f"\n  ❌ STOP {token}  P&L:{pnl:+.1f}%  (limit -{sl:.0f}%)")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.sl_strikes[token]  = self.sl_strikes.get(token, 0) + 1
                    if self.sl_strikes[token] >= 2:
                        self.sl_blacklist.add(token)
                        print(f"  🚫 Blacklisted {token} (2 stops)")
                    self._record(token, "STOP", pnl, position.pnl)
                    self.position_peaks.pop(token, None)
                    self.tp1_hit.pop(token, None)
                continue

            # ── TP1: 18% — sell 50% ──────────────────────────────────────────
            tp1 = self.tp1_pct * 100
            if pnl >= tp1 and not self.tp1_hit.get(token, False):
                sell_amt = position.amount * self.tp1_amount
                print(f"\n  💰 TP1 {token}  P&L:{pnl:+.1f}%  "
                      f"Locking {self.tp1_amount*100:.0f}% profit")
                trade = self.agent.execute_swap(token, "USDC", sell_amt)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp1_hit[token]     = True
                    self._record(token, "TP1", pnl, position.pnl * self.tp1_amount)
                continue

            # ── Trail: 8% from peak, activates at +12% ───────────────────────
            if peak >= self.trail_trigger:
                floor = peak - self.trail_dist
                if pnl <= floor:
                    print(f"\n  🏁 TRAIL {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  Floor:{floor:+.1f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                        self._record(token, "TRAIL", pnl, position.pnl)
                        self.position_peaks.pop(token, None)
                        self.tp1_hit.pop(token, None)
                else:
                    print(f"  🏃 trail {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  Floor:{floor:+.1f}%")

    def _record(self, token: str, exit_type: str, pnl_pct: float, pnl_usd: float):
        self.session_trades.append({
            "token": token, "exit": exit_type,
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "timestamp": datetime.now(),
            "win": pnl_pct > 0,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTE TRADE
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_buy(self, token: str, analysis: Dict):
        prob  = analysis.get("prob_score", 0.0)
        cur   = analysis.get("current_price", 0.0)
        if cur == 0:
            return

        total    = self.agent.portfolio.get_total_value()
        result   = self.sizer.get_size(prob, total, self.agent.portfolio.usdc_balance)

        if result == 0.0:
            print(f"  [skip] prob {prob:.0f} below sizer minimum")
            return

        usdc_amt, tier, adj_pct = result

        if usdc_amt < 10:
            print(f"  [skip] size ${usdc_amt:.2f} too small")
            return

        detail = analysis.get("buy_pts_detail", {})
        flags  = "".join([
            "T" if detail.get("trend")    else ".",
            "R" if detail.get("rsi_rise") else ".",
            "M" if detail.get("macd")     else ".",
            "S" if detail.get("slope")    else ".",
            "V" if detail.get("vol")      else ".",
            "P" if detail.get("pullback") else ".",
            "X" if detail.get("ema_x")    else ".",
        ])
        boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
        tag     = " [B]" if boosted else ""

        print(f"\n  ══ BUY{tag} {token} ══")
        print(f"     Prob:       {prob:.0f}%  →  tier {tier}")
        print(f"     Size:       ${usdc_amt:.2f}  ({adj_pct*100:.0f}% of portfolio)")
        print(f"     Sizer ×:    {self.sizer.multiplier:.2f}  (compounding)")
        print(f"     Price:      ${cur:.8f}")
        print(f"     RSI:        {analysis.get('rsi', 0):.0f}")
        print(f"     Soft:       {analysis.get('buy_pts',0)}/7  [{flags}]")
        print(f"     Stop:       -{self.stop_loss_pct*100:.0f}%  "
              f"TP1: +{self.tp1_pct*100:.0f}%  "
              f"Trail: +{self.trail_trigger:.0f}%/{self.trail_dist:.0f}%")

        trade = self.agent.execute_swap("USDC", token, usdc_amt)
        if trade:
            self.last_trades[token]    = datetime.now()
            self.trades_today         += 1
            self.position_peaks[token] = 0.0
            self.tp1_hit[token]        = False
            print(f"  ✅ Executed")
        else:
            print(f"  ❌ Failed")

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION STATS
    # ─────────────────────────────────────────────────────────────────────────

    def _print_stats(self):
        if not self.session_trades:
            return
        wins   = [t for t in self.session_trades if t["win"]]
        losses = [t for t in self.session_trades if not t["win"]]
        wr     = len(wins) / len(self.session_trades) * 100
        pnl    = sum(t["pnl_usd"] for t in self.session_trades)

        print(f"\n  ── Session Stats ──────────────────────────────")
        print(f"     Trades: {len(self.session_trades)}  "
              f"WR: {wr:.0f}%  ({len(wins)}W/{len(losses)}L)  "
              f"Net: ${pnl:+.2f}")
        if wins:
            print(f"     Avg win:  +{sum(t['pnl_pct'] for t in wins)/len(wins):.1f}%")
        if losses:
            print(f"     Avg loss:  {sum(t['pnl_pct'] for t in losses)/len(losses):.1f}%")
        print(f"     Sizer ×{self.sizer.multiplier:.2f}  "
              f"EqHWM: ${self.compounder.hwm:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, update_interval: int = 30):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 65)
        print("  SOLANA HWR + RAPID GROWTH TRADER  —  v2.0")
        print("=" * 65)
        print(f"\nPhilosophy: Trade LESS, size UP on conviction")
        print(f"\nRegime gate:  score ≥ {self.REGIME_MIN} (strict)")
        print(f"Hard filters: liq>${HWRSignalEngine.MIN_LIQUIDITY_USD/1000:.0f}k  "
              f"24h 5-{HWRSignalEngine.MAX_24H_CHANGE_PCT:.0f}%  "
              f"1h>{HWRSignalEngine.MIN_1H_CHANGE_PCT:.0f}%  "
              f"RSI {HWRSignalEngine.MIN_RSI}-{HWRSignalEngine.MAX_RSI}  "
              f"pressure>{HWRSignalEngine.MIN_BUY_PRESSURE:.2f}")
        print(f"\nPosition sizing (prob-weighted):")
        print(f"  prob ≥85%  → {PositionSizer.TIER_HIGH*100:.0f}% of portfolio")
        print(f"  prob 75-84 → {PositionSizer.TIER_MED*100:.0f}% of portfolio")
        print(f"  prob 65-74 → {PositionSizer.TIER_LOW*100:.0f}% of portfolio")
        print(f"  prob <65   → no trade")
        print(f"\nCompounding:")
        print(f"  New equity high → sizer ×+0.02 (cap {PositionSizer._max_multiplier}x)")
        print(f"  Drawdown >10%   → sizer ×-0.05 (floor {PositionSizer._min_multiplier}x)")
        print(f"\nExits (asymmetric):")
        print(f"  Stop:  -{self.stop_loss_pct*100:.0f}%")
        print(f"  TP1:   +{self.tp1_pct*100:.0f}% (sell {self.tp1_amount*100:.0f}%)")
        print(f"  Trail: +{self.trail_trigger:.0f}% trigger / {self.trail_dist:.0f}% distance")
        print(f"\nMax trades/day: {self.max_trades_per_day}  "
              f"Max concurrent: {self.max_concurrent}")
        print(f"\nCtrl+C to stop")
        print("=" * 65)

        iteration = 0
        try:
            while self.running:
                iteration += 1
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n{'='*65}")
                print(f"Cycle #{iteration} — {now_str}")
                print(f"{'='*65}")

                # Invalidate regime cache each cycle (prevents EMA drift)
                if hasattr(self.agent, "market_analyzer"):
                    self.agent.market_analyzer.invalidate_cache()

                # Token discovery
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_tokens()

                # Market data update
                print("\n[market] Updating prices...")
                self.agent.update_market_data()

                # Run analysis on all watched tokens
                SKIP_SET = SKIP
                tokens_analysis: Dict[str, Dict] = {}

                for token in list(self.agent.watched_tokens):
                    if token in SKIP_SET:
                        continue
                    buf      = self.agent.price_history.get(token, {})
                    live_pts = buf.get("live_points", 0)
                    prices   = buf.get("prices",      [])
                    volumes  = buf.get("volumes",     [])

                    # Skip tokens without enough real data
                    if live_pts < self.min_live_candles or not prices:
                        continue

                    meta     = self.token_metadata.get(token, {})
                    analysis = self.engine.analyze(
                        token, prices, volumes,
                        token_meta=meta,
                        min_soft_score=self.min_soft_score,
                    )
                    tokens_analysis[token] = analysis

                # ── Compounding engine update ─────────────────────────────────
                current_equity = self.agent.portfolio.get_total_value()
                self.compounder.update(current_equity)
                dd = self.compounder.drawdown_pct(current_equity)

                # ── Regime gate ───────────────────────────────────────────────
                allowed, regime_score, regime_msg = self._check_regime(tokens_analysis)
                print(f"\n{regime_msg}")

                if not allowed:
                    # Still monitor exits even in paused regime
                    if self.agent.portfolio.positions:
                        print("[positions] Monitoring stops only...")
                        self._monitor_exits()
                else:
                    # Monitor exits first
                    if self.agent.portfolio.positions:
                        print("\n[positions] Monitoring exits...")
                        self._monitor_exits()

                    # Scan for entries
                    print("\n[analysis] Scanning tokens...")
                    qualified = []

                    for token, analysis in tokens_analysis.items():
                        meta       = self.token_metadata.get(token, {})
                        boosted    = meta.get("is_boosted", False)
                        boost_tag  = "[B]" if boosted else "   "
                        hard_fail  = analysis.get("hard_fail", False)
                        hard_msg   = analysis.get("hard_fail_reason", "")
                        soft       = analysis.get("buy_pts", 0)
                        prob       = analysis.get("prob_score", 0.0)
                        rel_v      = analysis.get("relative_volume", 1.0)
                        ts         = datetime.now().strftime("%H:%M:%S")

                        detail = analysis.get("buy_pts_detail", {})
                        flags  = "".join([
                            "T" if detail.get("trend")    else ".",
                            "R" if detail.get("rsi_rise") else ".",
                            "M" if detail.get("macd")     else ".",
                            "S" if detail.get("slope")    else ".",
                            "V" if detail.get("vol")      else ".",
                            "P" if detail.get("pullback") else ".",
                            "X" if detail.get("ema_x")    else ".",
                        ])

                        sig  = analysis["signal"]
                        sd   = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(sig, "HOLD")

                        if hard_fail:
                            score_str = "✗HARD"
                        else:
                            score_str = f"{soft}/7[{flags}] p{prob:.0f}%"

                        print(f"  [{ts}] {boost_tag}{token:<12}  "
                              f"${analysis['current_price']:<12.6f}  "
                              f"RSI:{analysis['rsi']:.0f}  "
                              f"{sd}  Vol:{rel_v:.1f}x  {score_str}")

                        if hard_fail:
                            print(f"           ↳ {hard_msg[:80]}")
                        elif sig == "BUY":
                            qualified.append((token, analysis))

                    # Execute entries
                    for token, analysis in qualified:
                        if self._can_enter(token, analysis):
                            self._execute_buy(token, analysis)

                # ── Portfolio display ─────────────────────────────────────────
                perf = self.agent.portfolio.get_performance()
                print(f"\n[portfolio]")
                print(f"  USDC:         ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total:        ${perf['current_value']:.2f}")
                print(f"  Return:       ${perf['total_return']:+.2f} "
                      f"({perf['total_return_pct']:+.2f}%)")
                print(f"  HWM:          ${self.compounder.hwm:.2f}  "
                      f"DD: {dd:.1f}%")
                print(f"  Sizer ×:      {self.sizer.multiplier:.2f}")
                print(f"  Trades today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Regime:       {self._current_score:.0f}/100  "
                      f"(gate≥{self.REGIME_MIN})")
                print(f"  Watching:     {len(self.agent.watched_tokens)} tokens")

                if self.agent.portfolio.positions:
                    print(f"\n  Open positions:")
                    for tok, pos in self.agent.portfolio.positions.items():
                        peak  = self.position_peaks.get(tok, 0)
                        b_tag = "[B]" if self.token_metadata.get(tok, {}).get("is_boosted") else "   "
                        tp1_d = "✓TP1" if self.tp1_hit.get(tok) else "    "
                        print(f"    {b_tag}{tok:<12}  "
                              f"P&L:{pos.pnl_percentage:+.1f}%  "
                              f"Peak:{peak:+.1f}%  "
                              f"{tp1_d}  "
                              f"Stop:-{self.stop_loss_pct*100:.0f}%")

                if self.sl_blacklist:
                    print(f"\n  Blacklisted: {', '.join(self.sl_blacklist)}")

                self._print_stats()

                nxt = max(0, int(self.refresh_interval - (time.time() - self.last_refresh)))
                print(f"\n[wait] {update_interval}s  |  "
                      f"Discovery in {nxt//60}m{nxt%60}s")
                time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\n[stop] Shutting down...")
            self.running = False
            print("\n" + "=" * 65)
            print("  SESSION COMPLETE")
            print("=" * 65)
            self._print_stats()
            self.agent.show_portfolio()
            print()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _quick_rsi(prices, period=14):
    if len(prices) < period + 1:
        return None
    gains  = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solana HWR+Growth Trader v2.0")
    parser.add_argument("--mode",             choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",           default="")
    parser.add_argument("--balance",          type=float, default=1000.0)
    parser.add_argument("--interval",         type=int,   default=30)
    parser.add_argument("--refresh-interval", type=int,   default=600)
    parser.add_argument("--no-auto-discover", action="store_true")
    parser.add_argument("--manual-tokens",    nargs="+")

    args = parser.parse_args()
    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER

    wallet_instance = None
    if mode == TradingMode.LIVE:
        try:
            from wallet_integration import load_wallet_config, SolanaWallet
            cfg = load_wallet_config()
            if not cfg:
                print("[ERROR] No wallet config. Run: python setup_wallet.py --setup")
                return
            wallet_instance = SolanaWallet(cfg)
            sol_bal = wallet_instance.get_balance()
            if sol_bal < 0.01:
                if input(f"Low SOL ({sol_bal:.4f}). Continue? (yes/no): ").lower() not in ("yes", "y"):
                    return
            if input("\nType 'START' to confirm live trading: ") != "START":
                print("Cancelled.")
                return
        except ImportError:
            print("[ERROR] pip install solana solders base58")
            return
        except Exception as e:
            print(f"[ERROR] {e}")
            return

    agent = SolanaTradingAgent(mode, args.wallet, args.balance, wallet=wallet_instance)

    from market_condition_analyzer import MarketConditionAnalyzer
    agent.market_analyzer = MarketConditionAnalyzer()
    print("[market] MarketConditionAnalyzer v8 attached.")

    auto_discover = not args.no_auto_discover
    if not auto_discover and args.manual_tokens:
        for tok in args.manual_tokens:
            agent.add_token(tok.upper())

    trader = HWRGrowthTrader(agent, auto_discover=auto_discover)
    trader.refresh_interval = args.refresh_interval

    if auto_discover:
        print("\n[discover] Initial token scan...")
        trader.refresh_tokens()

    print("\n[market] Warming up (20 ticks)...")
    for i in range(20):
        agent.update_market_data()
        if (i + 1) % 5 == 0:
            print(f"  {i+1}/20")
        time.sleep(1)
    print("[OK] Ready\n")

    trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()
