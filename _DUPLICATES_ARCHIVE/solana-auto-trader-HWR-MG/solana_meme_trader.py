#!/usr/bin/env python3
"""
Solana Meme Coin Trader — v2.8 (Engine Integrity)
============================================================
Changes from v2.7:
  FIX 9  — HWR volume gate restructured: floor lowered to 0.6x (was ≥1.6x
            vol_confirmed), ceiling added at vol_explosion (3.0x). Previously
            the gate blocked low-vol structural setups (HWR's core) while
            letting vol_explosion entries through — routing them to HWR when
            they belong in MG. Both errors corrected simultaneously.
  FIX 10 — No-traction exit gap closed: NO_TRACTION_MAX_PEAK raised 10%→20%
            of TP1. Positions with peak 10-20% of TP1 (e.g. GDIG: peak=1.59%,
            TP1=10.4% → 15.3%) fell between stale-exit (≥50% floor) and
            no-traction (<10% floor) and were never evicted. Now covered.
  FIX 11 — No-traction hold reduced 15min→12min for faster HWR capital
            redeployment. HWR's high-WR premise requires quick loss decisions.
  FIX 12 — WIN_STREAK_REQ reduced 3→2. With multiple positions open at once,
            reaching 3 closed wins takes many cycles; sizer stayed at ×1.00
            permanently in most sessions.
  FIX 13 — Dashboard and execute_trade stop display now use engine-specific
            stored stop parameters (position_stop_atr / position_stop_max)
            instead of global MemeSignalEngine constants. HWR positions were
            displaying the global 5×ATR stop instead of their actual 3.5×/5%.

ARCHITECTURE:
  MemeCoinTrader
    ├─ HWREngine  — High Win Rate: tight filters, conservative exits
    ├─ MGEngine   — Momentum Growth: breakout/reclaim, wider TP
    ├─ CapitalAllocator — Dynamic regime-based allocation
    └─ Shared portfolio, circuit breaker, regime gate, correlation guard
"""

import time
import argparse
from datetime import datetime, date
from typing import Dict, Optional, List, Tuple

from solana_trading_agent import SolanaTradingAgent, TradingMode, TOKENS
from trending_tokens import EnhancedTrendingTokenDetector, AutoTokenSelector
from meme_signal_engine import MemeSignalEngine, _calc_rsi, MEME_VOL_EXPLOSION


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_POSITION_USD      = 150.0   # absolute hard cap per trade
BASE_POSITION_PCT     = 0.08    # 8% of portfolio base size
MAX_ATR_FOR_FULL_SIZE = 0.08    # ATR above 8% = scale down position
DAILY_LOSS_LIMIT_PCT  = 8.0     # halt if daily loss >= 8%
MAX_CONCURRENT        = 5       # max concurrent meme positions
WIN_STREAK_REQ        = 2       # wins before compounding up (was 3 — too slow when positions sit open)
WIN_STEP              = 0.03    # +3% per compounding step
LOSS_STEP             = 0.05    # -5% on any loss
MAX_MULT              = 1.80
MIN_MULT              = 0.40
DUMP_CANDLES          = 3       # peak-to-dump check window
MIN_PROB              = 45.0    # minimum probability to trade
COOLDOWN_AFTER_STOP   = 600     # 10 min cooldown after stop on a token

EVICTION_FAIL_LIMIT   = 8
EVICTION_COOLDOWN     = 900     # 15 min before an evicted token can re-enter watchlist

# MG drawdown throttle thresholds
DD_THROTTLE_PCT       = 10.0    # drawdown % at which MG size is reduced 30%
DD_DISABLE_PCT        = 18.0    # drawdown % at which MG is fully disabled

# [FIX 2] Liquidity thresholds (raised from $50k / $35k)
HWR_MIN_LIQUIDITY     = 80_000  # HWR engine entry gate
DISCOVERY_MIN_LIQ     = 60_000  # hard skip during discovery scan

# [FIX 3] Correlation guard — max same-cohort positions open simultaneously
MAX_COHORT_POSITIONS  = 2

# [FIX 4] Persistent hard-fail suppression
HARD_FAIL_BAN_CYCLES  = 5       # consecutive hard-fails before suppression
HARD_FAIL_BAN_SECS    = 1800    # 30 min suppression window

# [FIX 5] Stale position exit
# A position is "stale" when ALL three are true:
#   1. TP1 has NOT been hit
#   2. Current P&L is <= STALE_FADE_RATIO × TP1 threshold (peak has faded)
#   3. Position has been open > STALE_HOLD_SECS seconds
# Two-tier: warn at STALE_HOLD_SECS, exit at STALE_HOLD_SECS + STALE_GRACE_SECS
# if P&L is still fading. This avoids ejecting a trade that's just consolidating.
STALE_HOLD_SECS       = 600     # 10 min minimum hold before staleness can fire
STALE_GRACE_SECS      = 180     # 3 min grace after warning before forced exit
STALE_FADE_RATIO      = 0.40    # current pnl must be ≤ 40% of TP1 to be stale
STALE_MIN_PEAK        = 0.50    # peak must have been ≥ 50% of TP1 to qualify
# [FIX 8] No-traction exit: position that never found ground (peak < 10% TP1, currently neg)
NO_TRACTION_HOLD_SECS = 720      # 12 min — HWR should decide faster (was 900/15min)
NO_TRACTION_GRACE_SECS= 120     # 2 min grace period before forced exit
NO_TRACTION_MAX_PEAK  = 0.20    # peak must have stayed below 20% of TP1
                                 # (raised from 10% to catch the gap between
                                 #  stale-exit ≥50% and no-traction <10%:
                                 #  e.g. GDIG peak=1.59%, TP1=10.4% → 15.3%
                                 #  now correctly triggers no-traction exit)

# [FIX 6] Mid-cycle stop check
# When a position's current P&L is within STOP_WATCH_BUFFER × stop distance,
# the bot re-polls price mid-cycle so fast drops don't slip past the stop.
STOP_WATCH_BUFFER     = 2.0     # start watching when within 2× the stop distance
STOP_MIDCYCLE_SLEEP   = 5       # seconds to wait before mid-cycle re-check

# [FIX 7] MG breakout regime floor
# Below this score, MG breakout entries are blocked. Reclaim entries are
# still allowed since they don't require broad momentum continuation.
MG_BREAKOUT_MIN_REGIME = 65


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN DATA
# ─────────────────────────────────────────────────────────────────────────────

class TokenData:
    """
    Thin wrapper over an analysis dict that lets engine evaluate()
    methods use attribute-style access instead of dict.get() everywhere.
    """
    __slots__ = (
        "symbol", "liquidity", "rsi", "rel_volume", "parabolic_multiple",
        "rr_after_fees", "has_distribution", "velocity_pct", "atr_pct",
        "atr_expanding", "rsi_reclaim", "vol_confirmed",
    )

    def __init__(self, symbol: str, analysis: Dict, meta: Dict):
        self.symbol             = symbol
        self.liquidity          = meta.get("liquidity_usd", 0)
        self.rsi                = analysis.get("rsi", 50.0)
        self.rel_volume         = analysis.get("relative_volume", 1.0)
        self.parabolic_multiple = analysis.get("multiple_from_low", 1.0)
        self.rr_after_fees      = analysis.get("rr_ratio", 0.0)
        self.has_distribution   = analysis.get("sell_divergence", False)
        self.velocity_pct       = analysis.get("velocity_5", 0.0)
        self.atr_pct            = analysis.get("atr_pct", 0.05)
        # Use the true ATR-expansion flag emitted by MemeSignalEngine.analyze().
        # Falls back to the ema_slope proxy only if the key is absent (e.g. when
        # analysis comes from a different engine that doesn't compute ATR expansion).
        if "atr_expanding" in analysis:
            self.atr_expanding = analysis["atr_expanding"]
        else:
            # Proxy fallback: |ema_slope| > 0.004 approximates volatility expansion
            # but can misfire on trending-without-volatility moves.  Log a warning
            # so it's easy to spot in paper-mode output.
            import warnings
            warnings.warn(
                f"TokenData({symbol}): 'atr_expanding' missing from analysis dict — "
                "falling back to ema_slope proxy.  Update the signal engine to expose "
                "'atr_expanding' for accurate volatility gating.",
                stacklevel=2,
            )
            self.atr_expanding = abs(analysis.get("ema_slope", 0.0)) > 0.004
        self.rsi_reclaim        = (
            analysis.get("rsi_rising", False) and 45 <= self.rsi <= 60
        )
        detail = analysis.get("buy_pts_detail", {})
        self.vol_confirmed = bool(
            detail.get("vol_explosion") or
            detail.get("vol_spike")
        )


# ─────────────────────────────────────────────────────────────────────────────
# BASE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class BaseEngine:
    """Abstract base — every engine must implement evaluate()."""

    def __init__(self, name: str):
        self.name     = name
        self.disabled = False

    def evaluate(self, t: "TokenData", market_score: float) -> Optional[Dict]:
        raise NotImplementedError


