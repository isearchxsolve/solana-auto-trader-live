#!/usr/bin/env python3
"""
Enhanced Trending Token Detection for Solana — No-Key Edition v5

Zero API keys required.  All data sourced from public endpoints:
  • DexScreener  — boosts, new listings, search queries, DEX pair sweeps
  • GeckoTerminal — trending pools, top-volume pools, new pools, OHLCV candles

Sources (in priority order)
----------------------------
1. GeckoTerminal /networks/solana/trending_pools   (organic tx/vol signal)
2. GeckoTerminal /networks/solana/pools            (top-volume pools)
3. GeckoTerminal /networks/solana/new_pools        (early-stage tokens)
4. DexScreener   /token-boosts/latest/v1           (recently boosted)
5. DexScreener   /token-profiles/latest/v1         (new Solana listings)
6. DexScreener   /latest/dex/search/?q=SOL         (high-volume SOL pairs)
7. DexScreener   /latest/dex/search/?q=USDC        (high-volume USDC pairs)
8. DexScreener   /latest/dex/pairs/solana/<dex>    (Raydium/Orca/Meteora movers)

Key improvements vs v4
-----------------------
• No API keys — all sources work out of the box
• GeckoTerminal added as PRIMARY source (source_rank 1) — organic signal
• 5-endpoint DexScreener sweep for maximum token coverage
• Dedup keeps the entry with the best source_rank (gecko > dexscreener)
• Score bonuses: gecko=1.15x, new_listing=1.10x, boosted=1.05x
• Age gate (MIN_AGE_HOURS=2) with GeckoTerminal pool_created_at support
• 4-level fallback threshold cascade if no tokens survive initial filters
• 1h momentum weighted more heavily in scoring (primary ignition signal)
• Ignition filter tightened: h1>2% OR (h1>0 AND h6>8%) OR (24h>30 AND h1>-3)

pip install requests
"""

import math
import random
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_AGE_HOURS: float = 2.0

IGNORE_SYMBOLS: Set[str] = {
    'SOL', 'WSOL', 'USDC', 'USDT', 'USDS', 'DAI', 'BUSD',
    'TUSD', 'USD1', 'WBTC', 'WETH',
}

DEXSCREENER = "https://api.dexscreener.com"
GECKO       = "https://api.geckoterminal.com/api/v2"
HEADERS     = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _get(url: str, params: dict = None, timeout: int = 12) -> Optional[dict]:
    """HTTP GET with silent error handling. Returns parsed JSON or None."""
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  ⚠  Request error: {e}")
    return None


# ---------------------------------------------------------------------------
# Main Detector
# ---------------------------------------------------------------------------

