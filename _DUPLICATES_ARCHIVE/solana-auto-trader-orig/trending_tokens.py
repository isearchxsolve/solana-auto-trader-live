#!/usr/bin/env python3
"""
Trending Token Detection for Solana
Automatically finds and ranks trending tokens
"""

import requests
import time
from typing import List, Dict, Optional
from datetime import datetime, timedelta


class TrendingTokenDetector:
    """Detect and rank trending Solana tokens including brand-new meme coins."""

    def __init__(self):
        self.dexscreener_url = "https://api.dexscreener.com"
        self.cache = {}
        self.cache_duration = 300  # 5 minutes

    def get_trending_from_dexscreener(self) -> List[Dict]:
        """
        Two DexScreener sources merged:
          1. token-profiles/latest/v1  â€” brand-new tokens with fresh profiles
          2. search?q=WSOL             â€” high-volume established pairs
        """
        all_pairs = []
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            # Source 1: newly listed tokens
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
                        all_pairs.extend(pr.json().get('pairs', []))

            # Source 2: WSOL search â€” high-volume pairs
            wsol = "So11111111111111111111111111111111111111112"
            sr = requests.get(
                f"{self.dexscreener_url}/latest/dex/search/?q={wsol}",
                headers=headers, timeout=10
            )
            if sr.status_code == 200:
                all_pairs.extend(sr.json().get('pairs', []))

        except Exception as e:
            print(f"Error fetching from DexScreener: {e}")
        return all_pairs
    
    def get_trending_tokens(self, min_liquidity: float = 15000,
                            min_volume_24h: float = 30000,
                            max_tokens: int = 10) -> List[Dict]:
        """Return ranked trending Solana tokens (meme-coin friendly thresholds)."""
        cache_key = f"trending_{max_tokens}"
        if cache_key in self.cache:
            cached_time, cached_data = self.cache[cache_key]
            if time.time() - cached_time < self.cache_duration:
                return cached_data

        print("Fetching latest new & trending meme coins...")
        pairs = self.get_trending_from_dexscreener()

        trending_tokens = []
        seen_tokens = set()
        IGNORE = {'SOL', 'WSOL', 'USDC', 'USDT', 'USDS', 'DAI'}

        for pair in pairs:
            try:
                if pair.get('chainId') != 'solana':
                    continue
                base = pair.get('baseToken', {})
                quote = pair.get('quoteToken', {})
                base_sym = base.get('symbol', '').upper()
                target = quote if base_sym in IGNORE else base
                sym  = target.get('symbol', '').upper()
                addr = target.get('address', '')
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
                score = self._calculate_trend_score(vol, liq, ch24)
                trending_tokens.append({
                    'address': addr, 'symbol': sym,
                    'name': target.get('name', sym),
                    'price_usd': price, 'liquidity_usd': liq,
                    'volume_24h': vol, 'price_change_h1': ch1,
                    'price_change_h6': ch6, 'price_change_24h': ch24,
                    'trend_score': score, 'dex': pair.get('dexId', 'unknown'),
                    'pair_address': pair.get('pairAddress', '')
                })
                seen_tokens.add(addr)
            except Exception:
                continue

        trending_tokens.sort(key=lambda x: x['trend_score'], reverse=True)
        result = trending_tokens[:max_tokens]
        self.cache[cache_key] = (time.time(), result)
        return result
    
    def _calculate_trend_score(self, volume: float, liquidity: float,
                               price_change: float) -> float:
        """Trend score boosted for meme-coin volatility."""
        vol_score = min(volume    / 500000 * 30, 30)
        liq_score = min(liquidity / 200000 * 30, 30)
        price_score = min(price_change / 10 * 40, 40) if price_change > 0 else max(price_change / 10 * 20, -20)
        return vol_score + liq_score + price_score
    
    def filter_safe_tokens(self, tokens: List[Dict]) -> List[Dict]:
        """Meme-coin friendly filter â€” low minimums, no volatility cap."""
        return [t for t in tokens if t['liquidity_usd'] >= 10000 and t['volume_24h'] >= 20000]
    
    def get_token_details(self, token_address: str) -> Optional[Dict]:
        """Get detailed information about a specific token"""
        try:
            response = requests.get(
                f"{self.dexscreener_url}/tokens/{token_address}",
                timeout=10
            )
            
            if response.status_code == 200:
                return response.json()
            return None
            
        except Exception as e:
            print(f"Error fetching token details: {e}")
            return None
    
    def fetch_historical_prices(self, pair_address: str, n_points: int = 1000,
                                token_info: Optional[Dict] = None) -> List[float]:
        """
        Fetch up to 1000 real 1-minute candles from GeckoTerminal.
        Falls back to structured multi-regime synthetic on failure.
        """
        if pair_address:
            closes = self._fetch_geckoterminal(pair_address)
            if closes:
                print(f"    âœ“ GeckoTerminal: {len(closes)} real candles")
                return closes
        print(f"    ~ Generating structured synthetic history")
        return self._synthetic_history(token_info or {}, n_points)

    def _fetch_geckoterminal(self, pair_address: str) -> List[float]:
        """Single GeckoTerminal OHLCV call. Returns oldest-first close prices."""
        url     = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair_address}/ohlcv/minute"
        headers = {"Accept": "application/json;version=20230302"}
        params  = {"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"}
        try:
            resp  = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                return []
            ohlcv = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
            return [float(r[4]) for r in reversed(ohlcv)] if ohlcv else []
        except Exception:
            return []

    def _synthetic_history(self, token_info: Dict, n_points: int) -> List[float]:
        """Multi-regime GBM from real h1/h6/h24 price changes. Ends exactly at current price."""
        import random, math
        price_now  = float(token_info.get("price_usd",        1.0) or 1.0)
        change_h1  = float(token_info.get("price_change_h1",  0.0))
        change_h6  = float(token_info.get("price_change_h6",  0.0))
        change_h24 = float(token_info.get("price_change_24h", 0.0))
        p1h  = price_now / max(1 + change_h1  / 100, 0.01)
        p6h  = price_now / max(1 + change_h6  / 100, 0.01)
        p24h = price_now / max(1 + change_h24 / 100, 0.01)
        n_c = int(n_points * 0.25); n_b = int(n_points * 0.35); n_a = n_points - n_b - n_c
        def _gbm(s, e, n, v=0.002):
            if n <= 0 or s <= 0 or e <= 0: return [s] * max(n, 1)
            d = math.log(e / s) / n; vol = min(max(abs(d) * 0.6, v), 0.015)
            pts = []; p = s
            for _ in range(n):
                p = p * math.exp(d + vol * random.gauss(0, 1)); pts.append(p)
            return [x * (e / pts[-1]) for x in pts] if pts else [s] * n
        return _gbm(p24h, p6h, n_a, .003) + _gbm(p6h, p1h, n_b, .002) + _gbm(p1h, price_now, n_c, .0015)


    def display_trending(self, tokens: List[Dict]):
        """Display trending tokens in a formatted way"""
        print("\n" + "="*80)
        print("ðŸ”¥ TRENDING SOLANA TOKENS")
        print("="*80)
        print(f"{'Rank':<6} {'Symbol':<12} {'Price':<15} {'24h Vol':<15} {'24h Change':<12} {'Score':<8}")
        print("-"*80)
        
        for idx, token in enumerate(tokens, 1):
            symbol = token['symbol'][:10]
            price = f"${token['price_usd']:.8f}"
            volume = f"${token['volume_24h']:,.0f}"
            change = f"{token['price_change_24h']:+.2f}%"
            score = f"{token['trend_score']:.1f}"
            
            # Color code based on price change
            if token['price_change_24h'] > 0:
                change_indicator = "ðŸŸ¢"
            else:
                change_indicator = "ðŸ”´"
            
            print(f"{idx:<6} {symbol:<12} {price:<15} {volume:<15} {change_indicator} {change:<10} {score:<8}")
        
        print("="*80 + "\n")


