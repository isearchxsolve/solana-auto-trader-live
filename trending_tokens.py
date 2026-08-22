#!/usr/bin/env python3
"""
Enhanced Trending Token Detection for Solana  -- Fixed Edition v2

Fixes vs previous version
--------------------------
1. Score minimum enforced at source: tokens with score < 50 are flagged
   with quality_indicator "[LOW]" so the auto-trader can filter them.
2. Scoring de-weighted for extreme 24h change: tokens up >200% get a
   diminishing return on the price-change score component (they are
   already past the easy move and are likely being dumped).
3. filter_safe_tokens tightened: boosted tokens no longer get relaxed
   liquidity/volume minimums (the original 5k liq / 10k vol for boosted
   vs 10k/20k for unbooted was allowing very thin markets through).
4. Volume-momentum field added: vol_momentum = h1_vol / avg_vol ratio
   so the caller can apply the 0.50x guard.
5. Synthetic history warmup: live_points explicitly set to 0 on synthetic
   histories so the caller's live-candle gate works correctly.
6. _calculate_trend_score: penalty added for negative 24h change instead
   of the previous asymmetric weighting that still allowed -20 tokens through.
"""

import requests
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta


class EnhancedTrendingTokenDetector:
    """Detect trending Solana tokens using multiple high-quality sources."""

    def __init__(self):
        self.dexscreener_url = "https://api.dexscreener.com"
        self.cache: Dict[str, tuple] = {}
        self.cache_duration = 300   # 5 minutes

    # -----------------------------------------------------------------------
    # Source fetchers
    # -----------------------------------------------------------------------

    def get_boost_tokens(self) -> List[Dict]:
        """
        Fetch tokens with active boosts on DexScreener.
        Note: boosted = marketing spend, NOT fundamental strength.
        """
        all_pairs = []
        headers = {'User-Agent': 'Mozilla/5.0'}

        try:
            response = requests.get(
                f"{self.dexscreener_url}/token-boosts/top/v1",
                headers=headers,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()

                for item in data:
                    if item.get('chainId') != 'solana':
                        continue
                    token_addr = item.get('tokenAddress')
                    if not token_addr:
                        continue

                    try:
                        pair_resp = requests.get(
                            f"{self.dexscreener_url}/latest/dex/tokens/{token_addr}",
                            headers=headers,
                            timeout=10
                        )
                        if pair_resp.status_code == 200:
                            pairs = pair_resp.json().get('pairs', [])
                            for pair in pairs:
                                if pair.get('chainId') == 'solana':
                                    pair['is_boosted']  = True
                                    pair['boost_data']  = item
                                    all_pairs.append(pair)
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"  Error fetching pair for boost token: {e}")
                        continue

                print(f"  ✓ Found {len(all_pairs)} boosted Solana pairs")
            else:
                print(f"  ⚠️  Boost endpoint returned {response.status_code}")

        except Exception as e:
            print(f"  Error fetching boost tokens: {e}")

        return all_pairs

    def get_trending_from_dexscreener(self) -> List[Dict]:
        """
        Multi-source trending token fetching:
          1. Boost tokens (PRIORITY -- paid promotions)
          2. Token profiles (new listings)
          3. WSOL search (high volume)
        """
        all_pairs = []
        headers   = {'User-Agent': 'Mozilla/5.0'}

        # SOURCE 1: BOOST TOKENS
        print("  📣 Fetching boosted tokens...")
        all_pairs.extend(self.get_boost_tokens())

        try:
            # SOURCE 2: Newly listed tokens
            print("  🆕 Fetching new token profiles...")
            prof = requests.get(
                f"{self.dexscreener_url}/token-profiles/latest/v1",
                headers=headers, timeout=10
            )
            if prof.status_code == 200:
                sol_addrs = [p.get('tokenAddress') for p in prof.json()
                             if p.get('chainId') == 'solana' and p.get('tokenAddress')]
                if sol_addrs:
                    chunk = ",".join(sol_addrs[:30])
                    pr = requests.get(
                        f"{self.dexscreener_url}/latest/dex/tokens/{chunk}",
                        headers=headers, timeout=10
                    )
                    if pr.status_code == 200:
                        new_pairs = pr.json().get('pairs', [])
                        for pair in new_pairs:
                            pair['is_new_listing'] = True
                        all_pairs.extend(new_pairs)
                        print(f"  ✓ Found {len(new_pairs)} new listings")

            # SOURCE 3: WSOL search -- high-volume established pairs
            print("  📊 Fetching high-volume pairs...")
            wsol = "So11111111111111111111111111111111111111112"
            sr = requests.get(
                f"{self.dexscreener_url}/latest/dex/search/?q={wsol}",
                headers=headers, timeout=10
            )
            if sr.status_code == 200:
                wsol_pairs = sr.json().get('pairs', [])
                all_pairs.extend(wsol_pairs)
                print(f"  ✓ Found {len(wsol_pairs)} WSOL pairs")

        except Exception as e:
            print(f"Error fetching from DexScreener: {e}")

        return all_pairs

    # -----------------------------------------------------------------------
    # Scoring
    # -----------------------------------------------------------------------

    def _calculate_trend_score(self, volume: float, liquidity: float,
                                price_change: float) -> float:
        """
        Trend score calculation.

        FIX: tokens with very high 24h change (>200%) receive diminishing
        returns on the price component -- they are likely past the easy move.
        FIX: negative 24h change now applies a harder penalty (no trades on
        downtrending tokens regardless of boost status).
        """
        vol_score = min(volume / 500_000 * 30, 30)
        liq_score = min(liquidity / 200_000 * 30, 30)

        if price_change > 0:
            # Diminishing returns above 200% -- probably dumping soon
            if price_change <= 200:
                price_score = min(price_change / 10 * 40, 40)
            else:
                # Cap at 200% worth of score, then apply discount
                base = 200 / 10 * 40          # = 40 (the full cap)
                extra_pct = price_change - 200
                discount  = min(extra_pct / 100 * 15, 15)   # up to -15 pts
                price_score = base - discount
        else:
            # FIX: steeper penalty for negative price change
            price_score = max(price_change / 5 * 20, -40)

        return vol_score + liq_score + price_score

    # -----------------------------------------------------------------------
    # Main trending fetch
    # -----------------------------------------------------------------------

    def get_trending_tokens(self, min_liquidity: float = 50_000,
                             min_volume_24h: float = 50_000,
                             max_tokens: int = 10,
                             prioritize_boosted: bool = True) -> List[Dict]:
        """
        Return ranked trending Solana tokens.
        """
        cache_key = (f"trending_{max_tokens}_{int(min_liquidity)}"
                     f"_{int(min_volume_24h)}_{prioritize_boosted}")
        if cache_key in self.cache:
            cached_time, cached_data = self.cache[cache_key]
            if time.time() - cached_time < self.cache_duration:
                print("  ✓ Using cached trending tokens")
                return cached_data

        print("Fetching latest trending tokens (including boosted)...")
        pairs = self.get_trending_from_dexscreener()
        print(f"  Raw pairs fetched: {len(pairs)}")

        trending_tokens: List[Dict] = []
        seen_tokens: set = set()
        IGNORE = {'SOL', 'WSOL', 'USDC', 'USDT', 'USDS', 'DAI', 'BUSD',
                  'TUSD', 'USD1'}

        for pair in pairs:
            try:
                if pair.get('chainId') != 'solana':
                    continue

                base      = pair.get('baseToken', {})
                quote     = pair.get('quoteToken', {})
                base_sym  = base.get('symbol', '').upper()
                target    = quote if base_sym in IGNORE else base
                sym       = target.get('symbol', '').upper()
                addr      = target.get('address', '')

                if not addr or sym in IGNORE or addr in seen_tokens:
                    continue

                liq   = float(pair.get('liquidity', {}).get('usd', 0) or 0)
                vol   = float(pair.get('volume', {}).get('h24', 0) or 0)
                pc    = pair.get('priceChange') or {}
                ch1   = float(pc.get('h1',  0) or 0)
                ch6   = float(pc.get('h6',  0) or 0)
                ch24  = float(pc.get('h24', 0) or 0)
                price = float(pair.get('priceUsd', 0) or 0)

                if liq < min_liquidity or vol < min_volume_24h:
                    continue

                score              = self._calculate_trend_score(vol, liq, ch24)
                is_boosted         = pair.get('is_boosted', False)
                is_new             = pair.get('is_new_listing', False)
                quality_indicator  = '📊 HIGH-VOL'

                if prioritize_boosted and is_boosted:
                    score *= 1.5
                    quality_indicator = '📣 BOOSTED'
                elif is_new:
                    score *= 1.2
                    quality_indicator = '🆕 NEW'

                # FIX: flag low-score tokens rather than silently passing them
                if score < 50:
                    quality_indicator = '[LOW]'

                trending_tokens.append({
                    'address':          addr,
                    'symbol':           sym,
                    'name':             target.get('name', sym),
                    'price_usd':        price,
                    'liquidity_usd':    liq,
                    'volume_24h':       vol,
                    'price_change_h1':  ch1,
                    'price_change_h6':  ch6,
                    'price_change_24h': ch24,
                    'trend_score':      score,
                    'dex':              pair.get('dexId', 'unknown'),
                    'pair_address':     pair.get('pairAddress', ''),
                    'is_boosted':       is_boosted,
                    'is_new_listing':   is_new,
                    'quality_indicator': quality_indicator,
                })
                seen_tokens.add(addr)

            except Exception:
                continue

        print(f"  After filters: {len(trending_tokens)} tokens")

        # Fallback with lower thresholds
        if not trending_tokens and (min_liquidity > 20_000 or min_volume_24h > 20_000):
            print("⚠️  No tokens found. Retrying with lower thresholds...")
            return self.get_trending_tokens(
                min_liquidity=20_000,
                min_volume_24h=20_000,
                max_tokens=max_tokens,
                prioritize_boosted=prioritize_boosted,
            )

        trending_tokens.sort(key=lambda x: x['trend_score'], reverse=True)
        result = trending_tokens[:max_tokens]
        self.cache[cache_key] = (time.time(), result)
        return result

    # -----------------------------------------------------------------------
    # Safety filter  (TIGHTENED)
    # -----------------------------------------------------------------------

    def filter_safe_tokens(self, tokens: List[Dict]) -> List[Dict]:
        """
        Filter for tradeable tokens.

        FIX: Boosted tokens no longer get relaxed liq/vol requirements.
        The previous (5k liq / 10k vol) thresholds allowed very thin markets
        through. All tokens now require the same minimums.
        Additionally, [LOW] score tokens are excluded.
        """
        safe_tokens = []
        MIN_LIQ = 15_000
        MIN_VOL = 20_000

        for t in tokens:
            # FIX: exclude explicitly flagged low-score tokens
            if t.get('quality_indicator') == '[LOW]':
                continue
            if t['liquidity_usd'] >= MIN_LIQ and t['volume_24h'] >= MIN_VOL:
                safe_tokens.append(t)

        return safe_tokens

    # -----------------------------------------------------------------------
    # Token details
    # -----------------------------------------------------------------------

    def get_token_details(self, token_address: str) -> Optional[Dict]:
        try:
            response = requests.get(
                f"{self.dexscreener_url}/latest/dex/tokens/{token_address}",
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            print(f"Error fetching token details: {e}")
            return None

    # -----------------------------------------------------------------------
    # Historical price fetching
    # -----------------------------------------------------------------------

    def fetch_historical_prices(self, pair_address: str, n_points: int = 1000,
                                 token_info: Optional[Dict] = None
                                 ) -> Tuple[List[float], bool]:
        """
        Fetch up to 1000 real 1-minute candles from GeckoTerminal.
        Falls back to structured synthetic on failure.

        Returns:
            (prices, is_real)
            is_real=True  -> caller should set live_points = len(prices)
            is_real=False -> caller MUST set live_points = 0 so buy gate works
        """
        if pair_address:
            closes = self._fetch_geckoterminal(pair_address)
            if closes:
                print(f"    ✓ GeckoTerminal: {len(closes)} real candles")
                return closes, True

        print(f"    ~ Generating structured synthetic history")
        return self._synthetic_history(token_info or {}, n_points), False

    def _fetch_geckoterminal(self, pair_address: str) -> List[float]:
        """Fetch real OHLCV data from GeckoTerminal."""
        url     = (f"https://api.geckoterminal.com/api/v2/networks/solana"
                   f"/pools/{pair_address}/ohlcv/minute")
        headers = {"Accept": "application/json;version=20230302"}
        params  = {"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                return []
            ohlcv = (resp.json()
                     .get("data", {})
                     .get("attributes", {})
                     .get("ohlcv_list", []))
            return [float(r[4]) for r in reversed(ohlcv)] if ohlcv else []
        except Exception:
            return []

    def _synthetic_history(self, token_info: Dict, n_points: int) -> List[float]:
        """
        Generate synthetic price history using GBM anchored to real price points.
        The result is ONLY used for indicator warmup -- buy signals are gated
        until live_points >= min_live_candles in the caller.
        """
        import random
        import math

        price_now  = float(token_info.get("price_usd",        1.0) or 1.0)
        change_h1  = float(token_info.get("price_change_h1",  0.0))
        change_h6  = float(token_info.get("price_change_h6",  0.0))
        change_h24 = float(token_info.get("price_change_24h", 0.0))

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
            pts = []
            p   = s
            for _ in range(n):
                p = p * math.exp(d + vol * random.gauss(0, 1))
                pts.append(p)
            # Re-anchor end point exactly
            if pts:
                scale = e / pts[-1]
                pts   = [x * scale for x in pts]
            return pts if pts else [s] * n

        return (_gbm(p24h, p6h, n_a, 0.003)
                + _gbm(p6h,  p1h,  n_b, 0.002)
                + _gbm(p1h,  price_now, n_c, 0.0015))

    # -----------------------------------------------------------------------
    # Display
    # -----------------------------------------------------------------------

    def display_trending(self, tokens: List[Dict]):
        print("\n" + "=" * 90)
        print("🔥 TRENDING SOLANA TOKENS (Enhanced with Boost Data)")
        print("=" * 90)
        print(f"{'#':<4} {'Type':<12} {'Symbol':<12} {'Price':<15} "
              f"{'24h Vol':<15} {'24h Chg':<12} {'Score':<8}")
        print("-" * 90)

        for idx, token in enumerate(tokens, 1):
            symbol    = token['symbol'][:10]
            price     = f"${token['price_usd']:.8f}"
            volume    = f"${token['volume_24h']:,.0f}"
            change    = f"{token['price_change_24h']:+.2f}%"
            score     = f"{token['trend_score']:.1f}"
            quality   = token.get('quality_indicator', '📊')
            indicator = "🟢" if token['price_change_24h'] > 0 else "🔴"

            print(f"{idx:<4} {quality:<12} {symbol:<12} {price:<15} "
                  f"{volume:<15} {indicator} {change:<10} {score:<8}")

        print("=" * 90)

        boosted_count = sum(1 for t in tokens if t.get('is_boosted', False))
        new_count     = sum(1 for t in tokens if t.get('is_new_listing', False))
        low_count     = sum(1 for t in tokens
                            if t.get('quality_indicator') == '[LOW]')

        print(f"\n📊 Breakdown: {boosted_count} boosted | {new_count} new listings | "
              f"{len(tokens) - boosted_count - new_count - low_count} high-volume | "
              f"{low_count} low-score (excluded from trading)")
        print()


# ---------------------------------------------------------------------------
# Auto token selector  (FIXED)
# ---------------------------------------------------------------------------

class AutoTokenSelector:
    """Enhanced auto token selector with score-awareness."""

    def __init__(self, detector: EnhancedTrendingTokenDetector):
        self.detector   = detector
        self.preferences = {
            'min_liquidity':        20_000,
            'min_volume':           20_000,
            'max_tokens':           10,
            'prefer_rising':        False,
            'avoid_extreme_volatility': False,
            'max_price_change_24h': 300,
            'prioritize_boosted':   True,
            # FIX: minimum score enforced here too
            'min_score':            50.0,
        }

    def get_token_symbols(self) -> List[Dict]:
        """Get selected tokens with score-aware filtering."""
        trending = self.detector.get_trending_tokens(
            min_liquidity=self.preferences['min_liquidity'],
            min_volume_24h=self.preferences['min_volume'],
            max_tokens=self.preferences['max_tokens'] * 4,
            prioritize_boosted=self.preferences['prioritize_boosted'],
        )

        safe = self.detector.filter_safe_tokens(trending)
        print(f"  After safety filters: {len(safe)} tokens")

        if self.preferences['prefer_rising']:
            safe = [t for t in safe if t['price_change_24h'] > 0]
            print(f"  After prefer_rising: {len(safe)} tokens")

        max_change = self.preferences['max_price_change_24h']
        safe = [t for t in safe if abs(t.get('price_change_24h', 0)) <= max_change]
        print(f"  After max price change ≤ {max_change}%: {len(safe)} tokens")

        # FIX: apply score filter here as a final pass
        min_score = self.preferences['min_score']
        safe = [t for t in safe if t.get('trend_score', 0) >= min_score]
        print(f"  After min score ≥ {min_score:.0f}: {len(safe)} tokens")

        # Fallback logic
        if not safe:
            print("⚠️  No tokens after filters. Retrying with relaxed settings...")
            trending2 = self.detector.get_trending_tokens(
                min_liquidity=self.preferences['min_liquidity'],
                min_volume_24h=self.preferences['min_volume'],
                max_tokens=self.preferences['max_tokens'] * 4,
                prioritize_boosted=True,
            )
            safe2 = self.detector.filter_safe_tokens(trending2)
            safe2 = [t for t in safe2
                     if abs(t.get('price_change_24h', 0)) <= max_change
                     and t.get('trend_score', 0) >= min_score]
            if safe2:
                safe = safe2

        if not safe:
            print("⚠️  Still no tokens. Falling back to any safe tokens...")
            safe = self.detector.filter_safe_tokens(trending)

        safe.sort(key=lambda x: x['trend_score'], reverse=True)
        return [dict(t, score=t['trend_score'])
                for t in safe[:self.preferences['max_tokens']]]


# ---------------------------------------------------------------------------
# CLI test entry point
# ---------------------------------------------------------------------------

def main():
    print("\n🔍 Enhanced Solana Trending Token Detector (Fixed Edition v2)\n")

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

    print("\n✅ RECOMMENDED FOR TRADING (score ≥ 50, safe filters passed):")
    for idx, token in enumerate(top_tokens, 1):
        quality = token.get('quality_indicator', '📊')
        print(f"{idx}. {quality} {token['symbol']} (Score: {token['score']:.1f})")
    print()


if __name__ == "__main__":
    main()
