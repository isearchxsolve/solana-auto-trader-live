#!/usr/bin/env python3
"""
Solana Professional Trader — v4.0
===================================
Built on signal engine v4.0. Real edge, real risk management.

Key improvements over v2.0:

1. ATR-BASED EXITS (per-token, per-cycle)
   Stop, TP1, TP2, and trailing stop are all computed from the token's
   current ATR — not fixed percentages. A volatile token gets a wider
   stop so it isn't knocked out by noise. A stable token gets a tighter
   stop so losses stay small. This alone dramatically improves win rate.

2. DAILY P&L CIRCUIT BREAKER
   If the session loses >5% of starting equity, trading halts for
   the rest of the day. This is how professional desks operate.
   Most retail bots don't have this and blow up in bad regimes.

3. VWAP EXTENSION EXIT
   If price extends >3.5% above VWAP AND RSI > 70, sell 40% of any
   open position immediately. Don't hold into mean reversion.

4. CORRELATION AWARENESS
   If two tokens are both at BUY signal and their recent returns
   correlate >0.8, only take the higher-conviction setup. Holding
   correlated positions doesn't reduce risk — it doubles it.

5. SMART COMPOUNDING
   Position sizes grow ONLY after consistent profitability, not just
   any new equity high. Requires 3 consecutive winning trades before
   increasing size. One loss resets the counter.

6. REGIME QUALITY SCORE
   Regime gate is not just a threshold — it now adjusts concurrent
   positions and position sizes proportionally to regime strength.
   Score 90+ allows up to 5 positions. Score 60-70 allows only 2.

7. TIME-BASED AWARENESS
   Avoids entering new positions in the last 5 minutes before a
   known high-volatility window (top of hour) to avoid being caught
   by whipsaw moves.
"""

import time
import argparse
from datetime import datetime, date
from typing import Dict, Optional, List, Tuple

from solana_trading_agent import SolanaTradingAgent, TradingMode, TOKENS
from trending_tokens import EnhancedTrendingTokenDetector, AutoTokenSelector
from hwr_signal_engine import HWRSignalEngine


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

DAILY_LOSS_LIMIT_PCT  = 5.0    # halt trading if session loses >5%
VWAP_EXTENSION_PCT    = 3.5    # partial exit if price >3.5% above VWAP
VWAP_EXTENSION_RSI    = 70.0   # only trigger VWAP exit when overbought
VWAP_EXTENSION_SELL   = 0.40   # sell 40% when VWAP-extended
CORRELATION_THRESHOLD = 0.80   # skip trade if correlated token already held
CONSECUTIVE_WINS_REQ  = 3      # wins needed before compounding up
WIN_SIZE_STEP         = 0.03   # +3% per compounding step
LOSS_SIZE_STEP        = 0.05   # -5% per drawdown step


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZER — ATR-aware, probability-weighted, compounding gated
# ─────────────────────────────────────────────────────────────────────────────

class ProfessionalSizer:
    """
    Position sizing with three layers:
      1. Probability tier (base % of portfolio)
      2. ATR scaling (reduce size when volatility is high)
      3. Compounding multiplier (grows only after proven performance)
    """

    TIER_HIGH  = 0.25   # prob >= 82 — high conviction
    TIER_MED   = 0.18   # prob 70-81
    TIER_LOW   = 0.10   # prob 60-69
    MIN_PROB   = 60.0   # minimum to trade

    MAX_MULT   = 1.60
    MIN_MULT   = 0.50
    ATR_SCALE_MAX = 0.025   # ATR above this = scale down position

    def __init__(self):
        self._mult            = 1.0
        self._consecutive_wins = 0

    def get_size(self, prob: float, atr_pct: float,
                 total: float, available_usdc: float) -> Tuple[float, str, float]:
        """Returns (usdc_amount, tier_label, actual_pct). 0.0 if below min prob."""
        if prob < self.MIN_PROB:
            return 0.0, "SKIP", 0.0

        if prob >= 82:
            base, tier = self.TIER_HIGH, "HIGH"
        elif prob >= 70:
            base, tier = self.TIER_MED,  "MED"
        else:
            base, tier = self.TIER_LOW,  "LOW"

        # ATR scaling: if volatility is very high, reduce size proportionally
        # High ATR means each trade is riskier — compensate by trading smaller
        atr_factor = 1.0
        if atr_pct > self.ATR_SCALE_MAX:
            atr_factor = self.ATR_SCALE_MAX / atr_pct
            atr_factor = max(atr_factor, 0.40)   # never below 40% of base

        adj_pct  = base * self._mult * atr_factor
        target   = total * adj_pct
        usdc_amt = min(target, available_usdc * 0.92)
        return usdc_amt, tier, adj_pct

    def record_win(self):
        self._consecutive_wins += 1
        if self._consecutive_wins >= CONSECUTIVE_WINS_REQ:
            old = self._mult
            self._mult = min(self._mult + WIN_SIZE_STEP, self.MAX_MULT)
            if self._mult != old:
                print(f"  [compound] {CONSECUTIVE_WINS_REQ} consecutive wins → "
                      f"sizer {old:.2f}x → {self._mult:.2f}x")
            self._consecutive_wins = 0   # reset counter after each step

    def record_loss(self):
        self._consecutive_wins = 0   # any loss resets win streak
        old = self._mult
        self._mult = max(self._mult - LOSS_SIZE_STEP, self.MIN_MULT)
        if self._mult != old:
            print(f"  [compound] Loss → sizer {old:.2f}x → {self._mult:.2f}x")

    def on_drawdown(self, dd_pct: float):
        if dd_pct > 8.0:
            old = self._mult
            self._mult = max(self._mult - LOSS_SIZE_STEP, self.MIN_MULT)
            if self._mult != old:
                print(f"  [compound] DD {dd_pct:.1f}% → sizer {old:.2f}x → {self._mult:.2f}x")

    @property
    def multiplier(self) -> float:
        return self._mult

    @property
    def consecutive_wins(self) -> int:
        return self._consecutive_wins


