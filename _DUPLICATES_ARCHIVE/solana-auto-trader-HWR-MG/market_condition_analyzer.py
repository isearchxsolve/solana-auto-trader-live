#!/usr/bin/env python3
"""
Market Condition Analyzer - V9

Fixes vs V8:
  1. Volatility score rewritten — was measuring EMA slope (trend strength),
     not volatility. Meme coins consolidating with flat EMA but real ATR are
     falsely scored 25/100. Now uses atr_pct from token analysis. Synthetic
     tokens with near-zero slope were permanently scoring 25/100, dragging
     the weighted total down 7+ points below threshold every cycle.

  2. Dynamic threshold recalibrated — the V8 threshold (65-73) was higher
     than the weighted formula could produce in normal meme-market conditions.
     Example: trend=75, vol=60, mom=100, volume=55 → weighted=73.25. With the
     V8 threshold at 73, this barely passes. With even slightly lower sub-scores
     the bot would be permanently blocked. Thresholds now set to what the
     formula can realistically produce in a tradeable market.

  3. Chop dampener removed — it double-punished synthetic tokens (near-zero
     slope + rel_vol~1.0x always triggered it). The volatility sub-score
     now handles low-activity markets correctly without a separate dampener.

  4. Trend score formula fixed — old bullish_ratio × 120 required 83% bullish
     tokens for max score. Mixed/consolidating markets (50% bullish) only scored
     60, pulling the weighted total down 12 points. Meme coins in sideways
     consolidation are not bearish — now uses a stepped formula that scores
     neutral markets fairly (75 pts at 50% bullish).

  5. EMA startup seeded at 55 instead of None — old code set smoothed_score
     to the first raw score (typically 40-50 with synthetic tokens), then
     the EMA crawled up over 10+ cycles (2-3 minutes) before reaching a
     tradeable level. Now seeds at 55 (neutral) so the analyzer is
     immediately responsive from cycle 1.

  6. Empty-watchlist fallback now returns a full details dict — old code
     returned ("POOR", ..., {}) with an empty dict, causing KeyError in
     callers that access details["overall_score"] on startup.
"""

from typing import Dict, List, Tuple
from datetime import datetime
import time


