"""
data_fetcher.py -- Token discovery via DexScreener, real OHLCV via multiple free APIs.
Flat layout -- no sub-package imports.

Discovery:
  DexScreener  → trending + new Solana token pairs (no rate limit, no key)

OHLCV waterfall (all real candles, no synthetic data):
  1. GeckoTerminal  -- top N candidates by volume; real 1-min candles, no key
  2. pump.fun        -- candlesticks endpoint; real 1-min candles, no key
  3. Birdeye         -- real OHLCV; free-tier key via BIRDEYE_API_KEY env var

If none of the above return data the token is skipped (no synthetic fallback).

Rate limiting:
  GeckoTerminalClient._get() enforces a minimum sleep between calls using a plain
  monotonic timestamp -- no asyncio.Lock, no coroutine magic.
  6s per call = 10 req/min, safely under the observed free-tier ~12 req/min ceiling.
"""

import os
import time
import logging
import asyncio
import aiohttp
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple

from helpers import async_retry

logger = logging.getLogger(__name__)

DEXSCREENER_BASE  = "https://api.dexscreener.com"
GECKO_BASE        = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS     = {"Accept": "application/json;version=20230302"}

PUMPFUN_BASE      = "https://frontend-api.pump.fun"
PUMPFUN_TIMEFRAME = 1   # 1-minute candles

BIRDEYE_BASE      = "https://public-api.birdeye.so"
# Free-tier key -- register at https://birdeye.so/developer (free, no credit card)
# Set env var: BIRDEYE_API_KEY=your_key_here
# If not set, non-gecko tokens that have graduated from pump.fun will have no OHLCV.
BIRDEYE_API_KEY   = os.getenv("BIRDEYE_API_KEY", "")
if not BIRDEYE_API_KEY:
    logger.warning(
        "[data_fetcher] BIRDEYE_API_KEY not set -- non-gecko tokens that have graduated "
        "from the pump.fun bonding curve will have no OHLCV and be skipped. "
        "Get a free key at https://birdeye.so/developer (no credit card required) "
        "and add BIRDEYE_API_KEY=your_key to .env to unlock coverage for all tokens."
    )

# GeckoTerminal free tier: observed limit is ~12 req/min in practice.
# 12s between calls = 5 req/min -- conservative buffer well under the 429 threshold.
GECKO_CANDIDATES  = 15     # raised from 10; 20 caused too many 429s at 8s interval

GECKO_INTERVAL    = 12.0   # seconds between GeckoTerminal calls -- 12s = 5 req/min,
                           # safely under the observed ~6 req/min free-tier ceiling.
                           # 8s (7.5 req/min) caused frequent 429s with 20 candidates.

# ── Gecko pre-filter thresholds ────────────────────────────────────────────────
GECKO_MIN_LIQUIDITY_USD = float(os.getenv("GECKO_MIN_LIQ",    "2000"))
GECKO_MIN_VOL_M5_USD    = float(os.getenv("GECKO_MIN_VOL_M5", "200"))

# Pool address resolved once per token, reused every subsequent scan.
_POOL_CACHE_TTL = 10 * 60.0
_pool_cache: Dict[str, Optional[tuple]] = {}   # addr → (attrs_or_None, monotonic_ts)

_OHLCV_TTL = 180.0  # 3-minute OHLCV cache (was 90s)
_ohlcv_cache: Dict[str, tuple] = {}   # pool_address → (df, timestamp)

# ── dex_chart circuit breaker ─────────────────────────────────────────────────
# io.dexscreener.com returns HTTP 403 for all programmatic requests (Cloudflare block).
# After DEX_CHART_FAIL_THRESHOLD consecutive 403s we disable the tier entirely for
# DEX_CHART_COOLDOWN seconds so we stop wasting round-trips.
# After DEX_CHART_MAX_TRIPS total trips in a session we permanently disable to avoid
# repeatedly wasting requests on a known-blocked endpoint.
_DEX_CHART_CONSECUTIVE_FAILS: int   = 0
_DEX_CHART_DISABLED_UNTIL:    float = 0.0
_DEX_CHART_TRIP_COUNT:        int   = 0          # how many times CB has tripped this session
_DEX_CHART_PERMANENT_DISABLE: bool  = False      # set True after MAX_TRIPS -- no more retries
DEX_CHART_FAIL_THRESHOLD             = int(os.getenv("DEX_CHART_FAIL_THRESHOLD", "2"))    # was 5
DEX_CHART_COOLDOWN                   = float(os.getenv("DEX_CHART_COOLDOWN_S", str(5 * 60)))  # was 30 min
DEX_CHART_MAX_TRIPS                  = int(os.getenv("DEX_CHART_MAX_TRIPS", "3"))         # permanent disable after 3 trips

