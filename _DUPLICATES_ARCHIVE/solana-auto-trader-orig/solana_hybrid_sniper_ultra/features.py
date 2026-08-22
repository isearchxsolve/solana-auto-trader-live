"""
features.py -- Feature engineering for HWR, MG, and SNIPER modes.
All standard indicators + sniper microstructure signals.
No sub-package imports -- flat layout.
"""

import numpy as np
import pandas as pd
from typing import Dict


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> float:
    series = series.dropna()
    if len(series) < period + 1:
        # Not enough candles for a meaningful RSI -- return neutral 50
        return 50.0
    delta    = series.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    val = float(rsi.iloc[-1])
    # Guard against inf/nan that can slip through on flat price series
    if not np.isfinite(val):
        return 50.0
    return float(np.clip(val, 0.0, 100.0))


# ── Standard features ────────────────────────────────────────────────────────

def compute_standard_features(df: pd.DataFrame) -> Dict[str, float]:
    """df must have columns: open, high, low, close, volume"""
    close  = df["close"]
    volume = df["volume"]
    high   = df["high"]
    low    = df["low"]

    ema3  = _ema(close, 3)
    ema5  = _ema(close, 5)
    ema10 = _ema(close, 10)
    ema20 = _ema(close, 20)

    last_close = float(close.iloc[-1])

    def momentum(n: int) -> float:
        if len(close) <= n:
            return 0.0
        return float((close.iloc[-1] - close.iloc[-1 - n]) / (close.iloc[-1 - n] + 1e-12))

    vol_5m  = float(volume.iloc[-5:].mean())  if len(volume) >= 5  else float(volume.mean())
    vol_20m = float(volume.iloc[-20:].mean()) if len(volume) >= 20 else float(volume.mean())
    vol_ratio = vol_5m / (vol_20m + 1e-9)

    last_vol  = float(volume.iloc[-1])
    prev_avg  = float(volume.iloc[-6:-1].mean()) if len(volume) >= 6 else float(volume.mean())
    vol_spike = last_vol / (prev_avg + 1e-9)
    if not np.isfinite(vol_spike):
        vol_spike = 1.0
    if not np.isfinite(vol_ratio):
        vol_ratio = 1.0

    returns    = close.pct_change().dropna()
    volatility = float(returns.iloc[-20:].std()) if len(returns) >= 20 else float(returns.std())
    if not np.isfinite(volatility):
        volatility = 0.0

    hl           = (high - low).replace(0, np.nan)
    bp_raw       = ((close - low) / hl).iloc[-5:]
    buy_pressure = float(bp_raw.mean()) if bp_raw.notna().any() else 0.5
    if not np.isfinite(buy_pressure):
        buy_pressure = 0.5

    ema10_val = float(ema10.iloc[-1])
    ema20_val = float(ema20.iloc[-1])
    ema5_val  = float(ema5.iloc[-1])
    ema3_val  = float(ema3.iloc[-1])

    rsi_val = compute_rsi(close)

    ema10_slope = (
        float((ema10.iloc[-1] - ema10.iloc[-5]) / (ema10.iloc[-5] + 1e-12))
        if len(ema10) >= 5 else 0.0
    )

    # Normalised price ratios (scale-invariant, generalise across token price ranges)
    price_vs_ema5  = (last_close - ema5_val)  / (ema5_val  + 1e-12)
    price_vs_ema10 = (last_close - ema10_val) / (ema10_val + 1e-12)
    price_vs_ema20 = (last_close - ema20_val) / (ema20_val + 1e-12)
    ema5_vs_ema10  = (ema5_val   - ema10_val) / (ema10_val + 1e-12)
    ema10_vs_ema20 = (ema10_val  - ema20_val) / (ema20_val + 1e-12)

    return {
        # Relative ratios (scale-invariant)
        "price_vs_ema5":   float(np.clip(price_vs_ema5,  -1, 1)),
        "price_vs_ema10":  float(np.clip(price_vs_ema10, -1, 1)),
        "price_vs_ema20":  float(np.clip(price_vs_ema20, -1, 1)),
        "ema5_vs_ema10":   float(np.clip(ema5_vs_ema10,  -1, 1)),
        "ema10_vs_ema20":  float(np.clip(ema10_vs_ema20, -1, 1)),
        # Keep these -- they're already scale-invariant
        "rsi":            rsi_val,
        "momentum_1m":    momentum(1),
        "momentum_5m":    momentum(5),
        "vol_5m":         vol_5m,
        "vol_20m":        vol_20m,
        "vol_ratio":      vol_ratio,
        "vol_spike":      vol_spike,
        "volatility":     volatility,
        "buy_pressure":   buy_pressure,
        "ema10_gt_ema20": float(ema10_val > ema20_val),
        "price_gt_ema5":  float(last_close > ema5_val),
        "price_gt_ema10": float(last_close > ema10_val),
        "price_gt_ema3":  float(last_close > ema3_val),
        "ema10_slope":    ema10_slope,
        "last_close":     last_close,
    }


