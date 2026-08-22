# controller.py
# PRODUCTION CONTROLLER — wires all modules together
# FIX: all imports now point to real modules; correct method signatures

import time
from typing import Optional

import pandas as pd

import config
from dex_data      import DexScreenerData
from feature_builder import FeatureBuilder
from logger_setup  import get_logger
from ml_engine     import UltraMLStack
from ta_engine     import compute_ta
from trading_brain import TradingBrain
from trading_system import TradingSystem

log = get_logger("controller")


class Controller:
    """
    Orchestrates the full trading loop:
      1. Load/train ML model from historical candles
      2. Every N seconds: fetch live data → TA → features → ML predict → brain decide → execute
      3. Continuous stop-loss / take-profit guardian
    """

    def __init__(
        self,
        mode:           str = config.MODE,
        loop_delay:     int = config.LOOP_DELAY_SECONDS,
        rotate_every:   int = config.UNIVERSE_ROTATE_EVERY,
    ):
        self.mode         = mode
        self.loop_delay   = loop_delay
        self.rotate_every = rotate_every

        # Universe
        self.universe: list[dict] = config.TOKEN_UNIVERSE

        # Modules
        self.feature_builder = FeatureBuilder()
        self.ml              = UltraMLStack()
        self.brain           = TradingBrain()
        self.system          = TradingSystem(mode=mode)

        # Per-token DexScreener clients (lazy-init)
        self._clients: dict[str, DexScreenerData] = {}

        # Loop counter
        self._loop_count = 0

    # ------------------------------------------------------------------
    # DEX CLIENT FACTORY
    # ------------------------------------------------------------------
    def _client(self, address: str) -> DexScreenerData:
        if address not in self._clients:
            self._clients[address] = DexScreenerData(address)
        return self._clients[address]

    # ------------------------------------------------------------------
    # BOOTSTRAP — train global ML on all universe tokens
    # ------------------------------------------------------------------
    def bootstrap_training(self):
        """
        Attempts to load a saved model first.
        If none exists, trains from historical candles across the universe.
        """
        log.info("=== BOOTSTRAP: Loading / Training ML model ===")

        if self.ml.load_global():
            log.info("Pre-trained model loaded. Skipping training.")
            return

        all_frames: list[pd.DataFrame] = []

        for token in self.universe:
            symbol, address = token["symbol"], token["address"]
            log.info("  Fetching training candles: %s", symbol)
            try:
                client  = self._client(address)
                candles = client.get_candles(
                    timeframe=config.CANDLE_TIMEFRAME,
                    limit=config.CANDLE_LIMIT,
                )
                if len(candles) < config.MIN_CANDLES:
                    log.warning("  %s: too few candles (%d) — skipping", symbol, len(candles))
                    continue

                df_ta      = compute_ta(candles)
                df_features = self.feature_builder.build_features(df_ta)

                if df_features.empty or "target_up" not in df_features.columns:
                    log.warning("  %s: feature build failed — skipping", symbol)
                    continue

                df_features["_token"] = symbol
                all_frames.append(df_features)
                log.info("  %s: %d training samples", symbol, len(df_features))
                time.sleep(0.3)  # gentle rate-limiting

            except Exception as exc:
                log.exception("  Training data error for %s: %s", symbol, exc)

        if not all_frames:
            log.error("No training data collected — ML will return neutral probs")
            return

        combined = pd.concat(all_frames, ignore_index=True).drop(columns=["_token"], errors="ignore")
        combined = combined.dropna(subset=["target_up"])

        log.info("Training on %d total samples...", len(combined))
        self.ml.feature_columns = self.feature_builder.feature_columns
        self.ml.train_global(combined)

        # Per-token fine-tuning
        for frame in all_frames:
            token_symbol = frame.iloc[0].get("_token", "unknown") if "_token" in frame else "unknown"
            self.ml.train_token_model(token_symbol, frame.drop(columns=["_token"], errors="ignore"))

    # ------------------------------------------------------------------
    # PROCESS ONE TOKEN (single loop iteration)
    # ------------------------------------------------------------------
    def process_token(self, token: dict):
        symbol  = token["symbol"]
        address = token["address"]

        try:
            client = self._client(address)

            # --- Live price
            price = client.get_live_price()
            if price is None or price <= 0:
                log.warning("[%s] Could not fetch live price", symbol)
                return

            # --- Candles + TA + features
            candles = client.get_candles(
                timeframe=config.CANDLE_TIMEFRAME,
                limit=config.CANDLE_LIMIT,
            )
            if len(candles) < config.MIN_CANDLES:
                log.warning("[%s] Insufficient candles (%d)", symbol, len(candles))
                return

            df_ta       = compute_ta(candles)
            df_features = self.feature_builder.build_features(df_ta)

            if df_features.empty:
                log.warning("[%s] Empty feature frame", symbol)
                return

            # --- ML prediction
            ml_probs = self.ml.predict(symbol, df_features)

            # --- Brain decision
            signal    = self.brain.decide_action(symbol, df_features, ml_probs)
            pos_size  = self.brain.compute_position_size(ml_probs, df_features)

            log.info(
                "[%-15s] price=$%-12.6f up=%.2f bk=%.2f dn=%.2f → %s",
                symbol, price,
                ml_probs["up"], ml_probs["breakout"], ml_probs["down"],
                signal,
            )

            # --- Execute
            self.system.process(
                token         = symbol,
                price         = price,
                signal        = signal,
                size_fraction = pos_size,
            )

        except Exception as exc:
            log.exception("[%s] Error during token processing: %s", symbol, exc)

    # ------------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------------
    def run_forever(self):
        log.info("=" * 60)
        log.info("  TRADING BOT — mode=%s | universe=%d tokens", self.mode, len(self.universe))
        log.info("=" * 60)

        self.bootstrap_training()

        consecutive_errors = 0

        while True:
            try:
                # --- Stop-loss / take-profit check first
                live_prices = {}
                for token in self.universe:
                    p = self._client(token["address"]).get_live_price()
                    if p:
                        live_prices[token["symbol"]] = p
                self.system.check_risk_exits(live_prices)

                # --- Process each token in universe
                for token in self.universe:
                    self.process_token(token)

                # --- Equity update
                self.system.update_equity(live_prices)

                # --- Universe rotation (use configured token list,
                #     could be extended to rescore dynamically)
                self._loop_count += 1

                consecutive_errors = 0
                time.sleep(self.loop_delay)

            except KeyboardInterrupt:
                log.info("Shutdown requested — stopping bot.")
                break

            except Exception as exc:
                consecutive_errors += 1
                log.error("Runtime error #%d: %s", consecutive_errors, exc)
                # Exponential backoff up to 60s
                backoff = min(2 ** consecutive_errors, 60)
                log.info("Backing off for %ds...", backoff)
                time.sleep(backoff)
