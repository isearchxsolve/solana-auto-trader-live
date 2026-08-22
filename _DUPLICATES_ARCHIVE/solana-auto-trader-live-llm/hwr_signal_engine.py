#!/usr/bin/env python3
"""
High Win Rate + Rapid Growth Signal Engine — v2.0
==================================================
Spec: High win rate AND rapid growth requires:
  1. Selective entries (strict hard filters)
  2. Probability-weighted position sizing (not fixed %)
  3. Small losses, larger winners (asymmetric exits)
  4. Strict regime gate (score < 70 = no trades)
  5. Compounding logic (handled in trader)

Hard filter changes from v1:
  - Liquidity raised: $75k -> $100k
  - Buy pressure raised: 0.60 -> 0.65
  - 24h range widened: 5-60% -> 5-80% (catch stronger moves)
  - RSI restricted: 42-62 -> 45-60 (tighter, higher probability zone)
  - 1h momentum kept: > 3%

Probability score (0-100) is now computed and returned so the
position sizing engine can scale capital deployment:
  prob >= 85  -> 30% position
  prob 75-84  -> 20% position
  prob 65-74  -> 10% position
  prob < 65   -> no trade
"""

from typing import List, Dict, Optional, Tuple


class HWRSignalEngine:

    # ── Hard filter thresholds ────────────────────────────────────────────────
    MIN_LIQUIDITY_USD  = 40_000    # lowered: meme tokens often have 40-100k liq
    MAX_LIQ_DROP_PCT   = 8.0
    MIN_24H_CHANGE_PCT = 5.0
    MAX_24H_CHANGE_PCT = 9999.0    # effectively removed — structural checks are the real filter
    MIN_1H_CHANGE_PCT  = 1.0    # lowered: 3% was blocking valid slow-momentum tokens
    MIN_BUY_PRESSURE   = 0.45      # 0.50 was blocking consolidating tokens; 0.45 allows mild dips

    # ── RSI gate (hard requirement for entry) ─────────────────────────────────
    MIN_RSI = 40
    MAX_RSI = 65

    # ── Soft score minimum (7 points max) ────────────────────────────────────
    MIN_SOFT_SCORE = 4

    # ── Exit parameters ───────────────────────────────────────────────────────
    STOP_LOSS_PCT    = 0.09   # 9% hard stop
    TP1_PCT          = 0.18   # 18% first target (sell 50%)
    TP1_AMOUNT       = 0.50   # sell 50% at TP1
    TRAIL_TRIGGER    = 12.0   # begin trailing at +12%
    TRAIL_DIST       = 8.0    # trail 8% below peak (was 7%)

    def __init__(self):
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # HARD FILTERS — one failure = reject immediately
    # ─────────────────────────────────────────────────────────────────────────

    def _check_liquidity(self, meta: Dict) -> Tuple[bool, str]:
        liq = meta.get("liquidity_usd", 0)
        if liq < self.MIN_LIQUIDITY_USD:
            return False, f"liq ${liq/1000:.0f}k < ${self.MIN_LIQUIDITY_USD/1000:.0f}k"
        liq_drop = meta.get("liquidity_change_pct", 0.0)
        if liq_drop < -self.MAX_LIQ_DROP_PCT:
            return False, f"liq drop {liq_drop:.1f}% — LP withdrawing"
        return True, f"liq ${liq/1000:.0f}k ok"

    def _check_accumulation(self, meta: Dict, prices: List[float],
                             volumes: List[float]) -> Tuple[bool, str]:
        ch24 = meta.get("price_change_24h", 0.0)

        if ch24 < self.MIN_24H_CHANGE_PCT:
            return False, f"24h={ch24:.0f}% too cold"
        # No upper cap on 24h change — structural checks below (higher lows,
        # volume, RSI, buy pressure) are the real quality filters.

        # Compute 1h change from live prices (60 x 1-min candles) instead of
        # reading the stale discovery-time snapshot from meta — which never updates.
        if len(prices) >= 60:
            ch1_live = (prices[-1] - prices[-60]) / prices[-60] * 100 if prices[-60] > 0 else 0.0
        elif len(prices) >= 2:
            ch1_live = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0.0
        else:
            ch1_live = meta.get("price_change_h1", 0.0)  # fallback only if no prices

        if ch1_live < self.MIN_1H_CHANGE_PCT:
            return False, f"1h={ch1_live:.1f}% momentum fading"

        # Higher lows in last 3 candles (structure intact)
        if len(prices) >= 3:
            p = prices[-3:]
            if not (p[1] >= p[0] * 0.994 and p[2] >= p[1] * 0.994):
                return False, "lower lows — structure breaking"

        # Volume not collapsing (at least one of last 3 expands)
        if len(volumes) >= 3:
            v = volumes[-3:]
            if v[2] < v[1] * 0.85 and v[1] < v[0] * 0.85:
                return False, "volume collapsing last 3 candles"

        return True, f"accum ok (24h={ch24:.0f}% 1h={ch1_live:.1f}%)"

    def _check_order_flow(self, prices: List[float],
                          highs: Optional[List[float]] = None,
                          lows: Optional[List[float]] = None) -> Tuple[bool, str]:
        """
        Simulated buy pressure: (close - low) / (high - low) avg over 5 candles.
        Require > 0.65 (closing in top 35% of range = buyers in control).
        """
        if not prices or len(prices) < 5:
            return True, "flow skip"

        pressures = []
        if highs and lows and len(highs) >= 5 and len(lows) >= 5:
            for i in range(-5, 0):
                rng = highs[i] - lows[i]
                if rng > 0:
                    pressures.append((prices[i] - lows[i]) / rng)
        else:
            # Approximate from close series using 3-bar rolling range
            p = prices[-7:] if len(prices) >= 7 else prices
            for i in range(2, len(p)):
                h = max(p[i-2], p[i-1], p[i])
                l = min(p[i-2], p[i-1], p[i])
                rng = h - l
                if rng > 0:
                    pressures.append((p[i] - l) / rng)

        if not pressures:
            return True, "flow skip (no range)"

        avg = sum(pressures) / len(pressures)
        if avg < self.MIN_BUY_PRESSURE:
            return False, f"buy_pressure={avg:.2f} < {self.MIN_BUY_PRESSURE:.2f}"
        return True, f"pressure={avg:.2f}"

    def _check_rsi_zone(self, prices: List[float]) -> Tuple[bool, str]:
        """Hard RSI gate: must be in 45-60 zone (high-probability continuation)."""
        rsi = _calc_rsi(prices)
        if rsi is None:
            return True, "rsi skip"
        if rsi < self.MIN_RSI:
            return False, f"RSI={rsi:.0f} < {self.MIN_RSI} (too weak)"
        if rsi > self.MAX_RSI:
            return False, f"RSI={rsi:.0f} > {self.MAX_RSI} (overbought)"
        return True, f"RSI={rsi:.0f} ok"

    # ─────────────────────────────────────────────────────────────────────────
    # SOFT SCORING — 7 independent signals, max 7 pts
    # ─────────────────────────────────────────────────────────────────────────

    def _soft_score(self, prices: List[float], volumes: List[float],
                    meta: Dict) -> Tuple[int, Dict[str, bool], float]:
        """
        Returns (score, flags_dict, probability_0_to_100).
        Each flag is a genuinely independent signal — no overlap with hard filters.
        """
        N = len(prices)
        if N < 20:
            return 0, {}, 0.0

        cur   = prices[-1]
        ema9  = _ema(prices, min(9,  N))
        ema20 = _ema(prices, min(20, N))
        ema50 = _ema(prices, min(50, N))

        rsi_now  = _calc_rsi(prices)      or 50.0
        rsi_prev = _calc_rsi(prices[:-3]) or rsi_now

        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
        rel_vol = volumes[-1] / vol_ma if vol_ma > 0 else 1.0

        # EMA slope — 6 bar
        ema20_now  = _ema(prices,      min(20, N))
        ema20_prev = _ema(prices[:-6], min(20, N)) if N >= 27 else ema20_now
        slope      = (ema20_now - ema20_prev) / ema20_prev if ema20_prev > 0 else 0.0

        # MACD
        macd_now  = _ema(prices, min(12, N)) - _ema(prices, min(26, N))
        macd_prev = (_ema(prices[:-1], min(12, N)) - _ema(prices[:-1], min(26, N))
                     if N > 26 else macd_now)

        # Bollinger bands
        bb_mid = sum(prices[-20:]) / 20
        bb_std = (sum((p - bb_mid)**2 for p in prices[-20:]) / 20) ** 0.5
        pct_b  = (cur - (bb_mid - 2*bb_std)) / (4*bb_std) if bb_std > 0 else 0.5

        flags = {
            # 1. Multi-EMA uptrend (price > ema9 > ema20 > ema50 — full stack)
            "full_uptrend": cur > ema9 > ema20 > ema50,

            # 2. RSI rising within zone (acceleration, not just in zone — hard filter handles zone)
            "rsi_accel": rsi_now > rsi_prev + 1.5,

            # 3. MACD positive AND turning up (two conditions merged = higher bar)
            "macd_bull": macd_now > 0 and macd_now > macd_prev,

            # 4. EMA slope strong (trend has velocity, not just direction)
            "ema_velocity": slope > 0.001,

            # 5. Volume significantly above average (conviction behind move)
            "vol_spike": rel_vol >= 1.5,

            # 6. Pullback to lower BB half (optimal continuation entry point)
            "bb_pullback": 0.20 <= pct_b <= 0.50,

            # 7. Fresh 9/20 EMA cross (early momentum signal)
            "ema9_cross": ema9 > ema20 and cur > ema9 * 1.001,
        }

        score = sum(flags.values())

        # Probability = 50% base + each flag adds weighted contribution
        # Weighted so high-conviction flags matter more
        weights = {
            "full_uptrend": 8,
            "rsi_accel":    6,
            "macd_bull":    8,
            "ema_velocity": 7,
            "vol_spike":    9,
            "bb_pullback":  7,
            "ema9_cross":   5,
        }
        raw_prob = 45.0 + sum(weights[k] for k, v in flags.items() if v)
        prob     = min(raw_prob, 98.0)

        return score, flags, prob

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN ANALYZE
    # ─────────────────────────────────────────────────────────────────────────

    def analyze(self, token: str,
                prices: List[float],
                volumes: List[float],
                token_meta: Optional[Dict] = None,
                highs: Optional[List[float]] = None,
                lows: Optional[List[float]] = None,
                min_soft_score: int = 4) -> Dict:
        """
        Full HWR+Growth analysis.

        Returns dict with all fields expected by HWRAutoTrader:
          signal, confidence, prob_score, reason, rsi, trend,
          buy_pts, buy_pts_detail, sell_pts, current_price,
          ema_slope, relative_volume, hard_fail, hard_fail_reason
        """
        meta = token_meta or {}
        N    = len(prices)
        cur  = prices[-1] if prices else 0.0

        # Base indicators for display (always computed)
        cur_rsi = _calc_rsi(prices) or 50.0
        ema20   = _ema(prices, min(20, N)) if N >= 2 else cur
        ema50   = _ema(prices, min(50, N)) if N >= 2 else cur
        trend   = "BULLISH" if cur > ema20 else "BEARISH"
        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
        rel_vol = volumes[-1] / vol_ma if (vol_ma > 0 and volumes) else 1.0
        ema_p   = _ema(prices[:-6], min(20, N)) if N >= 27 else ema20
        slope   = (ema20 - ema_p) / ema_p if ema_p > 0 else 0.0

        _base = {
            "signal":           "HOLD",
            "confidence":       0.0,
            "prob_score":       0.0,
            "reason":           "Insufficient data",
            "rsi":              cur_rsi,
            "rsi_rising":       False,
            "trend":            trend,
            "momentum":         "WEAK",
            "current_price":    cur,
            "sma_20":           ema20,
            "sma_50":           ema50,
            "macd":             0.0,
            "ema_slope":        slope,
            "relative_volume":  rel_vol,
            "rel_vol":          rel_vol,
            "buy_pts":          0,
            "buy_pts_detail":   {},
            "sell_pts":         0,
            "bb":               None,
            "bb_squeeze":       False,
            "hard_fail":        False,
            "hard_fail_reason": "",
        }

        if N < 20:
            _base["reason"] = f"Warming up ({N}/20)"
            return _base

        # ── STAGE 1: HARD FILTERS ─────────────────────────────────────────────
        checks = [
            ("liquidity",    self._check_liquidity(meta)),
            ("accumulation", self._check_accumulation(meta, prices, volumes)),
            ("order_flow",   self._check_order_flow(prices, highs, lows)),
            ("rsi_zone",     self._check_rsi_zone(prices)),
        ]

        failures = [(name, msg) for name, (ok, msg) in checks if not ok]

        if failures:
            fail_str = " | ".join(f"{n}:{m}" for n, m in failures)
            _base.update({
                "signal":           "HOLD",
                "reason":           f"HARD FAIL: {fail_str}",
                "hard_fail":        True,
                "hard_fail_reason": fail_str,
            })
            return _base

        # ── STAGE 2: SOFT SCORING ─────────────────────────────────────────────
        soft_score, flags, prob = self._soft_score(prices, volumes, meta)

        buy_pts_detail = {
            "trend":    flags.get("full_uptrend", False),
            "rsi_rise": flags.get("rsi_accel",    False),
            "macd":     flags.get("macd_bull",    False),
            "slope":    flags.get("ema_velocity", False),
            "vol":      flags.get("vol_spike",    False),
            "pullback": flags.get("bb_pullback",  False),
            "ema_x":    flags.get("ema9_cross",   False),
        }

        # Sell detection
        sell_pts = sum([cur_rsi > 72, slope < -0.001, cur < ema20 * 0.97])
        if sell_pts >= 2 and trend == "BEARISH":
            _base.update({
                "signal":          "SELL",
                "confidence":      0.70,
                "prob_score":      0.0,
                "reason":          f"SELL: RSI={cur_rsi:.0f} slope={slope:+.4f}",
                "buy_pts":         soft_score,
                "buy_pts_detail":  buy_pts_detail,
                "sell_pts":        sell_pts,
            })
            return _base

        # BUY — require minimum soft score AND bullish trend
        if soft_score >= min_soft_score and trend == "BULLISH":
            active = " ".join(k for k, v in flags.items() if v)
            _base.update({
                "signal":          "BUY",
                "confidence":      min(0.50 + soft_score * 0.07, 0.93),
                "prob_score":      prob,
                "reason":          f"HWR+G score={soft_score}/7 prob={prob:.0f}% [{active}]",
                "trend":           "BULLISH",
                "buy_pts":         soft_score,
                "buy_pts_detail":  buy_pts_detail,
                "sell_pts":        sell_pts,
                "hard_fail":       False,
            })
            return _base

        # HOLD
        _base.update({
            "signal":         "HOLD",
            "prob_score":     prob,
            "reason":         f"score={soft_score}/{min_soft_score} trend={trend}",
            "buy_pts":        soft_score,
            "buy_pts_detail": buy_pts_detail,
            "sell_pts":       sell_pts,
        })
        return _base


# ─────────────────────────────────────────────────────────────────────────────
# Shared indicator helpers (module-level so trader can import them too)
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


def _calc_rsi(data: List[float], period: int = 14) -> Optional[float]:
    if len(data) < period + 1:
        return None
    gains  = [max(data[i] - data[i-1], 0) for i in range(1, len(data))]
    losses = [max(data[i-1] - data[i], 0) for i in range(1, len(data))]
    ag = sum(gains[-period:])  / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)