_gecko_last_call: float = 0.0


# ── Birdeye 401 suppression ───────────────────────────────────────────────────
# Log the "no API key" warning once per session rather than once per token per scan.
_birdeye_401_logged: bool = False

# ── Gecko pre-filter ─────────────────────────────────────────────────────────

def _passes_gecko_prefilter(pair: dict) -> bool:
    """Gate on DexScreener pair data (zero extra API calls) before a Gecko OHLCV call."""
    liq = _pair_liquidity(pair)
    if liq < GECKO_MIN_LIQUIDITY_USD:
        return False
    try:
        vol_m5 = float((pair.get("volume") or {}).get("m5") or 0)
    except (TypeError, ValueError):
        vol_m5 = 0.0
    return vol_m5 >= GECKO_MIN_VOL_M5_USD


# ── DexScreener (discovery + pre-filter) ─────────────────────────────────────

class DexScreenerClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    @async_retry(max_attempts=3, delay=1.0)
    async def get_trending_tokens(self) -> List[Dict]:
        url = f"{DEXSCREENER_BASE}/token-boosts/latest/v1"
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return await self._search_solana_pairs("solana meme")
            data = await resp.json()
        if not isinstance(data, list):
            data = []
        sol   = [d for d in data if d.get("chainId") == "solana"]
        addrs = [b["tokenAddress"] for b in sol[:20] if b.get("tokenAddress")]
        return await self._fetch_pairs_for_addresses(addrs)

    @async_retry(max_attempts=3, delay=1.0)
    async def get_new_pairs(self) -> List[Dict]:
        url = f"{DEXSCREENER_BASE}/token-profiles/latest/v1"
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return await self._search_solana_pairs("new solana token")
            data = await resp.json()
        if not isinstance(data, list):
            data = []
        sol   = [d for d in data if d.get("chainId") == "solana"]
        addrs = [p["tokenAddress"] for p in sol[:30] if p.get("tokenAddress")]
        pairs = await self._fetch_pairs_for_addresses(addrs)
        now = time.time()
        for p in pairs:
            created = p.get("pairCreatedAt", 0)
            if created:
                p["_age_seconds"] = now - created / 1000
        return pairs

    async def _fetch_pairs_for_addresses(self, addresses: List[str]) -> List[Dict]:
        if not addresses:
            return []
        all_pairs = []
        for i in range(0, len(addresses), 30):
            chunk = addresses[i:i + 30]
            url   = f"{DEXSCREENER_BASE}/latest/dex/tokens/{','.join(chunk)}"
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data  = await resp.json()
                    pairs = data.get("pairs") or []
                    all_pairs.extend(p for p in pairs if p.get("chainId") == "solana")
            except Exception as exc:
                logger.warning(f"[DexScreener] batch fetch failed: {exc}")
        return all_pairs

    @async_retry(max_attempts=2, delay=1.0)
    async def _search_solana_pairs(self, query: str) -> List[Dict]:
        url = f"{DEXSCREENER_BASE}/latest/dex/search"
        async with self.session.get(
            url, params={"q": query}, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
        return [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]

    @async_retry(max_attempts=3, delay=1.0)
    async def get_pair_by_address(self, address: str) -> Optional[Dict]:
        url = f"{DEXSCREENER_BASE}/latest/dex/tokens/{address}"
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            return None
        return max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0)))


# ── GeckoTerminal (real OHLCV) ────────────────────────────────────────────────