# ── Sniper microstructure features ──────────────────────────────────────────

def detect_micro_pullback(df: pd.DataFrame) -> float:
    if len(df) < 1:
        return 0.0
    last   = df.iloc[-1]
    body   = abs(last["close"] - last["open"])
    high_w = last["high"] - max(last["close"], last["open"])
    low_w  = min(last["close"], last["open"]) - last["low"]
    wick   = max(high_w, low_w)
    return float(wick > body)


def compute_sniper_features(
    df: pd.DataFrame,
    token_age_seconds: float,
    active_traders: int,
    liquidity_usd: float,
    lp_locked: bool,
) -> Dict[str, float]:
    standard = compute_standard_features(df)

    vols = df["volume"].values
    first_candles_rising = 0.0
    if len(vols) >= 3:
        if vols[-1] > vols[-2] > vols[-3]:
            first_candles_rising = 1.0
        elif vols[-1] > vols[-2]:
            first_candles_rising = 0.5

    returns     = df["close"].pct_change().dropna()
    launch_vol  = float(returns.iloc[:5].std()) if len(returns) >= 5 else float(returns.std() if len(returns) else 0)

    liquidity_score     = float(np.clip(liquidity_usd / 150_000, 0, 1))
    last_vol            = float(df["volume"].iloc[-1]) if len(df) > 0 else 0.0
    whale_buy_pressure  = float(np.clip(last_vol / (liquidity_usd + 1e-9), 0, 1))
    fm_trend            = standard["momentum_1m"] * standard["buy_pressure"]

    sniper = {
        "token_age_seconds":    token_age_seconds,
        "first_candles_rising": first_candles_rising,
        "active_traders":       float(active_traders),
        "lp_locked":            float(lp_locked),
        "micro_pullback":       detect_micro_pullback(df),
        "launch_volatility":    launch_vol,
        "liquidity_score":      liquidity_score,
        "whale_buy_pressure":   whale_buy_pressure,
        "fm_trend_strength":    fm_trend,
        "liquidity_usd":        liquidity_usd,
    }
    return {**standard, **sniper}


# ── Canonical ML feature column order ───────────────────────────────────────
# NOTE: Only scale-invariant features -- no absolute price values (ema3/5/10/20).
# Absolute prices don't generalise across tokens with different price magnitudes.

FEATURE_COLUMNS = [
    # Normalised price/EMA ratios (scale-invariant)
    "price_vs_ema5", "price_vs_ema10", "price_vs_ema20",
    "ema5_vs_ema10", "ema10_vs_ema20",
    # RSI, momentum, volume (already scale-invariant)
    "rsi", "momentum_1m", "momentum_5m",
    "vol_ratio", "vol_spike", "volatility",
    "buy_pressure",
    # Boolean flags
    "ema10_gt_ema20", "price_gt_ema5", "price_gt_ema10", "ema10_slope",
    # Sniper microstructure
    "token_age_seconds", "first_candles_rising",
    "active_traders", "lp_locked", "micro_pullback",
    "launch_volatility", "liquidity_score",
    "whale_buy_pressure", "fm_trend_strength",
]


def build_feature_vector(features: Dict[str, float]) -> "np.ndarray":
    return np.array([features.get(col, 0.0) for col in FEATURE_COLUMNS], dtype=np.float32)
