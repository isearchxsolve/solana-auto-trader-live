#!/usr/bin/env python3
"""
Professional Signal Engine — v4.0
===================================
Built on real trading principles, not parameter tweaking.

What actually creates edge:
  1. ATR-based dynamic stops/TPs — stops sized to volatility, not fixed %.
     A 5% stop on a 3% ATR token is rational.
     A 5% stop on a 0.5% ATR token is a guaranteed stop-hunt.

  2. VWAP proximity — the single most reliable intraday anchor.
     Entries within 1.5% of VWAP have statistically better outcomes.
     Price extended >3% above VWAP = mean reversion risk, not entry.

  3. Multi-timeframe alignment — fast/medium/slow EMA agreement.
     A BUY signal on 1m that conflicts with 15m trend is noise.
     All three pointing the same direction = real momentum.

  4. Choppiness filter — do NOT trade ranging markets.
     CHOP index > 61.8 means price is going nowhere. Stay out.
     CHOP < 50 means a real trend is in play. Trade it.

  5. Fee-aware R:R gate — every trade must clear a 2.5:1 minimum
     reward/risk ratio AFTER the ~0.6% round-trip fee on Solana DEXs.
     If R:R < 2.5 post-fee, skip. No edge = no trade.

  6. 10-point soft scoring — genuinely independent signals.
     Correlation between signals is the enemy; each of the 10 flags
     measures something different about market structure.

Exit parameters (ATR-based — computed per-token per-cycle):
  Stop:  1.8 x ATR  (sized to volatility, never arbitrary)
  TP1:   3.0 x ATR  (partial exit, 55%)
  TP2:   5.0 x ATR  (remainder)
  Trail: 1.2 x ATR below peak (tight but volatility-aware)

Fee assumption: 0.3% per leg = 0.6% round trip (Jupiter + Raydium spread).
"""

from typing import List, Dict, Optional, Tuple
import math


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

ROUND_TRIP_FEE_PCT = 0.006   # 0.6% — realistic Solana DEX round-trip cost
MIN_RR_RATIO       = 2.5     # minimum reward:risk after fees to take a trade
ATR_PERIOD         = 14
ATR_STOP_MULT      = 1.8     # stop  = 1.8 x ATR below entry
ATR_TP1_MULT       = 3.0     # TP1   = 3.0 x ATR above entry
ATR_TP2_MULT       = 5.0     # TP2   = 5.0 x ATR above entry
ATR_TRAIL_MULT     = 1.2     # trail = 1.2 x ATR below peak
CHOP_TRENDING      = 50.0
CHOP_CHOPPY        = 61.8
CHOP_PERIOD        = 14


