#!/usr/bin/env python3
"""
Run this once to find the correct GeckoTerminal pool addresses for each token.
It queries DexScreener for the best pool, then verifies it works on GeckoTerminal.

Usage:  python find_pairs.py
"""
import requests, time

TOKENS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY":  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
}

print("Finding working GeckoTerminal pool addresses...\n")
results = {}

for sym, mint in TOKENS.items():
    print(f"── {sym} ──────────────────────────────────────────────────")
    
    # Step 1: get all pairs from DexScreener
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        pairs = [p for p in r.json().get("pairs", [])
                 if p.get("chainId") == "solana"
                 and float(p.get("liquidity", {}).get("usd", 0) or 0) > 100_000]
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        print(f"  DexScreener: {len(pairs)} pairs with liq > $100k")
    except Exception as e:
        print(f"  DexScreener error: {e}")
        pairs = []

    # Step 2: try each pair on GeckoTerminal until one works
    working = None
    for p in pairs[:5]:
        addr  = p.get("pairAddress", "")
        liq   = float(p.get("liquidity", {}).get("usd", 0) or 0)
        dex   = p.get("dexId", "")
        price = p.get("priceUsd", "?")
        
        if not addr:
            continue
        
        # Test GeckoTerminal
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{addr}/ohlcv/minute"
        try:
            gr = requests.get(url,
                params={"aggregate": 5, "limit": 10, "currency": "usd", "token": "base"},
                headers={"Accept": "application/json;version=20230302"}, timeout=10)
            
            ohlcv = (gr.json().get("data", {}).get("attributes", {})
                     .get("ohlcv_list", []) if gr.status_code == 200 else [])
            
            status = f"✓ {len(ohlcv)} candles" if ohlcv else f"✗ HTTP {gr.status_code}"
            print(f"  {addr[:12]}… liq=${liq/1e6:.1f}M dex={dex} price=${price}  → GeckoTerminal: {status}")
            
            if ohlcv and not working:
                working = addr
                results[sym] = addr
        except Exception as e:
            print(f"  {addr[:12]}… → GeckoTerminal error: {e}")
        
        time.sleep(0.3)  # rate limit
    
    if not working:
        print(f"  !! No working GeckoTerminal address found for {sym}")
    print()

print("\n" + "="*60)
print("COPY THESE INTO solana_trading_agent.py  →  FALLBACK_PAIRS")
print("="*60)
for sym, addr in results.items():
    print(f'    "{sym}":  "{addr}",')
