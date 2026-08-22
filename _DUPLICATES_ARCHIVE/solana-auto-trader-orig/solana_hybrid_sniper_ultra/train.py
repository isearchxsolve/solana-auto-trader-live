"""
train.py -- Train XGBoost models for pump_probability and dump_risk.

Usage (from the folder containing this file):
    python train.py
    python train.py --data labeled.csv
"""

import os
import sys
import logging
import argparse
import numpy as np
import pandas as pd
import joblib
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score, classification_report
from xgboost import XGBClassifier

# Flat layout: features.py is in the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import FEATURE_COLUMNS

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.pkl")


def _make_ohlcv(rng, n_candles: int = 30, trend: float = 0.0, vol: float = 0.02) -> pd.DataFrame:
    """Simulate n_candles of 1-minute OHLCV. trend/vol control pump/dump intensity."""
    import time as _time
    base_price = rng.uniform(0.000001, 0.01)
    returns    = rng.normal(trend, vol, n_candles)
    closes     = base_price * np.cumprod(1 + returns)
    opens      = np.concatenate([[base_price], closes[:-1]])
    spreads    = np.abs(closes - opens) * rng.uniform(0.3, 1.5, n_candles)
    highs      = np.maximum(opens, closes) + spreads
    lows       = np.maximum(np.minimum(opens, closes) - spreads, 1e-12)
    base_vol   = rng.uniform(500, 50_000)
    volumes    = rng.exponential(base_vol, n_candles) * (1 + np.abs(returns) * 10)
    now        = int(_time.time())
    times      = [now - (n_candles - i) * 60 for i in range(n_candles)]
    return pd.DataFrame({"time": times, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": volumes})


def generate_synthetic_data(n: int = 8000) -> pd.DataFrame:
    """
    Build training rows by simulating realistic OHLCV candles and running
    compute_sniper_features() -- the exact same pipeline used at inference.
    This guarantees training and inference feature distributions match.
    """
    from features import compute_sniper_features

    rng  = np.random.default_rng(42)
    rows = []

    for _ in range(n):
        scenario   = rng.choice(["pump", "dump", "flat"], p=[0.35, 0.25, 0.40])
        trend      = {"pump": rng.uniform(0.006, 0.03),
                      "dump": rng.uniform(-0.03, -0.006),
                      "flat": rng.uniform(-0.006, 0.006)}[scenario]
        vol        = rng.uniform(0.005, 0.04)
        ohlcv      = _make_ohlcv(rng, n_candles=30, trend=trend, vol=vol)

        age        = rng.uniform(0, 7200)
        liquidity  = rng.uniform(1_000, 500_000)
        traders    = int(rng.uniform(1, 300))
        lp_locked  = bool(liquidity > 5_000 and age > 300 and rng.random() < 0.6)

        feats = compute_sniper_features(
            df=ohlcv,
            token_age_seconds=age,
            active_traders=traders,
            liquidity_usd=liquidity,
            lp_locked=lp_locked,
        )

        # Pump: clear uptrend + volume spike. Dump: clear downtrend.
        feats["pump_label"] = int(
            scenario == "pump"
            and feats.get("momentum_1m", 0) > 0.002
            and feats.get("vol_spike", 0) > 1.2
        )
        feats["dump_label"] = int(
            scenario == "dump"
            and feats.get("momentum_1m", 0) < -0.002
            and feats.get("volatility", 0) > 0.008
        )
        rows.append(feats)

    return pd.DataFrame(rows)


def train(data_path: str = None) -> dict:
    if data_path and os.path.exists(data_path):
        logger.info(f"Loading data from {data_path}")
        df = pd.read_csv(data_path)
    else:
        logger.info("Generating synthetic training data (8000 samples)...")
        df = generate_synthetic_data(n=8000)

    # Fill missing feature columns with 0 (handles any future column additions)
    for col in FEATURE_COLUMNS:
        if col not in df.columns:
            df[col] = 0.0

    X      = df[FEATURE_COLUMNS].fillna(0).values
    models = {}

    for target in ("pump_label", "dump_label"):
        if target not in df.columns:
            logger.warning(f"Column {target} missing -- skipping.")
            continue
        y         = df[target].values
        n_neg     = int((y == 0).sum())
        n_pos     = int((y == 1).sum())
        spw       = max(1.0, n_neg / max(n_pos, 1))   # scale_pos_weight
        logger.info(f"[{target}] pos={n_pos} neg={n_neg} scale_pos_weight={spw:.1f}")

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        model = XGBClassifier(
            n_estimators=300,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=spw,   # corrects class imbalance
            eval_metric="aucpr",    # area under precision-recall is better for imbalanced data
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

        y_pred = model.predict_proba(X_test)[:, 1]
        auc    = roc_auc_score(y_test, y_pred)
        logger.info(f"[{target}] AUC = {auc:.4f}")
        logger.info("\n" + classification_report(y_test, model.predict(X_test)))
        models[target] = model

    joblib.dump(models, MODEL_PATH)
    logger.info(f"Models saved to {MODEL_PATH}")
    return models


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train XGBoost pump/dump models")
    parser.add_argument("--data", default=None, help="Path to labeled CSV (optional)")
    args = parser.parse_args()
    train(args.data)
