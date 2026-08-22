#!/usr/bin/env python3
"""
Solana DEX Trading Agent - Established Tokens Edition
Trades SOL, JUP, RAY, BONK, WIF on 5-minute candles.

Pair address auto-discovery:
  At startup, queries DexScreener for the highest-liquidity pool for each
  token, then fetches real OHLCV candles from GeckoTerminal. Falls back to
  hardcoded addresses if the network call fails.
"""

import time, requests, math, random
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class TradingMode(Enum):
    PAPER = "paper"
    LIVE  = "live"


@dataclass
class Trade:
    timestamp:    str
    token_in:     str
    token_out:    str
    side:         str
    price:        float
    amount_in:    float
    amount_out:   float
    tx_signature: str
    status:       str


@dataclass
class Position:
    token:          str
    amount:         float
    avg_price_usdc: float
    current_price:  float
    pnl:            float
    pnl_percentage: float


# ── Token registry ─────────────────────────────────────────────────────────────
TOKENS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY":  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JTO":  "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
}

# Hardcoded fallback pair addresses (GeckoTerminal pool IDs on Solana).
# Used when DexScreener auto-discovery fails.
FALLBACK_PAIRS = {
    "SOL":  "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",  # SOL/USDC Raydium CLMM
    "JUP":  "3XzFeLzB3tS7pFSEMsyimLMhMkpRQHMGKFh77AUCwWNK",  # JUP/USDC Orca
    "RAY":  "6UmmUiYoBjSrhakAobJw8BYkpGQCQHMKUDsKfXHUFkGm",  # RAY/USDC Raydium v4
    "BONK": "8PhnCfgqpgFM7ZJvttGdBVMXHuU4Q23ACxCvWkbs1M71",  # BONK/SOL Orca
    "WIF":  "EP2ib6dYdEeqD8MfE2ezHCxX3kP3K2eLKkirfPm5eyMx",  # WIF/SOL Raydium CLMM
    "JTO":  "2NbqvvDmh3VqFvfDHqjFQfT4DzkCcEyaKFPFDtRNWzRz",  # JTO/USDC Orca
    "PYTH": "HktfL7iwGKT5QHjywQkcDnFXmeMoRxQDpGVMqgGUzXS9",  # PYTH/USDC Raydium
}


# ── Pair address discovery ──────────────────────────────────────────────────────
def discover_pair(symbol: str, mint: str) -> Optional[str]:
    """
    Find the highest-liquidity Solana DEX pair for this token via DexScreener.
    Returns the pairAddress string or None.
    """
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code != 200:
            return None

        pairs = [
            p for p in r.json().get("pairs", [])
            if p.get("chainId") == "solana"
            and float(p.get("liquidity", {}).get("usd", 0) or 0) > 50_000
        ]
        if not pairs:
            return None

        pairs.sort(
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
            reverse=True,
        )
        best  = pairs[0]
        addr  = best.get("pairAddress", "")
        liq   = float(best.get("liquidity", {}).get("usd", 0) or 0)
        price = best.get("priceUsd", "?")
        dex   = best.get("dexId", "?")
        print(f"    ✓ {symbol}: ${float(price or 0):.4f}  "
              f"liq=${liq/1e6:.1f}M  dex={dex}  pair={addr[:12]}…")
        return addr or None

    except Exception as e:
        print(f"    ✗ {symbol}: discovery error — {e}")
        return None


