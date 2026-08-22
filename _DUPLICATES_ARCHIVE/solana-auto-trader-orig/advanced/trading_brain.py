# trading_brain.py
# PRODUCTION — Hybrid Decision Engine
# FIX: safe column access; aligned with feature_builder output

import numpy as np
import pandas as pd

import config
from logger_setup import get_logger

log = get_logger("trading_brain")


def _safe_last(df: pd.DataFrame, col: str, default=0):
    """Return the last value of a column, or default if missing."""
    return df.iloc[-1][col] if col in df.columns else default


class TradingBrain:

    def __init__(self):
        self.UP_THRESHOLD       = config.UP_THRESHOLD
        self.DOWN_THRESHOLD     = config.DOWN_THRESHOLD
        self.BREAKOUT_THRESHOLD = config.BREAKOUT_THRESHOLD

        self.BASE_POSITION_SIZE = config.BASE_POSITION_FRACTION
        self.MAX_POSITIONS      = config.MAX_POSITIONS

    # ------------------------------------------------------------------
    # VOLATILITY REGIME DETECTION
    # ------------------------------------------------------------------
    def detect_volatility_regime(self, df: pd.DataFrame) -> str:
        if df.empty:
            return "normal"
        last  = df.iloc[-1]
        vol   = last.get("volatility_10", 0)
        bb    = last.get("bb_width_ratio", 0)
        atr   = last.get("atr_ratio", 0)
        score = vol * 0.5 + bb * 0.3 + atr * 0.2

        if score > 0.025:
            return "high"
        if score < 0.012:
            return "low"
        return "normal"

    # ------------------------------------------------------------------
    # SIGNAL MODES
    # ------------------------------------------------------------------
    def _confidence_signal(self, ml: dict) -> str:
        if ml["breakout"] > self.BREAKOUT_THRESHOLD:
            return "BUY_SMALL"
        if ml["up"] > self.UP_THRESHOLD:
            return "BUY"
        if ml["down"] > self.DOWN_THRESHOLD:
            return "SELL"
        return "HOLD"

    # ------------------------------------------------------------------
    # PRIMARY DECISION ENGINE
    # ------------------------------------------------------------------
    def decide_action(self, token: str, df: pd.DataFrame, ml: dict) -> str:
        if df.empty:
            return "HOLD"

        reversal = _safe_last(df, "reversal_signal", 0)
        regime_t = _safe_last(df, "trend_regime", 0)

        # Hard sell on reversal signal
        if reversal == 1:
            log.debug("[%s] Reversal → SELL", token)
            return "SELL"

        # Down-trend filter: require high conviction to BUY
        if regime_t == -1 and ml["up"] < 0.60:
            log.debug("[%s] Down-trend filter → SELL", token)
            return "SELL"

        vol_regime = self.detect_volatility_regime(df)

        if vol_regime == "high":
            if ml["breakout"] > self.BREAKOUT_THRESHOLD:
                return "BUY_SMALL"

        return self._confidence_signal(ml)

    # ------------------------------------------------------------------
    # MULTI-TOKEN SCORING
    # ------------------------------------------------------------------
    def score_token(self, token: str, df: pd.DataFrame, ml: dict) -> float:
        if df.empty:
            return 0.0

        trend    = _safe_last(df, "trend_strength", 0)
        mom      = _safe_last(df, "momentum_7_norm", 0)
        reversal = _safe_last(df, "reversal_signal", 0)

        score = (
            ml["up"]       * 0.40
            + ml["breakout"] * 0.30
            + trend          * 0.20
            + mom            * 0.10
            - ml["down"]     * 0.30
        )

        if reversal == 1:
            score -= 0.50

        return float(score)

    # ------------------------------------------------------------------
    # DYNAMIC POSITION SIZING
    # ------------------------------------------------------------------
    def compute_position_size(self, ml: dict, df: pd.DataFrame) -> float:
        if df.empty:
            return self.BASE_POSITION_SIZE

        vol  = _safe_last(df, "volatility_10", 0.015)
        size = self.BASE_POSITION_SIZE

        # Scale down in high vol, scale up in low vol
        if vol > 0.02:
            size *= 0.7
        elif vol < 0.01:
            size *= 1.3

        # ML confidence multiplier
        size *= (0.8 + ml["up"] * 0.4)

        return float(np.clip(size, config.MIN_POSITION_FRACTION, config.MAX_POSITION_FRACTION))