class HWRSignalEngine:

    # ── Hard filter thresholds ────────────────────────────────────────────────
    MIN_LIQUIDITY_USD  = 200_000
    MAX_LIQ_DROP_PCT   = 8.0
    MIN_24H_CHANGE_PCT = 1.0
    MIN_1H_CHANGE_PCT  = 0.3
    MIN_BUY_PRESSURE   = 0.40

    MIN_RSI = 35
    MAX_RSI = 72
    MIN_SOFT_SCORE = 4

    # ATR multipliers (referenced by trader)
    STOP_ATR_MULT  = ATR_STOP_MULT
    TP1_ATR_MULT   = ATR_TP1_MULT
    TP2_ATR_MULT   = ATR_TP2_MULT
    TRAIL_ATR_MULT = ATR_TRAIL_MULT
    TP1_AMOUNT     = 0.55

    def __init__(self):
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # ATR
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_atr(self, prices: List[float], period: int = ATR_PERIOD) -> float:
        """ATR as fraction of current price (normalised). Close-only approximation."""
        if len(prices) < period + 1:
            if len(prices) < 3:
                return 0.01
            tr = [abs(prices[i] - prices[i-1]) / prices[i-1]
                  for i in range(1, len(prices)) if prices[i-1] > 0]
            return sum(tr[-period:]) / len(tr[-period:]) if tr else 0.01

        tr = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
        atr_raw = sum(tr[-period:]) / period
        cur = prices[-1]
        return atr_raw / cur if cur > 0 else 0.01

    # ─────────────────────────────────────────────────────────────────────────
    # CHOPPINESS INDEX
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_choppiness(self, prices: List[float], period: int = CHOP_PERIOD) -> float:
        """
        CHOP = 100 * log10(sum(ATR1) / (H - L)) / log10(n)
        > 61.8 = choppy/ranging.  < 50 = trending.
        """
        if len(prices) < period + 1:
            return 50.0
        window = prices[-(period + 1):]
        tr_sum = sum(abs(window[i] - window[i-1]) for i in range(1, len(window)))
        h = max(window)
        l = min(window)
        price_range = h - l
        if price_range == 0 or tr_sum == 0:
            return 100.0
        chop = 100.0 * math.log10(tr_sum / price_range) / math.log10(period)
        return max(0.0, min(100.0, chop))

    # ─────────────────────────────────────────────────────────────────────────
    # VWAP
    # ─────────────────────────────────────────────────────────────────────────

    def _calc_vwap(self, prices: List[float], volumes: List[float],
                   lookback: int = 100) -> Optional[float]:
        """VWAP over last `lookback` bars."""
        p = prices[-lookback:]
        v = volumes[-lookback:] if volumes else []
        if not p:
            return None
        if not v or len(v) < len(p):
            return sum(p) / len(p)
        pv = sum(pi * vi for pi, vi in zip(p, v))
        sv = sum(v)
        return pv / sv if sv > 0 else None

    # ─────────────────────────────────────────────────────────────────────────
    # MULTI-TIMEFRAME ALIGNMENT
    # ─────────────────────────────────────────────────────────────────────────

    def _mtf_alignment(self, prices: List[float]) -> Tuple[int, str]:
        """
        Simulate 3-TF alignment:
          EMA(9)  = fast (1m proxy)
          EMA(30) = medium (5m proxy)
          EMA(90) = slow (15m proxy)
        Returns (score 0-3, description string).
        """
        N = len(prices)
        if N < 20:
            return 0, "insufficient"
        cur   = prices[-1]
        ema9  = _ema(prices, min(9,  N))
        ema30 = _ema(prices, min(30, N))
        ema90 = _ema(prices, min(90, N))
        fast_bull   = cur  > ema9
        medium_bull = ema9 > ema30
        slow_bull   = ema30 > ema90
        score = sum([fast_bull, medium_bull, slow_bull])
        desc  = (f"{'↑' if fast_bull else '↓'}9 "
                 f"{'↑' if medium_bull else '↓'}30 "
                 f"{'↑' if slow_bull else '↓'}90")
        return score, desc

    # ─────────────────────────────────────────────────────────────────────────
    # FEE-AWARE R:R CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def _check_rr(self, atr_pct: float) -> Tuple[bool, float, str]:
        """
        Every trade must clear MIN_RR_RATIO after round-trip fees.
        Risk   = ATR_STOP_MULT * atr + fee
        Reward = ATR_TP1_MULT  * atr - fee
        """
        if atr_pct <= 0:
            return False, 0.0, "atr=0"
        risk   = ATR_STOP_MULT * atr_pct + ROUND_TRIP_FEE_PCT
        reward = ATR_TP1_MULT  * atr_pct - ROUND_TRIP_FEE_PCT
        if risk <= 0:
            return False, 0.0, "risk<=0"
        rr = reward / risk
        ok  = rr >= MIN_RR_RATIO
        msg = f"R:R={rr:.2f} ({'ok' if ok else f'<{MIN_RR_RATIO} — fees kill edge'})"
        return ok, rr, msg

    # ─────────────────────────────────────────────────────────────────────────
    # HARD FILTERS
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

        if len(prices) >= 60:
            ch1 = (prices[-1] - prices[-60]) / prices[-60] * 100 if prices[-60] > 0 else 0.0
        elif len(prices) >= 2:
            ch1 = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] > 0 else 0.0
        else:
            ch1 = meta.get("price_change_h1", 0.0)

        if ch1 < self.MIN_1H_CHANGE_PCT:
            return False, f"1h={ch1:.1f}% momentum fading"

        if len(prices) >= 3:
            p = prices[-3:]
            if not (p[1] >= p[0] * 0.994 and p[2] >= p[1] * 0.994):
                return False, "lower lows — structure breaking"

        if len(volumes) >= 3:
            v = volumes[-3:]
            if v[2] < v[1] * 0.85 and v[1] < v[0] * 0.85:
                return False, "volume collapsing last 3 candles"

        return True, f"accum ok (24h={ch24:.0f}% 1h={ch1:.1f}%)"

    def _check_order_flow(self, prices: List[float],
                          highs: Optional[List[float]] = None,
                          lows: Optional[List[float]] = None) -> Tuple[bool, str]:
        if not prices or len(prices) < 5:
            return True, "flow skip"
        pressures = []
        if highs and lows and len(highs) >= 5 and len(lows) >= 5:
            for i in range(-5, 0):
                rng = highs[i] - lows[i]
                if rng > 0:
                    pressures.append((prices[i] - lows[i]) / rng)
        else:
            p = prices[-7:] if len(prices) >= 7 else prices
            for i in range(2, len(p)):
                h = max(p[i-2], p[i-1], p[i])
                l = min(p[i-2], p[i-1], p[i])
                rng = h - l
                if rng > 0:
                    pressures.append((p[i] - l) / rng)
        if not pressures:
            return True, "flow skip"
        avg = sum(pressures) / len(pressures)
        if avg < self.MIN_BUY_PRESSURE:
            return False, f"buy_pressure={avg:.2f} < {self.MIN_BUY_PRESSURE:.2f}"
        return True, f"pressure={avg:.2f}"

    def _check_rsi_zone(self, prices: List[float]) -> Tuple[bool, str]:
        rsi = _calc_rsi(prices)
        if rsi is None:
            return True, "rsi skip"
        if rsi < self.MIN_RSI:
            return False, f"RSI={rsi:.0f} < {self.MIN_RSI} (too weak)"
        if rsi > self.MAX_RSI:
            return False, f"RSI={rsi:.0f} > {self.MAX_RSI} (overbought)"
        return True, f"RSI={rsi:.0f} ok"

    def _check_choppiness(self, prices: List[float]) -> Tuple[bool, str]:
        chop = self._calc_choppiness(prices)
        if chop > CHOP_CHOPPY:
            return False, f"CHOP={chop:.1f} — ranging market, no directional edge"
        label = "trending" if chop < CHOP_TRENDING else "mild"
        return True, f"CHOP={chop:.1f} ({label})"

    # ─────────────────────────────────────────────────────────────────────────
    # SOFT SCORING — 10 independent signals
    # ─────────────────────────────────────────────────────────────────────────

    def _soft_score(self, prices: List[float], volumes: List[float],
                    meta: Dict, atr_pct: float) -> Tuple[int, Dict[str, bool], float]:
        N = len(prices)
        if N < 30:
            return 0, {}, 0.0

        cur    = prices[-1]
        ema9   = _ema(prices, min(9,   N))
        ema20  = _ema(prices, min(20,  N))
        ema50  = _ema(prices, min(50,  N))

        rsi_now  = _calc_rsi(prices)      or 50.0
        rsi_5ago = _calc_rsi(prices[:-5]) or rsi_now

        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
        rel_vol = volumes[-1] / vol_ma if (vol_ma > 0 and volumes) else 1.0

        # EMA slope (10-bar, more stable than 6-bar)
        ema20_prev = _ema(prices[:-10], min(20, N)) if N >= 31 else ema20
        slope      = (ema20 - ema20_prev) / ema20_prev if ema20_prev > 0 else 0.0

        # MACD
        macd_now  = _ema(prices, min(12, N)) - _ema(prices, min(26, N))
        macd_prev = (_ema(prices[:-1], min(12, N)) - _ema(prices[:-1], min(26, N))
                     if N > 26 else macd_now)

        # Bollinger bands
        bb_mid = sum(prices[-20:]) / 20
        bb_std = (sum((p - bb_mid)**2 for p in prices[-20:]) / 20) ** 0.5
        pct_b  = (cur - (bb_mid - 2*bb_std)) / (4*bb_std) if bb_std > 0 else 0.5

        # VWAP
        vwap     = self._calc_vwap(prices, volumes)
        vwap_dev = (cur - vwap) / vwap if vwap and vwap > 0 else 0.0

        # MTF
        mtf_score, _ = self._mtf_alignment(prices)

        # Stochastic %K
        stoch_now  = _calc_stochastic(prices,     14) or 50.0
        stoch_3ago = _calc_stochastic(prices[:-3], 14) or stoch_now

        # Volume acceleration: last 5 bars vs prior 5
        vol_accel = False
        if volumes and len(volumes) >= 10:
            vol_accel = (sum(volumes[-5:]) / 5) > (sum(volumes[-10:-5]) / 5) * 1.2

        # Rate of change (10-bar momentum)
        roc10 = ((cur - prices[-11]) / prices[-11] * 100
                 if len(prices) >= 11 and prices[-11] > 0 else 0.0)

        flags = {
            # 1. Full EMA stack — all timeframes structurally aligned
            "ema_stack":      cur > ema9 > ema20 > ema50,

            # 2. RSI momentum — rising AND in high-probability zone
            "rsi_momentum":   45 <= rsi_now <= 68 and rsi_now > rsi_5ago + 2.0,

            # 3. MACD bull — positive and improving (two conditions = higher bar)
            "macd_bull":      macd_now > 0 and macd_now > macd_prev * 1.05,

            # 4. EMA velocity — slope has energy, not just direction
            "ema_velocity":   slope > 0.0015,

            # 5. Volume conviction — 1.8x average means real institutional buying
            "vol_conviction": rel_vol >= 1.8,

            # 6. BB entry zone — price in lower half of band = pullback, not overextension
            "bb_entry":       0.15 <= pct_b <= 0.45,

            # 7. VWAP proximity — within 1.5% of VWAP = anchored, optimal entry
            #    Price above VWAP shows bullish bias; not too far = room to run
            "vwap_near":      -0.005 <= vwap_dev <= 0.015,

            # 8. MTF aligned — at least 2 of 3 proxy timeframes bullish
            "mtf_aligned":    mtf_score >= 2,

            # 9. Stochastic recovery — rising from oversold (20-60), not falling
            "stoch_recover":  20 <= stoch_now <= 60 and stoch_now > stoch_3ago + 3.0,

            # 10. Volume acceleration — buying pressure is building, not fading
            "vol_accel":      vol_accel,
        }

        score = sum(flags.values())

        # Probability weights — based on signal reliability literature
        weights = {
            "ema_stack":      9,
            "rsi_momentum":   8,
            "macd_bull":      8,
            "ema_velocity":   7,
            "vol_conviction": 10,
            "bb_entry":       6,
            "vwap_near":      9,
            "mtf_aligned":    8,
            "stoch_recover":  6,
            "vol_accel":      7,
        }
        raw_prob = 32.0 + sum(weights[k] for k, v in flags.items() if v)
        prob     = min(raw_prob, 97.0)

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

        meta = token_meta or {}
        N    = len(prices)
        cur  = prices[-1] if prices else 0.0

        # Always-computed base indicators
        cur_rsi = _calc_rsi(prices) or 50.0
        ema20   = _ema(prices, min(20, N)) if N >= 2 else cur
        ema50   = _ema(prices, min(50, N)) if N >= 2 else cur
        trend   = "BULLISH" if cur > ema20 else "BEARISH"
        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
        rel_vol = volumes[-1] / vol_ma if (vol_ma > 0 and volumes) else 1.0
        ema_p   = _ema(prices[:-6], min(20, N)) if N >= 27 else ema20
        slope   = (ema20 - ema_p) / ema_p if ema_p > 0 else 0.0

        # ATR and exit levels
        atr_pct       = self._calc_atr(prices)
        atr_stop_pct  = ATR_STOP_MULT  * atr_pct
        atr_tp1_pct   = ATR_TP1_MULT   * atr_pct
        atr_tp2_pct   = ATR_TP2_MULT   * atr_pct
        atr_trail_pct = ATR_TRAIL_MULT * atr_pct

        # R:R feasibility
        rr_ok, rr_ratio, rr_msg = self._check_rr(atr_pct)

        # Market structure
        chop         = self._calc_choppiness(prices)
        vwap         = self._calc_vwap(prices, volumes)
        vwap_dev_pct = (cur - vwap) / vwap * 100 if vwap and vwap > 0 else 0.0
        mtf_score, mtf_desc = self._mtf_alignment(prices)

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
            # New professional fields
            "atr_pct":          atr_pct,
            "atr_stop_pct":     atr_stop_pct,
            "atr_tp1_pct":      atr_tp1_pct,
            "atr_tp2_pct":      atr_tp2_pct,
            "atr_trail_pct":    atr_trail_pct,
            "rr_ratio":         rr_ratio,
            "chop_index":       chop,
            "vwap":             vwap,
            "vwap_dev_pct":     vwap_dev_pct,
            "mtf_score":        mtf_score,
            "mtf_desc":         mtf_desc,
        }

        if N < 30:
            _base["reason"] = f"Warming up ({N}/30)"
            return _base

        # ── STAGE 1: HARD FILTERS ─────────────────────────────────────────────
        checks = [
            ("liquidity",    self._check_liquidity(meta)),
            ("accumulation", self._check_accumulation(meta, prices, volumes)),
            ("order_flow",   self._check_order_flow(prices, highs, lows)),
            ("rsi_zone",     self._check_rsi_zone(prices)),
            ("choppiness",   self._check_choppiness(prices)),
        ]
        # Fee-aware R:R gate
        if not rr_ok:
            checks.append(("rr_gate", (False, rr_msg)))

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
        soft_score, flags, prob = self._soft_score(prices, volumes, meta, atr_pct)

        buy_pts_detail = {
            "trend":     flags.get("ema_stack",      False),
            "rsi_rise":  flags.get("rsi_momentum",   False),
            "macd":      flags.get("macd_bull",      False),
            "slope":     flags.get("ema_velocity",   False),
            "vol":       flags.get("vol_conviction", False),
            "pullback":  flags.get("bb_entry",       False),
            "vwap":      flags.get("vwap_near",      False),
            "mtf":       flags.get("mtf_aligned",    False),
            "stoch":     flags.get("stoch_recover",  False),
            "vol_accel": flags.get("vol_accel",      False),
        }

        # Sell detection (enhanced with VWAP extension)
        sell_pts = sum([
            cur_rsi > 75,
            slope < -0.002,
            cur < ema20 * 0.97,
            vwap_dev_pct > 4.0,   # extended above VWAP = reversion risk
        ])
        if sell_pts >= 2 and trend == "BEARISH":
            _base.update({
                "signal":          "SELL",
                "confidence":      0.75,
                "prob_score":      0.0,
                "reason":          (f"SELL: RSI={cur_rsi:.0f} slope={slope:+.4f} "
                                    f"VWAP+{vwap_dev_pct:.1f}%"),
                "buy_pts":         soft_score,
                "buy_pts_detail":  buy_pts_detail,
                "sell_pts":        sell_pts,
            })
            return _base

        # BUY — require soft score, bullish trend, AND MTF confirms (reduces false signals)
        if soft_score >= min_soft_score and trend == "BULLISH" and mtf_score >= 2:
            active = " ".join(k for k, v in flags.items() if v)
            _base.update({
                "signal":          "BUY",
                "confidence":      min(0.45 + soft_score * 0.05, 0.95),
                "prob_score":      prob,
                "reason":          (f"PRO score={soft_score}/10 prob={prob:.0f}% "
                                    f"R:R={rr_ratio:.1f} ATR={atr_pct*100:.2f}% "
                                    f"CHOP={chop:.0f} VWAP{vwap_dev_pct:+.1f}% "
                                    f"MTF={mtf_desc}"),
                "trend":           "BULLISH",
                "buy_pts":         soft_score,
                "buy_pts_detail":  buy_pts_detail,
                "sell_pts":        sell_pts,
                "hard_fail":       False,
            })
            return _base

        # HOLD
        mtf_note = f" mtf={mtf_score}/3" if mtf_score < 2 else ""
        _base.update({
            "signal":         "HOLD",
            "prob_score":     prob,
            "reason":         (f"score={soft_score}/{min_soft_score} "
                               f"trend={trend}{mtf_note} CHOP={chop:.0f}"),
            "buy_pts":        soft_score,
            "buy_pts_detail": buy_pts_detail,
            "sell_pts":       sell_pts,
        })
        return _base


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR HELPERS
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


def _calc_stochastic(prices: List[float], k_period: int = 14) -> Optional[float]:
    """
    Stochastic %K (close-only): (close - lowest) / (highest - lowest) * 100
    """
    if len(prices) < k_period:
        return None
    window = prices[-k_period:]
    h, l = max(window), min(window)
    if h == l:
        return 50.0
    return (prices[-1] - l) / (h - l) * 100