# ── Price & candle data ─────────────────────────────────────────────────────────
class PriceHistory:
    GECKO = "https://api.geckoterminal.com/api/v2/networks/solana/pools/{}/ohlcv/{}"

    def __init__(self):
        self._price_cache: Dict[str, Dict] = {}
        self.pair_map:     Dict[str, str]  = {}   # symbol -> pool address

    # ── Discovery ──────────────────────────────────────────────────────────────
    def discover_all(self, symbols: List[str]):
        """Auto-discover pair addresses at startup. Fills self.pair_map."""
        print("  Discovering pool addresses via DexScreener...")
        for sym in symbols:
            mint = TOKENS.get(sym, "")
            if not mint:
                continue
            addr = discover_pair(sym, mint)
            if addr:
                self.pair_map[sym] = addr
            else:
                # Fall back to hardcoded address
                fallback = FALLBACK_PAIRS.get(sym)
                if fallback:
                    print(f"    {sym}: using fallback address {fallback[:12]}…")
                    self.pair_map[sym] = fallback
                else:
                    print(f"    {sym}: no address available — will skip OHLCV")

    # ── Historical candles ─────────────────────────────────────────────────────
    def fetch_candles(self, symbol: str,
                      timeframe: str = "minute",
                      aggregate: int = 5,
                      limit: int = 1000) -> List[Dict]:
        """Fetch OHLCV candles from GeckoTerminal. Returns oldest-first list."""
        pair = self.pair_map.get(symbol)
        if not pair:
            return []

        url = self.GECKO.format(pair, timeframe)
        try:
            r = requests.get(
                url,
                params={"aggregate": aggregate, "limit": limit,
                        "currency": "usd", "token": "base"},
                headers={"Accept": "application/json;version=20230302"},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"    GeckoTerminal {r.status_code} for {symbol}")
                # Try the fallback address if primary failed
                fallback = FALLBACK_PAIRS.get(symbol)
                if fallback and fallback != pair:
                    print(f"    Retrying {symbol} with fallback address…")
                    self.pair_map[symbol] = fallback
                    return self.fetch_candles(symbol, timeframe, aggregate, limit)
                return []

            raw = (r.json()
                   .get("data", {})
                   .get("attributes", {})
                   .get("ohlcv_list", []))
            if not raw:
                return []

            # GeckoTerminal returns newest-first — reverse to oldest-first
            candles = []
            for row in reversed(raw):
                candles.append({
                    "ts":    row[0],
                    "open":  float(row[1]),
                    "high":  float(row[2]),
                    "low":   float(row[3]),
                    "close": float(row[4]),
                    "vol":   float(row[5]),
                })
            return candles

        except Exception as e:
            print(f"    Candle fetch error ({symbol}): {e}")
            return []

    # ── Live price ─────────────────────────────────────────────────────────────
    def get_live_price(self, symbol: str) -> float:
        """Current price from DexScreener (8-second cache)."""
        mint   = TOKENS.get(symbol, "")
        cached = self._price_cache.get(mint, {})
        if cached.get("price", 0) > 0 and time.time() - cached.get("ts", 0) < 8:
            # tiny jitter to avoid flat-line charts
            return cached["price"] * (1 + random.uniform(-0.0003, 0.0003))

        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                timeout=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                pairs = [
                    p for p in r.json().get("pairs", [])
                    if p.get("chainId") == "solana"
                ]
                if pairs:
                    pairs.sort(
                        key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
                        reverse=True,
                    )
                    price = float(pairs[0].get("priceUsd", 0) or 0)
                    if price > 0:
                        self._price_cache[mint] = {"price": price, "ts": time.time()}
                        return price
        except Exception:
            pass

        return cached.get("price", 0.0)


