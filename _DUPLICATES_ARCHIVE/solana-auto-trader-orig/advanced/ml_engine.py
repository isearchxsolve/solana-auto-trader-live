# ml_engine.py
# PRODUCTION — Ultra ML Stack
# FIX: guard predict-before-fit; token model fallback; safe meta stacking

import os

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

import config
from logger_setup import get_logger

log = get_logger("ml_engine")

os.makedirs(config.MODEL_DIR, exist_ok=True)

# Optional heavy libs
try:
    import xgboost as xgb
    log.info("XGBoost available")
except ImportError:
    xgb = None
    log.warning("XGBoost not installed — skipping")

try:
    import lightgbm as lgb
    log.info("LightGBM available")
except ImportError:
    lgb = None
    log.warning("LightGBM not installed — skipping")

try:
    from catboost import CatBoostClassifier
    log.info("CatBoost available")
except ImportError:
    CatBoostClassifier = None
    log.warning("CatBoost not installed — skipping")


class UltraMLStack:
    """
    Stacked ensemble: RF + XGBoost + LightGBM + CatBoost → LogReg meta-model.
    Token-specific RF can be layered on top via fine-tuning.
    """

    def __init__(self, feature_columns: list[str] | None = None):
        self.feature_columns = feature_columns
        self._is_fitted      = False

        self.rf = RandomForestClassifier(
            n_estimators=300, max_depth=10, random_state=42, n_jobs=-1
        )
        self.xgb = (
            xgb.XGBClassifier(
                n_estimators=400, max_depth=6, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, eval_metric="logloss",
                verbosity=0,
            )
            if xgb else None
        )
        self.lgbm = (
            lgb.LGBMClassifier(
                n_estimators=400, max_depth=-1, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, verbosity=-1,
            )
            if lgb else None
        )
        self.cat = (
            CatBoostClassifier(
                iterations=350, depth=6, learning_rate=0.04,
                verbose=False, loss_function="Logloss",
            )
            if CatBoostClassifier else None
        )
        self.meta = LogisticRegression(max_iter=500)
        self.token_models: dict = {}

    # ------------------------------------------------------------------
    # SAVE / LOAD
    # ------------------------------------------------------------------
    def save_global(self):
        path = os.path.join(config.MODEL_DIR, "global_model.pkl")
        joblib.dump(
            {
                "rf": self.rf, "xgb": self.xgb, "lgbm": self.lgbm,
                "cat": self.cat, "meta": self.meta,
                "feature_columns": self.feature_columns,
                "is_fitted": self._is_fitted,
            },
            path,
        )
        log.info("Global model saved → %s", path)

    def load_global(self) -> bool:
        path = os.path.join(config.MODEL_DIR, "global_model.pkl")
        if not os.path.exists(path):
            return False
        try:
            data = joblib.load(path)
            self.rf              = data["rf"]
            self.xgb             = data.get("xgb")
            self.lgbm            = data.get("lgbm")
            self.cat             = data.get("cat")
            self.meta            = data["meta"]
            self.feature_columns = data["feature_columns"]
            self._is_fitted      = data.get("is_fitted", True)
            log.info("Global model loaded from %s", path)
            return True
        except Exception as exc:
            log.error("Failed to load global model: %s", exc)
            return False

    def save_token_model(self, token: str):
        path = os.path.join(config.MODEL_DIR, f"{token.replace('/', '_')}_model.pkl")
        joblib.dump(self.token_models[token], path)

    def load_token_model(self, token: str):
        path = os.path.join(config.MODEL_DIR, f"{token.replace('/', '_')}_model.pkl")
        if os.path.exists(path):
            self.token_models[token] = joblib.load(path)
            log.info("Token model loaded: %s", token)

    # ------------------------------------------------------------------
    # TRAIN GLOBAL
    # ------------------------------------------------------------------
    def train_global(self, df: pd.DataFrame) -> bool:
        if df.empty:
            log.error("train_global called with empty DataFrame")
            return False

        feature_cols = self.feature_columns or [
            c for c in df.columns
            if c not in ["target_up", "target_down", "target_breakout"]
        ]
        self.feature_columns = feature_cols

        X    = df[feature_cols].values
        y_up = df["target_up"].values

        log.info("Training global model on %d samples, %d features", len(X), len(feature_cols))

        self.rf.fit(X, y_up)
        if self.xgb:  self.xgb.fit(X, y_up)
        if self.lgbm: self.lgbm.fit(X, y_up)
        if self.cat:  self.cat.fit(X, y_up)

        meta_X = self._base_predictions(X)
        self.meta.fit(meta_X, y_up)

        self._is_fitted = True
        self.save_global()
        log.info("Global model training complete")
        return True

    # ------------------------------------------------------------------
    # TOKEN FINE-TUNING
    # ------------------------------------------------------------------
    def train_token_model(self, token: str, df: pd.DataFrame) -> bool:
        if df.empty or not self._is_fitted:
            return False
        feature_cols = self.feature_columns
        X = df[feature_cols].values
        y = df["target_up"].values

        if token not in self.token_models:
            self.token_models[token] = RandomForestClassifier(
                n_estimators=150, max_depth=8, random_state=42, n_jobs=-1
            )
        self.token_models[token].fit(X, y)
        self.save_token_model(token)
        log.info("Token model updated: %s", token)
        return True

    # ------------------------------------------------------------------
    # BASE PREDICTIONS (stacking layer)
    # ------------------------------------------------------------------
    def _base_predictions(self, X: np.ndarray) -> np.ndarray:
        preds = [self.rf.predict_proba(X)[:, 1]]
        if self.xgb:  preds.append(self.xgb.predict_proba(X)[:, 1])
        if self.lgbm: preds.append(self.lgbm.predict_proba(X)[:, 1])
        if self.cat:  preds.append(self.cat.predict_proba(X)[:, 1])
        return np.vstack(preds).T

    # ------------------------------------------------------------------
    # PREDICT
    # ------------------------------------------------------------------
    def predict(self, token: str, df_live: pd.DataFrame) -> dict:
        """
        Returns {"up": float, "down": float, "breakout": float}.
        Safe to call before training — returns neutral 0.5 probs.
        """
        default = {"up": 0.5, "down": 0.5, "breakout": 0.5}

        if not self._is_fitted:
            log.warning("ML model not yet trained — returning neutral probs")
            return default

        if df_live.empty or self.feature_columns is None:
            return default

        missing = [c for c in self.feature_columns if c not in df_live.columns]
        if missing:
            log.error("Missing feature columns: %s", missing[:5])
            return default

        try:
            X          = df_live[self.feature_columns].fillna(0).values
            meta_input = self._base_predictions(X)
            up_prob    = float(self.meta.predict_proba(meta_input)[-1, 1])
            bk_prob    = float(self.rf.predict_proba(X)[-1, 1])

            # Blend in token-specific model if available
            if token in self.token_models:
                tok_prob = float(self.token_models[token].predict_proba(X)[-1, 1])
                up_prob  = up_prob * 0.7 + tok_prob * 0.3

            up_prob = float(np.clip(up_prob, 0.0, 1.0))
            bk_prob = float(np.clip(bk_prob, 0.0, 1.0))

            return {
                "up":       up_prob,
                "down":     float(np.clip(1.0 - up_prob, 0.0, 1.0)),
                "breakout": bk_prob,
            }
        except Exception as exc:
            log.exception("Predict error: %s", exc)
            return default