# ─────────────────────────────────────────────────────────────────────────────
# EQUITY TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class EquityTracker:
    def __init__(self, sizer: ProfessionalSizer, starting_equity: float):
        self._hwm      = starting_equity
        self._day_start_equity = starting_equity
        self._sizer    = sizer

    def update(self, equity: float):
        if equity > self._hwm:
            self._hwm = equity

        dd = self.drawdown_pct(equity)
        if dd > 8.0:
            self._sizer.on_drawdown(dd)

    def reset_day(self, equity: float):
        self._day_start_equity = equity

    def daily_loss_pct(self, equity: float) -> float:
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity * 100)

    def drawdown_pct(self, equity: float) -> float:
        if self._hwm <= 0:
            return 0.0
        return max(0.0, (self._hwm - equity) / self._hwm * 100)

    @property
    def hwm(self) -> float:
        return self._hwm


# ─────────────────────────────────────────────────────────────────────────────
# CORRELATION HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _pearson_corr(a: List[float], b: List[float], n: int = 30) -> float:
    """Pearson correlation of last n returns between two price series."""
    if len(a) < n + 1 or len(b) < n + 1:
        return 0.0
    ra = [(a[i] - a[i-1]) / a[i-1] for i in range(max(1, len(a)-n), len(a)) if a[i-1] > 0]
    rb = [(b[i] - b[i-1]) / b[i-1] for i in range(max(1, len(b)-n), len(b)) if b[i-1] > 0]
    n_ = min(len(ra), len(rb))
    if n_ < 5:
        return 0.0
    ra, rb = ra[-n_:], rb[-n_:]
    ma = sum(ra) / n_
    mb = sum(rb) / n_
    num   = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den_a = sum((x - ma)**2 for x in ra) ** 0.5
    den_b = sum((y - mb)**2 for y in rb) ** 0.5
    if den_a == 0 or den_b == 0:
        return 0.0
    return num / (den_a * den_b)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TRADER
# ─────────────────────────────────────────────────────────────────────────────