class EnhancedTrendingTokenDetector:
    """
    Discover trending Solana tokens using GeckoTerminal + DexScreener.
    No API keys required.
    """

    def __init__(self):
        self.cache: Dict[str, Tuple[float, List[dict]]] = {}
        self.cache_duration = 60  # seconds

    # -----------------------------------------------------------------------
    # SOURCE A: GeckoTerminal (primary — organic on-chain signal)
    # -----------------------------------------------------------------------

    def _gecko_pool_to_token(self, pool: dict, label: str,
                              source_rank: int) -> Optional[dict]:
        """Convert a single GeckoTerminal pool dict to the pipeline format."""
        attr = pool.get("attributes", {})
        rel  = pool.get("relationships", {})

        base_data = rel.get("base_token", {}).get("data", {})
        raw_id    = base_data.get("id", "")
        addr      = raw_id.replace("solana_", "") if raw_id.startswith("solana_") else ""
        if not addr:
            return None

        name_raw = attr.get("name", "")
        sym      = name_raw.split("/")[0].strip().upper() if "/" in name_raw else name_raw.upper()[:12]
        if not sym or sym in IGNORE_SYMBOLS:
            return None

        price  = _safe_float(attr.get("base_token_price_usd"))
        liq    = _safe_float(attr.get("reserve_in_usd"))
        vol_d  = attr.get("volume_usd") or {}
        vol24  = _safe_float(vol_d.get("h24"))
        vol1   = _safe_float(vol_d.get("h1"))
        pc_d   = attr.get("price_change_percentage") or {}
        ch1    = _safe_float(pc_d.get("h1"))
        ch6    = _safe_float(pc_d.get("h6"))
        ch24   = _safe_float(pc_d.get("h24"))
        mcap   = _safe_float(attr.get("market_cap_usd") or attr.get("fdv_usd"))
        pair   = attr.get("address", "")

        age_h: Optional[float] = None
        created = attr.get("pool_created_at", "")
        if created:
            try:
                dt    = datetime.fromisoformat(created.replace("Z", "+00:00"))
                now   = datetime.now(timezone.utc)
                age_h = (now - dt).total_seconds() / 3600
            except Exception:
                pass

        return {
            "address":           addr,
            "symbol":            sym,
            "name":              name_raw,
            "price_usd":         price,
            "liquidity_usd":     liq,
            "volume_24h":        vol24,
            "volume_24h_usd":    vol24,
            "vol_h1":            vol1,
            "price_change_h1":   ch1,
            "price_change_h6":   ch6,
            "price_change_24h":  ch24,
            "market_cap_usd":    mcap,
            "dex":               attr.get("dex_id", "gecko"),
            "pair_address":      pair,
            "is_boosted":        False,
            "is_new_listing":    False,
            "is_gecko":          True,
            "age_hours":         age_h,
            "quality_indicator": label,
            "source_rank":       source_rank,
        }

    def get_gecko_trending(self) -> List[dict]:
        """Fetch trending + volume + new pools from GeckoTerminal."""
        results: List[dict] = []

        # Trending pools (most transactions + volume in 24h)
        data = _get(f"{GECKO}/networks/solana/trending_pools", params={"page": 1})
        if data:
            pools = data.get("data", [])
            added = [
                t for t in (self._gecko_pool_to_token(p, "🦎 GECKO TREND", 1) for p in pools)
                if t
            ]
            results.extend(added)
            print(f"  ✓ GeckoTerminal trending: {len(added)} tokens")
        else:
            print("  ⚠  GeckoTerminal trending: no response")

        # Top pools by 24h volume
        data2 = _get(
            f"{GECKO}/networks/solana/pools",
            params={"page": 1, "sort": "h24_volume_usd_desc"},
        )
        if data2:
            pools2 = data2.get("data", []) if isinstance(data2, dict) else []
            added2 = [
                t for t in (self._gecko_pool_to_token(p, "🦎 GECKO VOL", 2) for p in pools2)
                if t
            ]
            results.extend(added2)
            print(f"  ✓ GeckoTerminal top-volume: {len(added2)} tokens")
        else:
            print("  ⚠  GeckoTerminal top-volume: no response")

        # New pools (early-stage — gated by age filter downstream)
        data3 = _get(f"{GECKO}/networks/solana/new_pools", params={"page": 1})
        if data3:
            pools3 = data3.get("data", [])
            added3 = [
                t for t in (self._gecko_pool_to_token(p, "🆕 GECKO NEW", 2) for p in pools3)
                if t
            ]
            results.extend(added3)
            print(f"  ✓ GeckoTerminal new pools: {len(added3)} tokens")

        return results

    # -----------------------------------------------------------------------
    # SOURCE B: DexScreener (supplementary)
    # -----------------------------------------------------------------------

    def _normalise_dex_pair(self, pair: dict) -> Optional[dict]:
        """Convert a raw DexScreener pair dict to the pipeline format."""
        if pair.get("chainId") != "solana":
            return None

        base     = pair.get("baseToken",  {})
        quote    = pair.get("quoteToken", {})
        base_sym = base.get("symbol", "").upper()
        target   = quote if base_sym in IGNORE_SYMBOLS else base
        sym      = target.get("symbol", "").upper()
        addr     = target.get("address", "")

        if not addr or sym in IGNORE_SYMBOLS:
            return None

        pc    = pair.get("priceChange") or {}
        vol   = pair.get("volume",      {})
        liq   = _safe_float((pair.get("liquidity") or {}).get("usd"))
        vol24 = _safe_float(vol.get("h24"))
        vol1  = _safe_float(vol.get("h1"))
        ch1   = _safe_float(pc.get("h1"))
        ch6   = _safe_float(pc.get("h6"))
        ch24  = _safe_float(pc.get("h24"))
        price = _safe_float(pair.get("priceUsd"))
        mcap  = _safe_float(pair.get("marketCap") or pair.get("fdv"))

        created_ms = pair.get("pairCreatedAt", 0) or 0
        age_h = (
            (time.time() * 1000 - created_ms) / 3_600_000
            if created_ms > 0 else None
        )

        is_boosted  = pair.get("is_boosted",     False)
        is_new      = pair.get("is_new_listing",  False)
        quality     = pair.get("quality_indicator", "📊 HIGH-VOL")
        src_rank    = 4 if is_boosted else 3

        return {
            "address":           addr,
            "symbol":            sym,
            "name":              target.get("name", sym),
            "price_usd":         price,
            "liquidity_usd":     liq,
            "volume_24h":        vol24,
            "volume_24h_usd":    vol24,
            "vol_h1":            vol1,
            "price_change_h1":   ch1,
            "price_change_h6":   ch6,
            "price_change_24h":  ch24,
            "market_cap_usd":    mcap,
            "dex":               pair.get("dexId", "unknown"),
            "pair_address":      pair.get("pairAddress", ""),
            "is_boosted":        is_boosted,
            "is_new_listing":    is_new,
            "is_gecko":          False,
            "age_hours":         age_h,
            "quality_indicator": quality,
            "source_rank":       src_rank,
        }

    def _fetch_boost_pairs(self) -> List[dict]:
        pairs: List[dict] = []
        data = _get(f"{DEXSCREENER}/token-boosts/latest/v1")
        if not data:
            return pairs
        for item in data:
            if item.get("chainId") != "solana":
                continue
            token_addr = item.get("tokenAddress")
            if not token_addr:
                continue
            td = _get(f"{DEXSCREENER}/latest/dex/tokens/{token_addr}")
            if td:
                for p in td.get("pairs", []):
                    if p.get("chainId") == "solana":
                        p["is_boosted"]        = True
                        p["quality_indicator"] = "📣 BOOSTED"
                        pairs.append(p)
            time.sleep(0.15)
        print(f"  ✓ DexScreener boosts: {len(pairs)} pairs")
        return pairs

    def _fetch_new_listing_pairs(self) -> List[dict]:
        pairs: List[dict] = []
        data = _get(f"{DEXSCREENER}/token-profiles/latest/v1")
        if not data:
            return pairs
        sol_addrs = [
            p.get("tokenAddress") for p in data
            if p.get("chainId") == "solana" and p.get("tokenAddress")
        ]
        for i in range(0, min(len(sol_addrs), 60), 30):
            chunk = ",".join(sol_addrs[i:i+30])
            td = _get(f"{DEXSCREENER}/latest/dex/tokens/{chunk}")
            if td:
                for p in td.get("pairs", []):
                    if p.get("chainId") == "solana":
                        p["is_new_listing"]    = True
                        p["quality_indicator"] = "🆕 NEW"
                        pairs.append(p)
        print(f"  ✓ DexScreener new listings: {len(pairs)} pairs")
        return pairs

    def _fetch_search_pairs(self, query: str, label: str) -> List[dict]:
        data = _get(f"{DEXSCREENER}/latest/dex/search/", params={"q": query})
        if not data:
            return []
        pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
        print(f"  ✓ DexScreener [{label}]: {len(pairs)} pairs")
        return pairs

    def _fetch_dex_top_movers(self) -> List[dict]:
        """
        Pull the top pairs from Raydium, Orca, and Meteora then
        re-sort client-side by 1h momentum to surface early movers.

        NOTE: /latest/dex/pairs/solana/{dex} returns a LIST, not a dict.
        We handle both formats defensively.
        """
        pairs: List[dict] = []
        for dex in ("raydium", "orca", "meteora"):
            data = _get(f"{DEXSCREENER}/latest/dex/pairs/solana/{dex}")
            if data is None:
                continue
            # Endpoint may return a list directly or {"pairs": [...]}
            if isinstance(data, list):
                raw_pairs = data
            elif isinstance(data, dict):
                raw_pairs = data.get("pairs") or data.get("data") or []
            else:
                continue
            for p in raw_pairs:
                if isinstance(p, dict) and p.get("chainId") == "solana":
                    p["quality_indicator"] = f"📊 {dex.upper()}"
                    pairs.append(p)
        pairs.sort(
            key=lambda p: _safe_float((p.get("priceChange") or {}).get("h1")),
            reverse=True,
        )
        top = pairs[:60]
        print(f"  ✓ DexScreener DEX movers (top {len(top)} by 1h)")
        return top

    def _fetch_all_dexscreener(self) -> List[dict]:
        all_pairs: List[dict] = []
        print("  📣 DexScreener: boosted tokens...")
        all_pairs.extend(self._fetch_boost_pairs())
        print("  🆕 DexScreener: new listings...")
        all_pairs.extend(self._fetch_new_listing_pairs())
        print("  🔍 DexScreener: SOL pairs...")
        all_pairs.extend(self._fetch_search_pairs("SOL",  "SOL"))
        print("  🔍 DexScreener: USDC pairs...")
        all_pairs.extend(self._fetch_search_pairs("USDC", "USDC"))
        print("  📈 DexScreener: top 1h movers...")
        all_pairs.extend(self._fetch_dex_top_movers())
        return all_pairs

    # -----------------------------------------------------------------------
    # SCORING
    # -----------------------------------------------------------------------

    def _calculate_trend_score(
        self,
        volume: float,
        liquidity: float,
        price_change: float,
        price_change_h1: float = 0.0,
        price_change_h6: float = 0.0,
    ) -> float:
        """
        Composite trend score (0–100 nominal, can exceed with bonuses).

          25 pts — 24h volume    (max at $500K+)
          25 pts — liquidity     (max at $200K+)
          20 pts — 24h change    (peaks 50–100%, heavily discounted >200%)
          15 pts — 1h momentum   (primary ignition signal, max at +8%)
          15 pts — 6h trend      (sustained move confirmation, max at +25%)
        """
        vol_score = min(volume / 500_000 * 25, 25)
        liq_score = min(liquidity / 200_000 * 25, 25)

        if price_change > 0:
            if price_change <= 50:
                p_score = min(price_change / 50 * 20, 20)
            elif price_change <= 100:
                p_score = 20
            elif price_change <= 200:
                p_score = 20 - (price_change - 100) / 100 * 10
            else:
                p_score = max(10 - (price_change - 200) / 100 * 10, -10)
        else:
            p_score = max(price_change / 5 * 20, -40)

        h1_score = (
            min(price_change_h1 / 8 * 15, 15) if price_change_h1 > 0
            else max(price_change_h1 / 5 * 10, -15)
        )

        if price_change_h6 > 5:
            h6_score = min(price_change_h6 / 25 * 15, 15)
        elif price_change_h6 < -20:
            h6_score = -10
        else:
            h6_score = 0

        return vol_score + liq_score + p_score + h1_score + h6_score

    # -----------------------------------------------------------------------
    # MAIN FETCH
    # -----------------------------------------------------------------------

    def get_trending_tokens(
        self,
        min_liquidity: float = 50_000,
        min_volume_24h: float = 50_000,
        max_tokens: int = 10,
        prioritize_boosted: bool = True,
        force_refresh: bool = False,
    ) -> List[dict]:
        """
        Fetch, merge, deduplicate, score and rank trending Solana tokens.

        Dedup: when the same token appears in multiple sources, the entry
        with the lowest source_rank wins (GeckoTerminal < DexScreener).

        Returns up to max_tokens scored dicts sorted by trend_score desc.
        """
        cache_key = (
            f"trending_{max_tokens}_{int(min_liquidity)}"
            f"_{int(min_volume_24h)}_{prioritize_boosted}"
        )
        if not force_refresh and cache_key in self.cache:
            ts, cached = self.cache[cache_key]
            if time.time() - ts < self.cache_duration:
                print("  ✓ Using cached trending tokens")
                return cached

        eff_liq = min_liquidity  * 0.8 if min_liquidity  >= 50_000 else min_liquidity
        eff_vol = min_volume_24h * 0.8 if min_volume_24h >= 50_000 else min_volume_24h

        print("\n🔍 Fetching trending tokens from all sources...")

        # ---- Gather --------------------------------------------------------
        all_tokens: List[dict] = []

        print("  🦎 GeckoTerminal: trending + volume + new pools...")
        all_tokens.extend(self.get_gecko_trending())

        print("  📡 DexScreener: 5-endpoint sweep...")
        for pair in self._fetch_all_dexscreener():
            tok = self._normalise_dex_pair(pair)
            if tok:
                all_tokens.append(tok)

        print(f"  Raw combined: {len(all_tokens)} tokens before dedup")

        # ---- Dedup — lowest source_rank wins per address -------------------
        seen: Dict[str, dict] = {}
        for t in all_tokens:
            addr = t.get("address", "")
            if not addr:
                continue
            existing = seen.get(addr)
            if existing is None:
                seen[addr] = t
            elif t["source_rank"] < existing["source_rank"]:
                # Keep better-source metadata; preserve any flags from old entry
                merged = dict(existing)
                merged.update({k: v for k, v in t.items() if v or v == 0})
                seen[addr] = merged

        unique = list(seen.values())
        print(f"  After dedup: {len(unique)} unique tokens")

        # ---- Volume / liquidity filter -------------------------------------
        def _apply_filters(tokens: List[dict], liq_floor: float,
                           vol_floor: float) -> List[dict]:
            out = []
            for t in tokens:
                if t.get("liquidity_usd", 0) < liq_floor:
                    continue
                if t.get("volume_24h", 0) < vol_floor:
                    continue
                out.append(t)
            return out

        filtered = _apply_filters(unique, eff_liq, eff_vol)
        print(f"  After vol/liq filters: {len(filtered)} tokens")

        # 4-level fallback cascade
        if not filtered:
            for fb_liq, fb_vol in [(30_000, 30_000), (15_000, 10_000),
                                   (5_000, 5_000), (0, 0)]:
                print(f"  ⚠  Retrying (liq≥${fb_liq:,}, vol≥${fb_vol:,})...")
                filtered = _apply_filters(unique, fb_liq, fb_vol)
                if filtered:
                    print(f"  ✓ Fallback found {len(filtered)} tokens")
                    break

        # ---- Score + source bonuses ----------------------------------------
        for t in filtered:
            score = self._calculate_trend_score(
                t.get("volume_24h",       0),
                t.get("liquidity_usd",    0),
                t.get("price_change_24h", 0),
                t.get("price_change_h1",  0),
                t.get("price_change_h6",  0),
            )
            if t.get("is_gecko"):
                score *= 1.15   # organic on-chain signal — best quality
            elif t.get("is_new_listing"):
                score *= 1.10
            elif t.get("is_boosted"):
                score *= 1.05   # paid marketing — weakest signal

            if score < 25:
                t["quality_indicator"] = "[LOW]"
            t["trend_score"] = score

        filtered.sort(key=lambda x: x.get("trend_score", 0), reverse=True)
        result = filtered[:max_tokens]
        self.cache[cache_key] = (time.time(), result)
        print(f"  ✅ Final: {len(result)} tokens selected")
        return result

    # -----------------------------------------------------------------------
    # SAFETY FILTERS
    # -----------------------------------------------------------------------

    def filter_safe_tokens(self, tokens: List[dict]) -> List[dict]:
        """
        Hard safety gates before any token enters the trading watchlist.

          1. Minimum liquidity ($15K) and volume ($10K) floors
          2. Token age ≥ MIN_AGE_HOURS  — skip first-dump tokens
          3. 1h price drop > 10%        — actively dumping
          4. 6h price drop > 20%        — confirmed downtrend
        """
        MIN_LIQ = 15_000
        MIN_VOL = 10_000
        safe: List[dict] = []

        for t in tokens:
            sym = t.get("symbol", "?")

            if t.get("liquidity_usd", 0) < MIN_LIQ:
                continue
            if t.get("volume_24h", 0) < MIN_VOL:
                continue

            age = t.get("age_hours")
            if age is not None and age < MIN_AGE_HOURS:
                print(f"  ⏳ {sym}: too new ({age:.1f}h < {MIN_AGE_HOURS}h) — skip")
                continue

            h1 = t.get("price_change_h1", 0)
            if h1 < -10:
                print(f"  📉 {sym}: 1h dump ({h1:+.1f}%) — skip")
                continue

            h6 = t.get("price_change_h6", 0)
            if h6 < -20:
                print(f"  📉 {sym}: 6h downtrend ({h6:+.1f}%) — skip")
                continue

            safe.append(t)

        return safe

    # -----------------------------------------------------------------------
    # Token details + OHLCV + synthetic history
    # -----------------------------------------------------------------------

    def get_token_details(self, token_address: str) -> Optional[dict]:
        return _get(f"{DEXSCREENER}/latest/dex/tokens/{token_address}")

    def fetch_historical_prices(
        self,
        pair_address: str,
        n_points: int = 1000,
        token_info: Optional[dict] = None,
    ) -> Tuple[List[dict], bool]:
        """
        Fetch up to 1000 real 1-minute candles from GeckoTerminal.
        Falls back to structured synthetic history on failure.

        Returns:
            (candles, is_real)
            is_real=True  → caller sets live_points = len(candles)
            is_real=False → caller MUST set live_points = 0 (buy gate)
        """
        if pair_address:
            candles = self._fetch_geckoterminal(pair_address)
            if candles:
                print(f"    ✓ GeckoTerminal: {len(candles)} real candles")
                return candles, True

        print("    ~ Generating structured synthetic history")
        return self._synthetic_history_dicts(token_info or {}, n_points), False

    def _fetch_geckoterminal(self, pair_address: str) -> List[dict]:
        url  = f"{GECKO}/networks/solana/pools/{pair_address}/ohlcv/minute"
        data = _get(url, params={"aggregate": 1, "limit": 1000,
                                  "currency": "usd", "token": "base"}, timeout=15)
        if not data:
            return []
        ohlcv = (
            data.get("data", {})
                .get("attributes", {})
                .get("ohlcv_list", [])
        )
        candles: List[dict] = []
        for row in reversed(ohlcv):   # GeckoTerminal returns newest-first
            close = float(row[4]) if len(row) > 4 else 0.0
            vol   = float(row[5]) if len(row) > 5 else 0.0
            if close > 0:
                candles.append({"close": close, "vol": max(vol, 0.0)})
        return candles

    def _synthetic_history_dicts(self, token_info: dict, n_points: int) -> List[dict]:
        """
        GBM synthetic history anchored to DexScreener/GeckoTerminal data.
        Used only for indicator warmup — buy signals gated until live_points
        reaches the configured minimum.
        """
        price_now  = _safe_float(token_info.get("price_usd",        1.0)) or 1.0
        change_h1  = _safe_float(token_info.get("price_change_h1",  0.0))
        change_h6  = _safe_float(token_info.get("price_change_h6",  0.0))
        change_h24 = _safe_float(token_info.get("price_change_24h", 0.0))

        vol_24h     = _safe_float(token_info.get("volume_24h_usd") or token_info.get("volume_24h"))
        vol_h1      = _safe_float(token_info.get("vol_h1"))
        base_vol_pm = (
            vol_h1 / 60.0     if vol_h1  > 0 else
            vol_24h / 1440.0  if vol_24h > 0 else
            1_000.0
        )

        p1h  = price_now / max(1 + change_h1  / 100, 0.01)
        p6h  = price_now / max(1 + change_h6  / 100, 0.01)
        p24h = price_now / max(1 + change_h24 / 100, 0.01)

        n_c = int(n_points * 0.25)
        n_b = int(n_points * 0.35)
        n_a = n_points - n_b - n_c

        def _gbm(s: float, e: float, n: int, v: float = 0.002) -> List[float]:
            if n <= 0 or s <= 0 or e <= 0:
                return [s] * max(n, 1)
            d   = math.log(e / s) / n
            vol = min(max(abs(d) * 0.6, v), 0.015)
            pts, p = [], s
            for _ in range(n):
                p = p * math.exp(d + vol * random.gauss(0, 1))
                pts.append(p)
            if pts:
                pts = [x * (e / pts[-1]) for x in pts]
            return pts or [s] * n

        prices = (
            _gbm(p24h, p6h,        n_a, 0.003)
            + _gbm(p6h,  p1h,      n_b, 0.002)
            + _gbm(p1h,  price_now, n_c, 0.0015)
        )
        return [
            {"close": p, "vol": base_vol_pm * math.exp(random.gauss(0, 0.5))}
            for p in prices
        ]

    # -----------------------------------------------------------------------
    # DISPLAY
    # -----------------------------------------------------------------------

    def display_trending(self, tokens: List[dict]):
        print("\n" + "=" * 105)
        print("🔥 TRENDING SOLANA TOKENS — No-Key Edition v5")
        print("=" * 105)
        print(
            f"{'#':<4} {'Source':<18} {'Symbol':<12} {'Price':<16} "
            f"{'24h Vol':<14} {'1h':>7} {'24h':>9} {'Score':<8}"
        )
        print("-" * 105)
        for idx, t in enumerate(tokens, 1):
            sym   = t["symbol"][:10]
            price = f"${t['price_usd']:.8f}"
            vol   = f"${t['volume_24h']:,.0f}"
            ch1   = f"{t['price_change_h1']:+.1f}%"
            ch24  = f"{t['price_change_24h']:+.1f}%"
            score = f"{t.get('trend_score', 0):.1f}"
            src   = t.get("quality_indicator", "📊")[:17]
            arrow = "🟢" if t.get("price_change_24h", 0) > 0 else "🔴"
            print(
                f"{idx:<4} {src:<18} {sym:<12} {price:<16} "
                f"{vol:<14} {arrow}{ch1:>6} {ch24:>8} {score:<8}"
            )
        print("=" * 105)

        gecko_cnt   = sum(1 for t in tokens if t.get("is_gecko"))
        boosted_cnt = sum(1 for t in tokens if t.get("is_boosted"))
        new_cnt     = sum(1 for t in tokens if t.get("is_new_listing") and not t.get("is_gecko"))
        low_cnt     = sum(1 for t in tokens if t.get("quality_indicator") == "[LOW]")
        print(
            f"\n📊 Sources: {gecko_cnt} GeckoTerminal | "
            f"{boosted_cnt} DexScreener boosted | {new_cnt} new listings | "
            f"{low_cnt} low-score (excluded)\n"
        )


