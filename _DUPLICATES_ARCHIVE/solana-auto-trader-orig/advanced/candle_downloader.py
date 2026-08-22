# candle_downloader.py
# PRODUCTION — Standalone candle downloader utility
# Downloads and caches candles for all universe tokens.
# Run independently: python candle_downloader.py

import time

import config
from dex_data     import DexScreenerData
from logger_setup import get_logger

log = get_logger("candle_downloader")


def download_all(timeframes: list[str] | None = None, limit: int = config.CANDLE_LIMIT):
    """Download and cache candles for every token in TOKEN_UNIVERSE."""
    timeframes = timeframes or [config.CANDLE_TIMEFRAME]

    for token in config.TOKEN_UNIVERSE:
        symbol  = token["symbol"]
        address = token["address"]
        client  = DexScreenerData(address)

        for tf in timeframes:
            log.info("Downloading %s — %s (%d candles)...", symbol, tf, limit)
            df = client.get_candles(timeframe=tf, limit=limit)
            if df.empty:
                log.warning("No data for %s %s", symbol, tf)
            else:
                log.info("  → %d candles cached", len(df))
            time.sleep(0.3)


if __name__ == "__main__":
    download_all(timeframes=["1m", "5m", "15m"])