# ── Technical indicators ────────────────────────────────────────────────────────
class TradingAI:
    """
    Trend-following pullback strategy on 5-minute candles.
    **RELAXED PARAMETERS VERSION** - More trade opportunities

    BUY  — ALL must be true:
      1. EMA20 > EMA50  AND  EMA20 6-bar slope > +0.05%
         OR NEUTRAL trend with positive slope
      2. RSI in 38–68  (pullback, not overbought)
      3. RSI rose vs 3 bars ago by ≥ 1 point  (turning back up)
      4. MACD histogram > previous histogram  (momentum improving)
      5. Price ≥ EMA20 × 0.93  (not broken below trend)
      6. Volume ≥ 0.30x average (relaxed from 0.40x)

    SELL — ANY is true:
      1. RSI > 72
      2. Price < EMA20 × 0.93
      3. MACD hist negative AND worse than previous
    """

    def ema(self, vals: List[float], period: int) -> float:
        if not vals:
            return 0.0
        n   = min(period, len(vals))
        val = sum(vals[:n]) / n
        m   = 2 / (period + 1)
        for v in vals[n:]:
            val = v * m + val * (1 - m)
        return val

    def rsi(self, closes: List[float], period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        ag     = sum(gains[-period:])  / period
        al     = sum(losses[-period:]) / period
        return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)

    def macd_histogram(self, closes: List[float]) -> Tuple[float, float]:
        """Returns (current_histogram, prev_histogram)."""
        if len(closes) < 36:
            return 0.0, 0.0

        def _h(c: List[float]) -> float:
            if len(c) < 35:
                return 0.0
            ef = self.ema(c, 12)
            es = self.ema(c, 26)
            mv = ef - es
            # 9-period signal line built from rolling MACD values
            mc = []
            for i in range(min(9, len(c) - 26)):
                sc = c[:-(8 - i)] if (8 - i) > 0 else c
                mc.append(self.ema(sc, 12) - self.ema(sc, 26))
            if not mc:
                return 0.0
            return mv - self.ema(mc, min(9, len(mc)))

        return _h(closes), _h(closes[:-1])

    def analyze(self, symbol: str,
                closes:  List[float],
                volumes: List[float]) -> Dict:
        if len(closes) < 60:
            return self._hold(closes, f"warming up ({len(closes)}/60)")

        cur   = closes[-1]
        ema20 = self.ema(closes, 20)
        ema50 = self.ema(closes, 50)
        rsi_v = self.rsi(closes)
        if rsi_v is None:
            return self._hold(closes, "RSI not ready")

        hist, phist = self.macd_histogram(closes)

        # EMA slope (6-bar)
        ema20_6   = self.ema(closes[:-6], 20) if len(closes) > 26 else ema20
        ema_slope = (ema20 - ema20_6) / ema20_6 if ema20_6 > 0 else 0.0

        # RSI direction (3-bar)
        rsi_3ago   = self.rsi(closes[:-3]) if len(closes) > 17 else None
        rsi_rising = rsi_3ago is not None and rsi_v > rsi_3ago + 1.0

        # Volume
        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1
        rel_vol = volumes[-1] / vol_ma if vol_ma > 0 else 1.0

        # Trend
        if ema20 > ema50 and ema_slope > 0.0005:
            trend = "BULLISH"
        elif ema20 < ema50 and ema_slope < -0.0005:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

        # ── BUY ──────────────────────────────────────────────────────────
        buy_ok   = True
        b_ok     = []
        b_block  = []

        if trend == "BULLISH" or (trend == "NEUTRAL" and ema_slope > 0):
            b_ok.append("uptrend")
        else:
            buy_ok = False;  b_block.append(f"trend={trend}")

        if 38 <= rsi_v <= 68:
            b_ok.append(f"RSI={rsi_v:.0f}✓")
        else:
            buy_ok = False;  b_block.append(f"RSI={rsi_v:.0f} outside 38–68")

        if rsi_rising:
            b_ok.append("RSI↑")
        else:
            buy_ok = False;  b_block.append("RSI not rising")

        if hist > phist:
            b_ok.append("MACD↑")
        else:
            buy_ok = False;  b_block.append("MACD not improving")

        if cur < ema20 * 0.93:
            buy_ok = False;  b_block.append("price<EMA×0.93")

        if rel_vol < 0.30:
            buy_ok = False;  b_block.append(f"vol={rel_vol:.2f}x<0.30")

        # ── SELL ─────────────────────────────────────────────────────────
        sell_ok  = False
        s_ok     = []

        if rsi_v > 72:
            sell_ok = True;  s_ok.append(f"RSI={rsi_v:.0f}>72")
        if cur < ema20 * 0.93:
            sell_ok = True;  s_ok.append("price<EMA×0.93")
        if hist < 0 and hist < phist and phist < 0:
            sell_ok = True;  s_ok.append("MACD↓")

        # ── Final ────────────────────────────────────────────────────────
        if buy_ok:
            signal, conf, reason = "BUY",  0.80, "; ".join(b_ok)
        elif sell_ok:
            signal, conf, reason = "SELL", 0.75, "; ".join(s_ok)
        else:
            signal, conf, reason = "HOLD", 0.50, "; ".join(b_block) or f"RSI={rsi_v:.0f}"

        return {
            "signal":        signal,
            "confidence":    conf,
            "reason":        reason,
            "rsi":           rsi_v,
            "rsi_rising":    rsi_rising,
            "trend":         trend,
            "ema20":         ema20,
            "ema50":         ema50,
            "ema_slope":     ema_slope,
            "macd_hist":     hist,
            "prev_hist":     phist,
            "rel_vol":       rel_vol,
            "current_price": cur,
        }

    def _hold(self, closes: List[float], reason: str) -> Dict:
        cur = closes[-1] if closes else 0
        return {
            "signal": "HOLD", "confidence": 0.0, "reason": reason,
            "rsi": 50.0, "rsi_rising": False, "trend": "NEUTRAL",
            "ema20": cur, "ema50": cur, "ema_slope": 0.0,
            "macd_hist": 0.0, "prev_hist": 0.0,
            "rel_vol": 1.0, "current_price": cur,
        }


