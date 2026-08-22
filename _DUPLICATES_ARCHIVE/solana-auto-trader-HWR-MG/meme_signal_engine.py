#!/usr/bin/env python3
"""
Meme Coin Signal Engine — v2.2 (Integrated Physics Edition)

Goals:
- Preserve full external interface
- Unified internal risk math
- Survivors stop logic
- True symmetric fee modeling
- Ignition with volatility expansion
- Anti-top and anti-distribution safeguards
- Full 8-point weighted soft scoring system
- Regime-aware mania scaling
"""

from typing import List, Dict, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS (REAL-MONEY TUNED)
# ─────────────────────────────────────────────────────────────────────────────

MEME_FEE_PCT        = 0.030
MEME_ATR_PERIOD     = 10

# Survivors stop logic
MEME_STOP_MULT      = 5.0
MEME_MAX_STOP_PCT   = 0.12
MEME_MIN_STOP_FLOOR        = 0.015
MEME_MIN_STOP_FLOOR_STRONG = 0.025

# Targets
MEME_TP1_MULT       = 6.0
MEME_TP2_MULT       = 12.0
MEME_TRAIL_MULT     = 3.5
MEME_TP1_MIN_PCT    = 0.06

# Safety bounds
MEME_MIN_LIQ        = 50_000
MEME_MAX_LIQ        = 10_000_000
MEME_MIN_AGE_HOURS  = 2.0
MEME_MAX_MCAP_USD   = 50_000_000

# Volatility + RR
MEME_MIN_ATR_PCT    = 0.002
MEME_MIN_RR         = 0.50

# Volume states
MEME_VOL_EXPLOSION  = 3.0
MEME_VOL_SPIKE      = 1.6

# RSI bounds
MEME_MIN_RSI        = 35
MEME_MAX_RSI_ENTRY  = 75

