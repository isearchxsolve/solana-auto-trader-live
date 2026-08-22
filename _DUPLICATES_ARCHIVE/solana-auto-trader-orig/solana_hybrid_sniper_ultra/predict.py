"""
predict.py -- Load trained XGBoost models and produce pump/dump predictions.
Flat layout -- no sub-package imports.
"""

import os
import sys
import logging
import numpy as np
import joblib
from typing import Dict, Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import FEATURE_COLUMNS, build_feature_vector

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.pkl")

_models: Optional[dict] = None


def _load_models() -> dict:
    global _models
    if _models is None:
        if not os.path.exists(MODEL_PATH):
            logger.warning("model.pkl not found -- auto-training now...")
            from train import train
            train()
        _models = joblib.load(MODEL_PATH)
        logger.info(f"[ML] Models loaded from {MODEL_PATH}")
    return _models


def predict(feature_vector: np.ndarray) -> Tuple[float, float]:
    """
    Returns (pump_probability, dump_risk) in [0, 1].
    """
    models    = _load_models()
    vec       = feature_vector.reshape(1, -1)
    pump_prob = float(models["pump_label"].predict_proba(vec)[0, 1]) if "pump_label" in models else 0.5
    dump_risk = float(models["dump_label"].predict_proba(vec)[0, 1]) if "dump_label" in models else 0.5
    logger.debug(f"[ML] pump={pump_prob:.3f}  dump={dump_risk:.3f}")
    return pump_prob, dump_risk


def predict_from_features(features: Dict[str, float]) -> Tuple[float, float]:
    vec = build_feature_vector(features)
    return predict(vec)