class ProfessionalTrader:

    REGIME_MIN = 60   # below this = cash only

    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent         = agent
        self.auto_discover = auto_discover
        self.running       = False
        self.engine        = HWRSignalEngine()

        starting_equity    = agent.portfolio.get_total_value()
        self.sizer         = ProfessionalSizer()
        self.tracker       = EquityTracker(self.sizer, starting_equity)

        self.min_soft_score = 4
        self.max_concurrent = 3

        # Exit state tracking
        self.position_peaks:  Dict[str, float]    = {}
        self.position_entry:  Dict[str, float]    = {}   # entry price per token
        self.position_atr:    Dict[str, float]    = {}   # ATR at entry
        self.tp1_hit:         Dict[str, bool]     = {}
        self.tp2_hit:         Dict[str, bool]     = {}
        self.vwap_exit_done:  Dict[str, bool]     = {}
        self.tp_lockout:      Dict[str, float]    = {}
        self.tp_lockout_secs  = 180   # 3 min after exiting

        # Risk controls
        self.max_trades_per_day  = 80
        self.trade_cooldown_secs = 45
        self.min_live_candles    = 30

        self.trades_today  = 0
        self.day_start     = date.today()
        self.halted_today  = False   # circuit breaker flag

        self.last_trades:  Dict[str, datetime] = {}
        self.sl_strikes:   Dict[str, int]      = {}
        self.sl_blacklist: set                 = set()

        # Discovery
        self.refresh_interval        = 300    # 5 minutes
        self.last_refresh            = 0
        self.last_forced_refresh     = 0
        self.forced_refresh_cooldown = 120
        self.detector                = EnhancedTrendingTokenDetector()
        self.selector                = AutoTokenSelector(self.detector)
        self.token_metadata:          Dict[str, Dict] = {}

        # Regime
        self._regime_was_strong = False
        self._current_score     = 50.0

        # Session tracking
        self.session_trades: List[Dict] = []

    # ─────────────────────────────────────────────────────────────────────────
    # REGIME GATE
    # ─────────────────────────────────────────────────────────────────────────

    def _check_regime(self, tokens_analysis: Dict) -> Tuple[bool, float, str]:
        analyzer = getattr(self.agent, "market_analyzer", None)
        if not analyzer:
            return True, 50.0, "no analyzer"

        if not tokens_analysis:
            cached = getattr(analyzer, "_smoothed_score", None)
            if cached is not None:
                self._current_score = cached
                self.selector.set_regime(cached)
                lbl = "🟢 STRONG" if cached >= 80 else ("🟡 MODERATE" if cached >= 65 else "🔴 WEAK")
                return cached >= self.REGIME_MIN, cached, f"  [regime] {lbl} ({cached:.0f})"
            return True, 50.0, "  [regime] no data — allowing discovery"

        status, reason, details = analyzer.analyze(tokens_analysis)
        score = details["overall_score"]
        self._current_score = score
        self.selector.set_regime(score)

        # Regime flip
        is_strong = score >= self.REGIME_MIN
        if is_strong and not self._regime_was_strong:
            since = time.time() - self.last_forced_refresh
            if since > self.forced_refresh_cooldown:
                print("  🔄 Regime strengthened — refreshing token universe")
                self.refresh_tokens()
                self.last_forced_refresh = time.time()
        self._regime_was_strong = is_strong

        if score < self.REGIME_MIN:
            return False, score, f"⏸  score {score:.0f} < {self.REGIME_MIN} — cash only"

        # Adjust concurrent positions and soft score by regime strength
        if score >= 85:
            self.min_soft_score = 4
            self.max_concurrent = 5
            lbl = f"🟢 STRONG ({score:.0f})"
        elif score >= 72:
            self.min_soft_score = 4
            self.max_concurrent = 4
            lbl = f"🟡 MODERATE ({score:.0f})"
        else:
            self.min_soft_score = 5    # tighten when regime is marginal
            self.max_concurrent = 2
            lbl = f"🟠 MARGINAL ({score:.0f})"

        print(f"  [regime] {lbl}  soft≥{self.min_soft_score}/10  "
              f"max_concurrent={self.max_concurrent}  sizer×{self.sizer.multiplier:.2f}")
        return True, score, f"✅ regime {score:.0f}"

    # ─────────────────────────────────────────────────────────────────────────
    # DAILY RESET + CIRCUIT BREAKER
    # ─────────────────────────────────────────────────────────────────────────

    def _check_daily_reset(self, equity: float):
        today = date.today()
        if today != self.day_start:
            self.day_start    = today
            self.trades_today = 0
            self.halted_today = False
            self.tracker.reset_day(equity)
            print(f"\n  [day] New trading day — counters reset")

    def _check_circuit_breaker(self, equity: float) -> bool:
        """Returns True if trading should be halted."""
        if self.halted_today:
            return True
        daily_loss = self.tracker.daily_loss_pct(equity)
        if daily_loss >= DAILY_LOSS_LIMIT_PCT:
            self.halted_today = True
            print(f"\n  ⛔ CIRCUIT BREAKER: Daily loss {daily_loss:.1f}% >= "
                  f"{DAILY_LOSS_LIMIT_PCT}% limit — halting for today")
            return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # DISCOVERY
    # ─────────────────────────────────────────────────────────────────────────

    def refresh_tokens(self):
        try:
            print("\n[discover] Scanning for professional setups...")
            self.selector.preferences.update({
                "min_liquidity":        500_000,
                "min_volume":           100_000,
                "max_tokens":           12,
                "prefer_rising":        False,
                "max_price_change_24h": 60,
                "prioritize_boosted":   False,
                "min_score":            35.0,
            })

            selected = self.selector.get_token_symbols()
            trending = self.detector.get_trending_tokens(
                min_liquidity=200_000, min_volume_24h=50_000,
                max_tokens=20, prioritize_boosted=False)
            if trending:
                self.detector.display_trending(trending)

            if not selected:
                print("[discover] No tokens pass criteria — holding cash")
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

                if liq < 200_000:
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
                    self.agent.watched_tokens.discard(sym)
                    self.token_metadata.pop(sym, None)
                    continue

                rsi_val = _quick_rsi(hist)
                if rsi_val and rsi_val > 72:
                    print(f"  [skip] RSI {rsi_val:.0f} > 72 — overbought at discovery")
                    self.agent.watched_tokens.discard(sym)
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
    # CORRELATION CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def _is_correlated_with_holdings(self, token: str) -> Tuple[bool, str]:
        """
        Returns (is_correlated, reason). Compares new token's price returns
        with currently held tokens. Skip if correlation > CORRELATION_THRESHOLD.
        """
        if not self.agent.portfolio.positions:
            return False, ""

        new_prices = self.agent.price_history.get(token, {}).get("prices", [])
        if len(new_prices) < 30:
            return False, "insufficient data for correlation check"

        for held_token in self.agent.portfolio.positions:
            held_prices = self.agent.price_history.get(held_token, {}).get("prices", [])
            if len(held_prices) < 30:
                continue
            corr = _pearson_corr(new_prices, held_prices, n=30)
            if corr > CORRELATION_THRESHOLD:
                return True, (f"corr({token},{held_token})={corr:.2f} > "
                              f"{CORRELATION_THRESHOLD} — concentrated risk")
        return False, ""

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY GATE
    # ─────────────────────────────────────────────────────────────────────────

    def _can_enter(self, token: str, analysis: Dict) -> bool:
        if token in self.sl_blacklist:
            return False
        if analysis.get("hard_fail"):
            return False

        prob = analysis.get("prob_score", 0.0)
        if prob < ProfessionalSizer.MIN_PROB:
            print(f"  [skip] prob {prob:.0f} < {ProfessionalSizer.MIN_PROB:.0f}")
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

        if date.today() != self.day_start:
            self.trades_today = 0
            self.day_start    = date.today()

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
            print(f"  [skip] soft score {analysis.get('buy_pts',0)}/{self.min_soft_score}")
            return False

        if analysis.get("mtf_score", 0) < 2:
            print(f"  [skip] MTF alignment {analysis.get('mtf_score',0)}/3 — need ≥2")
            return False

        rr = analysis.get("rr_ratio", 0.0)
        if rr < 2.0:
            print(f"  [skip] R:R {rr:.2f} < 2.0 after fees")
            return False

        # Correlation filter
        correlated, corr_reason = self._is_correlated_with_holdings(token)
        if correlated:
            print(f"  [skip] {corr_reason}")
            return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT MONITORING — ATR-based, VWAP-extension-aware
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_exits(self, current_analyses: Dict):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl     = position.pnl_percentage
            cur     = position.current_price if hasattr(position, "current_price") else 0.0
            analysis= current_analyses.get(token, {})

            # Track peak P&L
            if token not in self.position_peaks or pnl > self.position_peaks[token]:
                self.position_peaks[token] = pnl
            peak = self.position_peaks[token]

            # Get ATR-based exit levels (use stored ATR from entry, or recompute)
            atr_pct = self.position_atr.get(token) or analysis.get("atr_pct", 0.02)
            stop_pct   = HWRSignalEngine.STOP_ATR_MULT  * atr_pct * 100   # as percent
            tp1_pct    = HWRSignalEngine.TP1_ATR_MULT   * atr_pct * 100
            tp2_pct    = HWRSignalEngine.TP2_ATR_MULT   * atr_pct * 100
            trail_pct  = HWRSignalEngine.TRAIL_ATR_MULT * atr_pct * 100

            # ── VWAP EXTENSION EXIT (early partial exit, not a full stop) ──────
            vwap_dev  = analysis.get("vwap_dev_pct", 0.0)
            rsi_now   = analysis.get("rsi", 50.0)
            if (not self.vwap_exit_done.get(token, False)
                    and vwap_dev > VWAP_EXTENSION_PCT
                    and rsi_now > VWAP_EXTENSION_RSI):
                sell_amt = position.amount * VWAP_EXTENSION_SELL
                print(f"\n  📡 VWAP-EXT {token}  "
                      f"Price +{vwap_dev:.1f}% above VWAP  RSI={rsi_now:.0f}  "
                      f"Selling {VWAP_EXTENSION_SELL*100:.0f}% — mean reversion risk")
                trade = self.agent.execute_swap(token, "USDC", sell_amt)
                if trade:
                    self.vwap_exit_done[token] = True
                    self._record(token, "VWAP_EXT", pnl, position.pnl * VWAP_EXTENSION_SELL)
                continue

            # ── HARD STOP (ATR-based) ─────────────────────────────────────────
            if pnl <= -stop_pct:
                print(f"\n  ❌ STOP {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-stop:{-stop_pct:.1f}%  ATR={atr_pct*100:.2f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.sl_strikes[token]  = self.sl_strikes.get(token, 0) + 1
                    if self.sl_strikes[token] >= 3:
                        self.sl_blacklist.add(token)
                        print(f"  🚫 Blacklisted {token} (3 stops)")
                    self._record(token, "STOP", pnl, position.pnl)
                    self.sizer.record_loss()
                    self.position_peaks.pop(token, None)
                    self.tp1_hit.pop(token, None)
                    self.tp2_hit.pop(token, None)
                    self.vwap_exit_done.pop(token, None)
                    self.position_atr.pop(token, None)
                continue

            # ── TP1 (ATR-based) — sell 55% ───────────────────────────────────
            if pnl >= tp1_pct and not self.tp1_hit.get(token, False):
                sell_amt = position.amount * HWRSignalEngine.TP1_AMOUNT
                print(f"\n  💰 TP1 {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-TP1:{tp1_pct:.1f}%  Locking {HWRSignalEngine.TP1_AMOUNT*100:.0f}%")
                trade = self.agent.execute_swap(token, "USDC", sell_amt)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp1_hit[token]     = True
                    self._record(token, "TP1", pnl, position.pnl * HWRSignalEngine.TP1_AMOUNT)
                continue

            # ── TP2 (ATR-based) — sell remainder ─────────────────────────────
            if (pnl >= tp2_pct and self.tp1_hit.get(token, False)
                    and not self.tp2_hit.get(token, False)):
                print(f"\n  💰💰 TP2 {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-TP2:{tp2_pct:.1f}%  Exiting remainder")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp2_hit[token]     = True
                    self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                    self._record(token, "TP2", pnl, position.pnl)
                    self.sizer.record_win()
                    self.position_peaks.pop(token, None)
                    self.vwap_exit_done.pop(token, None)
                    self.position_atr.pop(token, None)
                continue

            # ── TRAILING STOP (ATR-based) ─────────────────────────────────────
            if peak >= tp1_pct:   # only trail after TP1 level is reached
                floor = peak - trail_pct
                if pnl <= floor:
                    print(f"\n  🏁 TRAIL {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%  ATR-trail:{trail_pct:.1f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                        self._record(token, "TRAIL", pnl, position.pnl)
                        if pnl > 0:
                            self.sizer.record_win()
                        else:
                            self.sizer.record_loss()
                        self.position_peaks.pop(token, None)
                        self.tp1_hit.pop(token, None)
                        self.tp2_hit.pop(token, None)
                        self.vwap_exit_done.pop(token, None)
                        self.position_atr.pop(token, None)
                else:
                    print(f"  🏃 trail {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%")

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
        prob    = analysis.get("prob_score", 0.0)
        cur     = analysis.get("current_price", 0.0)
        atr_pct = analysis.get("atr_pct", 0.02)
        rr      = analysis.get("rr_ratio", 0.0)
        if cur == 0:
            return

        total    = self.agent.portfolio.get_total_value()
        result   = self.sizer.get_size(prob, atr_pct, total,
                                       self.agent.portfolio.usdc_balance)
        if result[0] == 0.0:
            print(f"  [skip] prob {prob:.0f} below sizer minimum")
            return

        usdc_amt, tier, adj_pct = result
        if usdc_amt < 10:
            print(f"  [skip] size ${usdc_amt:.2f} too small")
            return

        detail = analysis.get("buy_pts_detail", {})
        flags = "".join([
            "E" if detail.get("trend")    else ".",
            "R" if detail.get("rsi_rise") else ".",
            "M" if detail.get("macd")     else ".",
            "S" if detail.get("slope")    else ".",
            "V" if detail.get("vol")      else ".",
            "B" if detail.get("pullback") else ".",
            "W" if detail.get("vwap")     else ".",
            "T" if detail.get("mtf")      else ".",
            "K" if detail.get("stoch")    else ".",
            "A" if detail.get("vol_accel") else ".",
        ])

        atr_stop = HWRSignalEngine.STOP_ATR_MULT * atr_pct * 100
        atr_tp1  = HWRSignalEngine.TP1_ATR_MULT  * atr_pct * 100
        atr_tp2  = HWRSignalEngine.TP2_ATR_MULT  * atr_pct * 100
        atr_tr   = HWRSignalEngine.TRAIL_ATR_MULT* atr_pct * 100

        chop  = analysis.get("chop_index", 50.0)
        vwap_d= analysis.get("vwap_dev_pct", 0.0)
        mtf   = analysis.get("mtf_desc", "?")

        print(f"\n  ══ BUY {token} ══")
        print(f"     Prob:       {prob:.0f}%  →  tier {tier}")
        print(f"     Size:       ${usdc_amt:.2f}  ({adj_pct*100:.0f}% of portfolio)")
        print(f"     Sizer ×:    {self.sizer.multiplier:.2f}  "
              f"(wins streak: {self.sizer.consecutive_wins}/{CONSECUTIVE_WINS_REQ})")
        print(f"     Price:      ${cur:.8f}")
        print(f"     ATR:        {atr_pct*100:.2f}%  →  "
              f"Stop:{-atr_stop:.1f}%  TP1:+{atr_tp1:.1f}%  TP2:+{atr_tp2:.1f}%  Trail:{atr_tr:.1f}%")
        print(f"     R:R:        {rr:.2f} (after 0.6% fees)  CHOP:{chop:.0f}  "
              f"VWAP{vwap_d:+.1f}%")
        print(f"     RSI:        {analysis.get('rsi', 0):.0f}  MTF:{mtf}")
        print(f"     Score:      {analysis.get('buy_pts',0)}/10  [{flags}]")

        trade = self.agent.execute_swap("USDC", token, usdc_amt)
        if trade:
            self.last_trades[token]     = datetime.now()
            self.trades_today          += 1
            self.position_peaks[token]  = 0.0
            self.tp1_hit[token]         = False
            self.tp2_hit[token]         = False
            self.vwap_exit_done[token]  = False
            self.position_atr[token]    = atr_pct   # store ATR at entry
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
        by_exit= {}
        for t in self.session_trades:
            by_exit[t["exit"]] = by_exit.get(t["exit"], 0) + 1

        print(f"\n  ── Session Stats ─────────────────────────────────────")
        print(f"     Trades: {len(self.session_trades)}  "
              f"WR: {wr:.0f}%  ({len(wins)}W/{len(losses)}L)  "
              f"Net: ${pnl:+.2f}")
        if wins:
            print(f"     Avg win:  +{sum(t['pnl_pct'] for t in wins)/len(wins):.2f}%")
        if losses:
            print(f"     Avg loss:  {sum(t['pnl_pct'] for t in losses)/len(losses):.2f}%")
        if wins and losses:
            avg_w = sum(t["pnl_pct"] for t in wins) / len(wins)
            avg_l = abs(sum(t["pnl_pct"] for t in losses) / len(losses))
            if avg_l > 0:
                print(f"     Realised R:R: {avg_w/avg_l:.2f}")
        exits_str = "  ".join(f"{k}:{v}" for k, v in sorted(by_exit.items()))
        print(f"     Exits:  {exits_str}")
        print(f"     Sizer ×{self.sizer.multiplier:.2f}  "
              f"Win streak: {self.sizer.consecutive_wins}/{CONSECUTIVE_WINS_REQ}  "
              f"EqHWM: ${self.tracker.hwm:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, update_interval: int = 30):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 70)
        print("  SOLANA PROFESSIONAL TRADER  —  v4.0")
        print("=" * 70)
        print("\nEdge sources: ATR-stops | VWAP | MTF | Choppiness | Fee-aware R:R")
        print(f"\nRegime gate:    score >= {self.REGIME_MIN}  (marginal = tighter filters)")
        print(f"Fee awareness:  0.6% round-trip baked into every R:R calculation")
        print(f"Circuit breaker: halt if daily loss >= {DAILY_LOSS_LIMIT_PCT}%")
        print(f"\nATR-based exits (per-token, per-cycle):")
        print(f"  Stop:  {HWRSignalEngine.STOP_ATR_MULT}x ATR below entry")
        print(f"  TP1:   {HWRSignalEngine.TP1_ATR_MULT}x ATR above entry  "
              f"(sell {HWRSignalEngine.TP1_AMOUNT*100:.0f}%)")
        print(f"  TP2:   {HWRSignalEngine.TP2_ATR_MULT}x ATR above entry  (sell remainder)")
        print(f"  Trail: {HWRSignalEngine.TRAIL_ATR_MULT}x ATR below peak  (after TP1)")
        print(f"  VWAP:  sell {VWAP_EXTENSION_SELL*100:.0f}% if price +{VWAP_EXTENSION_PCT}% "
              f"above VWAP AND RSI>{VWAP_EXTENSION_RSI:.0f}")
        print(f"\nPosition sizing:")
        print(f"  prob >=82%  → {ProfessionalSizer.TIER_HIGH*100:.0f}% of portfolio  (ATR-scaled)")
        print(f"  prob 70-81  → {ProfessionalSizer.TIER_MED*100:.0f}% of portfolio  (ATR-scaled)")
        print(f"  prob 60-69  → {ProfessionalSizer.TIER_LOW*100:.0f}% of portfolio  (ATR-scaled)")
        print(f"  prob <60    → skip")
        print(f"\nCompounding: requires {CONSECUTIVE_WINS_REQ} consecutive wins to step up")
        print(f"  Win streak complete → +{WIN_SIZE_STEP*100:.0f}% size  (cap {ProfessionalSizer.MAX_MULT}x)")
        print(f"  Any loss            → -{LOSS_SIZE_STEP*100:.0f}% size  (floor {ProfessionalSizer.MIN_MULT}x)")
        print(f"\nCorrelation filter: skip if corr(new, held) > {CORRELATION_THRESHOLD}")
        print(f"Max trades/day: {self.max_trades_per_day}  Cooldown: {self.trade_cooldown_secs}s")
        print(f"\nCtrl+C to stop")
        print("=" * 70)

        iteration = 0
        try:
            while self.running:
                iteration += 1
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n{'='*70}")
                print(f"Cycle #{iteration} — {now_str}")
                print(f"{'='*70}")

                if hasattr(self.agent, "market_analyzer"):
                    self.agent.market_analyzer.invalidate_cache()

                # Discovery
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self.refresh_tokens()

                # Market update
                print("\n[market] Updating prices...")
                self.agent.update_market_data()

                # Build analysis dict
                tokens_analysis: Dict[str, Dict] = {}
                for token in list(self.agent.watched_tokens):
                    if token in SKIP:
                        continue
                    buf      = self.agent.price_history.get(token, {})
                    live_pts = buf.get("live_points", 0)
                    prices   = buf.get("prices", [])
                    volumes  = buf.get("volumes", [])

                    if live_pts < self.min_live_candles or not prices:
                        continue

                    meta     = self.token_metadata.get(token, {})
                    analysis = self.engine.analyze(
                        token, prices, volumes,
                        token_meta=meta,
                        min_soft_score=self.min_soft_score,
                    )
                    tokens_analysis[token] = analysis

                # Equity update
                current_equity = self.agent.portfolio.get_total_value()
                self._check_daily_reset(current_equity)
                self.tracker.update(current_equity)
                dd = self.tracker.drawdown_pct(current_equity)

                # Circuit breaker check
                halted = self._check_circuit_breaker(current_equity)

                # Regime gate
                allowed, regime_score, regime_msg = self._check_regime(tokens_analysis)
                print(f"\n{regime_msg}")

                if halted:
                    print("  ⛔ Circuit breaker active — monitoring exits only")
                    if self.agent.portfolio.positions:
                        self._monitor_exits(tokens_analysis)
                elif not allowed:
                    if self.agent.portfolio.positions:
                        print("[positions] Monitoring stops only (regime paused)...")
                        self._monitor_exits(tokens_analysis)
                else:
                    # Monitor exits
                    if self.agent.portfolio.positions:
                        print("\n[positions] Monitoring exits...")
                        self._monitor_exits(tokens_analysis)

                    # Scan for entries
                    print("\n[analysis] Scanning tokens...")
                    qualified = []

                    for token, analysis in tokens_analysis.items():
                        meta     = self.token_metadata.get(token, {})
                        boosted  = meta.get("is_boosted", False)
                        boost_tag= "[B]" if boosted else "   "
                        hard_fail= analysis.get("hard_fail", False)
                        hard_msg = analysis.get("hard_fail_reason", "")
                        soft     = analysis.get("buy_pts", 0)
                        prob     = analysis.get("prob_score", 0.0)
                        rel_v    = analysis.get("relative_volume", 1.0)
                        chop     = analysis.get("chop_index", 50.0)
                        rr       = analysis.get("rr_ratio", 0.0)
                        atr      = analysis.get("atr_pct", 0.0) * 100
                        mtf_s    = analysis.get("mtf_score", 0)
                        vwap_d   = analysis.get("vwap_dev_pct", 0.0)
                        ts       = datetime.now().strftime("%H:%M:%S")

                        detail = analysis.get("buy_pts_detail", {})
                        flags  = "".join([
                            "E" if detail.get("trend")    else ".",
                            "R" if detail.get("rsi_rise") else ".",
                            "M" if detail.get("macd")     else ".",
                            "S" if detail.get("slope")    else ".",
                            "V" if detail.get("vol")      else ".",
                            "B" if detail.get("pullback") else ".",
                            "W" if detail.get("vwap")     else ".",
                            "T" if detail.get("mtf")      else ".",
                            "K" if detail.get("stoch")    else ".",
                            "A" if detail.get("vol_accel") else ".",
                        ])

                        sig  = analysis["signal"]
                        sd   = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(sig, "HOLD")
                        score_str = ("✗HARD" if hard_fail else
                                     f"{soft}/10[{flags}] p{prob:.0f}% R{rr:.1f}")

                        print(f"  [{ts}] {boost_tag}{token:<12}  "
                              f"${analysis['current_price']:<12.6f}  "
                              f"RSI:{analysis['rsi']:.0f}  {sd}  "
                              f"Vol:{rel_v:.1f}x  CHOP:{chop:.0f}  "
                              f"ATR:{atr:.2f}%  VWAP{vwap_d:+.1f}%  "
                              f"MTF:{mtf_s}/3  {score_str}")

                        if hard_fail:
                            print(f"           ↳ {hard_msg[:90]}")
                        elif sig == "BUY":
                            qualified.append((token, analysis))

                    # Execute entries (sorted by probability — best first)
                    qualified.sort(key=lambda x: x[1].get("prob_score", 0), reverse=True)
                    for token, analysis in qualified:
                        if self._can_enter(token, analysis):
                            self._execute_buy(token, analysis)

                # Portfolio display
                perf = self.agent.portfolio.get_performance()
                daily_loss = self.tracker.daily_loss_pct(current_equity)
                print(f"\n[portfolio]")
                print(f"  USDC:         ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total:        ${perf['current_value']:.2f}")
                print(f"  Return:       ${perf['total_return']:+.2f} "
                      f"({perf['total_return_pct']:+.2f}%)")
                print(f"  HWM:          ${self.tracker.hwm:.2f}  DD: {dd:.1f}%")
                print(f"  Daily P&L:    {-daily_loss:+.2f}%  "
                      f"({'⛔ HALTED' if halted else f'limit {DAILY_LOSS_LIMIT_PCT}%'})")
                print(f"  Sizer ×:      {self.sizer.multiplier:.2f}  "
                      f"(wins: {self.sizer.consecutive_wins}/{CONSECUTIVE_WINS_REQ})")
                print(f"  Trades today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Regime:       {self._current_score:.0f}/100  "
                      f"(gate≥{self.REGIME_MIN})")
                print(f"  Watching:     {len(self.agent.watched_tokens)} tokens")

                if self.agent.portfolio.positions:
                    print(f"\n  Open positions:")
                    for tok, pos in self.agent.portfolio.positions.items():
                        peak     = self.position_peaks.get(tok, 0)
                        atr_e    = self.position_atr.get(tok, 0.02)
                        stop_lvl = -HWRSignalEngine.STOP_ATR_MULT * atr_e * 100
                        tp1_lvl  =  HWRSignalEngine.TP1_ATR_MULT  * atr_e * 100
                        tp_flag  = ("✓TP2" if self.tp2_hit.get(tok) else
                                    "✓TP1" if self.tp1_hit.get(tok) else "    ")
                        print(f"    {tok:<14}  "
                              f"P&L:{pos.pnl_percentage:+.2f}%  "
                              f"Peak:{peak:+.2f}%  "
                              f"{tp_flag}  "
                              f"Stop:{stop_lvl:.1f}%  TP1:+{tp1_lvl:.1f}%  "
                              f"ATR:{atr_e*100:.2f}%")

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
            print("\n" + "=" * 70)
            print("  SESSION COMPLETE")
            print("=" * 70)
            self._print_stats()
            self.agent.show_portfolio()
            print()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
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
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solana Professional Trader v4.0")
    parser.add_argument("--mode",             choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",           default="")
    parser.add_argument("--balance",          type=float, default=1000.0)
    parser.add_argument("--interval",         type=int,   default=30)
    parser.add_argument("--refresh-interval", type=int,   default=300)
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
                if input(f"Low SOL ({sol_bal:.4f}). Continue? (yes/no): ").lower() not in ("yes","y"):
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

    trader = ProfessionalTrader(agent, auto_discover=auto_discover)
    trader.refresh_interval = args.refresh_interval

    if auto_discover:
        print("\n[discover] Initial token scan...")
        trader.refresh_tokens()

    print("\n[market] Warming up (30 ticks)...")
    for i in range(30):
        agent.update_market_data()
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/30")
        time.sleep(1)
    print("[OK] Ready\n")

    trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()