# Anti-dump
MEME_DUMP_DROP_PCT  = 0.08


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MemeSignalEngine:

    STOP_ATR_MULT  = MEME_STOP_MULT
    TP1_ATR_MULT   = MEME_TP1_MULT
    TP2_ATR_MULT   = MEME_TP2_MULT
    TRAIL_ATR_MULT = MEME_TRAIL_MULT
    TP1_AMOUNT     = 0.60
    MAX_STOP_PCT   = MEME_MAX_STOP_PCT
    DUMP_DROP_PCT  = MEME_DUMP_DROP_PCT

    MIN_SOFT_SCORE = 2

    def __init__(self):
        pass
    # ─────────────────────────────────────────────────────────────────────────
    # CORE METRICS
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_atr(self, prices: List[float]) -> float:
        """
        ATR expressed as fraction of current price.
        Short period to react to meme volatility.
        """
        if len(prices) < 3:
            return 0.05

        tr = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        period = min(MEME_ATR_PERIOD, len(tr))
        atr_raw = sum(tr[-period:]) / period

        cur = prices[-1]
        return atr_raw / cur if cur > 0 else 0.05


    def _ema(self, data: List[float], period: int) -> float:
        if not data:
            return 0.0
        if len(data) < period:
            return sum(data) / len(data)

        k = 2.0 / (period + 1)
        e = sum(data[:period]) / period
        for v in data[period:]:
            e = v * k + e * (1 - k)
        return e


    def _calc_rsi(self, data: List[float], period: int = 21) -> Optional[float]:
        """
        Wilder-smoothed RSI for meme noise reduction.
        Uses Wilder's EMA (alpha=1/period) instead of simple average of last N bars,
        which prevents single synthetic candles from spiking RSI on flat price series.
        """
        if len(data) < period + 1:
            return None

        deltas = [data[i] - data[i - 1] for i in range(1, len(data))]
        gains  = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]

        # Seed with simple average over first period
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Wilder smoothing over remaining bars
        alpha = 1.0 / period
        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = avg_gain * (1 - alpha) + g * alpha
            avg_loss = avg_loss * (1 - alpha) + l * alpha

        if avg_loss == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


    def _price_velocity(self, prices: List[float], window: int = 5) -> float:
        """
        Percent price change over `window` candles.
        """
        if len(prices) < window + 1:
            return 0.0

        prev = prices[-(window + 1)]
        if prev <= 0:
            return 0.0

        return (prices[-1] - prev) / prev * 100.0


    def _volume_state(self, volumes: List[float]) -> Tuple[float, str]:
        """
        Relative volume classification.
        """
        if not volumes or len(volumes) < 5:
            return 1.0, "insufficient"

        ma = sum(volumes[-20:]) / min(20, len(volumes))
        if ma == 0:
            return 1.0, "zero_vol"

        rv = volumes[-1] / ma

        if rv >= MEME_VOL_EXPLOSION:
            label = f"🚀 EXPLOSION {rv:.1f}x"
        elif rv >= MEME_VOL_SPIKE:
            label = f"⚡ SPIKE {rv:.1f}x"
        elif rv >= 1.5:
            label = f"📈 ELEVATED {rv:.1f}x"
        else:
            label = f"😴 LOW {rv:.1f}x"

        return rv, label
    # ─────────────────────────────────────────────────────────────────────────
    # MEME PHYSICS (ANTI-TOP / ANTI-DISTRIBUTION)
    # ─────────────────────────────────────────────────────────────────────────

    def _is_parabolic(self, prices: List[float], lookback: int = 60) -> Tuple[bool, float]:
        """
        Detect blow-off tops.
        Flags if price > 5x from recent low.
        """
        if len(prices) < 5:
            return False, 1.0

        window = prices[-lookback:] if len(prices) >= lookback else prices
        low = min(window)

        if low <= 0:
            return False, 1.0

        multiple = prices[-1] / low
        return multiple > 5.0, multiple


    def _sell_divergence(self, prices: List[float], volumes: List[float]) -> bool:
        """
        Detect distribution:
        - Price rising
        - Volume fading sharply
        - Weak closing pressure
        """
        if len(prices) < 10 or len(volumes) < 10:
            return False

        price_up = prices[-1] > prices[-10]

        recent_vol = sum(volumes[-5:]) / 5
        prior_vol  = sum(volumes[-10:-5]) / 5

        vol_fading = recent_vol < prior_vol * 0.55

        # Micro pressure filter
        recent_prices = prices[-5:]
        high = max(recent_prices)
        low  = min(recent_prices)

        if high - low == 0:
            return False

        pressure = (recent_prices[-1] - low) / (high - low)
        pressure_weak = pressure < 0.35

        return price_up and vol_fading and pressure_weak


    def _bearish_wick_rejection(self, prices: List[float]) -> bool:
        """
        Detect breakout rejection:
        New local high but closes weak.
        """
        if len(prices) < 6:
            return False

        recent = prices[-3:]
        prior  = prices[-6:-3]

        if max(recent) <= max(prior):
            return False

        recent_high = max(recent)
        recent_low  = min(recent)
        midpoint = (recent_high + recent_low) / 2

        return prices[-1] < midpoint
    # ─────────────────────────────────────────────────────────────────────────
    # UNIFIED RISK MODEL (SINGLE SOURCE OF TRUTH)
    # ─────────────────────────────────────────────────────────────────────────

    def _risk_model(self, atr_pct: float, regime: float) -> Dict[str, float]:
        """
        Calculates:
        - Stop %
        - TP1 %
        - TP2 %
        - Trailing %
        - Risk/Reward (fee adjusted)
        """

        # Regime-aware stop floor
        stop_floor = (
            MEME_MIN_STOP_FLOOR_STRONG
            if regime >= 80
            else MEME_MIN_STOP_FLOOR
        )

        # Survivors stop logic
        raw_stop = MEME_STOP_MULT * atr_pct
        stop_pct = max(min(raw_stop, MEME_MAX_STOP_PCT), stop_floor)

        # Target logic
        tp1_pct = max(MEME_TP1_MULT * atr_pct, MEME_TP1_MIN_PCT)
        tp2_pct = MEME_TP2_MULT * atr_pct
        trail_pct = MEME_TRAIL_MULT * atr_pct

        # True symmetric fee modeling
        entry_fee = MEME_FEE_PCT / 2
        exit_fee  = MEME_FEE_PCT / 2

        risk = stop_pct + exit_fee
        reward = tp1_pct - entry_fee

        rr = reward / risk if risk > 0 else 0.0

        return {
            "stop_pct": stop_pct,
            "tp1_pct": tp1_pct,
            "tp2_pct": tp2_pct,
            "trail_pct": trail_pct,
            "rr": rr
        }
    # ─────────────────────────────────────────────────────────────────────────
    # HARD FILTERS (STRUCTURAL PROTECTION)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_liquidity(self, meta: Dict) -> Tuple[bool, str]:
        liq = meta.get("liquidity_usd", 0)

        if liq < MEME_MIN_LIQ:
            return False, f"rug_risk: thin_liq ${liq:,.0f}"

        if liq > MEME_MAX_LIQ:
            return False, f"overpriced_liq ${liq:,.0f}"

        return True, "liq_ok"


    def _check_age(self, meta: Dict) -> Tuple[bool, str]:
        age = meta.get("age_hours")

        if age is None:
            return True, "age_unknown"

        if age < MEME_MIN_AGE_HOURS:
            return False, f"too_new {age:.1f}h"

        return True, "age_ok"


    def _check_market_cap(self, meta: Dict) -> Tuple[bool, str]:
        mcap = meta.get("market_cap_usd")

        if mcap and mcap > MEME_MAX_MCAP_USD:
            return False, f"too_large ${mcap:,.0f}"

        return True, "mcap_ok"


    def _check_rsi_entry(self, prices: List[float], regime: float) -> Tuple[bool, str]:
        rsi = self._calc_rsi(prices)

        if rsi is None:
            return True, "rsi_skip"

        max_rsi = 85 if regime >= 80 else MEME_MAX_RSI_ENTRY

        if rsi < MEME_MIN_RSI:
            return False, f"rsi_weak {rsi:.0f}"

        if rsi > max_rsi:
            return True, f"rsi_soft_overextended {rsi:.0f}"

        return True, "rsi_ok"


    def _check_atr_floor(self, atr_pct: float) -> Tuple[bool, str]:
        if atr_pct < MEME_MIN_ATR_PCT * 0.25:
            return False, f"atr_extremely_low {atr_pct*100:.2f}%"
        if atr_pct < MEME_MIN_ATR_PCT:
            return True, f"atr_soft_low {atr_pct*100:.2f}%"
        return True, "atr_ok"


    def _check_rr_gate(self, rr: float) -> Tuple[bool, str]:
        if rr < MEME_MIN_RR:
            return False, f"bad_rr {rr:.2f} < {MEME_MIN_RR}"

        return True, "rr_ok"
    # ─────────────────────────────────────────────────────────────────────────
    # IGNITION MODE (REGIME-AWARE ACCELERATION)
    # ─────────────────────────────────────────────────────────────────────────

    def _check_ignition(
        self,
        prices: List[float],
        volumes: List[float],
        atr_pct: float,
        regime: float,
        is_para: bool,
        sell_div: bool
    ) -> bool:
        """
        Acceleration trigger:
        - Multi-window velocity
        - Volume confirmation
        - ATR expansion
        - Anti-top + Anti-distribution guard
        """

        if len(prices) < 15:
            return False

        vel5 = self._price_velocity(prices, 5)
        vel10 = self._price_velocity(prices, 10)

        rv, _ = self._volume_state(volumes)

        # ATR expansion check (stabilized)
        atr_prev = self._calc_atr(prices[:-8]) if len(prices) > 15 else atr_pct
        atr_expanding = atr_pct > atr_prev * 1.18

        # Regime-aware ignition thresholds
        vel5_req  = 1.5 if regime >= 85 else 2.0
        vel10_req = 2.5 if regime >= 85 else 3.5
        rv_req    = 1.3 if regime >= 85 else 1.6

        ignition_conditions = (
            vel5 > vel5_req and
            vel10 > vel10_req and
            rv >= rv_req and
            atr_expanding
        )

        # Never buy tops or distribution
        if ignition_conditions and not is_para and not sell_div:
            return True

        return False
    # ─────────────────────────────────────────────────────────────────────────
    # SOFT SCORING (FULL 8-FACTOR MOMENTUM SYSTEM)
    # ─────────────────────────────────────────────────────────────────────────

    def _soft_score(
        self,
        prices: List[float],
        volumes: List[float],
        atr_pct: float,
        regime: float
    ) -> Tuple[int, Dict[str, bool], float]:

        N = len(prices)
        if N < 20:
            return 0, {}, 0.0

        cur = prices[-1]

        ema9  = self._ema(prices, 9)
        ema20 = self._ema(prices, 20)

        rsi = self._calc_rsi(prices) or 50.0

        rv, _ = self._volume_state(volumes)

        vel5  = self._price_velocity(prices, 5)
        vel15 = self._price_velocity(prices, 15)

        # MACD approximation
        macd_now  = self._ema(prices, 12) - self._ema(prices, 26)
        macd_prev = (
            self._ema(prices[:-1], 12) - self._ema(prices[:-1], 26)
            if N > 26 else macd_now
        )

        # ATR expansion check
        atr_prev = self._calc_atr(prices[:-8]) if len(prices) > 15 else atr_pct
        atr_expanding = atr_pct > atr_prev * 1.18

        # Parabolic multiple for room-to-run
        low = min(prices[-60:]) if len(prices) >= 60 else min(prices)
        multiple = cur / low if low > 0 else 1.0

        # EMA slope
        ema9_prev = self._ema(prices[:-5], 9) if len(prices) > 10 else ema9
        ema_slope = (ema9 - ema9_prev) / ema9_prev if ema9_prev > 0 else 0.0

        flags = {
            "vol_explosion": rv >= MEME_VOL_EXPLOSION,
            "vol_spike": MEME_VOL_SPIKE <= rv < MEME_VOL_EXPLOSION,
            "price_velocity": vel5 > 1.5 and vel15 > 0.5,
            "rsi_sweet": 38 <= rsi <= 70,
            "macd_cross": macd_now > macd_prev and macd_now > -0.0001,
            "room_to_run": multiple < 2.5,
            "ema_velocity": ema_slope > 0.003 and cur > ema9,
            "atr_expanding": atr_expanding
        }

        soft_score = sum(flags.values())

        weights = {
            "vol_explosion": 20,
            "vol_spike": 12,
            "price_velocity": 15,
            "rsi_sweet": 9,
            "macd_cross": 8,
            "room_to_run": 7,
            "ema_velocity": 10,
            "atr_expanding": 10
        }

        raw_prob = 35.0 + sum(weights[k] for k, v in flags.items() if v)
        prob = min(raw_prob, 96.0)

        return soft_score, flags, prob
    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ANALYZE (FULL INTEGRATION)
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(
        self,
        token: str,
        prices: List[float],
        volumes: List[float],
        token_meta: Optional[Dict] = None,
        min_soft_score: int = 3
    ) -> Dict:

        meta = token_meta or {}
        regime = meta.get("regime_score", 50)

        N = len(prices)
        cur = prices[-1] if prices else 0.0

        atr_pct = self._calc_atr(prices)
        risk = self._risk_model(atr_pct, regime)

        # True ATR expansion: compare current ATR to ATR 8 candles ago.
        # Used by TokenData in solana_meme_trader.py — exposed in the return dict
        # so downstream consumers don't need to proxy it via ema_slope.
        atr_prev = self._calc_atr(prices[:-8]) if len(prices) > 15 else atr_pct
        atr_expanding = atr_pct > atr_prev * 1.18

        rv, vol_label = self._volume_state(volumes)

        # Meme physics
        is_para, multiple = self._is_parabolic(prices)
        sell_div = self._sell_divergence(prices, volumes)
        cur_rsi = self._calc_rsi(prices) or 50.0

        vel5 = self._price_velocity(prices, 5)

        # Base return structure (preserved)
        _base = {
            "signal": "HOLD",
            "confidence": 0.0,
            "prob_score": 0.0,
            "reason": "initializing",
            "rsi": cur_rsi,
            "current_price": cur,
            "atr_pct": atr_pct,
            "atr_stop_pct": risk["stop_pct"],
            "atr_tp1_pct": risk["tp1_pct"],
            "atr_tp2_pct": risk["tp2_pct"],
            "atr_trail_pct": risk["trail_pct"],
            "rr_ratio": risk["rr"],
            "relative_volume": rv,
            "volume_state": vol_label,
            "velocity_5": vel5,
            "multiple_from_low": multiple,
            "is_parabolic": is_para,
            "sell_divergence": sell_div,
            "buy_pts": 0,
            "buy_pts_detail": {},
            "sell_pts": 0,
            "hard_fail": False,
            "hard_fail_reason": "",
            # True ATR-based expansion flag (atr_now > atr_8_bars_ago * 1.18).
            # Exposed here so TokenData doesn't need the ema_slope proxy.
            "atr_expanding": atr_expanding,
        }

        if N < 20:
            _base["reason"] = f"Warming up ({N}/20)"
            return _base

        # ─────────────────────────────────────────────────────────────────────
        # HARD FILTERS
        # ─────────────────────────────────────────────────────────────────────

        for check in (
            self._check_liquidity(meta),
            self._check_age(meta),
            self._check_market_cap(meta),
            self._check_rsi_entry(prices, regime),
            self._check_atr_floor(atr_pct),
            self._check_rr_gate(risk["rr"]),
        ):
            if not check[0]:
                _base["hard_fail"] = True
                _base["hard_fail_reason"] = check[1]
                _base["reason"] = f"HARD FAIL: {check[1]}"
                return _base

        # Anti-top + anti-dump enforcement
        if is_para and regime < 85:
            _base["hard_fail"] = True
            _base["reason"] = "HARD FAIL: peak_risk_parabolic"
            return _base

        if sell_div:
            _base["hard_fail"] = True
            _base["reason"] = "HARD FAIL: distribution_detected"
            return _base

        # ─────────────────────────────────────────────────────────────────────
        # IGNITION MODE
        # ─────────────────────────────────────────────────────────────────────

        if self._check_ignition(prices, volumes, atr_pct, regime, is_para, sell_div):
            return {
                **_base,
                "signal": "BUY",
                "confidence": 0.85,
                "prob_score": 92.0,
                "reason": "IGNITION MODE — mania acceleration",
                "buy_pts": 8,
                "buy_pts_detail": {"ignition": True}
            }

        # ─────────────────────────────────────────────────────────────────────
        # SOFT SCORING
        # ─────────────────────────────────────────────────────────────────────

        soft_score, flags, prob = self._soft_score(prices, volumes, atr_pct, regime)

        # Always populate scoring fields for observability — even when we don't BUY.
        # Previously p0% / 0/8 appeared whenever cur<=EMA20, hiding real signal strength.
        _base["prob_score"]     = prob
        _base["buy_pts"]        = soft_score
        _base["buy_pts_detail"] = flags

        required = (
            1 if regime >= 85
            else 2 if regime >= 80
            else min_soft_score
        )

        ema20 = self._ema(prices, 20)
        price_above_ema = cur > ema20

        vel10=self._price_velocity(prices,10)
        if not (vel5>=2 and vel10>=1):
            return _base
        vel10=self._price_velocity(prices,10)
        if not (vel5>=2 and vel10>=1):
            return _base
        if soft_score >= required and price_above_ema:
            base_conf    = 0.40 + soft_score * 0.08
            regime_boost = 0.05 if regime >= 85 else (0.03 if regime >= 80 else 0.0)
            return {
                **_base,
                "signal":     "BUY",
                "confidence": min(base_conf + regime_boost, 0.95),
                "reason":     f"Soft-Entry score={soft_score}/8",
            }

        # HOLD — surface the specific blocking condition
        if soft_score < required:
            _base["reason"] = f"score {soft_score}/{required} insufficient"
        elif not price_above_ema:
            _base["reason"] = f"below EMA20 ({cur:.6f}<{ema20:.6f})"
        return _base
# ─────────────────────────────────────────────────────────────────────────────
# OPTIONAL GLOBAL HELPERS (SAFE FALLBACKS)
# ─────────────────────────────────────────────────────────────────────────────

def _ema(data: List[float], period: int) -> float:
    if not data:
        return 0.0
    if len(data) < period:
        return sum(data) / len(data)

    k = 2.0 / (period + 1)
    e = sum(data[:period]) / period
    for v in data[period:]:
        e = v * k + e * (1 - k)
    return e


def _calc_rsi(data: List[float], period: int = 21) -> Optional[float]:
    if len(data) < period + 1:
        return None

    gains = [max(data[i] - data[i-1], 0) for i in range(1, len(data))]
    losses = [max(data[i-1] - data[i], 0) for i in range(1, len(data))]

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