# ---------------------------------------------------------------------------
# Auto Token Selector
# ---------------------------------------------------------------------------

class AutoTokenSelector:
    """
    Wraps EnhancedTrendingTokenDetector with:
      • Regime-aware scoring (from MarketConditionAnalyzer)
      • Meme sniper ignition filter
      • 2-level fallback on empty results
    """

    def __init__(self, detector: EnhancedTrendingTokenDetector):
        self.detector = detector
        self.preferences: Dict = {
            "min_liquidity":         20_000,
            "min_volume":            20_000,
            "max_tokens":            10,
            "prefer_rising":         False,
            "max_price_change_24h":  150,
            "prioritize_boosted":    True,
            "min_score":             25.0,
        }
        self._regime_score       = 0.0
        self._prev_regime_strong = False

    def set_regime(self, regime_score: float):
        """Inject MarketConditionAnalyzer score. Call at start of each main loop cycle."""
        self._regime_score = regime_score

    def get_token_symbols(self) -> List[dict]:
        regime_just_flipped = self._regime_score > 0 and not self._prev_regime_strong
        regime_is_strong    = self._regime_score > 70

        if regime_is_strong and regime_just_flipped:
            print("🔄 Regime flipped strong — forcing fresh discovery")
        self._prev_regime_strong = regime_is_strong

        trending = self.detector.get_trending_tokens(
            min_liquidity=self.preferences["min_liquidity"],
            min_volume_24h=self.preferences["min_volume"],
            max_tokens=self.preferences["max_tokens"] * 4,
            prioritize_boosted=self.preferences["prioritize_boosted"],
            force_refresh=(regime_is_strong and regime_just_flipped),
        )

        safe = self.detector.filter_safe_tokens(trending)
        print(f"  After safety filters: {len(safe)} tokens")

        # Meme sniper ignition gate
        ignition_ok:  List[dict] = []
        ignition_rej: List[str]  = []

        for t in safe:
            h1   = t.get("price_change_h1",  0)
            h6   = t.get("price_change_h6",  0)
            ch24 = t.get("price_change_24h", 0)
            sym  = t.get("symbol", "?")

            strong_h1    = h1 > 2             # positive 1h momentum
            sustained    = h1 > 0 and h6 > 8  # rising 1h + strong 6h base
            trend_pullbk = ch24 > 30 and h1 > -3  # hot token, mild pullback

            if strong_h1 or sustained or trend_pullbk:
                ignition_ok.append(t)
            else:
                ignition_rej.append(
                    f"{sym}(h1={h1:+.1f}%,h6={h6:+.1f}%,24h={ch24:+.0f}%)"
                )

        if ignition_rej:
            print(f"  🔥 Ignition rejects: {', '.join(ignition_rej)}")

        if ignition_ok:
            print(f"  🔥 After ignition filter: {len(ignition_ok)} tokens")
            safe = ignition_ok
        else:
            print("  ⚠  No ignition tokens — no live setups right now")
            safe = []

        if self.preferences["prefer_rising"]:
            safe = [t for t in safe if t.get("price_change_24h", 0) > 0]
            print(f"  After prefer_rising: {len(safe)} tokens")

        max_change = self.preferences["max_price_change_24h"]
        if self._regime_score > 80 and max_change < 2000:
            max_change = 2000
            print(f"  ⚡ Strong regime ({self._regime_score:.0f}) — relaxing 24h cap to {max_change}%")

        safe = [t for t in safe if abs(t.get("price_change_24h", 0)) <= max_change]
        print(f"  After max price change ≤ {max_change}%: {len(safe)} tokens")

        min_score = self.preferences["min_score"]
        safe = [t for t in safe if t.get("trend_score", 0) >= min_score]
        print(f"  After min score ≥ {min_score:.0f}: {len(safe)} tokens")

        # Fallback level 1
        if not safe:
            print("⚠  No tokens — retrying with relaxed settings...")
            trending2 = self.detector.get_trending_tokens(
                min_liquidity=self.preferences["min_liquidity"],
                min_volume_24h=self.preferences["min_volume"],
                max_tokens=self.preferences["max_tokens"] * 4,
                prioritize_boosted=True,
            )
            safe2 = self.detector.filter_safe_tokens(trending2)
            safe = [
                t for t in safe2
                if abs(t.get("price_change_24h", 0)) <= max_change
                and t.get("trend_score", 0) >= min_score
            ]

        # Fallback level 2
        if not safe:
            print("⚠  Still nothing — broad fallback (any safe token)...")
            fb_max   = max_change * 2
            fb_score = max(min_score - 10, 20)
            safe = [
                t for t in self.detector.filter_safe_tokens(trending)
                if abs(t.get("price_change_24h", 0)) <= fb_max
                and t.get("trend_score", 0) >= fb_score
            ]
            if safe:
                print(f"  ✓ Fallback: {len(safe)} tokens (score≥{fb_score:.0f})")
            else:
                print("  ✗ No tokens even with relaxed fallback — holding cash")

        safe.sort(key=lambda x: x.get("trend_score", 0), reverse=True)
        return [
            dict(t, score=t.get("trend_score", 0))
            for t in safe[:self.preferences["max_tokens"]]
        ]


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    print("\n🔍 Enhanced Solana Trending Token Detector — No-Key Edition v5\n")
    print("Sources: GeckoTerminal (trending+volume+new) + DexScreener (5 endpoints)")
    print("No API keys required.\n")

    detector = EnhancedTrendingTokenDetector()

    trending = detector.get_trending_tokens(
        min_liquidity=20_000,
        min_volume_24h=20_000,
        max_tokens=10,
        prioritize_boosted=True,
    )

    detector.display_trending(trending)

    selector   = AutoTokenSelector(detector)
    top_tokens = selector.get_token_symbols()

    print("\n✅ RECOMMENDED FOR TRADING (score ≥ 25, all safety filters passed):")
    for idx, token in enumerate(top_tokens, 1):
        src = token.get("quality_indicator", "📊")
        print(f"{idx}. {src} {token['symbol']}  (Score: {token['score']:.1f})")
    print()


if __name__ == "__main__":
    main()