# ─────────────────────────────────────────────────────────────────────────────
# HWR ENGINE — High Win Rate
# ─────────────────────────────────────────────────────────────────────────────

class HWREngine(BaseEngine):
    """
    High Win Rate engine.
    Prioritises win rate over frequency.

    v2.3 R:R fix:
      - TP1 raised 6x → 8x ATR so average win (+~8% on 1% ATR token) reliably
        exceeds the average loss.
      - Stop cap tightened 5x → 3.5x ATR (hard cap 7% → 5%) so a single loss
        cannot wipe multiple wins.  On a 1% ATR token: old stop=-5%, new=-3.5%.
      - Trail tightened 3.5x → 2.5x so partial profits lock faster after TP1.
      - Liquidity guard raised to HWR_MIN_LIQUIDITY ($80k) — see FIX 2.
    """

    TP1_ATR   = 8.0    # was 6.0 — raises avg win to justify wider entries
    TP2_ATR   = 14.0   # was 10.0
    TRAIL_ATR = 2.5    # was 3.5 — tighter trail locks profit faster
    STOP_ATR  = 3.5    # explicit stop multiplier for HWR (overrides global MAX_STOP)
    MAX_STOP  = 0.05   # hard cap 5% (was 7% via global STOP_ATR_MULT=5 × 1.4% ATR)
    SIZE_PCT  = 0.06   # 6% of portfolio

    def __init__(self):
        super().__init__("HWR")

    def evaluate(self, t: "TokenData", market_score: float) -> Optional[Dict]:
        if self.disabled:
            return None

        # [FIX 2] Raised liquidity gate
        if t.liquidity < HWR_MIN_LIQUIDITY:
            return None
        if not (42 <= t.rsi <= 62):
            return None

        # HWR targets structural setups, NOT momentum explosions.
        # Require modest activity (≥0.6x) but cap below vol_explosion so that
        # those high-momentum entries are routed to MG instead. Previously the
        # vol_confirmed gate (≥1.6x spike) both blocked low-vol setups and let
        # vol_explosion entries through — inverting both goals simultaneously.
        if t.rel_volume < 1.0:
            return None
        if t.rel_volume >= MEME_VOL_EXPLOSION:   # explosion → MG's territory
            return None

        if t.parabolic_multiple > 2.5:
            return None

        # [FIX 1] Recalculate R:R with new TP1/stop so we only enter when
        # the setup actually offers positive expected value at 60% win rate.
        # EV = 0.6 × TP1_ATR - 0.4 × STOP_ATR  must be > 0  (always true at 8/3.5)
        # but gate on the token's own rr_after_fees to reject low-quality setups.
        if t.rr_after_fees < 0.60:   # slightly tighter than MG (0.50)
            return None

        if t.has_distribution:
            return None

        return {
            "engine":    "HWR",
            "size_pct":  self.SIZE_PCT,
            "tp1_atr":   self.TP1_ATR,
            "tp2_atr":   self.TP2_ATR,
            "trail_atr": self.TRAIL_ATR,
            "stop_atr":  self.STOP_ATR,   # engine-specific override
            "max_stop":  self.MAX_STOP,   # engine-specific override
        }


# ─────────────────────────────────────────────────────────────────────────────
# MG ENGINE — Momentum Growth
# ─────────────────────────────────────────────────────────────────────────────

class MGEngine(BaseEngine):
    """
    Momentum Growth engine.
    Targets breakout continuations and RSI reclaim setups.
    """

    TP1_ATR       = 4.0
    TP2_ATR       = 12.0
    TRAIL_ATR     = 2.5
    SIZE_PCT      = 0.09    # 9% of portfolio (before aggression scaling)
    size_reduction = 1.0    # multiplied in when drawdown throttle fires

    def __init__(self):
        super().__init__("MG")

    def evaluate(self, t: "TokenData", market_score: float) -> Optional[Dict]:
        if self.disabled:
            return None

        # Breakout pattern: Acceleration phase logic
        breakout = (
            45 <= t.rsi <= 72
            and t.rel_volume >= 1.8          # 1.4x = Acceleration phase
            and t.velocity_pct > 1.5         # 1.5% = Real movement
            and t.parabolic_multiple <= 5.0
            and not t.has_distribution
        )

        # Pattern 2: RSI reclaim
        reclaim = (
            t.rsi_reclaim
            and (t.rel_volume >= 1.0 or t.vol_confirmed)
            and t.atr_expanding
        )

        if not (breakout or reclaim):
            return None

        # [FIX 7] Breakout entries need broad momentum to sustain. In a
        # neutral-to-declining regime (< MG_BREAKOUT_MIN_REGIME) breakouts
        # fail at ~50% WR per session data. Block them; reclaim is still OK
        # since it relies on individual token structure, not market momentum.
        if breakout and market_score < MG_BREAKOUT_MIN_REGIME:
            return None

        if t.rr_after_fees < 0.50:
            return None

        pattern = "breakout" if breakout else "reclaim"

        return {
            "engine":    "MG",
            "pattern":   pattern,
            "size_pct":  self.SIZE_PCT * self.size_reduction,
            "tp1_atr":   self.TP1_ATR,
            "tp2_atr":   self.TP2_ATR,
            "trail_atr": self.TRAIL_ATR,
        }


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZER
# ─────────────────────────────────────────────────────────────────────────────

class MemeSizer:
    def __init__(self):
        self._mult             = 1.0
        self._consecutive_wins = 0

    def get_size(self, prob: float, atr_pct: float,
                 total: float, available_usdc: float) -> Tuple[float, str]:
        if prob < MIN_PROB:
            return 0.0, "SKIP"

        base_usdc = total * BASE_POSITION_PCT * self._mult

        atr_factor = 1.0
        if atr_pct > MAX_ATR_FOR_FULL_SIZE:
            atr_factor = MAX_ATR_FOR_FULL_SIZE / atr_pct
            atr_factor = max(atr_factor, 0.35)

        scaled_usdc = base_usdc * atr_factor
        capped_usdc = min(scaled_usdc, MAX_POSITION_USD)
        final_usdc = min(capped_usdc, available_usdc * 0.90)

        label = "CAP" if scaled_usdc > MAX_POSITION_USD else "STD"
        return final_usdc, label

    def record_win(self):
        self._consecutive_wins += 1
        if self._consecutive_wins >= WIN_STREAK_REQ:
            old = self._mult
            self._mult = min(self._mult + WIN_STEP, MAX_MULT)
            self._consecutive_wins = 0
            if self._mult != old:
                print(f"  [compound] {WIN_STREAK_REQ} wins → "
                      f"sizer {old:.2f}x → {self._mult:.2f}x 🚀")

    def record_loss(self):
        self._consecutive_wins = 0
        old = self._mult
        self._mult = max(self._mult - LOSS_STEP, MIN_MULT)
        if self._mult != old:
            print(f"  [compound] Loss → sizer {old:.2f}x → {self._mult:.2f}x")

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
    def __init__(self, starting: float):
        self._hwm              = starting
        self._day_start_equity = starting

    def update(self, equity: float):
        if equity > self._hwm:
            self._hwm = equity

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
# MAIN TRADER
# ─────────────────────────────────────────────────────────────────────────────

