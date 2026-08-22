# dex_data.py
# PRODUCTION — DexScreener data client
# FIX: removed deprecated pd.np; improved error handling; unified candle API

import json
import math
import os
import time

import pandas as pd
import requests

import config
from logger_setup import get_logger

log = get_logger("dex_data")

os.makedirs(config.CACHE_DIR, exist_ok=True)

# DexScreener endpoints
_OHLCV_URL = "https://api.dexscreener.com/latest/dex/ohlcv/{chain}/{pair}/{tf}?limit={limit}"
_PAIR_URL  = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair}"


class DexScreenerData:
    """
    Fetches OHLCV candles and live price from DexScreener.
    Falls back to local cache when the API is unreachable.
    """

    def __init__(self, pair_address: str, chain: str = config.CHAIN):
        self.pair  = pair_address
        self.chain = chain
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # INTERNAL — resilient GET
    # ------------------------------------------------------------------
    def _get(self, url: str) -> dict | None:
        for attempt in range(config.API_RETRIES):
            try:
                resp = self._session.get(url, timeout=config.API_TIMEOUT)
                if resp.status_code == 200:
                    return resp.json()
                log.warning("HTTP %s for %s (attempt %d)", resp.status_code, url, attempt + 1)
            except requests.RequestException as exc:
                log.warning("Request error (attempt %d): %s", attempt + 1, exc)
            time.sleep(0.5 * (attempt + 1))
        return None

    # ------------------------------------------------------------------
    # LIVE PRICE
    # ------------------------------------------------------------------
    def get_live_price(self) -> float | None:
        url  = _PAIR_URL.format(chain=self.chain, pair=self.pair)
        data = self._get(url)
        try:
            return float(data["pairs"][0]["priceUsd"])
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    # ------------------------------------------------------------------
    # OHLCV CANDLES
    # timeframe: "1m" | "5m" | "15m" | "1h"
    # ------------------------------------------------------------------
    def get_candles(
        self,
        timeframe: str = config.CANDLE_TIMEFRAME,
        limit: int     = config.CANDLE_LIMIT,
    ) -> pd.DataFrame:
        cache_path = os.path.join(
            config.CACHE_DIR, f"{self.pair}_{timeframe}.json"
        )

        url  = _OHLCV_URL.format(
            chain=self.chain, pair=self.pair, tf=timeframe, limit=limit
        )
        data = self._get(url)

        if data and "data" in data and data["data"]:
            raw = data["data"]
            # Persist to cache
            try:
                with open(cache_path, "w") as fh:
                    json.dump(raw, fh)
            except OSError as exc:
                log.warning("Cache write failed: %s", exc)

        elif os.path.exists(cache_path):
            log.warning("API unavailable, loading cache for %s %s", self.pair, timeframe)
            try:
                with open(cache_path) as fh:
                    raw = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                log.error("Cache read failed: %s", exc)
                return pd.DataFrame()
        else:
            log.error("No data and no cache for %s %s", self.pair, timeframe)
            return pd.DataFrame()

        return self._parse_candles(raw)

    # ------------------------------------------------------------------
    # PARSE RAW CANDLE LIST → clean DataFrame
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_candles(raw: list) -> pd.DataFrame:
        if not raw:
            return pd.DataFrame()
        try:
            df = pd.DataFrame(raw)

            # Normalise column names (DexScreener returns lowercase already)
            df.columns = [c.lower() for c in df.columns]

            # Timestamp
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
            elif "time" in df.columns:
                df["timestamp"] = pd.to_datetime(df["time"], unit="ms", utc=True)
                df = df.drop(columns=["time"])

            required = ["timestamp", "open", "high", "low", "close", "volume"]
            missing  = [c for c in required if c not in df.columns]
            if missing:
                log.error("Candle data missing columns: %s", missing)
                return pd.DataFrame()

            df = df[required].copy()
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            df = df.dropna().sort_values("timestamp").reset_index(drop=True)
            return df

        except Exception as exc:
            log.exception("Candle parse error: %s", exc)
            return pd.DataFrame()