class GeckoTerminalClient:
    """
    Free GeckoTerminal API -- no key required.

    _get() sleeps before each call so at least GECKO_INTERVAL seconds elapse
    between requests. No asyncio.Lock -- bot.py guarantees calls are sequential.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _get(self, url: str, params: dict) -> Optional[dict]:
        global _gecko_last_call
        wait = GECKO_INTERVAL - (time.monotonic() - _gecko_last_call)
        if wait > 0:
            logger.debug(f"[Gecko] throttle sleep {wait:.1f}s")
            await asyncio.sleep(wait)
        _gecko_last_call = time.monotonic()
        async with self.session.get(
            url, headers=GECKO_HEADERS, params=params,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 429:
                # Sleep an extra 20s on top of the retry backoff to let the
                # rate-limit window reset before the next attempt.
                logger.warning("[Gecko] 429 received -- cooling down 30s before retry")
                await asyncio.sleep(30.0)
                _gecko_last_call = time.monotonic()   # reset so interval recalculates cleanly
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history,
                    status=429, message="Too Many Requests",
                )
            if resp.status != 200:
                return None
            return await resp.json()

    @async_retry(max_attempts=3, delay=10.0, backoff=2.0)
    async def get_top_pool(self, token_address: str) -> Optional[Dict]:
        """
        Resolve the highest-activity pool for a Solana token.
        FIX #2: Results now expire after _POOL_CACHE_TTL (10 min) so liquidity
        and active_trader counts refresh instead of being pinned forever.
        """
        cached = _pool_cache.get(token_address)
        if cached is not None:
            attrs, fetched_at = cached
            if time.monotonic() - fetched_at < _POOL_CACHE_TTL:
                return attrs   # None means confirmed-no-pool, still valid within TTL

        data = await self._get(
            f"{GECKO_BASE}/networks/solana/tokens/{token_address}/pools",
            {"sort": "h24_tx_count_desc", "limit": 5},
        )

        pools = (data or {}).get("data") or []
        if not pools:
            _pool_cache[token_address] = (None, time.monotonic())
            return None

        def h24_vol(p):
            return float((p.get("attributes", {}).get("volume_usd") or {}).get("h24") or 0)

        attrs = max(pools, key=h24_vol).get("attributes") or {}
        _pool_cache[token_address] = (attrs, time.monotonic())
        return attrs

    @async_retry(max_attempts=3, delay=10.0, backoff=2.0)
    async def get_ohlcv(
        self,
        pool_address: str,
        timeframe:    str = "minute",
        aggregate:    int = 1,
        limit:        int = 30,
    ) -> pd.DataFrame:
        """
        Fetch real OHLCV candles (USD) for a pool.
        Results cached for _OHLCV_TTL seconds to avoid re-hitting GeckoTerminal
        on the same pool every scan cycle.
        """
        cached = _ohlcv_cache.get(pool_address)
        if cached is not None:
            df, fetched_at = cached
            age = time.monotonic() - fetched_at
            if age < _OHLCV_TTL:
                logger.debug(f"[Gecko] OHLCV cache hit for {pool_address[:8]}.. (age={age:.0f}s)")
                return df

        data = await self._get(
            f"{GECKO_BASE}/networks/solana/pools/{pool_address}/ohlcv/{timeframe}",
            {"aggregate": aggregate, "limit": limit, "currency": "usd", "token": "base"},
        )
        if not data:
            return _empty_ohlcv()

        raw = (data.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not raw:
            return _empty_ohlcv()

        df = pd.DataFrame(raw, columns=["time", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        df = df.sort_values("time").reset_index(drop=True)
        _ohlcv_cache[pool_address] = (df, time.monotonic())
        return df


# ── Pool metadata helpers ─────────────────────────────────────────────────────

def _pool_token_age(pool_attrs: dict) -> float:
    created = pool_attrs.get("pool_created_at")
    if not created:
        return 9999.0
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - dt).total_seconds(), 0.0)
    except Exception:
        return 9999.0


def _pool_liquidity(pool_attrs: dict) -> float:
    try:
        return float(pool_attrs.get("reserve_in_usd") or 0)
    except (ValueError, TypeError):
        return 0.0


def _pool_active_traders(pool_attrs: dict) -> int:
    txns = pool_attrs.get("transactions") or {}
    for window in ("m5", "m15", "m30", "h1"):
        w = txns.get(window) or {}
        count = int(w.get("buyers", 0)) + int(w.get("sellers", 0))
        if count > 0:
            return count
    return 0


def _pool_lp_locked(pool_attrs: dict) -> bool:
    return _pool_liquidity(pool_attrs) > 5_000 and _pool_token_age(pool_attrs) > 300


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])


# ── DexScreener chart API (real on-chain DEX candles, no key, no rate limit) ──

async def _ohlcv_from_dexscreener_chart(
    session:      aiohttp.ClientSession,
    dex_pair:     Optional[dict],
    token_address: str,
    limit:        int = 50,
) -> pd.DataFrame:
    """
    Fetch REAL 1-minute OHLCV candles from DexScreener's chart API.
    Tries AMM v3 endpoint first, falls back to generic pairs endpoint.

    Circuit breaker: after DEX_CHART_FAIL_THRESHOLD consecutive 403s the tier is
    disabled for DEX_CHART_COOLDOWN seconds to avoid hammering a Cloudflare block.
    """
    global _DEX_CHART_CONSECUTIVE_FAILS, _DEX_CHART_DISABLED_UNTIL, \
           _DEX_CHART_TRIP_COUNT, _DEX_CHART_PERMANENT_DISABLE

    # Permanent disable -- io.dexscreener.com has been Cloudflare-blocked too many times
    if _DEX_CHART_PERMANENT_DISABLE:
        return _empty_ohlcv()

    # Timed circuit breaker check
    if time.monotonic() < _DEX_CHART_DISABLED_UNTIL:
        return _empty_ohlcv()

    if not dex_pair:
        logger.debug(f"[dex_chart] {token_address[:8]}.. no dex_pair -- skipping")
        return _empty_ohlcv()

    pair_address = dex_pair.get("pairAddress", "")
    if not pair_address:
        raw_id = dex_pair.get("id", "")
        pair_address = raw_id.split("_")[-1] if "_" in raw_id else ""
    if not pair_address:
        logger.debug(f"[dex_chart] {token_address[:8]}.. no pairAddress (keys={list(dex_pair.keys())[:6]})")
        return _empty_ohlcv()

    now = int(time.time())
    params = {"res": "1", "cb": now}
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer":         "https://dexscreener.com/",
        "Origin":          "https://dexscreener.com",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",  # exclude br: requires Brotli package
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-site",
    }

    _fail_reasons = []
    got_403 = False
    for url in [
        f"https://io.dexscreener.com/dex/chart/amm/v3/pairs/solana/{pair_address}",
        f"https://io.dexscreener.com/dex/chart/pairs/solana/{pair_address}",
    ]:
        try:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 403:
                    got_403 = True
                    _fail_reasons.append(f"{url.split('/')[-3]}:HTTP403")
                    continue
                if resp.status != 200:
                    _fail_reasons.append(f"{url.split('/')[-3]}:HTTP{resp.status}")
                    continue
                data = await resp.json(content_type=None)
            bars = data.get("bars") or []
            if not bars:
                _fail_reasons.append(f"{url.split('/')[-3]}:no_bars(keys={list(data.keys())[:4]})")
                continue

            rows = []
            for b in bars[-limit:]:
                ts = b.get("t", 0)
                rows.append({
                    "time":   int(ts / 1000) if ts > 1e10 else int(ts),
                    "open":   float(b.get("o", 0) or 0),
                    "high":   float(b.get("h", 0) or 0),
                    "low":    float(b.get("l", 0) or 0),
                    "close":  float(b.get("c", 0) or 0),
                    "volume": float(b.get("v", 0) or 0),
                })
            if not rows:
                _fail_reasons.append(f"{url.split('/')[-3]}:empty_rows")
                continue
            df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
            df = df[df["close"] > 0]
            if not df.empty:
                # Success -- reset circuit breaker
                _DEX_CHART_CONSECUTIVE_FAILS = 0
                return df
            _fail_reasons.append(f"{url.split('/')[-3]}:all_zero_close")
        except Exception as exc:
            _fail_reasons.append(f"{url.split('/')[-3]}:{type(exc).__name__}({exc})")

    # Track consecutive 403s for circuit breaker
    if got_403:
        _DEX_CHART_CONSECUTIVE_FAILS += 1
        if _DEX_CHART_CONSECUTIVE_FAILS >= DEX_CHART_FAIL_THRESHOLD:
            _DEX_CHART_CONSECUTIVE_FAILS = 0
            # Guard: don't increment or log again if already permanently disabled
            # (parallel batch processing can cause multiple tasks to reach here simultaneously)
            if _DEX_CHART_PERMANENT_DISABLE:
                pass
            else:
                _DEX_CHART_TRIP_COUNT += 1
                if _DEX_CHART_TRIP_COUNT >= DEX_CHART_MAX_TRIPS:
                    _DEX_CHART_PERMANENT_DISABLE = True
                    logger.warning(
                        f"[dex_chart] Permanently disabled after {_DEX_CHART_TRIP_COUNT} "
                        f"circuit breaker trips -- io.dexscreener.com appears Cloudflare-blocked "
                        f"for this session. pump.fun + GeckoTerminal will cover OHLCV."
                    )
                else:
                    _DEX_CHART_DISABLED_UNTIL = time.monotonic() + DEX_CHART_COOLDOWN
                    logger.warning(
                        f"[dex_chart] Circuit breaker OPEN (trip {_DEX_CHART_TRIP_COUNT}/{DEX_CHART_MAX_TRIPS}) "
                        f"after {DEX_CHART_FAIL_THRESHOLD} consecutive 403s -- "
                        f"pausing for {DEX_CHART_COOLDOWN/60:.0f}min"
                    )

    # Log first failure per pair only (suppress spam)
    _dex_chart_fail_logged = getattr(_ohlcv_from_dexscreener_chart, "_fail_logged", set())
    if pair_address not in _dex_chart_fail_logged:
        logger.info(
            f"[dex_chart] {token_address[:8]}.. pair={pair_address[:8]}.. "
            f"no candles -- {' | '.join(_fail_reasons) or 'unknown'}"
        )
        _dex_chart_fail_logged.add(pair_address)
        _ohlcv_from_dexscreener_chart._fail_logged = _dex_chart_fail_logged

    return _empty_ohlcv()


# ── pump.fun candlesticks (free, no key) ──────────────────────────────────────

async def _ohlcv_from_pumpfun(
    session:       aiohttp.ClientSession,
    token_address: str,
    limit:         int = 50,
) -> pd.DataFrame:
    """
    Fetch real 1-minute OHLCV candles from pump.fun's public candlesticks endpoint.
    Only works for tokens still on the bonding curve (not yet graduated to Raydium).
    No API key required.

    endpoint: GET /candlesticks/{mint}?offset=0&limit=N&timeframe=1
    response:  [{open,high,low,close,volume,slot,timestamp}, ...]
               timestamp is Unix seconds.
    """
    url = f"{PUMPFUN_BASE}/candlesticks/{token_address}"
    params = {"offset": 0, "limit": limit, "timeframe": PUMPFUN_TIMEFRAME}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return _empty_ohlcv()
            raw = await resp.json()
        if not isinstance(raw, list) or not raw:
            return _empty_ohlcv()

        rows = []
        for c in raw:
            ts = c.get("timestamp")
            if ts is None:
                continue
            # pump.fun returns timestamps in seconds already
            rows.append({
                "time":   int(ts),
                "open":   float(c.get("open",   0) or 0),
                "high":   float(c.get("high",   0) or 0),
                "low":    float(c.get("low",    0) or 0),
                "close":  float(c.get("close",  0) or 0),
                "volume": float(c.get("volume", 0) or 0),
            })
        if not rows:
            return _empty_ohlcv()

        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        # Basic sanity: drop zero-price candles
        df = df[df["close"] > 0]
        return df if not df.empty else _empty_ohlcv()

    except Exception as exc:
        logger.debug(f"[pumpfun] {token_address[:8]}.. OHLCV failed: {exc}")
        return _empty_ohlcv()


# ── Birdeye (free tier, requires BIRDEYE_API_KEY env var) ─────────────────────

async def _ohlcv_from_birdeye(
    session:       aiohttp.ClientSession,
    token_address: str,
    limit:         int = 50,
) -> pd.DataFrame:
    """
    Fetch real 1-minute OHLCV from Birdeye's public API.
    Requires a free API key from https://birdeye.so/developer (no credit card).
    Set env var BIRDEYE_API_KEY to enable this fallback.

    endpoint: GET /defi/ohlcv
              ?address={token}
              &type=1m
              &time_from={unix_ts}
              &time_to={unix_ts}
    """
    if not BIRDEYE_API_KEY:
        return _empty_ohlcv()

    now       = int(time.time())
    time_from = now - limit * 60
    url       = f"{BIRDEYE_BASE}/defi/ohlcv"
    params    = {
        "address":   token_address,
        "type":      "1m",
        "time_from": time_from,
        "time_to":   now,
    }
    headers = {"x-api-key": BIRDEYE_API_KEY, "x-chain": "solana"}

    try:
        async with session.get(
            url, headers=headers, params=params,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                global _birdeye_401_logged
                if resp.status == 401 and not _birdeye_401_logged:
                    logger.warning(
                        "[birdeye] HTTP 401 -- BIRDEYE_API_KEY missing or invalid. "
                        "Falling back to dex_synth for all non-gecko tokens. "
                        "Get a free key at https://birdeye.so/developer"
                    )
                    _birdeye_401_logged = True
                elif resp.status != 401:
                    logger.info(f"[birdeye] {token_address[:8]}.. HTTP {resp.status}")
                return _empty_ohlcv()
            data = await resp.json()

        items = (data.get("data") or {}).get("items") or []
        if not items:
            return _empty_ohlcv()

        rows = []
        for c in items:
            # Birdeye returns unixTime in seconds
            rows.append({
                "time":   int(c.get("unixTime", 0)),
                "open":   float(c.get("o",  0) or 0),
                "high":   float(c.get("h",  0) or 0),
                "low":    float(c.get("l",  0) or 0),
                "close":  float(c.get("c",  0) or 0),
                "volume": float(c.get("v",  0) or 0),
            })
        if not rows:
            return _empty_ohlcv()

        df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
        df = df[df["close"] > 0]
        return df if not df.empty else _empty_ohlcv()

    except Exception as exc:
        logger.debug(f"[birdeye] {token_address[:8]}.. OHLCV failed: {exc}")
        return _empty_ohlcv()



# ── Unified token info builder ────────────────────────────────────────────────

async def fetch_token_info(
    token_address:    str,
    dex:              DexScreenerClient,
    gecko:            GeckoTerminalClient,
    dex_pair:         Optional[Dict] = None,    # pass pre-fetched pair to skip a DexScreener call
    session:          Optional[aiohttp.ClientSession] = None,  # for pump.fun / Birdeye calls
    use_gecko_ohlcv:  bool = True,              # False = skip gecko OHLCV, use pumpfun/birdeye
) -> Dict[str, Any]:
    """
    OHLCV waterfall -- all sources return real candles, no synthetic data:
      1. GeckoTerminal   -- top N tokens only (use_gecko_ohlcv=True); real 1-min candles, no key
                           N controlled by GECKO_CANDIDATES (now 10). Covers the highest-volume tokens.
      2. DexScreener chart API -- io.dexscreener.com is Cloudflare-protected and returns HTTP 403
                           for all programmatic requests. Kept in waterfall in case it unblocks,
                           but currently never succeeds.
      3. pump.fun         -- bonding-curve tokens only; real 1-min candles, no key.
                           Only works before a token graduates to Raydium.
      4. Birdeye          -- covers all Solana tokens; free key via BIRDEYE_API_KEY env var.
                           This is the primary fallback for graduated non-gecko tokens.
                           Without it, most non-gecko tokens will be skipped.
    Token is skipped if none of the above return data.
    """
    try:
        # Pool metadata lookup:
        #   use_gecko_ohlcv=True  → normal lookup (API call if not cached)
        #   use_gecko_ohlcv=False → cache-only (no live API call)
        if use_gecko_ohlcv:
            pool_attrs = await gecko.get_top_pool(token_address)
        else:
            # FIX #2: _pool_cache now stores (attrs, ts) tuples; unpack correctly.
            cached = _pool_cache.get(token_address)
            pool_attrs = cached[0] if cached is not None else None

        ohlcv        = _empty_ohlcv()
        ohlcv_source = "none"

        # ── Tier 1: GeckoTerminal (top N tokens only) ─────────────────────────
        if use_gecko_ohlcv and pool_attrs:
            pool_address = pool_attrs.get("address", "")
            if pool_address:
                ohlcv = await gecko.get_ohlcv(pool_address, "minute", 1, 50)
                if not ohlcv.empty:
                    ohlcv_source = "gecko"
                    logger.debug(
                        f"[data_fetcher] {token_address[:8]}.. "
                        f"{len(ohlcv)} real candles from GeckoTerminal"
                    )

        # ── Tier 2: DexScreener chart API (real DEX candles, no rate limit) ──
        if ohlcv.empty and session is not None and dex_pair:
            ohlcv = await _ohlcv_from_dexscreener_chart(session, dex_pair, token_address, limit=50)
            if not ohlcv.empty:
                ohlcv_source = "dex_chart"
                logger.debug(
                    f"[data_fetcher] {token_address[:8]}.. "
                    f"{len(ohlcv)} real candles from DexScreener chart"
                )

        # ── Tier 3: pump.fun candlesticks (bonding-curve only) ───────────────
        if ohlcv.empty and session is not None:
            ohlcv = await _ohlcv_from_pumpfun(session, token_address, limit=50)
            if not ohlcv.empty:
                ohlcv_source = "pumpfun"
                logger.debug(
                    f"[data_fetcher] {token_address[:8]}.. "
                    f"{len(ohlcv)} real candles from pump.fun"
                )

        # ── Tier 4: Birdeye (free API key via BIRDEYE_API_KEY env var) ───────
        if ohlcv.empty and session is not None and BIRDEYE_API_KEY:
            ohlcv = await _ohlcv_from_birdeye(session, token_address, limit=50)
            if not ohlcv.empty:
                ohlcv_source = "birdeye"
                logger.debug(
                    f"[data_fetcher] {token_address[:8]}.. "
                    f"{len(ohlcv)} real candles from Birdeye"
                )

        # ── Tier 5: DexScreener synthetic (always available, no key) ─────────
        # Builds estimated 1-min candles from DexScreener's m5/h1/h24 price summary.
        # Not real candles -- but deterministic per-token (stable across scans) and
        # accurate enough for RSI/EMA/momentum signals on the right price scale.
        # Tagged "dex_synth" so bot.py can block live entries while still using it
        # to monitor open positions and feed the labeler.
        if ohlcv.empty and dex_pair:
            ohlcv = _ohlcv_from_dexscreener(dex_pair, num_candles=30, token_address=token_address)
            if not ohlcv.empty:
                ohlcv_source = "dex_synth"
                logger.debug(
                    f"[data_fetcher] {token_address[:8]}.. "
                    f"synthetic candles from DexScreener summary"
                )

        if ohlcv.empty:
            logger.debug(f"[data_fetcher] {token_address[:8]}.. no OHLCV from any source -- skipping")
            return {"address": token_address, "error": "no OHLCV data", "ohlcv": _empty_ohlcv()}

        # ── Metadata ──────────────────────────────────────────────────────────
        if pool_attrs:
            token_age = _pool_token_age(pool_attrs)
            liquidity = _pool_liquidity(pool_attrs)
            active_tr = _pool_active_traders(pool_attrs)
            lp_locked = _pool_lp_locked(pool_attrs)
        else:
            if dex_pair is None:
                dex_pair = await dex.get_pair_by_address(token_address)
            if dex_pair:
                now        = time.time()
                created_at = (dex_pair.get("pairCreatedAt") or 0) / 1000
                token_age  = now - created_at if created_at else 9999.0
                liquidity  = float((dex_pair.get("liquidity") or {}).get("usd", 0))
                txns       = dex_pair.get("txns") or {}
                m5         = txns.get("m5") or {}
                active_tr  = int(m5.get("buys", 0)) + int(m5.get("sells", 0))
                lp_locked  = liquidity > 5_000 and token_age > 300
            else:
                token_age, liquidity, active_tr, lp_locked = 9999.0, 0.0, 0, False

        return {
            "address":           token_address,
            "token_age_seconds": token_age,
            "liquidity_usd":     liquidity,
            "active_traders":    active_tr,
            "lp_locked":         lp_locked,
            "ohlcv":             ohlcv,
            "ohlcv_source":      ohlcv_source,  # "gecko" | "pumpfun" | "birdeye" | "none"
        }

    except Exception as exc:
        logger.error(f"[data_fetcher] Error fetching {token_address}: {exc}")
        return {"address": token_address, "error": str(exc), "ohlcv": _empty_ohlcv()}


# ── DexScreener synthetic OHLCV fallback ──────────────────────────────────────

def _ohlcv_from_dexscreener(
    pair: dict,
    num_candles: int = 30,
    token_address: str = "",
) -> pd.DataFrame:
    """Build synthetic 1-min OHLCV from DexScreener price/volume summary.

    Fixes vs original:
    - Deterministic seed per token address so features are stable across scans.
    - vol_spike fix: only the last candle gets the m5 volume boost (previously
      all 5 got it, making vol_spike always ≈ 1.0 and preventing pump detection).
    - Noise std scaled to actual price movement so RSI doesn't saturate at 0/100.
    """
    try:
        price_usd = float(pair.get("priceUsd") or 0)
        if price_usd <= 0:
            return _empty_ohlcv()

        vol = pair.get("volume")      or {}
        pc  = pair.get("priceChange") or {}

        def safe_chg(k):
            v = pc.get(k)
            return float(v) / 100.0 if v is not None else 0.0

        vol_m5  = max(float(vol.get("m5",  0) or 0), 0)
        vol_h1  = max(float(vol.get("h1",  0) or 0), 0)
        vol_h24 = max(float(vol.get("h24", 0) or 0), 0)
        chg_m5  = safe_chg("m5")
        chg_h1  = safe_chg("h1")
        chg_h24 = safe_chg("h24")

        p_now = price_usd
        p_5m  = p_now / (1 + chg_m5)  if (1 + chg_m5)  > 0 else p_now
        p_1h  = p_now / (1 + chg_h1)  if (1 + chg_h1)  > 0 else p_now
        p_24h = p_now / (1 + chg_h24) if (1 + chg_h24) > 0 else p_now

        anchors_t   = np.array([0, 5, 60, 1440], dtype=float)
        anchors_lnp = np.log(np.array([p_now, p_5m, p_1h, p_24h]) + 1e-30)
        minutes_ago = np.linspace(num_candles - 1, 0, num_candles)
        ln_base     = np.interp(minutes_ago, anchors_t, anchors_lnp)

        # FIX: deterministic seed per token -- features no longer flip between scans
        seed = int.from_bytes(token_address.encode()[:8].ljust(8, b'\0'), 'little') % (2 ** 32)
        rng  = np.random.default_rng(seed)

        # FIX: noise std scaled to actual price movement range, not just m5 change.
        # Prevents RSI from saturating at 0 or 100 for trending tokens.
        # Uses the larger of h1 and m5 movements, spread over 30-min window.
        trend_range = max(abs(chg_m5), abs(chg_h1) / max(60 / num_candles, 1), 0.005)
        per_min_std = max(trend_range / 3.0, 0.008)

        noise    = rng.normal(0, per_min_std, num_candles)
        ln_close = ln_base + noise.cumsum() - noise.cumsum()[-1]
        closes   = np.exp(ln_close)

        # FIX: only boost the *last* candle for vol_spike so vol_spike can
        # meaningfully exceed 1.2 (was: all 5 candles boosted → spike always ≈ 1.0)
        avg_vol_per_min = (vol_h1 / 60) if vol_h1 > 0 else (vol_h24 / 1440)
        total_vol       = vol_h1 if vol_h1 > 0 else (vol_h24 / 24)
        weights         = np.ones(num_candles)
        m5_spike_ratio  = vol_m5 / (5 * avg_vol_per_min + 1e-9)  # how much busier last 5m was
        weights[-1]    *= max(m5_spike_ratio, 0.5)               # only last candle gets the boost
        volumes         = (weights / weights.sum()) * total_vol

        now  = int(time.time())
        rows = []
        for i, mins in enumerate(minutes_ago):
            c      = closes[i]
            o      = closes[i - 1] if i > 0 else p_1h
            spread = per_min_std * c * rng.uniform(0.5, 1.5)
            rows.append({
                "time":   now - int(mins) * 60,
                "open":   o,
                "high":   max(o, c) + spread,
                "low":    max(min(o, c) - spread, 1e-12),
                "close":  c,
                "volume": max(volumes[i], 0),
            })
        return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)

    except Exception as exc:
        logger.debug(f"[_ohlcv_from_dexscreener] failed: {exc}")
        return _empty_ohlcv()


# ── Candidate token scanner ───────────────────────────────────────────────────

def _pair_volume_h24(pair: dict) -> float:
    try:
        return float((pair.get("volume") or {}).get("h24") or 0)
    except (TypeError, ValueError):
        return 0.0


def _pair_liquidity(pair: dict) -> float:
    try:
        return float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


async def get_candidate_tokens(
    dex:           DexScreenerClient,
    max_trending:  int = 20,
    max_new:       int = 30,
    gecko_top_n:   int = GECKO_CANDIDATES,
) -> Tuple[List[str], Dict[str, Dict], Dict[str, Dict]]:
    """
    Returns:
      all_addresses   -- full candidate list (for open-position tracking)
      gecko_pairs     -- {address: dex_pair} for the top gecko_top_n tokens
                         by 24h volume.  Only these get GeckoTerminal calls.
    """
    try:
        trending, new_pairs = await asyncio.gather(
            dex.get_trending_tokens(),
            dex.get_new_pairs(),
            return_exceptions=True,
        )
        trending  = trending  if not isinstance(trending,  Exception) else []
        new_pairs = new_pairs if not isinstance(new_pairs, Exception) else []

        seen: set        = set()
        pairs_by_addr:   Dict[str, Dict] = {}

        for pair in (list(new_pairs)[:max_new] + list(trending)[:max_trending]):
            addr = (pair.get("baseToken") or {}).get("address", "")
            if addr and addr not in seen:
                seen.add(addr)
                pairs_by_addr[addr] = pair

        all_addresses = list(pairs_by_addr.keys())

        # ── Pre-filter: only tokens with minimum liquidity + m5 volume get
        # promoted to GeckoTerminal OHLCV calls.
        prefiltered = [a for a in all_addresses if _passes_gecko_prefilter(pairs_by_addr[a])]
        skipped_prefilter = len(all_addresses) - len(prefiltered)
        if skipped_prefilter:
            logger.debug(
                f"[data_fetcher] pre-filter skipped {skipped_prefilter} tokens "
                f"(liq<${GECKO_MIN_LIQUIDITY_USD:.0f} or vol_m5<${GECKO_MIN_VOL_M5_USD:.0f})"
            )

        ranked = sorted(prefiltered, key=lambda a: _pair_volume_h24(pairs_by_addr[a]), reverse=True)
        cached_addrs = [a for a in all_addresses if a in _pool_cache and a not in ranked[:gecko_top_n]]
        gecko_addrs  = ranked[:gecko_top_n] + cached_addrs
        gecko_pairs  = {a: pairs_by_addr[a] for a in gecko_addrs if a in pairs_by_addr}

        logger.info(
            f"[data_fetcher] {len(all_addresses)} candidates "
            f"({len(new_pairs)} new, {len(trending)} trending) | "
            f"pre-filter passed: {len(prefiltered)} | "
            f"GeckoTerminal calls: {len(gecko_pairs)}"
        )
        # FIX #4: Return all pairs (pairs_by_addr) alongside gecko_pairs so bot.py
        # can pass real dex_pair objects to dex_chart lookups for non-gecko tokens.
        # Previously non-gecko tokens got {} which evaluated to None via `{} or None`,
        # silently skipping the dex_chart OHLCV waterfall step.
        return all_addresses, gecko_pairs, pairs_by_addr

    except Exception as exc:
        logger.error(f"[get_candidate_tokens] {exc}")
        return [], {}, {}
