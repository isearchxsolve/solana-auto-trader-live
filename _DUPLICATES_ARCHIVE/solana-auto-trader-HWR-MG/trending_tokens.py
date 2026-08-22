#!/usr/bin/env python3
"""
Enhanced Trending Token Detector — DexScreener Edition v4

Changes vs v3:
  - Removed ALL external API key dependencies (no Birdeye, no Jupiter).
  - Jupiter verification removed — was causing network failures and blocking
    tokens with the unverified-token liq<30k rule.
  - Wash-trade detection kept but threshold raised (>20x) to avoid
    killing legitimate high-volume meme tokens.
  - DexScreener sources expanded: WSOL, Raydium search, new listings,
    boost tokens, and Pump.fun graduation detection.
  - _merge_and_score replaced with simpler _score_and_filter operating
    directly on normalised DexScreener pairs — no cross-source lookup.
  - filter_safe_tokens: removed Jupiter-gate; liq floor lowered to $15k.
    solana_meme_trader.py enforces $35k at discovery — we just need enough
    candidates to survive its own filters.
  - Boost multiplier kept at 1.2x.
  - Pump.fun graduation bonus: +8 pts (early-move premium).
  - Age-based score bonus: tokens < 6h old get +5 pts (fresh launch).
  - AutoTokenSelector API identical to v2/v3 — drop-in compatible.

No environment variables or API keys needed.
"""

import time
import math
import random
import requests
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STABLE_COINS: Set[str] = {
    'SOL', 'WSOL', 'USDC', 'USDT', 'USDS', 'DAI', 'BUSD', 'TUSD', 'USD1',
    'USDH', 'USDCO', 'USDR',
}

DEXSCREENER = "https://api.dexscreener.com"
GECKO_BASE  = "https://api.geckoterminal.com/api/v2"

_HEADERS = {'User-Agent': 'Mozilla/5.0'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val or default)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