# ── Portfolio ──────────────────────────────────────────────────────────────────
class Portfolio:

    def __init__(self, initial_usdc: float):
        self.initial_usdc = initial_usdc
        self.usdc         = initial_usdc
        self.positions:   Dict[str, Position] = {}
        self.trades:      List[Trade]         = []

    def buy(self, token: str, usdc_amount: float, price: float, tx: str) -> Trade:
        tokens_out  = (usdc_amount / price) * 0.99   # 1% slippage
        entry_price = price * 1.01
        self.usdc  -= usdc_amount
        if token in self.positions:
            pos            = self.positions[token]
            total_cost     = pos.avg_price_usdc * pos.amount + usdc_amount
            pos.amount    += tokens_out
            pos.avg_price_usdc = total_cost / pos.amount
        else:
            self.positions[token] = Position(
                token=token, amount=tokens_out,
                avg_price_usdc=entry_price, current_price=price,
                pnl=0.0, pnl_percentage=0.0)
        t = Trade(datetime.now().isoformat(), "USDC", token, "buy",
                  price, usdc_amount, tokens_out, tx, "filled")
        self.trades.append(t)
        return t

    def sell(self, token: str, price: float, tx: str) -> Optional[Trade]:
        if token not in self.positions:
            return None
        pos       = self.positions[token]
        usdc_out  = pos.amount * price * 0.99
        self.usdc += usdc_out
        t = Trade(datetime.now().isoformat(), token, "USDC", "sell",
                  price, pos.amount, usdc_out, tx, "filled")
        self.trades.append(t)
        del self.positions[token]
        return t

    def update_prices(self, prices: Dict[str, float]):
        for tok, price in prices.items():
            if tok in self.positions:
                pos               = self.positions[tok]
                pos.current_price = price
                pos.pnl           = (price - pos.avg_price_usdc) * pos.amount
                if pos.avg_price_usdc > 0:
                    pos.pnl_percentage = (
                        (price - pos.avg_price_usdc) / pos.avg_price_usdc * 100
                    )

    def total_value(self) -> float:
        return self.usdc + sum(
            p.amount * p.current_price for p in self.positions.values()
        )

    def stats(self) -> Dict:
        tv  = self.total_value()
        ret = tv - self.initial_usdc
        return {
            "total_value": tv, "usdc": self.usdc,
            "return_usd":  ret,
            "return_pct":  ret / self.initial_usdc * 100,
            "n_trades":    len(self.trades),
            "n_positions": len(self.positions),
        }