class MemeCoinTrader:

    REGIME_MIN = 0

    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent         = agent
        self.auto_discover = auto_discover
        self.running       = False
        self.engine        = MemeSignalEngine()

        self.hwr_engine = HWREngine()
        self.mg_engine  = MGEngine()

        starting           = agent.portfolio.get_total_value()
        self.sizer         = MemeSizer()
        self.tracker       = EquityTracker(starting)

        self.min_soft_score = 3

        self.position_peaks:    Dict[str, float]    = {}
        self.position_entry:    Dict[str, float]     = {}
        self.position_atr:      Dict[str, float]     = {}
        self.position_peak_ts:  Dict[str, float]     = {}
        self.position_engine:   Dict[str, str]       = {}
        self.position_tp1_atr:  Dict[str, float]     = {}
        self.position_tp2_atr:  Dict[str, float]     = {}
        self.position_trail_atr:Dict[str, float]     = {}
        self.position_stop_atr: Dict[str, float]     = {}  # [FIX 1] engine-specific stop multiplier
        self.position_stop_max: Dict[str, float]     = {}  # [FIX 1] engine-specific stop cap
        self.tp1_hit:           Dict[str, bool]      = {}
        self.tp2_hit:           Dict[str, bool]      = {}
        self.tp_lockout:        Dict[str, float]     = {}
        self.tp_lockout_secs    = 120

        self.hwr_positions: set = set()
        self.mg_positions:  set = set()

        self.max_trades_per_day  = 60
        self.trade_cooldown_secs = 30
        self.min_live_candles    = 15

        self.trades_today  = 0
        self.day_start     = date.today()
        self.halted_today  = False

        self.last_trades:   Dict[str, datetime] = {}
        self.sl_strikes:    Dict[str, int]       = {}
        self.sl_blacklist:  set                  = set()
        self.sl_cooldown:   Dict[str, float]     = {}

        self.eviction_fails:     Dict[str, int]   = {}
        self.eviction_blacklist: Dict[str, float] = {}
        self.discovery_rsi_skip: Dict[str, float] = {}
        self.DISCOVERY_RSI_SKIP_SECS = 180

        # [FIX 3] Correlation guard: track which discovery cohort each token
        # belongs to so we don't hold more than MAX_COHORT_POSITIONS tokens
        # from the same scan batch simultaneously.
        self.token_cohort:       Dict[str, int]   = {}  # token → cohort_id
        self._cohort_counter     = 0

        # [FIX 4] Persistent hard-fail suppression: tokens that structurally
        # fail repeatedly without ever signalling BUY get suppressed so they
        # stop consuming scan cycles.
        self.hard_fail_cycles:   Dict[str, int]   = {}  # token → consecutive hard-fail count
        self.hard_fail_suppress: Dict[str, float] = {}  # token → suppression expiry timestamp

        # [FIX 5] Stale position exit: entry timestamp per position.
        self.position_entry_ts: Dict[str, float] = {}  # token → entry epoch timestamp

        self.refresh_interval        = 30
        self.last_refresh            = 0
        self.detector                = EnhancedTrendingTokenDetector()
        self.selector                = AutoTokenSelector(self.detector)
        self.token_metadata:          Dict[str, Dict] = {}

        self._current_score     = 50.0
        self.session_trades:    List[Dict] = []

    # ─────────────────────────────────────────────────────────────────────────
    # REGIME CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def _check_regime(self, tokens_analysis: Dict) -> Tuple[bool, float, str]:
        analyzer = getattr(self.agent, "market_analyzer", None)
        if not analyzer:
            return True, 50.0, "no analyzer"

        if not tokens_analysis:
            cached = getattr(analyzer, "_smoothed_score", None)
            if cached is not None:
                self._current_score = cached
                lbl = "🟢" if cached >= 70 else ("🟡" if cached >= 50 else "🔴")
                return cached >= self.REGIME_MIN, cached, f"  [regime] {lbl} ({cached:.0f})"
            print(f"  [regime] ⏳ warming up (no watched tokens yet)")
            return True, 50.0, "  [regime] no data"

        status, reason, details = analyzer.analyze(tokens_analysis)
        score = details["overall_score"]
        self._current_score = score
        self.selector.set_regime(score)

        if score < self.REGIME_MIN:
            return False, score, f"⏸  regime {score:.0f} < {self.REGIME_MIN} — cash only"

        lbl = "🟢 BULLISH" if score >= 70 else "🟡 NEUTRAL"
        print(f"  [regime] {lbl} ({score:.0f})  sizer×{self.sizer.multiplier:.2f}")
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
            print(f"\n  [day] New day — counters reset")

    def _check_circuit_breaker(self, equity: float) -> bool:
        if self.halted_today:
            return True
        loss = self.tracker.daily_loss_pct(equity)
        if loss >= DAILY_LOSS_LIMIT_PCT:
            self.halted_today = True
            print(f"\n  ⛔ CIRCUIT BREAKER: Daily loss {loss:.1f}% — halted for today")
            return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # CAPITAL BUCKET CONTROLS (DYNAMIC)
    # ─────────────────────────────────────────────────────────────────────────

    def _get_cap_ratios(self) -> Tuple[float, float]:
        """Dynamic allocation based on regime score."""
        score = self._current_score
        if score >= 85:
            return 0.40, 0.60   # MG dominant in strong bull
        elif score >= 75:
            return 0.50, 0.50
        elif score >= 65:
            return 0.60, 0.40
        else:
            return 0.70, 0.30   # HWR stabilizes in weak markets

    def _hwr_exposure(self) -> float:
        total = 0.0
        for tok in self.hwr_positions:
            pos = self.agent.portfolio.positions.get(tok)
            if pos:
                total += pos.amount * pos.current_price
        return total

    def _mg_exposure(self) -> float:
        total = 0.0
        for tok in self.mg_positions:
            pos = self.agent.portfolio.positions.get(tok)
            if pos:
                total += pos.amount * pos.current_price
        return total

    def _hwr_cap_available(self, total_value: float) -> bool:
        hwr_ratio, _ = self._get_cap_ratios()
        return self._hwr_exposure() < hwr_ratio * total_value

    def _mg_cap_available(self, total_value: float) -> bool:
        _, mg_ratio = self._get_cap_ratios()
        return self._mg_exposure() < mg_ratio * total_value

    def _aggression_factor(self, regime_score: float) -> float:
        # Linear scale from 0.30 (score=50) to 1.0 (score=90+).
        # Old formula: (score-50)/40 gave ~0.025 at score=51, making MG trades
        # $2-3 on a $1k portfolio — silently killed by the $5 minimum guard.
        # New floor at 0.30 keeps MG alive but conservatively sized in neutral markets.
        return max(0.30, min(1.0, 0.30 + (regime_score - 50.0) / 57.0))

    # ─────────────────────────────────────────────────────────────────────────
    # DRAWDOWN THROTTLE
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_drawdown_throttle(self, dd_pct: float):
        if dd_pct > DD_DISABLE_PCT:
            if not self.mg_engine.disabled:
                print(f"  [throttle] 🔴 DD {dd_pct:.1f}% > {DD_DISABLE_PCT}% — MG DISABLED")
                self.mg_engine.disabled      = True
                self.mg_engine.size_reduction = 1.0
        elif dd_pct > DD_THROTTLE_PCT:
            self.mg_engine.disabled      = False
            new_reduction                = 0.70   # 30% size reduction
            if self.mg_engine.size_reduction != new_reduction:
                print(f"  [throttle] 🟡 DD {dd_pct:.1f}% > {DD_THROTTLE_PCT}% — MG size ×0.70")
                self.mg_engine.size_reduction = new_reduction
        else:
            if self.mg_engine.disabled or self.mg_engine.size_reduction != 1.0:
                print(f"  [throttle] 🟢 DD {dd_pct:.1f}% — MG restored")
                self.mg_engine.disabled      = False
                self.mg_engine.size_reduction = 1.0

    # ─────────────────────────────────────────────────────────────────────────
    # DUAL-ENGINE ENTRY EVALUATION
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate_entries(
        self,
        tokens_analysis: Dict,
        regime_score: float,
    ) -> List[Tuple[str, Dict, Dict]]:
        candidates = []
        total_value = self.agent.portfolio.get_total_value()

        for token, analysis in tokens_analysis.items():
            if analysis.get("signal") != "BUY":
                continue
            if token in self.agent.portfolio.positions:
                continue

            meta = self.token_metadata.get(token, {})
            t    = TokenData(token, analysis, meta)

            hwr_sig = self.hwr_engine.evaluate(t, regime_score)
            if hwr_sig and self._hwr_cap_available(total_value):
                candidates.append((token, analysis, hwr_sig))
                continue
            elif hwr_sig:
                print(f"  [skip] {token}: HWR cap full")

            mg_sig = self.mg_engine.evaluate(t, regime_score)
            if mg_sig and self._mg_cap_available(total_value):
                af = self._aggression_factor(regime_score)
                mg_sig = dict(mg_sig)
                mg_sig["size_pct"] *= af
                mg_sig["aggression_factor"] = af
                candidates.append((token, analysis, mg_sig))
            elif mg_sig:
                print(f"  [skip] {token}: MG cap full")
            else:
                # Signal engine said BUY but neither engine pattern matched.
                # Log the specific blocker so the user isn't left wondering.
                hwr_rsi_ok = 42 <= t.rsi <= 62
                mg_rsi_ok  = 45 <= t.rsi <= 72
                reclaim_ok = (t.rsi_reclaim
                              and (t.rel_volume >= 1.0 or t.vol_confirmed)
                              and t.atr_expanding)
                if not hwr_rsi_ok and not mg_rsi_ok:
                    block = f"RSI {t.rsi:.0f} outside engine bounds (HWR:42-62, MG:45-72)"
                elif mg_rsi_ok and t.velocity_pct <= 1.5 and not reclaim_ok:
                    block = (f"MG: vel {t.velocity_pct:+.1f}% < 1.5% "
                             f"and no reclaim (rsi_reclaim={t.rsi_reclaim} "
                             f"atr_exp={t.atr_expanding})")
                elif mg_rsi_ok and t.velocity_pct > 1.5 and t.rel_volume < 1.4:
                    block = f"MG: vol {t.rel_volume:.1f}x < 1.4x for breakout"
                else:
                    block = (f"no pattern  RSI={t.rsi:.0f} "
                             f"vel={t.velocity_pct:+.1f}% vol={t.rel_volume:.1f}x")
                print(f"  [skip] {token}: BUY blocked by engines — {block}")

        candidates.sort(key=lambda x: x[1].get("prob_score", 0), reverse=True)
        return candidates

    # ─────────────────────────────────────────────────────────────────────────
    # DISCOVERY
    # ─────────────────────────────────────────────────────────────────────────

    def refresh_tokens(self):
        try:
            print("\n[discover] Scanning for meme setups...")
            self.selector.preferences.update({
                "min_liquidity":        50_000,
                "min_volume":           20_000,
                "max_tokens":           15,
                "prefer_rising":        False,
                "max_price_change_24h": 9999,
                "prioritize_boosted":   True,
                "min_score":            25.0,
            })

            selected = self.selector.get_token_symbols()
            trending = self.detector.get_trending_tokens(
                min_liquidity=50_000, min_volume_24h=20_000,
                max_tokens=20, prioritize_boosted=True)
            if trending:
                self.detector.display_trending(trending)

            if not selected:
                print("[discover] No tokens pass meme criteria — waiting")
                self.last_refresh = time.time()
                return

            print("\n[discover] MEME CANDIDATES:")
            print("-" * 65)
            added = skipped_rsi = skipped_liq = skipped_para = 0

            # [FIX 3] Assign a new cohort ID for this scan batch
            self._cohort_counter += 1
            current_cohort = self._cohort_counter

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
                mcap      = token.get("market_cap_usd", 0)
                age_h     = token.get("age_hours", None)

                if sym in self.agent.watched_tokens:
                    continue

                evict_ts = self.eviction_blacklist.get(sym)
                if evict_ts is not None:
                    remaining = EVICTION_COOLDOWN - (time.time() - evict_ts)
                    if remaining > 0:
                        print(f"  [skip] {sym}: evicted — cooldown {remaining/60:.0f}m remaining")
                        continue
                    else:
                        del self.eviction_blacklist[sym]

                rsi_skip_ts = self.discovery_rsi_skip.get(sym)
                if rsi_skip_ts is not None:
                    remaining_rsi = self.DISCOVERY_RSI_SKIP_SECS - (time.time() - rsi_skip_ts)
                    if remaining_rsi > 0:
                        continue
                    else:
                        del self.discovery_rsi_skip[sym]

                # [FIX 4] Check persistent hard-fail suppression
                suppress_until = self.hard_fail_suppress.get(sym)
                if suppress_until is not None:
                    if time.time() < suppress_until:
                        rem = int(suppress_until - time.time())
                        print(f"  [skip] {sym}: hard-fail suppressed — {rem//60}m{rem%60}s remaining")
                        continue
                    else:
                        del self.hard_fail_suppress[sym]
                        self.hard_fail_cycles.pop(sym, None)

                # [FIX 2] Raised liquidity threshold for discovery
                if liq < DISCOVERY_MIN_LIQ:
                    print(f"  [skip] {sym}: liq ${liq/1000:.0f}k — rug risk (< ${DISCOVERY_MIN_LIQ/1000:.0f}k)")
                    skipped_liq += 1
                    continue
                if liq > 10_000_000:
                    print(f"  [skip] {sym}: liq ${liq/1e6:.1f}M — pump already in")
                    skipped_liq += 1
                    continue
                if mcap and mcap > 50_000_000:
                    print(f"  [skip] {sym}: mcap ${mcap/1e6:.0f}M — too big for meme play")
                    skipped_liq += 1
                    continue

                print(f"  Fetching {sym} {'🚀[BOOST]' if boosted else ''}...")
                hist, is_real = self.detector.fetch_historical_prices(
                    pair_addr, n_points=500, token_info=token)

                if not hist:
                    print(f"  [skip] {sym}: no price history")
                    continue

                closes = [c["close"] if isinstance(c, dict) else c for c in hist]
                rsi_val = _calc_rsi(closes)
                if rsi_val is not None:
                    if rsi_val > 83:
                        print(f"  [skip] {sym}: RSI {rsi_val:.0f} > 83 — parabolic, skip")
                        self.discovery_rsi_skip[sym] = time.time()
                        skipped_rsi += 1
                        continue
                    if rsi_val < 22:
                        print(f"  [skip] {sym}: RSI {rsi_val:.0f} < 22 — dying, skip")
                        self.discovery_rsi_skip[sym] = time.time()
                        skipped_rsi += 1
                        continue

                if sym not in TOKENS:
                    TOKENS[sym] = addr
                self.agent.add_token(sym, addr)

                # [FIX 3] Tag this token with the current cohort ID
                self.token_cohort[sym] = current_cohort

                self.token_metadata[sym] = {
                    "is_boosted":           boosted,
                    "quality_indicator":    quality,
                    "score":                score_t,
                    "price_change_24h":     ch24,
                    "price_change_h1":      ch1,
                    "liquidity_usd":        liq,
                    "liquidity_change_pct": token.get("liquidity_change_pct", 0.0),
                    "market_cap_usd":       mcap,
                    "age_hours":            age_h,
                    "volume_24h_usd":       token.get("volume_24h_usd", 0),
                    "is_synthetic":         not is_real,
                }

                self.agent.prefill_price_history(sym, hist)
                buf = self.agent.price_history[sym]
                buf["live_points"] = len(hist) if is_real else 0
                status_s = f"[OK] {len(hist)} candles" if is_real else "[~] synthetic"
                age_s  = f"{age_h:.1f}h" if age_h else "age?"
                mcap_s = f"${mcap/1000:.0f}k" if mcap else "mcap?"
                rsi_s  = f"{rsi_val:.0f}" if rsi_val else "?"
                print(f"  → {added+1}. {sym:<10} {quality:<10}  "
                      f"24h={ch24:+.0f}%  liq=${liq/1000:.0f}k  "
                      f"age={age_s}  mcap={mcap_s}  RSI={rsi_s}  {status_s}")
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
            self.last_refresh = time.time() - self.refresh_interval + 30

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY GATE
    # ─────────────────────────────────────────────────────────────────────────

    def _can_enter(self, token: str, analysis: Dict) -> bool:
        if token in self.sl_blacklist:
            return False
        if analysis.get("hard_fail"):
            return False

        if token in self.sl_cooldown:
            if time.time() < self.sl_cooldown[token]:
                rem = int(self.sl_cooldown[token] - time.time())
                print(f"  [skip] stop cooldown {rem}s")
                return False
            del self.sl_cooldown[token]

        prob = analysis.get("prob_score", 0.0)
        if prob < MIN_PROB:
            print(f"  [skip] prob {prob:.0f} < {MIN_PROB:.0f}")
            return False

        if token in self.tp_lockout:
            if time.time() < self.tp_lockout[token]:
                rem = int(self.tp_lockout[token] - time.time())
                print(f"  [skip] TP lockout {rem}s")
                return False
            del self.tp_lockout[token]

        if token in self.last_trades:
            elapsed = (datetime.now() - self.last_trades[token]).total_seconds()
            if elapsed < self.trade_cooldown_secs:
                print(f"  [skip] cooldown {int(self.trade_cooldown_secs - elapsed)}s")
                return False

        if date.today() != self.day_start:
            self.trades_today = 0
        if self.trades_today >= self.max_trades_per_day:
            print(f"  [skip] daily limit {self.trades_today}/{self.max_trades_per_day}")
            return False

        if token in self.agent.portfolio.positions:
            print(f"  [skip] already holding")
            return False

        if len(self.agent.portfolio.positions) >= MAX_CONCURRENT:
            print(f"  [skip] max {MAX_CONCURRENT} meme positions")
            return False

        total = self.agent.portfolio.get_total_value()
        if self.agent.portfolio.usdc_balance < 15:
            print(f"  [skip] insufficient USDC")
            return False

        if analysis.get("buy_pts", 0) < self.min_soft_score:
            print(f"  [skip] soft {analysis.get('buy_pts',0)}/{self.min_soft_score}")
            return False

        if analysis.get("sell_divergence", False):
            print(f"  [skip] sell divergence — insiders distributing")
            return False

        # [FIX 3] Correlation guard: count how many open positions share
        # the same discovery cohort as this token and block if at limit.
        token_cohort = self.token_cohort.get(token)
        if token_cohort is not None:
            cohort_open = sum(
                1 for tok in self.agent.portfolio.positions
                if self.token_cohort.get(tok) == token_cohort
            )
            if cohort_open >= MAX_COHORT_POSITIONS:
                print(f"  [skip] {token}: cohort #{token_cohort} already has "
                      f"{cohort_open}/{MAX_COHORT_POSITIONS} open — correlation guard")
                return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT MONITORING
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_exits(self, current_analyses: Dict):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl      = position.pnl_percentage
            analysis = current_analyses.get(token, {})

            if token not in self.position_peaks or pnl > self.position_peaks[token]:
                self.position_peaks[token]   = pnl
                self.position_peak_ts[token] = time.time()
            peak    = self.position_peaks[token]
            peak_ts = self.position_peak_ts.get(token, time.time())

            atr_pct    = self.position_atr.get(token) or analysis.get("atr_pct", 0.05)
            tp1_mult   = self.position_tp1_atr.get(token,   MemeSignalEngine.TP1_ATR_MULT)
            tp2_mult   = self.position_tp2_atr.get(token,   MemeSignalEngine.TP2_ATR_MULT)
            trail_mult = self.position_trail_atr.get(token, MemeSignalEngine.TRAIL_ATR_MULT)
            engine_lbl = self.position_engine.get(token, "?")

            # [FIX 1] Use engine-specific stop override when present so HWR
            # positions use the tighter 3.5x / 5% cap, not the global defaults.
            stop_atr_mult = self.position_stop_atr.get(token, MemeSignalEngine.STOP_ATR_MULT)
            stop_max_pct  = self.position_stop_max.get(token, MemeSignalEngine.MAX_STOP_PCT)
            stop_pct  = min(stop_atr_mult * atr_pct, stop_max_pct) * 100
            tp1_pct   = tp1_mult   * atr_pct * 100
            tp2_pct   = tp2_mult   * atr_pct * 100
            trail_pct = trail_mult * atr_pct * 100

            drop_from_peak = peak - pnl
            time_at_peak   = time.time() - peak_ts
            if (peak > tp1_pct * 0.5
                    and drop_from_peak >= MemeSignalEngine.DUMP_DROP_PCT * 100
                    and time_at_peak < 180):
                print(f"\n  🚨 DUMP [{engine_lbl}] {token}  "
                      f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                      f"Drop:{drop_from_peak:.1f}% in {time_at_peak:.0f}s — EXIT NOW")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self._record(token, "DUMP_EXIT", pnl, position.pnl, engine_lbl)
                    self.sizer.record_loss() if pnl < 0 else self.sizer.record_win()
                    self._cleanup_position(token)
                continue

            # [FIX 5] Stale position exit.
            # Fires when a position has been open long enough, never hit TP1,
            # and the peak has significantly faded — meaning the move is over
            # and the capital should be redeployed.
            if not self.tp1_hit.get(token, False):
                entry_ts   = self.position_entry_ts.get(token, time.time())
                hold_secs  = time.time() - entry_ts
                stale_warn_ts_key = f"__stale_warn_{token}"
                # Conditions: held long enough, peak was meaningful, now faded
                peak_was_real  = peak >= tp1_pct * STALE_MIN_PEAK
                now_faded      = pnl  <= tp1_pct * STALE_FADE_RATIO
                held_long      = hold_secs >= STALE_HOLD_SECS

                if held_long and peak_was_real and now_faded:
                    # Check if we already issued a warning
                    warn_ts = getattr(self, stale_warn_ts_key, None)
                    if warn_ts is None:
                        # First time: issue warning, start grace period
                        setattr(self, stale_warn_ts_key, time.time())
                        print(f"\n  ⏳ STALE [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"TP1:{tp1_pct:.1f}%  Hold:{hold_secs/60:.1f}m  "
                              f"— grace {STALE_GRACE_SECS}s before exit")
                    elif time.time() - warn_ts >= STALE_GRACE_SECS:
                        # Grace period elapsed and still stale — exit now
                        print(f"\n  🕰 STALE EXIT [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"TP1:{tp1_pct:.1f}%  Hold:{hold_secs/60:.1f}m  "
                              f"— dead capital, redeploying")
                        trade = self.agent.execute_swap(token, "USDC", position.amount)
                        if trade:
                            self.last_trades[token] = datetime.now()
                            self._record(token, "STALE", pnl, position.pnl, engine_lbl)
                            self.sizer.record_loss() if pnl < 0 else self.sizer.record_win()
                            self._cleanup_position(token)
                            # Clear the warning attribute
                            try:
                                delattr(self, stale_warn_ts_key)
                            except AttributeError:
                                pass
                        continue
                    else:
                        rem = int(STALE_GRACE_SECS - (time.time() - warn_ts))
                        print(f"  ⏳ stale [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"exit in {rem}s if no recovery")
                elif held_long and getattr(self, stale_warn_ts_key, None) is not None:
                    # Was warned but has recovered — cancel the warning
                    try:
                        delattr(self, stale_warn_ts_key)
                    except AttributeError:
                        pass
                    print(f"  ✅ stale warn cancelled [{engine_lbl}] {token}  "
                          f"P&L:{pnl:+.1f}% — recovered")

                # [FIX 8] No-traction exit.
                # Fires when a position never found positive ground:
                # held >15min, peak never reached 10% of TP1, currently negative.
                # Catches trades that went red immediately and stayed there —
                # the stale exit (FIX 5) doesn't fire in this case because
                # it requires peak ≥ 50% TP1 first.
                no_traction_warn_key = f"__no_traction_warn_{token}"
                never_peaked   = peak < tp1_pct * NO_TRACTION_MAX_PEAK
                currently_neg  = pnl < 0
                held_long_nt   = hold_secs >= NO_TRACTION_HOLD_SECS

                if held_long_nt and never_peaked and currently_neg:
                    warn_ts_nt = getattr(self, no_traction_warn_key, None)
                    if warn_ts_nt is None:
                        setattr(self, no_traction_warn_key, time.time())
                        print(f"\n  ⏳ NO-TRACTION [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"TP1:{tp1_pct:.1f}%  Hold:{hold_secs/60:.1f}m  "
                              f"— never found ground, grace {NO_TRACTION_GRACE_SECS}s")
                    elif time.time() - warn_ts_nt >= NO_TRACTION_GRACE_SECS:
                        print(f"\n  🕰 NO-TRACTION EXIT [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"Hold:{hold_secs/60:.1f}m  — redeploying capital")
                        trade = self.agent.execute_swap(token, "USDC", position.amount)
                        if trade:
                            self.last_trades[token] = datetime.now()
                            self._record(token, "NO_TRACTION", pnl, position.pnl, engine_lbl)
                            self.sizer.record_loss() if pnl < 0 else self.sizer.record_win()
                            self._cleanup_position(token)
                            try:
                                delattr(self, no_traction_warn_key)
                            except AttributeError:
                                pass
                        continue
                    else:
                        rem = int(NO_TRACTION_GRACE_SECS - (time.time() - warn_ts_nt))
                        print(f"  ⏳ no-traction [{engine_lbl}] {token}  "
                              f"P&L:{pnl:+.1f}%  Peak:{peak:+.1f}%  "
                              f"exit in {rem}s if no recovery")
                elif getattr(self, no_traction_warn_key, None) is not None:
                    # Conditions no longer met (e.g. went positive) — cancel warning
                    try:
                        delattr(self, no_traction_warn_key)
                    except AttributeError:
                        pass
                    print(f"  ✅ no-traction warn cancelled [{engine_lbl}] {token}  "
                          f"P&L:{pnl:+.1f}% — found traction")

            if pnl <= -stop_pct:
                print(f"\n  ❌ STOP [{engine_lbl}] {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-stop:{-stop_pct:.1f}%  ATR={atr_pct*100:.1f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token]   = datetime.now()
                    self.sl_strikes[token]    = self.sl_strikes.get(token, 0) + 1
                    self.sl_cooldown[token]   = time.time() + COOLDOWN_AFTER_STOP
                    if self.sl_strikes[token] >= 2:
                        self.sl_blacklist.add(token)
                        print(f"  🚫 Blacklisted {token} (2 stops)")
                    self._record(token, "STOP", pnl, position.pnl, engine_lbl)
                    self.sizer.record_loss()
                    self._cleanup_position(token)
                continue

            if pnl >= tp1_pct and not self.tp1_hit.get(token, False):
                sell_amt = position.amount * MemeSignalEngine.TP1_AMOUNT
                print(f"\n  💰 TP1 [{engine_lbl}] {token}  P&L:{pnl:+.1f}%  "
                      f"TP1:{tp1_pct:.1f}%  Locking {MemeSignalEngine.TP1_AMOUNT*100:.0f}%")
                trade = self.agent.execute_swap(token, "USDC", sell_amt)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp1_hit[token]     = True
                    self._record(token, "TP1", pnl, position.pnl * MemeSignalEngine.TP1_AMOUNT, engine_lbl)
                continue

            if (pnl >= tp2_pct and self.tp1_hit.get(token)
                    and not self.tp2_hit.get(token, False)):
                print(f"\n  🚀💰 TP2 [{engine_lbl}] {token}  P&L:{pnl:+.1f}%  "
                      f"TP2:{tp2_pct:.1f}%  Exiting remainder")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp2_hit[token]     = True
                    self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                    self._record(token, "TP2", pnl, position.pnl, engine_lbl)
                    self.sizer.record_win()
                    self._cleanup_position(token)
                continue

            if peak >= tp1_pct:
                floor = peak - trail_pct
                if pnl <= floor:
                    print(f"\n  🏁 TRAIL [{engine_lbl}] {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%  Trail:{trail_pct:.1f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                        self._record(token, "TRAIL", pnl, position.pnl, engine_lbl)
                        self.sizer.record_win() if pnl > 0 else self.sizer.record_loss()
                        self._cleanup_position(token)
                else:
                    print(f"  🏃 trail [{engine_lbl}] {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%")

    def _cleanup_position(self, token: str):
        self.position_peaks.pop(token, None)
        self.position_peak_ts.pop(token, None)
        self.position_atr.pop(token, None)
        self.position_engine.pop(token, None)
        self.position_tp1_atr.pop(token, None)
        self.position_tp2_atr.pop(token, None)
        self.position_trail_atr.pop(token, None)
        self.position_stop_atr.pop(token, None)   # [FIX 1]
        self.position_stop_max.pop(token, None)   # [FIX 1]
        self.position_entry_ts.pop(token, None)   # [FIX 5]
        # Clear any stale warning attribute for this token
        try:
            delattr(self, f"__stale_warn_{token}")
        except AttributeError:
            pass
        # Clear any no-traction warning attribute for this token  [FIX 8]
        try:
            delattr(self, f"__no_traction_warn_{token}")
        except AttributeError:
            pass
        self.tp1_hit.pop(token, None)
        self.tp2_hit.pop(token, None)
        self.hwr_positions.discard(token)
        self.mg_positions.discard(token)

    def _record(self, token: str, exit_type: str, pnl_pct: float,
                pnl_usd: float, engine: str = "?"):
        self.session_trades.append({
            "token":    token,
            "exit":     exit_type,
            "pnl_pct":  pnl_pct,
            "pnl_usd":  pnl_usd,
            "engine":   engine,
            "timestamp":datetime.now(),
            "win":      pnl_pct > 0,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTE TRADE
    # ─────────────────────────────────────────────────────────────────────────

    def execute_trade(self, token: str, analysis: Dict, signal: Dict):
        engine_name = signal["engine"]
        prob        = analysis.get("prob_score", 0.0)
        cur         = analysis.get("current_price", 0.0)
        atr_pct     = analysis.get("atr_pct", 0.05)
        rr          = analysis.get("rr_ratio", 0.0)
        rv          = analysis.get("relative_volume", 1.0)
        vol_lbl     = analysis.get("volume_state", "?")
        vel         = analysis.get("velocity_5", 0.0)
        mult        = analysis.get("multiple_from_low", 1.0)
        if cur == 0:
            return

        total       = self.agent.portfolio.get_total_value()
        base_usdc   = total * signal["size_pct"] * self.sizer.multiplier
        atr_factor  = 1.0
        if atr_pct > MAX_ATR_FOR_FULL_SIZE:
            atr_factor = max(MAX_ATR_FOR_FULL_SIZE / atr_pct, 0.35)
        usdc_amt    = min(base_usdc * atr_factor, MAX_POSITION_USD)
        usdc_amt    = min(usdc_amt, self.agent.portfolio.usdc_balance * 0.90)

        if usdc_amt < 5:
            print(f"  [skip] size ${usdc_amt:.2f} too small")
            return

        tp1_atr   = signal["tp1_atr"]
        tp2_atr   = signal["tp2_atr"]
        trail_atr = signal["trail_atr"]
        stop_atr_mult = signal.get("stop_atr", MemeSignalEngine.STOP_ATR_MULT)
        stop_max_pct  = signal.get("max_stop", MemeSignalEngine.MAX_STOP_PCT)
        stop_pct  = min(stop_atr_mult * atr_pct, stop_max_pct) * 100
        tp1_pct   = tp1_atr   * atr_pct * 100
        tp2_pct   = tp2_atr   * atr_pct * 100

        detail = analysis.get("buy_pts_detail", {})
        flags  = "".join([
            "V" if detail.get("vol_explosion") else ("v" if detail.get("vol_spike") else "."),
            "P" if detail.get("price_velocity") else ".",
            "R" if detail.get("rsi_sweet")      else ".",
            "M" if detail.get("macd_cross")     else ".",
            "X" if detail.get("room_to_run")    else ".",
            "E" if detail.get("ema_velocity")   else ".",
            "A" if detail.get("atr_expanding")  else ".",
        ])

        boosted  = self.token_metadata.get(token, {}).get("is_boosted", False)
        age_h    = self.token_metadata.get(token, {}).get("age_hours", None)
        engine_emoji = "🛡" if engine_name == "HWR" else "🔥"
        af_str   = (f"  aggression×{signal.get('aggression_factor', 1.0):.2f}"
                    if engine_name == "MG" else "")
        pattern  = signal.get("pattern", "")

        print(f"\n  ══ {engine_emoji} BUY [{engine_name}] {token} "
              f"{'🚀[BOOST]' if boosted else ''} ══")
        if pattern:
            print(f"     Pattern:    {pattern}")
        print(f"     Prob:       {prob:.0f}%  |  Score: {analysis.get('buy_pts',0)}/8  [{flags}]")
        print(f"     Size:       ${usdc_amt:.2f}  sizer×{self.sizer.multiplier:.2f}{af_str}")
        print(f"     Price:      ${cur:.8f}")
        print(f"     Vol:        {vol_lbl}  Velocity: {vel:+.1f}%/5bars")
        print(f"     From low:   {mult:.1f}x  R:R: {rr:.2f}")
        print(f"     ATR:        {atr_pct*100:.1f}%  →  "
              f"Stop:{-stop_pct:.1f}%  TP1:+{tp1_pct:.1f}%  TP2:+{tp2_pct:.1f}%")
        if age_h:
            print(f"     Age:        {age_h:.1f}h")
        print(f"     Engine TP1: {tp1_atr}x  TP2: {tp2_atr}x  Trail: {trail_atr}x")

        trade = self.agent.execute_swap("USDC", token, usdc_amt)
        if trade:
            self.last_trades[token]           = datetime.now()
            self.trades_today                += 1
            self.position_peaks[token]        = 0.0
            self.position_peak_ts[token]      = time.time()
            self.position_atr[token]          = atr_pct
            self.position_engine[token]       = engine_name
            self.position_tp1_atr[token]      = tp1_atr
            self.position_tp2_atr[token]      = tp2_atr
            self.position_trail_atr[token]    = trail_atr
            # [FIX 1] Store engine-specific stop parameters if provided
            self.position_stop_atr[token]     = signal.get("stop_atr", MemeSignalEngine.STOP_ATR_MULT)
            self.position_stop_max[token]     = signal.get("max_stop", MemeSignalEngine.MAX_STOP_PCT)
            self.position_entry_ts[token]     = time.time()   # [FIX 5] stale exit timer
            self.tp1_hit[token]               = False
            self.tp2_hit[token]               = False
            if engine_name == "HWR":
                self.hwr_positions.add(token)
            else:
                self.mg_positions.add(token)
            print(f"  ✅ Executed  [{engine_name}]")
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

        print(f"\n  ── Meme Session Stats ────────────────────────────────")
        print(f"     Trades: {len(self.session_trades)}  "
              f"WR: {wr:.0f}%  ({len(wins)}W/{len(losses)}L)  "
              f"Net: ${pnl:+.2f}")
        if wins:
            print(f"     Avg win:  +{sum(t['pnl_pct'] for t in wins)/len(wins):.1f}%")
        if losses:
            print(f"     Avg loss:  {sum(t['pnl_pct'] for t in losses)/len(losses):.1f}%")
        if wins and losses:
            avg_w = sum(t["pnl_pct"] for t in wins) / len(wins)
            avg_l = abs(sum(t["pnl_pct"] for t in losses) / len(losses))
            if avg_l > 0:
                print(f"     Realised R:R: {avg_w/avg_l:.2f}")
        exits_str = "  ".join(f"{k}:{v}" for k, v in sorted(by_exit.items()))
        print(f"     Exits:  {exits_str}")

        for eng in ("HWR", "MG"):
            e_trades = [t for t in self.session_trades if t.get("engine") == eng]
            if not e_trades:
                continue
            e_wins  = [t for t in e_trades if t["win"]]
            e_wr    = len(e_wins) / len(e_trades) * 100
            e_pnl   = sum(t["pnl_usd"] for t in e_trades)
            icon    = "🛡" if eng == "HWR" else "🔥"
            print(f"     {icon}[{eng}]  {len(e_trades)} trades  "
                  f"WR:{e_wr:.0f}%  Net:${e_pnl:+.2f}")

        print(f"     Sizer ×{self.sizer.multiplier:.2f}  "
              f"Win streak: {self.sizer.consecutive_wins}/{WIN_STREAK_REQ}  "
              f"EqHWM: ${self.tracker.hwm:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, update_interval: int = 15):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 70)
        print("  🚀 SOLANA MEME COIN TRADER  —  v2.8  (Engine Integrity)")
        print("=" * 70)
        print(f"\nEngines:")
        print(f"  🛡 HWR  size:{HWREngine.SIZE_PCT*100:.0f}%  "
              f"TP1:{HWREngine.TP1_ATR}x  TP2:{HWREngine.TP2_ATR}x  "
              f"Trail:{HWREngine.TRAIL_ATR}x  Stop:{HWREngine.STOP_ATR}x(cap {HWREngine.MAX_STOP*100:.0f}%)  "
              f"liq>${HWR_MIN_LIQUIDITY/1000:.0f}k  RSI 42-62  vol 0.6-{MEME_VOL_EXPLOSION:.0f}x")
        print(f"  🔥 MG   size:{MGEngine.SIZE_PCT*100:.0f}%  "
              f"TP1:{MGEngine.TP1_ATR}x  TP2:{MGEngine.TP2_ATR}x  "
              f"Trail:{MGEngine.TRAIL_ATR}x  breakout≥{MG_BREAKOUT_MIN_REGIME}  reclaim≥0")
        print(f"\nFixes active (v2.3→2.8):")
        print(f"  [1] HWR stop tightened → {HWREngine.STOP_ATR}x ATR / {HWREngine.MAX_STOP*100:.0f}% cap  "
              f"| TP1 raised → {HWREngine.TP1_ATR}x ATR")
        print(f"  [2] Liquidity gate raised → HWR ${HWR_MIN_LIQUIDITY/1000:.0f}k  "
              f"| Discovery ${DISCOVERY_MIN_LIQ/1000:.0f}k")
        print(f"  [3] Correlation guard → max {MAX_COHORT_POSITIONS} same-cohort positions")
        print(f"  [4] Transient hard-fail suppression → ban after {HARD_FAIL_BAN_CYCLES} cycles / "
              f"{HARD_FAIL_BAN_SECS//60}m")
        print(f"  [5] Stale exit → hold>{STALE_HOLD_SECS//60}m + peak≥{STALE_MIN_PEAK*100:.0f}%TP1 "
              f"+ now≤{STALE_FADE_RATIO*100:.0f}%TP1 → grace {STALE_GRACE_SECS}s → exit")
        print(f"  [6] Mid-cycle stop watch → re-poll in {STOP_MIDCYCLE_SLEEP}s when within "
              f"{STOP_WATCH_BUFFER}× stop distance")
        print(f"  [7] MG breakout floor regime≥{MG_BREAKOUT_MIN_REGIME} | reclaim always allowed")
        print(f"  [8] No-traction exit → hold>{NO_TRACTION_HOLD_SECS//60}m + peak<{NO_TRACTION_MAX_PEAK*100:.0f}%TP1 "
              f"+ pnl<0 → grace {NO_TRACTION_GRACE_SECS}s → exit")
        print(f"  [9] HWR vol gate: 0.6x floor + explosion cap (vol≥3x → MG only)")
        print(f"  [10] No-traction gap closed: MAX_PEAK 10%→20% TP1")
        print(f"  [11] No-traction hold 15m→{NO_TRACTION_HOLD_SECS//60}m for faster HWR redeployment")
        print(f"  [12] WIN_STREAK_REQ 3→{WIN_STREAK_REQ} — compounds after fewer closed wins")
        print(f"  [13] Stop display uses engine-specific stored params (was global constants)")
        print(f"\nDynamic Allocation (Regime-Based):")
        print(f"  Score 85+ (Mania)     → MG:60%  HWR:40%")
        print(f"  Score <65 (Defensive) → MG:30%  HWR:70%")
        print(f"\nDrawdown throttle:")
        print(f"  DD>{DD_THROTTLE_PCT:.0f}% → MG size ×0.70  |  "
              f"DD>{DD_DISABLE_PCT:.0f}% → MG disabled")
        print(f"\nCycle: {update_interval}s  Discovery: every {self.refresh_interval}s")
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

                tokens_analysis: Dict[str, Dict] = {}
                for token in list(self.agent.watched_tokens):
                    if token in SKIP:
                        continue
                    buf      = self.agent.price_history.get(token, {})
                    live_pts = buf.get("live_points", 0)
                    prices   = buf.get("prices",  [])
                    volumes  = buf.get("volumes", [])
                    if live_pts < self.min_live_candles or not prices:
                        continue
                    # Inject current regime score so hard-filter ceilings and
                    # required soft-score thresholds adapt to live market conditions.
                    meta = dict(self.token_metadata.get(token, {}))
                    meta["regime_score"] = self._current_score
                    analysis = self.engine.analyze(
                        token, prices, volumes,
                        token_meta=meta,
                        min_soft_score=self.min_soft_score,
                    )
                    tokens_analysis[token] = analysis

                equity = self.agent.portfolio.get_total_value()
                self._check_daily_reset(equity)
                self.tracker.update(equity)
                dd           = self.tracker.drawdown_pct(equity)
                daily_loss   = self.tracker.daily_loss_pct(equity)
                halted       = self._check_circuit_breaker(equity)

                self._apply_drawdown_throttle(dd)
                allowed, regime_score, regime_msg = self._check_regime(tokens_analysis)
                print(f"\n{regime_msg}")

                if halted:
                    print("  ⛔ Circuit breaker — monitoring exits only")
                    if self.agent.portfolio.positions:
                        self._monitor_exits(tokens_analysis)
                elif not allowed:
                    if self.agent.portfolio.positions:
                        print("[positions] Monitoring stops (regime paused)...")
                        self._monitor_exits(tokens_analysis)
                else:
                    if self.agent.portfolio.positions:
                        print("\n[positions] Monitoring exits...")
                        self._monitor_exits(tokens_analysis)

                    print("\n[analysis] Scanning meme tokens...")
                    qualified = []

                    for token, analysis in tokens_analysis.items():
                        meta      = self.token_metadata.get(token, {})
                        boosted   = meta.get("is_boosted", False)
                        hard_fail = analysis.get("hard_fail", False)
                        hard_msg  = analysis.get("hard_fail_reason", "")
                        soft      = analysis.get("buy_pts", 0)
                        prob      = analysis.get("prob_score", 0.0)
                        rv        = analysis.get("relative_volume", 1.0)
                        vel       = analysis.get("velocity_5", 0.0)
                        mult      = analysis.get("multiple_from_low", 1.0)
                        rr        = analysis.get("rr_ratio", 0.0)
                        atr       = analysis.get("atr_pct", 0.0) * 100
                        sell_div  = analysis.get("sell_divergence", False)
                        ts        = datetime.now().strftime("%H:%M:%S")

                        detail = analysis.get("buy_pts_detail", {})
                        flags  = "".join([
                            "V" if detail.get("vol_explosion") else ("v" if detail.get("vol_spike") else "."),
                            "P" if detail.get("price_velocity")  else ".",
                            "R" if detail.get("rsi_sweet")       else ".",
                            "M" if detail.get("macd_cross")      else ".",
                            "X" if detail.get("room_to_run")     else ".",
                            "E" if detail.get("ema_velocity")    else ".",
                            "A" if detail.get("atr_expanding")   else ".",
                        ])

                        boost_tag = "🚀" if boosted else "  "
                        sig       = analysis["signal"]
                        sd        = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(sig, "HOLD")
                        score_str = ("✗HARD" if hard_fail else
                                     f"{soft}/8[{flags}] p{prob:.0f}% R{rr:.1f}")
                        div_warn  = " ⚠DIV" if sell_div else ""

                        print(f"  [{ts}] {boost_tag}{token:<12}  "
                              f"${analysis['current_price']:<12.8f}  "
                              f"RSI:{analysis['rsi']:.0f}  {sd}  "
                              f"vol:{rv:.1f}x  vel:{vel:+.1f}%  "
                              f"{mult:.1f}x-low  ATR:{atr:.1f}%  "
                              f"{score_str}{div_warn}")

                        if hard_fail:
                            print(f"           ↳ {hard_msg[:90]}")
                            structural_keys = (
                                "liquidity", "age", "mcap",
                                "parabolic", "distribution", "volume"
                            )
                            is_structural = any(k in hard_msg for k in structural_keys)
                            if is_structural:
                                self.eviction_fails[token] = self.eviction_fails.get(token, 0) + 1
                                fails = self.eviction_fails[token]
                                if fails >= EVICTION_FAIL_LIMIT:
                                    if token in self.agent.watched_tokens:
                                        self.agent.watched_tokens.remove(token)
                                    self.token_metadata.pop(token, None)
                                    self.eviction_blacklist[token] = time.time()
                                    self.eviction_fails.pop(token, None)
                                    print(f"           ⏏  {token} evicted after "
                                          f"{EVICTION_FAIL_LIMIT} structural hard-fails")
                                else:
                                    print(f"           ↳ [{fails}/{EVICTION_FAIL_LIMIT}] "
                                          f"structural hard-fails")
                            else:
                                # [FIX 4] Transient hard-fails (rug_risk, atr_low) that
                                # repeat without ever clearing are tracked separately.
                                # Once they hit HARD_FAIL_BAN_CYCLES, the token is
                                # removed from the watchlist and suppressed in discovery
                                # so it stops consuming scan+analysis cycles.
                                self.eviction_fails.pop(token, None)
                                self.hard_fail_cycles[token] = self.hard_fail_cycles.get(token, 0) + 1
                                hf_count = self.hard_fail_cycles[token]
                                if hf_count >= HARD_FAIL_BAN_CYCLES:
                                    if token in self.agent.watched_tokens:
                                        self.agent.watched_tokens.remove(token)
                                    self.token_metadata.pop(token, None)
                                    self.hard_fail_suppress[token] = time.time() + HARD_FAIL_BAN_SECS
                                    self.hard_fail_cycles.pop(token, None)
                                    print(f"           🚫 {token} suppressed {HARD_FAIL_BAN_SECS//60}m "
                                          f"({HARD_FAIL_BAN_CYCLES} transient hard-fails)")
                                else:
                                    print(f"           ↳ transient (RSI/RR) — watching for recovery "
                                          f"[{hf_count}/{HARD_FAIL_BAN_CYCLES}]")
                        elif sig == "BUY":
                            self.eviction_fails.pop(token, None)
                            self.hard_fail_cycles.pop(token, None)   # [FIX 4] reset on BUY
                        else:
                            self.eviction_fails.pop(token, None)
                            self.hard_fail_cycles.pop(token, None)   # [FIX 4] reset on HOLD

                    engine_candidates = self.evaluate_entries(tokens_analysis, regime_score)

                    mg_status = ("DISABLED" if self.mg_engine.disabled
                                 else f"×{self.mg_engine.size_reduction:.1f}")
                    hwr_exp   = self._hwr_exposure()
                    mg_exp    = self._mg_exposure()
                    total_v   = self.agent.portfolio.get_total_value()
                    hwr_r, mg_r = self._get_cap_ratios()
                    
                    print(f"\n  [engines] HWR: ${hwr_exp:.0f}/${total_v*hwr_r:.0f} "
                          f"| MG: ${mg_exp:.0f}/${total_v*mg_r:.0f} ({mg_status}) "
                          f"  aggression×{self._aggression_factor(regime_score):.2f}")

                    for token, analysis, signal in engine_candidates:
                        if not self._can_enter(token, analysis):
                            continue
                        self.execute_trade(token, analysis, signal)

                perf = self.agent.portfolio.get_performance()
                print(f"\n[portfolio]")
                print(f"  USDC:         ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total:        ${perf['current_value']:.2f}")
                print(f"  Return:       ${perf['total_return']:+.2f} ({perf['total_return_pct']:+.2f}%)")
                print(f"  HWM:          ${self.tracker.hwm:.2f}  DD: {dd:.1f}%")
                print(f"  Daily P&L:    {-daily_loss:+.2f}%  "
                      f"({'⛔ HALTED' if halted else f'limit {DAILY_LOSS_LIMIT_PCT}%'})")
                print(f"  Sizer ×:      {self.sizer.multiplier:.2f}  "
                      f"wins: {self.sizer.consecutive_wins}/{WIN_STREAK_REQ}")
                print(f"  Trades today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Regime:       {self._current_score:.0f}/100")
                print(f"  Watching:     {len(self.agent.watched_tokens)} meme tokens")

                if self.agent.portfolio.positions:
                    print(f"\n  Open positions:")
                    for tok, pos in self.agent.portfolio.positions.items():
                        peak       = self.position_peaks.get(tok, 0)
                        atr_e      = self.position_atr.get(tok, 0.05)
                        eng        = self.position_engine.get(tok, "?")
                        tp1_m      = self.position_tp1_atr.get(tok, MemeSignalEngine.TP1_ATR_MULT)
                        stop_atr_m = self.position_stop_atr.get(tok, MemeSignalEngine.STOP_ATR_MULT)
                        stop_max_p = self.position_stop_max.get(tok, MemeSignalEngine.MAX_STOP_PCT)
                        stop_l     = -min(stop_atr_m * atr_e, stop_max_p) * 100
                        tp1_l      = tp1_m * atr_e * 100
                        tp_flag    = ("✓TP2" if self.tp2_hit.get(tok) else
                                      "✓TP1" if self.tp1_hit.get(tok) else "    ")
                        boosted_t  = "🚀" if self.token_metadata.get(tok, {}).get("is_boosted") else "  "
                        eng_icon   = "🛡" if eng == "HWR" else ("🔥" if eng == "MG" else "?")
                        print(f"    {boosted_t}{eng_icon}[{eng}] {tok:<10}  "
                              f"P&L:{pos.pnl_percentage:+.2f}%  "
                              f"Peak:{peak:+.2f}%  "
                              f"{tp_flag}  "
                              f"Stop:{stop_l:.1f}%  TP1:+{tp1_l:.1f}%  "
                              f"ATR:{atr_e*100:.1f}%")

                if self.sl_blacklist:
                    print(f"\n  🚫 Blacklisted: {', '.join(self.sl_blacklist)}")

                self._print_stats()

                nxt = max(0, int(self.refresh_interval - (time.time() - self.last_refresh)))
                print(f"\n[wait] {update_interval}s  |  Discovery in {nxt//60}m{nxt%60}s")

                # [FIX 6] Mid-cycle stop watch.
                # If any open position is within STOP_WATCH_BUFFER × its stop
                # distance, sleep briefly then re-poll price and check stops.
                # This catches fast drops (like AI16Z's -9.1% vs -5.4% stop)
                # that would otherwise slip through the full 15s cycle gap.
                if self.agent.portfolio.positions:
                    at_risk = []
                    for tok, pos in self.agent.portfolio.positions.items():
                        pnl_now  = pos.pnl_percentage
                        atr_e    = self.position_atr.get(tok, 0.05)
                        stop_m   = self.position_stop_atr.get(tok, MemeSignalEngine.STOP_ATR_MULT)
                        stop_cap = self.position_stop_max.get(tok, MemeSignalEngine.MAX_STOP_PCT)
                        stop_pct = min(stop_m * atr_e, stop_cap) * 100
                        # Within STOP_WATCH_BUFFER × stop distance = at risk
                        if pnl_now <= -(stop_pct / STOP_WATCH_BUFFER):
                            at_risk.append(tok)

                    if at_risk:
                        print(f"  ⚡ Stop-watch: {', '.join(at_risk)} near stop — "
                              f"re-checking in {STOP_MIDCYCLE_SLEEP}s")
                        time.sleep(STOP_MIDCYCLE_SLEEP)
                        self.agent.update_market_data()
                        # Run a targeted stop-only check (pass empty analysis
                        # so only price-based stops fire, not stale/TP logic)
                        partial_analysis = {}
                        for tok in at_risk:
                            existing = {}
                            atr_e = self.position_atr.get(tok, 0.05)
                            existing["atr_pct"] = atr_e
                            partial_analysis[tok] = existing
                        self._monitor_exits(partial_analysis)
                        remaining = max(0, update_interval - STOP_MIDCYCLE_SLEEP)
                        time.sleep(remaining)
                    else:
                        time.sleep(update_interval)
                else:
                    time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\n[stop] Shutting down...")
            self.running = False
            print("\n" + "=" * 70)
            print("  🏁 MEME SESSION COMPLETE")
            print("=" * 70)
            self._print_stats()
            self.agent.show_portfolio()
            print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solana Meme Coin Trader v2.7")
    parser.add_argument("--mode",             choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",           default="")
    parser.add_argument("--balance",          type=float, default=1000.0)
    parser.add_argument("--interval",         type=int,   default=15)   # 15s cycles
    parser.add_argument("--refresh-interval", type=int,   default=60)   # 60s discovery
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
            print("\n⚠  MEME COIN TRADING IS EXTREMELY HIGH RISK")
            print("   Rug pulls, dumps, and 100% losses can and do happen.")
            print("   Only trade what you can afford to lose entirely.\n")
            if input("Type 'MEME' to confirm live meme trading: ") != "MEME":
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

    trader = MemeCoinTrader(agent, auto_discover=auto_discover)
    trader.refresh_interval = args.refresh_interval

    if auto_discover:
        print("\n[discover] Initial meme token scan...")
        trader.refresh_tokens()

    print("\n[market] Warming up (60 ticks → 15 synthetic candles)...")
    for i in range(60):
        agent.update_market_data()
        if (i + 1) % 20 == 0:
            print(f"  {(i+1)//4}/{60//4} candles")
        time.sleep(1)
    print("[OK] Ready\n")

    trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()