class EnhancedTrendingTokenDetector:
    """
    Multi-source trending token detector — DexScreener only edition.

    Sources (all free, no auth required):
      1. DexScreener WSOL pairs          — broad high-volume coverage
      2. DexScreener Raydium search      — Raydium-specific pairs
      3. DexScreener new listings        — token-profiles endpoint
      4. DexScreener boosted tokens      — paid promotions (lower weight)
      5. Pump.fun graduation detection   — Raydium pairs <48h old, liq>$20k
    """

    def __init__(self, birdeye_api_key: str = ""):
        # birdeye_api_key param kept for backwards-compat — silently ignored
        self.dexscreener_url = DEXSCREENER
        self.cache: Dict[str, tuple] = {}
        self.cache_duration = 60  # seconds

    # -----------------------------------------------------------------------
    # Source fetchers
    # -----------------------------------------------------------------------

    def _fetch_dexscreener_search(self, query: str, label: str) -> List[Dict]:
        try:
            resp = requests.get(
                f"{self.dexscreener_url}/latest/dex/search/?q={query}",
                headers=_HEADERS, timeout=10
            )
            if resp.status_code == 200:
                pairs = [p for p in resp.json().get('pairs', [])
                         if p.get('chainId') == 'solana']
                print(f"  ✓ {label}: {len(pairs)} pairs")
                return pairs
        except Exception as e:
            print(f"  ⚠️  {label} error: {e}")
        return []

    def _fetch_new_listings(self) -> List[Dict]:
        try:
            prof = requests.get(
                f"{self.dexscreener_url}/token-profiles/latest/v1",
                headers=_HEADERS, timeout=10
            )
            if prof.status_code != 200:
                return []
            sol_addrs = [
                p.get('tokenAddress') for p in prof.json()
                if p.get('chainId') == 'solana' and p.get('tokenAddress')
            ]
            if not sol_addrs:
                return []
            chunk = ",".join(sol_addrs[:30])
            pr = requests.get(
                f"{self.dexscreener_url}/latest/dex/tokens/{chunk}",
                headers=_HEADERS, timeout=10
            )
            if pr.status_code == 200:
                pairs = [p for p in pr.json().get('pairs', [])
                         if p.get('chainId') == 'solana']
                for p in pairs:
                    p['_is_new_listing'] = True
                print(f"  ✓ New listings: {len(pairs)} pairs")
                return pairs
        except Exception as e:
            print(f"  ⚠️  New listings error: {e}")
        return []

    def _fetch_boost_tokens(self) -> List[Dict]:
        result = []
        try:
            resp = requests.get(
                f"{self.dexscreener_url}/token-boosts/latest/v1",
                headers=_HEADERS, timeout=10
            )
            if resp.status_code != 200:
                return []
            boosted_addrs = [
                item.get('tokenAddress') for item in resp.json()
                if item.get('chainId') == 'solana' and item.get('tokenAddress')
            ][:20]
            for addr in boosted_addrs:
                try:
                    pr = requests.get(
                        f"{self.dexscreener_url}/latest/dex/tokens/{addr}",
                        headers=_HEADERS, timeout=8
                    )
                    if pr.status_code == 200:
                        for p in pr.json().get('pairs', []):
                            if p.get('chainId') == 'solana':
                                p['_is_boosted'] = True
                                result.append(p)
                    time.sleep(0.15)
                except Exception:
                    continue
            print(f"  ✓ Boosted tokens: {len(result)} pairs")
        except Exception as e:
            print(f"  ⚠️  Boost tokens error: {e}")
        return result

    def _get_all_raw_pairs(self) -> List[Dict]:
        all_pairs: List[Dict] = []

        print("  📊 WSOL pairs...")
        wsol = "So11111111111111111111111111111111111111112"
        all_pairs.extend(self._fetch_dexscreener_search(wsol, "WSOL"))

        print("  📊 Raydium pairs...")
        all_pairs.extend(self._fetch_dexscreener_search("raydium", "Raydium"))

        print("  🆕 New listings...")
        all_pairs.extend(self._fetch_new_listings())

        print("  📣 Boosted tokens...")
        all_pairs.extend(self._fetch_boost_tokens())

        # Tag Pump.fun graduations: Raydium pairs < 48h old with liq > $20k
        now_ms    = time.time() * 1000
        cutoff_ms = now_ms - (48 * 3_600_000)
        for p in all_pairs:
            if (
                p.get('dexId') == 'raydium'
                and _safe_float(p.get('pairCreatedAt')) >= cutoff_ms
                and _safe_float((p.get('liquidity') or {}).get('usd')) >= 20_000
            ):
                p['_is_pump_graduation'] = True

        graduated = sum(1 for p in all_pairs if p.get('_is_pump_graduation'))
        if graduated:
            print(f"  🎓 Pump.fun graduations detected: {graduated} pairs")

        return all_pairs

    # -----------------------------------------------------------------------
    # Normalise a raw DexScreener pair
    # -----------------------------------------------------------------------

    def _normalise_pair(self, pair: Dict) -> Optional[Dict]:
        try:
            base     = pair.get('baseToken') or {}
            quote    = pair.get('quoteToken') or {}
            base_sym = (base.get('symbol') or '').upper()
            target   = quote if base_sym in STABLE_COINS else base
            sym      = (target.get('symbol') or '').upper()
            addr     = (target.get('address') or '').strip()

            if not addr or not sym or sym in STABLE_COINS:
                return None

            liq    = _safe_float((pair.get('liquidity') or {}).get('usd'))
            vol    = _safe_float((pair.get('volume') or {}).get('h24'))
            vol_h1 = _safe_float((pair.get('volume') or {}).get('h1'))
            pc     = pair.get('priceChange') or {}
            ch1    = _safe_float(pc.get('h1'))
            ch6    = _safe_float(pc.get('h6'))
            ch24   = _safe_float(pc.get('h24'))
            price  = _safe_float(pair.get('priceUsd'))
            mcap   = _safe_float(pair.get('marketCap') or pair.get('fdv'))
            created_ms = _safe_float(pair.get('pairCreatedAt'))
            age_h  = ((time.time() * 1000 - created_ms) / 3_600_000
                      if created_ms > 0 else None)

            return {
                'address':             addr,
                'symbol':              sym,
                'name':                (target.get('name') or sym),
                'price_usd':           price,
                'liquidity_usd':       liq,
                'volume_24h':          vol,
                'volume_24h_usd':      vol,
                'vol_h1':              vol_h1,
                'volume_1h':           vol_h1,
                'price_change_h1':     ch1,
                'price_change_h6':     ch6,
                'price_change_24h':    ch24,
                'market_cap_usd':      mcap,
                'age_hours':           age_h,
                'dex':                 (pair.get('dexId') or 'unknown'),
                'pair_address':        (pair.get('pairAddress') or ''),
                'is_boosted':          bool(pair.get('_is_boosted')),
                'is_new_listing':      bool(pair.get('_is_new_listing')),
                'is_pump_graduation':  bool(pair.get('_is_pump_graduation')),
                # Compat stubs so downstream code doesn't KeyError
                'holder_count':        0,
                'trade_24h':           0,
                'is_jupiter_verified': False,
                'cross_source':        False,
                'source':              'dexscreener',
            }
        except Exception:
            return None

    # -----------------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------------

    def _calculate_trend_score(
        self,
        volume:             float,
        liquidity:          float,
        price_change:       float,
        price_change_h1:    float = 0.0,
        price_change_h6:    float = 0.0,
        is_pump_graduation: bool  = False,
        is_boosted:         bool  = False,
        age_hours:          Optional[float] = None,
    ) -> float:
        """
        Trend score (rough range 0–100, bonuses may push above).

        Components:
          Volume        25 pts  — on-chain activity
          Liquidity     25 pts  — depth / rug-safety proxy
          24h price     20 pts  — momentum (penalised if over-extended)
          1h momentum   15 pts  — early-move detection
          6h momentum   15 pts  — sustained trend confirmation

        Bonuses:
          Pump graduation  +8 pts  — highest-probability early-move window
          Token age <6h    +5 pts  — fresh launch premium
          Boosted          ×1.2   — applied last (marketing signal)

        Penalties:
          Wash-trade >20x vol/liq  −10 pts
        """
        vol_score = min(volume / 500_000 * 25, 25)
        liq_score = min(liquidity / 200_000 * 25, 25)

        if price_change > 0:
            if price_change <= 50:
                price_score_24h = min(price_change / 50 * 20, 20)
            elif price_change <= 100:
                price_score_24h = 20
            elif price_change <= 200:
                price_score_24h = 20 - (price_change - 100) / 100 * 10
            else:
                price_score_24h = max(10 - (price_change - 200) / 100 * 10, -10)
        else:
            price_score_24h = max(price_change / 5 * 20, -40)

        h1_score = (min(price_change_h1 / 10 * 15, 15) if price_change_h1 > 0
                    else max(price_change_h1 / 5 * 10, -15))

        if price_change_h6 > 0:
            h6_score = min(price_change_h6 / 30 * 15, 15)
        elif price_change_h6 < -20:
            h6_score = -10
        else:
            h6_score = 0

        score = vol_score + liq_score + price_score_24h + h1_score + h6_score

        if is_pump_graduation:
            score += 8
        if age_hours is not None and age_hours < 6:
            score += 5
        if liquidity > 0 and volume / liquidity > 20:
            score -= 10
        if is_boosted:
            score *= 1.2

        return score

    # -----------------------------------------------------------------------
    # Public: get_trending_tokens
    # -----------------------------------------------------------------------

    def get_trending_tokens(
        self,
        min_liquidity:      float = 15_000,
        min_volume_24h:     float = 20_000,
        max_tokens:         int   = 10,
        prioritize_boosted: bool  = True,
        force_refresh:      bool  = False,
    ) -> List[Dict]:
        cache_key = f"trending_{max_tokens}_{int(min_liquidity)}_{int(min_volume_24h)}"

        if not force_refresh and cache_key in self.cache:
            ts, data = self.cache[cache_key]
            if time.time() - ts < self.cache_duration:
                print("  ✓ Using cached trending tokens")
                return data

        # Adaptive threshold scaling — prevents over-filtering in slow markets
        if min_volume_24h >= 50_000:
            min_volume_24h *= 0.8
        if min_liquidity >= 50_000:
            min_liquidity *= 0.8

        print("Fetching trending tokens from all sources...")
        raw_pairs = self._get_all_raw_pairs()
        print(f"  Raw pairs fetched: {len(raw_pairs)}")

        seen:   Set[str]   = set()
        tokens: List[Dict] = []

        for pair in raw_pairs:
            t = self._normalise_pair(pair)
            if t is None:
                continue
            addr = t['address']

            if addr in seen:
                # If a later copy of the same token is a pump graduation, tag it
                for existing in tokens:
                    if existing['address'] == addr and pair.get('_is_pump_graduation'):
                        existing['is_pump_graduation'] = True
                continue
            seen.add(addr)

            if t['liquidity_usd'] < min_liquidity:
                continue
            if t['volume_24h'] < min_volume_24h:
                continue

            t['trend_score'] = self._calculate_trend_score(
                volume             = t['volume_24h'],
                liquidity          = t['liquidity_usd'],
                price_change       = t['price_change_24h'],
                price_change_h1    = t['price_change_h1'],
                price_change_h6    = t['price_change_h6'],
                is_pump_graduation = t['is_pump_graduation'],
                is_boosted         = t['is_boosted'],
                age_hours          = t['age_hours'],
            )

            score = t['trend_score']
            if t['is_pump_graduation']:
                t['quality_indicator'] = '🎓 GRADUATED'
            elif t['is_boosted'] and score >= 50:
                t['quality_indicator'] = '📣 BOOSTED'
            elif t['is_new_listing'] and score >= 50:
                t['quality_indicator'] = '🆕 NEW'
            elif score >= 70:
                t['quality_indicator'] = '📊 HIGH-VOL'
            elif score >= 50:
                t['quality_indicator'] = '📊 MID'
            else:
                t['quality_indicator'] = '[LOW]'

            tokens.append(t)

        print(f"  After normalise + min filters: {len(tokens)} tokens")
        tokens.sort(key=lambda x: x['trend_score'], reverse=True)

        if not tokens and (min_liquidity > 10_000 or min_volume_24h > 10_000):
            print("⚠️  No tokens found — retrying with lower thresholds...")
            return self.get_trending_tokens(
                min_liquidity  = 10_000,
                min_volume_24h = 10_000,
                max_tokens     = max_tokens,
                force_refresh  = True,
            )

        result = tokens[:max_tokens * 4]
        self.cache[cache_key] = (time.time(), result)
        return result

    # -----------------------------------------------------------------------
    # Safety filter
    # -----------------------------------------------------------------------

    def filter_safe_tokens(self, tokens: List[Dict]) -> List[Dict]:
        """
        Lightweight safety pass. Keeps candidate pool large enough for
        solana_meme_trader.py's own stricter gates to do their job.

        Removed vs v3: Jupiter unverified liq gate (was network-blocking all tokens).
        """
        safe = []
        MIN_LIQ = 15_000
        MIN_VOL = 10_000

        for t in tokens:
            liq = _safe_float(t.get('liquidity_usd'))
            vol = _safe_float(t.get('volume_24h'))
            h1  = _safe_float(t.get('price_change_h1'))
            sym = t.get('symbol', '?')

            if liq < MIN_LIQ or vol < MIN_VOL:
                continue
            if h1 < -20:
                print(f"  ⛔ {sym}: 1h dump ({h1:+.1f}%) — excluded")
                continue
            if liq > 0 and vol / liq > 20:
                print(f"  🚩 {sym}: vol/liq={vol/liq:.1f}x — likely wash trade")
                continue

            safe.append(t)

        return safe

    # -----------------------------------------------------------------------
    # Historical price fetching (unchanged from v2/v3)
    # -----------------------------------------------------------------------

    def get_token_details(self, token_address: str) -> Optional[Dict]:
        try:
            resp = requests.get(
                f"{self.dexscreener_url}/latest/dex/tokens/{token_address}",
                timeout=10
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            print(f"Error fetching token details: {e}")
            return None

    def fetch_historical_prices(
        self,
        pair_address: str,
        n_points:     int = 1000,
        token_info:   Optional[Dict] = None,
    ) -> Tuple[List[Dict], bool]:
        """
        Fetch historical price/volume candles for a token.

        Priority:
          1. GeckoTerminal OHLCV (real candles, best for signal engine)
          2. DexScreener chart endpoint (fallback if GT is rate-limited)
          3. Synthetic GBM history (last resort — always tradeable ATR)

        A 1.5-second inter-token delay is inserted before each GeckoTerminal
        call to reduce 429 rate-limit pressure when discovering multiple tokens
        in a single 60-second cycle.
        """
        if pair_address:
            # Slight delay to respect GeckoTerminal's rate limit across tokens
            time.sleep(1.5)
            candles = self._fetch_geckoterminal(pair_address)
            if candles:
                print(f"    ✓ GeckoTerminal: {len(candles)} real candles")
                return candles, True

            # DexScreener chart fallback
            ds_candles = self._fetch_dexscreener_chart(pair_address)
            if ds_candles:
                print(f"    ✓ DexScreener chart: {len(ds_candles)} candles")
                return ds_candles, True

        print("    ~ Generating structured synthetic history")
        synthetic = self._synthetic_history_dicts(token_info or {}, n_points)
        if token_info is not None:
            token_info["is_synthetic"] = True
        return synthetic, False

    def _fetch_dexscreener_chart(self, pair_address: str) -> List[Dict]:
        """
        DexScreener chart API fallback.
        Returns 1-hour candles as close+vol dicts, same format as GeckoTerminal.
        """
        try:
            url  = f"{self.dexscreener_url}/latest/dex/chart/solana/{pair_address}"
            resp = requests.get(url, headers=_HEADERS, timeout=10)
            if resp.status_code != 200:
                return []
            bars = resp.json().get("bars") or resp.json().get("candles") or []
            if not bars:
                return []
            candles = []
            for b in bars[-1000:]:
                close = _safe_float(b.get("c") or b.get("close"))
                vol   = _safe_float(b.get("v") or b.get("volume", 0))
                if close > 0:
                    candles.append({"close": close, "vol": max(vol, 0.0)})
            return candles
        except Exception:
            return []

    def _fetch_geckoterminal(self, pair_address: str) -> List[Dict]:
        """
        Fetch OHLCV candles from GeckoTerminal with retry + fallback timeframe.

        Strategy:
          1. Try 1-minute candles (limit=1000) — best granularity
          2. On 429 rate-limit: wait 12s and retry once
          3. On continued failure: try 5-minute candles (less rate-limited)
          4. Final fallback: hourly candles (always available)
          5. Always log the actual HTTP status — no more silent failures
        """
        headers = {"Accept": "application/json;version=20230302"}

        def _parse_ohlcv(resp_json: dict) -> List[Dict]:
            ohlcv = (resp_json
                     .get("data", {})
                     .get("attributes", {})
                     .get("ohlcv_list", []))
            candles = []
            for r in reversed(ohlcv or []):
                close = _safe_float(r[4] if len(r) > 4 else 0)
                vol   = _safe_float(r[5] if len(r) > 5 else 0)
                if close > 0:
                    candles.append({"close": close, "vol": max(vol, 0.0)})
            return candles

        def _fetch_tf(timeframe: str, aggregate: int, limit: int) -> List[Dict]:
            url    = f"{GECKO_BASE}/networks/solana/pools/{pair_address}/ohlcv/{timeframe}"
            params = {"aggregate": aggregate, "limit": limit,
                      "currency": "usd", "token": "base"}
            for attempt in range(2):
                try:
                    resp = requests.get(url, params=params, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        return _parse_ohlcv(resp.json())
                    elif resp.status_code == 429:
                        print(f"    ⚠️  GeckoTerminal 429 rate-limit — waiting 12s...")
                        time.sleep(12)
                    elif resp.status_code == 404:
                        print(f"    ⚠️  GeckoTerminal 404 — pool not yet indexed")
                        return []
                    else:
                        print(f"    ⚠️  GeckoTerminal HTTP {resp.status_code} ({timeframe}/{aggregate}min)")
                        return []
                except Exception as exc:
                    print(f"    ⚠️  GeckoTerminal error ({timeframe}): {exc}")
                    if attempt == 0:
                        time.sleep(3)
            return []

        # Attempt 1: 1-minute candles (best granularity)
        candles = _fetch_tf("minute", 1, 1000)
        if candles:
            return candles

        # Attempt 2: 5-minute candles (lower rate-limit pressure)
        print("    ~ Retrying with 5-minute candles...")
        time.sleep(2)
        candles = _fetch_tf("minute", 5, 500)
        if candles:
            return candles

        # Attempt 3: hourly candles (always available, coarser ATR)
        print("    ~ Retrying with hourly candles...")
        time.sleep(2)
        return _fetch_tf("hour", 1, 168)

    def _synthetic_history_dicts(self, token_info: Dict, n_points: int) -> List[Dict]:
        """
        Generate structured synthetic OHLCV history from DexScreener metadata.

        CRITICAL: Synthetic ATR must match real meme coin physics (3-12% per candle
        on 1-minute bars) or the R:R gate will always fail, blocking all trades.
        The old cap of vol=0.015 produced ATR~0.1%, which is an order of magnitude
        too low. Meme coins are volatile instruments — the synthetic history must
        reflect that or the signal engine can never score a tradeable setup.

        ATR calibration:
          - Meme coin 1-min ATR is typically 0.3-2% per bar
          - We target a GBM step-vol that produces ~0.5-1.5% bar-to-bar moves
          - This is achieved with base_vol=0.008 (0.8%) scaling with 24h change
        """
        price_now  = _safe_float(token_info.get("price_usd"), 1.0) or 1.0
        change_h1  = _safe_float(token_info.get("price_change_h1"))
        change_h6  = _safe_float(token_info.get("price_change_h6"))
        change_h24 = _safe_float(token_info.get("price_change_24h"))

        vol_24h = _safe_float(token_info.get("volume_24h_usd") or
                               token_info.get("volume_24h"))
        vol_h1  = _safe_float(token_info.get("vol_h1") or
                               token_info.get("volume_1h"))
        base_vol_pm = (vol_h1 / 60.0 if vol_h1 > 0
                       else vol_24h / (24.0 * 60.0) if vol_24h > 0
                       else 1_000.0)

        p1h  = price_now / max(1 + change_h1  / 100, 0.01)
        p6h  = price_now / max(1 + change_h6  / 100, 0.01)
        p24h = price_now / max(1 + change_h24 / 100, 0.01)

        n_c = int(n_points * 0.25)
        n_b = int(n_points * 0.35)
        n_a = n_points - n_b - n_c

        # Volatility calibrated to real meme coin ATR on 1-minute bars.
        # Meme coins show 0.3-2% bar-to-bar moves; base_vol=0.008 (0.8%) with
        # scaling for high-24h-change tokens achieves this range.
        # OLD code: vol = min(abs(d) * 0.6, 0.015) → always ~0.001 on stable tokens
        # NEW code: floor of 0.006 ensures ATR is never lower than ~0.5%
        abs_24h = abs(change_h24)
        if abs_24h > 200:
            base_step_vol = 0.018   # very volatile token — 1.8% per bar
        elif abs_24h > 100:
            base_step_vol = 0.013   # high momentum — 1.3% per bar
        elif abs_24h > 50:
            base_step_vol = 0.010   # normal meme — 1.0% per bar
        elif abs_24h > 20:
            base_step_vol = 0.008   # moderate — 0.8% per bar
        else:
            base_step_vol = 0.006   # quiet meme — 0.6% per bar (floor)

        def _gbm(s: float, e: float, n: int) -> List[float]:
            if n <= 0 or s <= 0 or e <= 0:
                return [s] * max(n, 1)
            d   = math.log(e / s) / n
            # Ensure per-bar vol is always at least base_step_vol (the meme floor)
            vol = max(base_step_vol, abs(d) * 0.5)
            pts, p = [], s
            for _ in range(n):
                p = p * math.exp(d + vol * random.gauss(0, 1))
                pts.append(p)
            if pts:
                scale = e / pts[-1]
                pts   = [x * scale for x in pts]
            return pts or [s] * n

        prices = (
            _gbm(p24h, p6h,      n_a)
            + _gbm(p6h,  p1h,    n_b)
            + _gbm(p1h,  price_now, n_c)
        )
        # Volume: lognormal with higher variance to simulate realistic meme vol bursts
        return [{"close": p, "vol": base_vol_pm * math.exp(random.gauss(0, 0.7))}
                for p in prices]

    # -----------------------------------------------------------------------
    # Display
    # -----------------------------------------------------------------------

    def display_trending(self, tokens: List[Dict]):
        print("\n" + "=" * 100)
        print("🔥 TRENDING SOLANA TOKENS — DexScreener Edition v4")
        print("=" * 100)
        print(f"{'#':<4} {'Type':<14} {'Symbol':<12} {'Price':<15} "
              f"{'24h Vol':<14} {'Liq':<12} {'1h':<8} {'24h':<10} Score")
        print("-" * 100)

        for idx, token in enumerate(tokens, 1):
            sym   = token['symbol'][:10]
            price = f"${token['price_usd']:.8f}"
            vol   = f"${token['volume_24h']:,.0f}"
            liq   = f"${token['liquidity_usd']:,.0f}"
            ch1   = f"{token['price_change_h1']:+.1f}%"
            ch24  = f"{token['price_change_24h']:+.1f}%"
            score = f"{token['trend_score']:.1f}"
            qual  = token.get('quality_indicator', '📊')
            ind   = "🟢" if token['price_change_24h'] > 0 else "🔴"

            print(f"{idx:<4} {qual:<14} {sym:<12} {price:<15} "
                  f"{vol:<14} {liq:<12} {ch1:<8} {ind}{ch24:<9} {score}")

        print("=" * 100)
        boosted = sum(1 for t in tokens if t.get('is_boosted'))
        new_lst = sum(1 for t in tokens if t.get('is_new_listing'))
        grad    = sum(1 for t in tokens if t.get('is_pump_graduation'))
        low     = sum(1 for t in tokens if t.get('quality_indicator') == '[LOW]')
        print(f"\n📊 {len(tokens)} tokens | {boosted} boosted | {new_lst} new | "
              f"{grad} pump-graduated | {low} low-score")
        print()


# ---------------------------------------------------------------------------
# Auto token selector — identical API to v2/v3, drop-in compatible
# ---------------------------------------------------------------------------

class AutoTokenSelector:

    def __init__(self, detector: EnhancedTrendingTokenDetector):
        self.detector    = detector
        self.preferences = {
            'min_liquidity':            20_000,
            'min_volume':               20_000,
            'max_tokens':               10,
            'prefer_rising':            False,
            'avoid_extreme_volatility': False,
            'max_price_change_24h':     150,
            'prioritize_boosted':       True,
            'min_score':                50.0,
        }
        self._regime_score       = 0
        self._prev_regime_strong = False

    def set_regime(self, regime_score: float):
        self._regime_score = regime_score

    def get_token_symbols(self) -> List[Dict]:
        regime_just_flipped = (
            self._regime_score > 0
            and not getattr(self, '_prev_regime_strong', False)
        )
        regime_is_strong = self._regime_score > 70
        if regime_is_strong and regime_just_flipped:
            print("🔄 Regime flipped strong — forcing fresh discovery")
        self._prev_regime_strong = regime_is_strong

        trending = self.detector.get_trending_tokens(
            min_liquidity      = self.preferences['min_liquidity'],
            min_volume_24h     = self.preferences['min_volume'],
            max_tokens         = self.preferences['max_tokens'] * 4,
            prioritize_boosted = self.preferences['prioritize_boosted'],
            force_refresh      = (regime_is_strong and regime_just_flipped),
        )

        safe = self.detector.filter_safe_tokens(trending)
        print(f"  After safety filters: {len(safe)} tokens")

        # Ignition filter
        ignition_candidates = []
        ignition_rejects    = []

        for t in safe:
            h1   = _safe_float(t.get("price_change_h1"))
            h6   = _safe_float(t.get("price_change_h6"))
            ch24 = _safe_float(t.get("price_change_24h"))
            sym  = t.get("symbol", "?")

            strong_h1    = h1 > 3
            sustained    = h1 > 1 and h6 > 5
            trend_pullbk = ch24 > 30 and h1 > -3
            grad_pass    = t.get('is_pump_graduation', False) and ch24 > 20 and h1 > -5
            # NEW: early accumulation — strong 24h trend still intact (h6>0 = 6h still green),
            # normal consolidation pullback in h1 (not a dump). This is the ideal entry zone:
            # price moved significantly over 24h but is resting/pulling back in the last hour.
            # The previous filter required h1>3% which forced entry DURING the pump (RSI 70+)
            # and eliminated the consolidation phase (the correct low-risk entry point).
            early_accum  = ch24 > 20 and h6 > 0 and h1 > -8

            if strong_h1 or sustained or trend_pullbk or grad_pass or early_accum:
                ignition_candidates.append(t)
            else:
                ignition_rejects.append(f"{sym}(h1={h1:+.1f}%,h6={h6:+.1f}%,24h={ch24:+.0f}%)")

        if ignition_rejects:
            print(f"  🔥 Ignition rejects: {', '.join(ignition_rejects)}")

        if ignition_candidates:
            print(f"  🔥 After ignition filter: {len(ignition_candidates)} tokens")
            safe = ignition_candidates
        else:
            print("  ⚠️  No ignition tokens — skipping dead consolidations")
            safe = []

        if self.preferences['prefer_rising']:
            safe = [t for t in safe if _safe_float(t.get('price_change_24h')) > 0]

        max_change   = self.preferences['max_price_change_24h']
        regime_score = self._regime_score

        if regime_score > 80 and max_change < 2000:
            max_change = 2000
            print(f"  ⚡ Strong regime ({regime_score:.0f}) — relaxing 24h cap to {max_change}%")

        safe = [t for t in safe if abs(_safe_float(t.get('price_change_24h'))) <= max_change]
        print(f"  After max price change ≤ {max_change}%: {len(safe)} tokens")

        min_score = self.preferences['min_score']
        safe = [t for t in safe if _safe_float(t.get('trend_score')) >= min_score]
        print(f"  After min score ≥ {min_score:.0f}: {len(safe)} tokens")

        # Fallback 1
        if not safe:
            print("⚠️  No tokens after filters — retrying with relaxed settings...")
            trending2 = self.detector.get_trending_tokens(
                min_liquidity      = self.preferences['min_liquidity'],
                min_volume_24h     = self.preferences['min_volume'],
                max_tokens         = self.preferences['max_tokens'] * 4,
                prioritize_boosted = True,
            )
            safe2 = self.detector.filter_safe_tokens(trending2)
            safe2 = [t for t in safe2
                     if abs(_safe_float(t.get('price_change_24h'))) <= max_change
                     and _safe_float(t.get('trend_score')) >= min_score]
            if safe2:
                safe = safe2

        # Fallback 2
        if not safe:
            print("⚠️  Still no tokens — falling back with relaxed thresholds...")
            fallback_max = max_change * 2
            fallback_min = max(self.preferences['min_score'] - 15, 25)
            all_safe     = self.detector.filter_safe_tokens(trending)
            safe = [t for t in all_safe
                    if abs(_safe_float(t.get('price_change_24h'))) <= fallback_max
                    and _safe_float(t.get('trend_score')) >= fallback_min]
            if safe:
                print(f"  ✓ Fallback: {len(safe)} tokens "
                      f"(≤{fallback_max:.0f}% 24h, score≥{fallback_min:.0f})")
            else:
                print("  ✗ No tokens — holding cash")

        safe.sort(key=lambda x: _safe_float(x.get('trend_score')), reverse=True)
        return [dict(t, score=t['trend_score'])
                for t in safe[:self.preferences['max_tokens']]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Solana trending token detector — DexScreener edition v4"
    )
    parser.add_argument("--min-liq", type=float, default=20_000)
    parser.add_argument("--min-vol", type=float, default=20_000)
    parser.add_argument("--max",     type=int,   default=10)
    parser.add_argument("--birdeye-key", default="")   # ignored, kept for compat
    args = parser.parse_args()

    print("\n🔍 Solana Trending Token Detector — DexScreener Edition v4\n")

    detector   = EnhancedTrendingTokenDetector()
    trending   = detector.get_trending_tokens(
        min_liquidity=args.min_liq, min_volume_24h=args.min_vol,
        max_tokens=args.max, prioritize_boosted=True,
    )
    detector.display_trending(trending)

    selector   = AutoTokenSelector(detector)
    top_tokens = selector.get_token_symbols()

    print("\n✅ RECOMMENDED FOR TRADING:")
    for idx, token in enumerate(top_tokens, 1):
        qual = token.get('quality_indicator', '📊')
        age  = f"| {token['age_hours']:.1f}h old" if token.get('age_hours') else ""
        grad = "| 🎓 GRADUATED" if token.get('is_pump_graduation') else ""
        print(f"  {idx}. {qual} {token['symbol']} (Score: {token['score']:.1f}) {age} {grad}")
    print()


if __name__ == "__main__":
    main()
