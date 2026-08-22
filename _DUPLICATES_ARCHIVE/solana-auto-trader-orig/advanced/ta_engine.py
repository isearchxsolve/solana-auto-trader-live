# ta_engine.py
# PRODUCTION — Technical Analysis Engine (30+ indicators)

import numpy as np
import pandas as pd

from logger_setup import get_logger

log = get_logger("ta_engine")


def _safe_roll(series: pd.Series, window: int, func: str = "mean") -> pd.Series:
    """Rolling helper that handles series shorter than the window."""
    if len(series) < window:
        return pd.Series(np.nan, index=series.index)
    funcs = {
        "mean": series.rolling(window).mean,
        "std":  series.rolling(window).std,
        "min":  series.rolling(window).min,
        "max":  series.rolling(window).max,
        "sum":  series.rolling(window).sum,
    }
    return funcs[func]()


def compute_ta(df: pd.DataFrame) -> pd.DataFrame:
    """
    Receives a clean OHLCV DataFrame.
    Returns the same DataFrame with 30+ TA columns appended.
    All NaN values are forward-filled then zero-filled at the end.
    """
    if df.empty:
        return df

    df = df.copy()

    # ------------------------------------------------------------------
    # BASIC PRICE FEATURES
    # ------------------------------------------------------------------
    df["return"]       = df["close"].pct_change().fillna(0)
    df["log_return"]   = np.log(df["close"].div(df["close"].shift(1)).replace(0, np.nan)).fillna(0)
    df["body"]         = df["close"] - df["open"]
    df["range"]        = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - np.maximum(df["open"], df["close"])
    df["lower_shadow"] = np.minimum(df["open"], df["close"]) - df["low"]

    # ------------------------------------------------------------------
    # MOVING AVERAGES
    # ------------------------------------------------------------------
    for w in [5, 10, 20, 50]:
        df[f"sma_{w}"] = _safe_roll(df["close"], w)
        df[f"ema_{w}"] = df["close"].ewm(span=w, adjust=False).mean()

    # ------------------------------------------------------------------
    # VOLATILITY
    # ------------------------------------------------------------------
    df["volatility_10"] = _safe_roll(df["return"], 10, "std").fillna(0)
    df["volatility_20"] = _safe_roll(df["return"], 20, "std").fillna(0)
    df["true_range"]    = df["high"] - df["low"]
    df["atr"]           = _safe_roll(df["true_range"], 14, "mean").fillna(0)

    # ------------------------------------------------------------------
    # VOLUME
    # ------------------------------------------------------------------
    df["volume_ma_10"] = _safe_roll(df["volume"], 10).fillna(0)
    df["volume_ma_20"] = _safe_roll(df["volume"], 20).fillna(0)
    df["volume_change"] = df["volume"].pct_change().fillna(0)

    # ------------------------------------------------------------------
    # RSI (14)
    # ------------------------------------------------------------------
    delta = df["close"].diff().fillna(0)
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    df["rsi_14"] = 100 - 100 / (1 + rs)

    # ------------------------------------------------------------------
    # MACD
    # ------------------------------------------------------------------
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]        = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]

    # ------------------------------------------------------------------
    # STOCHASTIC (14,3)
    # ------------------------------------------------------------------
    low14  = _safe_roll(df["low"],  14, "min")
    high14 = _safe_roll(df["high"], 14, "max")
    df["stoch_k"] = ((df["close"] - low14) / (high14 - low14 + 1e-9)) * 100
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    # ------------------------------------------------------------------
    # BOLLINGER BANDS (20, 2)
    # ------------------------------------------------------------------
    sma20 = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["bb_upper"]    = sma20 + 2 * std20
    df["bb_lower"]    = sma20 - 2 * std20
    df["bb_width"]    = df["bb_upper"] - df["bb_lower"]
    df["bb_position"] = (df["close"] - df["bb_lower"]) / (df["bb_width"] + 1e-9)

    # ------------------------------------------------------------------
    # MOMENTUM
    # ------------------------------------------------------------------
    for w in [3, 7, 14]:
        df[f"momentum_{w}"] = df["close"] - df["close"].shift(w)

    # ------------------------------------------------------------------
    # BREAKOUT FEATURES
    # ------------------------------------------------------------------
    df["prev_high_20"] = _safe_roll(df["high"], 20, "max")
    df["prev_low_20"]  = _safe_roll(df["low"],  20, "min")
    df["breakout_up"]   = (df["close"] > df["prev_high_20"]).astype(int)
    df["breakout_down"] = (df["close"] < df["prev_low_20"]).astype(int)

    # ------------------------------------------------------------------
    # TREND REGIME
    # ------------------------------------------------------------------
    df["trend_ma_diff"]  = df["ema_10"] - df["ema_20"]
    df["trend_strength"] = df["trend_ma_diff"].rolling(5).mean().fillna(0)
    df["trend_regime"]   = np.select(
        [df["trend_strength"] > 0, df["trend_strength"] < 0],
        [1, -1],
        default=0
    )

    # ------------------------------------------------------------------
    # REVERSAL SIGNAL
    # ------------------------------------------------------------------
    df["reversal_signal"] = (
        (df["macd_hist"] < 0) &
        (df["stoch_k"]   < 20) &
        (df["rsi_14"]    < 30)
    ).astype(int)

    # ------------------------------------------------------------------
    # FINAL CLEANUP
    # ------------------------------------------------------------------
    df = df.ffill().fillna(0)
    return df