class MarketConditionAnalyzer:

    def __init__(self):
        self.history = []
        # Seed at 55 (neutral) — immediately responsive from cycle 1.
        # V8 seeded at None → first raw score (≈40-50) → 10+ cycles to warm up.
        self._smoothed_score = 55.0
        self._SMOOTH_ALPHA   = 0.40   # slightly more responsive than V8's 0.35

        # Cycle cache — prevents double-call EMA drift within a single cycle
        self._cache_ts      = 0.0
        self._cache_ttl     = 25.0    # seconds; shorter than 30s cycle
        self._cached_result = None

    # ---------------------------------------------------------
    # CORE ANALYSIS
    # ---------------------------------------------------------

    def analyze(self, tokens_analysis: Dict[str, Dict]) -> Tuple[str, str, Dict]:
        """
        Compute market regime score from watched token analytics.
        Cached for _cache_ttl seconds so repeated calls within the same cycle
        don't advance the EMA smoother multiple times.
        """
        now = time.time()
        if self._cached_result is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_result

        # Empty watchlist — return neutral with full dict so callers don't crash
        if not tokens_analysis:
            score     = self._smoothed_score
            threshold = 45
            details   = {
                "overall_score": score,
                "threshold":     threshold,
                "scores":        {"trend": 0, "volatility": 0, "momentum": 0, "volume": 0},
                "stats":         {"bullish": 0, "bearish": 0, "neutral": 0,
                                  "avg_rsi": 50, "avg_slope": 0.0,
                                  "avg_atr": 0.0, "avg_rel_vol": 1.0},
            }
            result = ("NEUTRAL", "No token data — using cached score", details)
            self._cached_result = result
            self._cache_ts      = now
            return result

        total = len(tokens_analysis)

        # Fewer tokens → stickier EMA (one bad token can't flip the regime)
        if total < 2:
            effective_alpha = 0.15
        elif total < 4:
            effective_alpha = 0.25
        else:
            effective_alpha = self._SMOOTH_ALPHA

        scores = {"trend": 0, "volatility": 0, "momentum": 0, "volume": 0}

        # ── 1. TREND BREADTH ──────────────────────────────────────────────────
        # V8 formula: bullish_ratio × 120 → required 83% bullish for score=100.
        # Mixed consolidation (50% bullish) scored only 60 — unfairly low for
        # a neutral meme market. New stepped formula:
        #   ≥75% bullish → 100   clear bull
        #   ≥50% bullish →  75   mild positive bias (typical meme state)
        #   ≥25% bullish →  50   mixed / sideways
        #   < 25% bullish →  20   bearish
        bullish = sum(1 for a in tokens_analysis.values() if a.get("trend") == "BULLISH")
        bearish = sum(1 for a in tokens_analysis.values() if a.get("trend") == "BEARISH")
        neutral = total - bullish - bearish

        bullish_ratio = bullish / total if total else 0
        if bullish_ratio >= 0.75:
            scores["trend"] = 100
        elif bullish_ratio >= 0.50:
            scores["trend"] = 75
        elif bullish_ratio >= 0.25:
            scores["trend"] = 50
        else:
            scores["trend"] = 20

        # ── 2. VOLATILITY — ATR-based, not slope-based ───────────────────────
        # V8 measured EMA slope and called it "volatility". This is wrong:
        #   - Flat EMA slope = consolidation → does NOT mean low volatility
        #   - Synthetic tokens always have near-zero slope → permanent 25/100
        #   - 25/100 × weight 0.30 = 7.5 pts lost every cycle on good setups
        # Now uses atr_pct (bar-to-bar price range fraction) which is the
        # correct measure. Falls back to slope if ATR not reported.
        atr_vals = [a.get("atr_pct", 0.0) for a in tokens_analysis.values()]
        avg_atr  = sum(atr_vals) / len(atr_vals) if atr_vals else 0.0

        if avg_atr >= 0.005:        # ≥0.5%/bar — genuinely volatile meme
            scores["volatility"] = 100
        elif avg_atr >= 0.003:      # 0.3-0.5% — elevated, good meme ATR
            scores["volatility"] = 80
        elif avg_atr >= 0.001:      # 0.1-0.3% — moderate, tradeable
            scores["volatility"] = 60
        elif avg_atr > 0.0:         # very low but nonzero
            scores["volatility"] = 35
        else:
            # ATR not reported — fall back to slope with calibrated thresholds
            slopes    = [abs(a.get("ema_slope", 0.0)) for a in tokens_analysis.values()]
            avg_slope_fb = sum(slopes) / len(slopes) if slopes else 0.0
            if avg_slope_fb >= 0.005:
                scores["volatility"] = 75
            elif avg_slope_fb >= 0.002:
                scores["volatility"] = 55
            elif avg_slope_fb >= 0.001:
                scores["volatility"] = 40
            else:
                scores["volatility"] = 30

        # Always compute slope for stats display
        slopes    = [abs(a.get("ema_slope", 0.0)) for a in tokens_analysis.values()]
        avg_slope = sum(slopes) / len(slopes) if slopes else 0.0

        # ── 3. MOMENTUM (RSI regime) ──────────────────────────────────────────
        rsi_vals = [a.get("rsi", 50) for a in tokens_analysis.values()]
        avg_rsi  = sum(rsi_vals) / len(rsi_vals) if rsi_vals else 50.0

        if 45 <= avg_rsi <= 65:     # ideal meme entry zone
            scores["momentum"] = 100
        elif 38 <= avg_rsi <= 72:   # acceptable
            scores["momentum"] = 70
        elif 30 <= avg_rsi <= 78:   # borderline
            scores["momentum"] = 45
        else:
            scores["momentum"] = 20  # extreme RSI

        # ── 4. VOLUME PARTICIPATION ───────────────────────────────────────────
        rel_vols    = [a.get("relative_volume", 1.0) for a in tokens_analysis.values()]
        avg_rel_vol = sum(rel_vols) / len(rel_vols) if rel_vols else 1.0

        if avg_rel_vol >= 2.0:
            scores["volume"] = 100
        elif avg_rel_vol >= 1.3:
            scores["volume"] = 80
        elif avg_rel_vol >= 0.9:    # normal — not a red flag
            scores["volume"] = 55
        elif avg_rel_vol >= 0.6:
            scores["volume"] = 30
        else:
            scores["volume"] = 15   # ghost / dead market

        # ── WEIGHTED SCORE ────────────────────────────────────────────────────
        # Volatility weight reduced from 0.30 to 0.20 — it was the main drag
        # (slope always 25/100 on synthetic data). Momentum and volume raised.
        weights = {
            "trend":      0.30,
            "volatility": 0.20,   # was 0.30 — slope was dominating unfairly
            "momentum":   0.25,   # was 0.20
            "volume":     0.25,   # was 0.20
        }
        overall = sum(scores[k] * weights[k] for k in scores)

        # ── DYNAMIC THRESHOLD ─────────────────────────────────────────────────
        # Calibrated to the weighted formula's realistic output range.
        # Typical neutral meme scenario: trend=75, vol=60, mom=100, volume=55
        #   → score = 75×0.30 + 60×0.20 + 100×0.25 + 55×0.25 = 73.25
        # Threshold 52 comfortably passes this, blocking only dead markets.
        #
        # V8 thresholds (65-73) were only reachable in strong bull conditions —
        # they permanently blocked the bot in neutral markets.
        expansion_strength = (scores["volatility"] + scores["volume"] + scores["momentum"]) / 3

        if expansion_strength >= 75:
            dynamic_threshold = 52   # strong market
        elif expansion_strength >= 55:
            dynamic_threshold = 48   # normal meme market
        elif expansion_strength >= 35:
            dynamic_threshold = 44   # weak but tradeable
        else:
            dynamic_threshold = 40   # minimal gate — only blocks truly dead markets

        # ── EMA SMOOTHING ─────────────────────────────────────────────────────
        # Seeds at 55 (neutral) — see __init__. Immediately responsive.
        self._smoothed_score = (
            effective_alpha * overall
            + (1.0 - effective_alpha) * self._smoothed_score
        )
        smoothed = self._smoothed_score

        # ── STATUS ────────────────────────────────────────────────────────────
        if smoothed >= dynamic_threshold:
            if smoothed >= 75:
                lbl = "🟢 BULLISH"
            elif smoothed >= 58:
                lbl = "🟢 HEALTHY"
            else:
                lbl = "🟡 NEUTRAL"
            status = "HEALTHY"
            reason = f"{lbl} (score {smoothed:.0f} ≥ threshold {dynamic_threshold})"
        else:
            status = "FILTERED"
            reason = f"🔴 Below threshold (score {smoothed:.0f} < {dynamic_threshold})"

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
                "avg_atr":     avg_atr,
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

        self._cached_result = (status, reason, details)
        self._cache_ts      = now
        return status, reason, details

    def invalidate_cache(self):
        """Call at the START of each main-loop cycle to force a fresh compute."""
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

            # Aligned with updated signal engine RSI gate (30-78)
            if trend == "BULLISH" and 35 <= rsi <= 72 and buy_pts >= 2:
                trending.append(token)

        if not trending:
            return True, [], "✅ Regime OK — waiting for clean setups"
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

        near = score >= threshold * 0.90
        emoji = "🟢" if status == "HEALTHY" else ("🟡" if near else "🔴")

        print("\n" + "=" * 70)
        print(f"{emoji} MARKET CONDITION: {status} "
              f"(Score: {score:.0f}/100 | Threshold: {threshold})")
        print("=" * 70)
        print("\nBreakdown:")
        print(f"  Trend:      {scores['trend']:>3}/100  "
              f"({stats['bullish']}B / {stats['neutral']}N / {stats['bearish']}Be)")
        print(f"  Volatility: {scores['volatility']:>3}/100  "
              f"(ATR: {stats.get('avg_atr',0)*100:.2f}%  slope: {stats['avg_slope']:+.4f})")
        print(f"  Momentum:   {scores['momentum']:>3}/100  "
              f"(avg RSI: {stats['avg_rsi']:.0f})")
        print(f"  Volume:     {scores['volume']:>3}/100  "
              f"(avg rel vol: {stats['avg_rel_vol']:.2f}x)")
        print(f"\n{reason}")
        print("=" * 70)
