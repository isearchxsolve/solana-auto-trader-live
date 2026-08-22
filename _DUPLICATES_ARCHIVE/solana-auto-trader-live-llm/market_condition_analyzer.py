#!/usr/bin/env python3
"""
Market Condition Analyzer - V8 Fixed Edition

Fixes vs V7:
  1. analyze() is idempotent within the same cycle — score is computed once
     and cached for that cycle so double-calls don't drift the EMA.
  2. Dynamic threshold lowered to be consistent with the regime labels used
     in adjust_entry_rules() (MODERATE >= 65 -> threshold <= 65).
  3. Sniper RSI window widened to 42-62 to match the trade-gate RSI window
     (40-60) and avoid a silent double-gating conflict.
  4. buy_pts threshold in sniper filter lowered to 3 (from 4) because the
     trade-gate in solana_auto_trader_trending.py already enforces >=4/5/6.
  5. Soft-chop dampener made less aggressive (0.85x instead of 0.75x) to
     prevent the score from collapsing in mild consolidation.
"""

from typing import Dict, List, Tuple
from datetime import datetime
import time


class MarketConditionAnalyzer:

    def __init__(self):
        self.history = []
        self._smoothed_score = None
        self._SMOOTH_ALPHA   = 0.35   # lower = more stable

        # FIX #1: cycle cache -- prevents double-call EMA drift
        self._cache_ts      = 0.0     # timestamp of last full compute
        self._cache_ttl     = 25.0    # seconds (shorter than the 30s cycle)
        self._cached_result = None    # (status, reason, details)

    # ---------------------------------------------------------
    # CORE ANALYSIS
    # ---------------------------------------------------------
    def analyze(self, tokens_analysis: Dict[str, Dict]) -> Tuple[str, str, Dict]:
        """
        Compute market regime.  Results are cached for _cache_ttl seconds so
        that a second call within the same 30-second cycle returns the SAME
        score without advancing the EMA smoother again.
        """
        # Return cached result if still fresh
        now = time.time()
        if self._cached_result is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_result

        if not tokens_analysis:
            result = ("POOR", "No token data available", {})
            self._cached_result = result
            self._cache_ts      = now
            return result

        total = len(tokens_analysis)

        scores = {
            "trend":      0,
            "volatility": 0,
            "momentum":   0,
            "volume":     0
        }

        # 1. TREND BREADTH
        bullish = sum(1 for a in tokens_analysis.values()
                      if a.get("trend") == "BULLISH")
        bearish = sum(1 for a in tokens_analysis.values()
                      if a.get("trend") == "BEARISH")
        neutral = total - bullish - bearish

        bullish_ratio   = bullish / total if total else 0
        scores["trend"] = min(100, int(bullish_ratio * 120))

        # 2. VOLATILITY (EMA slope strength)
        slopes    = [abs(a.get("ema_slope", 0)) for a in tokens_analysis.values()]
        avg_slope = sum(slopes) / len(slopes) if slopes else 0

        if avg_slope < 0.0015:
            scores["volatility"] = 25
        elif avg_slope < 0.003:
            scores["volatility"] = 55
        elif avg_slope < 0.01:
            scores["volatility"] = 85
        else:
            scores["volatility"] = 100

        # 3. MOMENTUM (RSI regime) -- widened slightly for better sensitivity
        rsi_vals = [a.get("rsi", 50) for a in tokens_analysis.values()]
        avg_rsi  = sum(rsi_vals) / len(rsi_vals) if rsi_vals else 50

        if 48 <= avg_rsi <= 65:
            scores["momentum"] = 100
        elif 42 <= avg_rsi <= 72:
            scores["momentum"] = 70
        else:
            scores["momentum"] = 30

        # 4. VOLUME PARTICIPATION
        rel_vols    = [a.get("relative_volume", 1.0) for a in tokens_analysis.values()]
        avg_rel_vol = sum(rel_vols) / len(rel_vols) if rel_vols else 1.0

        if avg_rel_vol < 0.9:
            scores["volume"] = 20
        elif avg_rel_vol < 1.2:
            scores["volume"] = 45
        elif avg_rel_vol < 2.0:
            scores["volume"] = 80
        else:
            scores["volume"] = 100

        # WEIGHTED SCORE
        weights = {
            "trend":      0.30,
            "volatility": 0.30,
            "momentum":   0.20,
            "volume":     0.20
        }
        overall = sum(scores[k] * weights[k] for k in scores)

        # DYNAMIC THRESHOLD SYSTEM
        # HWR+Growth v2: thresholds raised to align with the strict 70 regime
        # gate in the trader.  "HEALTHY" now only fires when the score is high
        # enough that the trader would actually open new positions.
        #
        #   expansion_strength > 85  -> threshold 65  (strong, still selective)
        #   expansion_strength > 70  -> threshold 68  (moderate, near trader gate)
        #   expansion_strength > 55  -> threshold 70  (borderline, at gate floor)
        #   else                     -> threshold 73  (weak market, protective)
        expansion_strength = (
            scores["volatility"] +
            scores["volume"]     +
            scores["momentum"]
        ) / 3

        if expansion_strength > 85:
            dynamic_threshold = 65
        elif expansion_strength > 70:
            dynamic_threshold = 68
        elif expansion_strength > 55:
            dynamic_threshold = 70
        else:
            dynamic_threshold = 73

        # FIX #3: Softer chop dampener (was 0.75x which crushed scores too hard)
        if avg_rel_vol < 0.95 and avg_slope < 0.002:
            overall *= 0.85   # mild dampening, not a collapse

        # Exponential smoothing (prevents rapid flip-flopping between cycles)
        if self._smoothed_score is None:
            self._smoothed_score = overall
        else:
            self._smoothed_score = (
                self._SMOOTH_ALPHA * overall
                + (1 - self._SMOOTH_ALPHA) * self._smoothed_score
            )
        smoothed = self._smoothed_score

        # STATUS
        if smoothed >= dynamic_threshold:
            status = "HEALTHY"
            reason = (
                f"Strong regime "
                f"(score {smoothed:.0f}/100 | threshold {dynamic_threshold})"
            )
        else:
            status = "FILTERED"
            reason = (
                f"Below regime threshold "
                f"(score {smoothed:.0f} < {dynamic_threshold})"
            )

        details = {
            "overall_score": smoothed,
            "threshold":     dynamic_threshold,
            "scores":        scores,
            "stats": {
                "bullish":     bullish,
                "bearish":     bearish,
                "neutral":     neutral,
                "avg_rsi":     avg_rsi,
                "avg_slope":   avg_slope,
                "avg_rel_vol": avg_rel_vol,
            }
        }

        self.history.append({
            "timestamp": datetime.now(),
            "status":    status,
            "score":     smoothed,
        })
        if len(self.history) > 100:
            self.history.pop(0)

        # Cache the result so a second call this cycle returns the same score
        self._cached_result = (status, reason, details)
        self._cache_ts      = now

        return status, reason, details

    def invalidate_cache(self):
        """Call this at the START of each main-loop cycle to force a fresh compute."""
        self._cached_result = None
        self._cache_ts      = 0.0

    # ---------------------------------------------------------
    # STRICT TRADE GATE
    # ---------------------------------------------------------
    def should_trade_with_trending(
        self,
        tokens_analysis: Dict
    ) -> Tuple[bool, List[str], str]:

        status, reason, details = self.analyze(tokens_analysis)

        score     = details["overall_score"]
        threshold = details["threshold"]

        if score < threshold:
            return False, [], f"⏸️ {reason}"

        trending = []

        for token, analysis in tokens_analysis.items():
            trend   = analysis.get("trend",   "NEUTRAL")
            rsi     = analysis.get("rsi",     50)
            buy_pts = analysis.get("buy_pts", 0)

            # HWR+Growth: RSI gate tightened to 45-62 to match signal engine hard filter.
            # buy_pts >= 3 here; trader gate enforces the higher threshold.
            if (
                trend == "BULLISH"
                and 45 <= rsi <= 62
                and buy_pts >= 3
            ):
                trending.append(token)

        if not trending:
            return True, [], "✅ Regime strong — waiting clean setups"

        return True, trending, f"🚀 {len(trending)} high-probability tokens"

    # ---------------------------------------------------------
    # PRINT STATUS
    # ---------------------------------------------------------
    def print_status(self, tokens_analysis: Dict[str, Dict]):

        status, reason, details = self.analyze(tokens_analysis)

        score     = details["overall_score"]
        threshold = details["threshold"]
        scores    = details["scores"]
        stats     = details["stats"]

        emoji = "🟢" if status == "HEALTHY" else "🔴"

        print("\n" + "=" * 70)
        print(f"{emoji} MARKET CONDITION: {status} "
              f"(Score: {score:.0f}/100 | Threshold: {threshold})")
        print("=" * 70)

        print("\nBreakdown:")
        print(f"  Trend:      {scores['trend']:>3}/100 "
              f"({stats['bullish']} bullish)")
        print(f"  Volatility: {scores['volatility']:>3}/100 "
              f"(avg slope: {stats['avg_slope']:+.4f})")
        print(f"  Momentum:   {scores['momentum']:>3}/100 "
              f"(avg RSI: {stats['avg_rsi']:.0f})")
        print(f"  Volume:     {scores['volume']:>3}/100 "
              f"(avg rel vol: {stats['avg_rel_vol']:.2f}x)")

        print(f"\n{reason}")
        print("=" * 70)