class AutoTokenSelector:
    """Automatically select best meme/trending tokens for trading."""

    def __init__(self, detector: TrendingTokenDetector):
        self.detector = detector
        self.preferences = {
            'min_liquidity': 15000, 'min_volume': 30000,
            'max_tokens': 10, 'prefer_rising': True,
            'avoid_extreme_volatility': False,
        }

    def get_token_symbols(self) -> List[Dict]:
        trending = self.detector.get_trending_tokens(
            min_liquidity=self.preferences['min_liquidity'],
            min_volume_24h=self.preferences['min_volume'],
            max_tokens=self.preferences['max_tokens'] * 2
        )
        safe = self.detector.filter_safe_tokens(trending)
        if self.preferences['prefer_rising']:
            safe = [t for t in safe if t['price_change_24h'] > 0]
        safe.sort(key=lambda x: x['trend_score'], reverse=True)
        return [{'symbol': t['symbol'], 'address': t['address'], 'score': t['trend_score']}
                for t in safe[:self.preferences['max_tokens']]]

    def select_tokens(self) -> List[str]:
        return [t['address'] for t in self.get_token_symbols()]


def main():
    """Test trending token detection"""
    print("\nðŸ” Solana Trending Token Detector\n")
    
    detector = TrendingTokenDetector()
    
    # Get trending tokens
    trending = detector.get_trending_tokens(
        min_liquidity=50000,
        min_volume_24h=100000,
        max_tokens=10
    )
    
    # Display results
    detector.display_trending(trending)
    
    # Show top 5 recommendations
    selector = AutoTokenSelector(detector)
    top_tokens = selector.get_token_symbols()
    
    print("\nâœ… RECOMMENDED FOR TRADING:")
    for idx, token in enumerate(top_tokens, 1):
        print(f"{idx}. {token['symbol']} (Score: {token['score']:.1f})")
    print()


if __name__ == "__main__":
    main()