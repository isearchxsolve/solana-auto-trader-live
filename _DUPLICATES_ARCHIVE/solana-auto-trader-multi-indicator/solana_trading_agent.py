#!/usr/bin/env python3
"""
Solana DEX Trading AI Agent
Supports Jupiter Aggregator integration with paper and live trading modes
"""

import json
import time
import base64
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import argparse
import sys
from dataclasses import dataclass, asdict
from enum import Enum


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Trade:
    timestamp: str
    token_in: str
    token_out: str
    side: str
    price: float
    amount_in: float
    amount_out: float
    tx_signature: str
    status: str


@dataclass
class Position:
    token: str
    amount: float
    avg_price_usdc: float
    current_price: float
    pnl: float
    pnl_percentage: float


# Solana token addresses
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
}


class JupiterClient:
    """Jupiter Aggregator API Client"""
    
    def __init__(self, mode: TradingMode = TradingMode.PAPER):
        self.mode = mode
        self.base_url = "https://quote-api.jup.ag/v6"
        self.price_url = "https://price.jup.ag/v4"
        
    def get_quote(self, input_mint: str, output_mint: str, amount: int, 
                  slippage_bps: int = 50) -> Optional[Dict]:
        """Get swap quote from Jupiter"""
        if self.mode == TradingMode.PAPER:
            return self._simulate_quote(input_mint, output_mint, amount)
        
        try:
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": amount,
                "slippageBps": slippage_bps,
                "onlyDirectRoutes": False
            }
            
            response = requests.get(f"{self.base_url}/quote", params=params)
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Quote error: {response.status_code}")
                return None
        except Exception as e:
            print(f"Error getting quote: {e}")
            return None
    
    def _simulate_quote(self, input_mint: str, output_mint: str, amount: int) -> Dict:
        """Simulate swap quote using real cached prices for accuracy."""
        # Use real cached prices if available, else fall back to last known values
        def _price(mint: str) -> float:
            if hasattr(self, '_price_cache') and mint in self._price_cache:
                return self._price_cache[mint]
            # Last-resort hardcoded fallbacks (only hit if DexScreener never reached)
            fallback = {
                TOKENS["SOL"]: 110.0, TOKENS["USDC"]: 1.0,
                TOKENS["USDT"]: 1.0,  TOKENS["RAY"]:  1.5,
                TOKENS["BONK"]: 0.00002, TOKENS["JUP"]: 0.85,
                TOKENS["WIF"]: 2.3,   TOKENS["JTO"]:  3.1,
                TOKENS["PYTH"]: 0.42,
            }
            return fallback.get(mint, 1.0)

        input_price  = _price(input_mint)
        output_price = _price(output_mint)

        input_value   = (amount / 1e9) * input_price
        output_amount = int((input_value / output_price) * 1e6 * 0.995)  # 0.5% slippage

        return {
            "inputMint":     input_mint,
            "outputMint":    output_mint,
            "inAmount":      str(amount),
            "outAmount":     str(output_amount),
            "priceImpactPct": 0.1,
            "routePlan":     [{"swapInfo": {"label": "Simulated"}}]
        }
    
    def get_token_price(self, token_mint: str) -> float:
        """
        Get real-time token price in USDC from DexScreener.

        Paper AND live mode both use real market prices so that technical
        indicators (RSI, SMA, MACD) reflect genuine market movements.

        Rate-limiting: DexScreener prices are cached per-mint for 10 seconds.
        Within the cache window the last known price is returned with ±0.1%
        micro-jitter (simulating bid/ask spread) — not a random walk.
        """
        import random, time as _time

        # ── Initialise per-instance caches on first call ─────────────────────────
        if not hasattr(self, "_price_cache"):
            self._price_cache: Dict[str, float] = {}
        if not hasattr(self, "_price_ts"):
            self._price_ts:    Dict[str, float] = {}

        CACHE_TTL = 10  # seconds — refresh real price at most every 10s per token

        now = _time.time()
        cached_price = self._price_cache.get(token_mint, 0.0)
        last_fetch   = self._price_ts.get(token_mint, 0.0)

        # Return cached price with tiny spread jitter if cache is still fresh
        if cached_price > 0 and (now - last_fetch) < CACHE_TTL:
            return cached_price * (1 + random.uniform(-0.001, 0.001))

        # ── Fetch real price from DexScreener ────────────────────────────────────
        try:
            resp = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                timeout=6
            )
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                # Prefer Solana pairs; fall back to any pair
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                best = sol_pairs[0] if sol_pairs else (pairs[0] if pairs else None)
                if best:
                    real_price = float(best.get("priceUsd", 0) or 0)
                    if real_price > 0:
                        self._price_cache[token_mint] = real_price
                        self._price_ts[token_mint]    = now
                        return real_price * (1 + random.uniform(-0.001, 0.001))
        except Exception:
            pass

        # ── Fallback: return stale cache if available (no random walk) ────────────
        if cached_price > 0:
            return cached_price * (1 + random.uniform(-0.001, 0.001))

        return 0.0  # Completely unknown token — skip
    
    def execute_swap(self, quote: Dict, wallet_address: Optional[str] = None) -> Optional[str]:
        """Execute swap (or simulate in paper mode)"""
        if self.mode == TradingMode.PAPER:
            # Simulate transaction
            import random
            tx_signature = f"sim_{int(time.time())}_{random.randint(1000, 9999)}"
            return tx_signature
        
        # In live mode, would need to:
        # 1. Get swap transaction from Jupiter
        # 2. Sign with wallet
        # 3. Send to Solana network
        # This requires solana-py library and wallet integration
        
        print("⚠️  Live trading requires wallet integration")
        print("    Install: pip install solana solders")
        print("    See documentation for setup")
        return None


class TradingAI:
    """
    Multi-indicator AI trading decision engine for Solana tokens.

    Indicators used
    ───────────────
    Trend / MA layer
      • SMA-20 / SMA-50  — primary trend filter
      • EMA-12 / EMA-26  — shorter momentum
      • EMA-9 (MACD signal) — MACD crossover

    Oscillators
      • RSI-14           — overbought / oversold + direction
      • Stochastic %K/%D — fast oscillator confirmation
      • Williams %R      — momentum reversal
      • CCI-20           — trend strength / extremes

    Volatility
      • Bollinger Bands (20, 2σ) — price channel & squeeze
      • ATR-14           — volatility filter, position sizing hint

    Volume
      • OBV              — on-balance volume (accumulation / distribution)
      • VWAP             — intraday fair-value anchor

    Divergence
      • RSI divergence   — price makes higher-high but RSI lower-high (bearish)
                          price makes lower-low but RSI higher-low (bullish)
      • MACD divergence  — same logic applied to MACD histogram
    """

    def __init__(self):
        self.indicators_cache = {}

    # ─────────────────────────── BASIC BUILDING BLOCKS ───────────────────────

    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Wilder-smoothed RSI. Returns None when data is insufficient."""
        if len(prices) < period + 1:
            return None
        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]
        # Wilder smoothing seed
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            return 50.0 if avg_gain == 0 else 100.0  # flat market → 50, pure up → 100
        return 100 - (100 / (1 + avg_gain / avg_loss))

    def calculate_sma(self, prices: List[float], period: int) -> float:
        """Simple Moving Average."""
        if not prices:
            return 0.0
        window = prices[-period:] if len(prices) >= period else prices
        return sum(window) / len(window)

    def calculate_ema(self, prices: List[float], period: int) -> float:
        """Exponential Moving Average."""
        if not prices:
            return 0.0
        period = min(period, len(prices))
        k   = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    def calculate_ema_series(self, prices: List[float], period: int) -> List[float]:
        """Return the full EMA series (one value per price point)."""
        if len(prices) < period:
            return [prices[i] for i in range(len(prices))]
        k      = 2 / (period + 1)
        series = [sum(prices[:period]) / period]
        for p in prices[period:]:
            series.append(p * k + series[-1] * (1 - k))
        # Pad head with NaN-equivalent (use first EMA value) for alignment
        head = [series[0]] * (period - 1)
        return head + series

    # ─────────────────────────── OSCILLATORS ─────────────────────────────────

    def calculate_stochastic(self, prices: List[float],
                             period: int = 14, smooth: int = 3
                             ) -> Tuple[Optional[float], Optional[float]]:
        """
        Stochastic Oscillator (%K, %D).
        %K = (close - lowest_low) / (highest_high - lowest_low) * 100
        %D = SMA-3 of %K
        """
        if len(prices) < period + smooth:
            return None, None
        k_series = []
        for i in range(period - 1, len(prices)):
            window = prices[i - period + 1: i + 1]
            lo, hi = min(window), max(window)
            k_series.append(0.0 if hi == lo else (prices[i] - lo) / (hi - lo) * 100)
        if len(k_series) < smooth:
            return None, None
        pct_k = k_series[-1]
        pct_d = sum(k_series[-smooth:]) / smooth
        return pct_k, pct_d

    def calculate_williams_r(self, prices: List[float],
                              period: int = 14) -> Optional[float]:
        """Williams %R  ( −100 … 0 ).  ≤ −80 = oversold, ≥ −20 = overbought."""
        if len(prices) < period:
            return None
        window = prices[-period:]
        hi, lo = max(window), min(window)
        if hi == lo:
            return -50.0
        return ((hi - prices[-1]) / (hi - lo)) * -100

    def calculate_cci(self, prices: List[float], period: int = 20) -> Optional[float]:
        """
        Commodity Channel Index.
        CCI = (typical_price − SMA) / (0.015 × mean_deviation)
        Here we approximate typical_price ≈ close (no H/L available).
        """
        if len(prices) < period:
            return None
        window = prices[-period:]
        tp_sma = sum(window) / period
        mad    = sum(abs(p - tp_sma) for p in window) / period
        if mad == 0:
            return 0.0
        return (prices[-1] - tp_sma) / (0.015 * mad)

    # ─────────────────────────── VOLATILITY ──────────────────────────────────

    def calculate_bollinger_bands(self, prices: List[float],
                                  period: int = 20, num_std: float = 2.0
                                  ) -> Tuple[float, float, float]:
        """Returns (upper_band, middle_band, lower_band)."""
        if len(prices) < period:
            mid = prices[-1] if prices else 0.0
            return mid, mid, mid
        window = prices[-period:]
        mid    = sum(window) / period
        std    = (sum((p - mid) ** 2 for p in window) / period) ** 0.5
        return mid + num_std * std, mid, mid - num_std * std

    def calculate_atr(self, prices: List[float], period: int = 14) -> Optional[float]:
        """
        Average True Range approximation using only close prices.
        True range ≈ |close[i] − close[i-1]|
        """
        if len(prices) < period + 1:
            return None
        trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        # Wilder smoothing
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    # ─────────────────────────── VOLUME ──────────────────────────────────────

    def calculate_obv(self, prices: List[float],
                      volumes: List[float]) -> Optional[float]:
        """
        On-Balance Volume.  Returns the final OBV value and its recent slope
        as a tuple (obv_value, obv_trend) where obv_trend ∈ {+1, 0, -1}.
        """
        if len(prices) < 2 or len(volumes) < 2:
            return None
        n   = min(len(prices), len(volumes))
        obv = 0.0
        obv_series = [0.0]
        for i in range(1, n):
            if prices[i] > prices[i - 1]:
                obv += volumes[i]
            elif prices[i] < prices[i - 1]:
                obv -= volumes[i]
            obv_series.append(obv)
        # OBV slope: compare last 10 values' SMA vs previous 10
        look = min(10, len(obv_series) // 2)
        if look < 2:
            return obv
        recent = sum(obv_series[-look:]) / look
        prior  = sum(obv_series[-2 * look: -look]) / look
        return obv  # raw value stored; slope used separately

    def calculate_obv_slope(self, prices: List[float],
                            volumes: List[float], look: int = 10) -> int:
        """Returns +1 (rising OBV), -1 (falling OBV), 0 (flat)."""
        if len(prices) < look * 2 + 1:
            return 0
        n   = min(len(prices), len(volumes))
        obv = 0.0
        obv_series = [0.0]
        for i in range(1, n):
            if prices[i] > prices[i - 1]:
                obv += volumes[i]
            elif prices[i] < prices[i - 1]:
                obv -= volumes[i]
            obv_series.append(obv)
        look = min(look, len(obv_series) // 2)
        if look < 2:
            return 0
        recent = sum(obv_series[-look:]) / look
        prior  = sum(obv_series[-2 * look: -look]) / look
        if recent > prior * 1.01:
            return 1
        if recent < prior * 0.99:
            return -1
        return 0

    def calculate_vwap(self, prices: List[float],
                       volumes: List[float], period: int = 50) -> float:
        """Volume-Weighted Average Price over the last `period` candles."""
        n   = min(len(prices), len(volumes), period)
        if n == 0:
            return prices[-1] if prices else 0.0
        p_slice = prices[-n:]
        v_slice = volumes[-n:]
        total_vol = sum(v_slice)
        if total_vol == 0:
            return sum(p_slice) / n
        return sum(p * v for p, v in zip(p_slice, v_slice)) / total_vol

    # ─────────────────────────── MACD HELPERS ────────────────────────────────

    def _build_macd_histogram(self, prices: List[float],
                              fast: int = 12, slow: int = 26,
                              signal: int = 9) -> List[float]:
        """
        Proper MACD histogram series.
        histogram[i] = MACD_line[i] − signal_line[i]
        Uses full EMA series so every histogram value is meaningful.
        """
        if len(prices) < slow + signal:
            return []
        ema_fast_s   = self.calculate_ema_series(prices, fast)
        ema_slow_s   = self.calculate_ema_series(prices, slow)
        # Align both series to same length (slow is longer seed)
        n            = min(len(ema_fast_s), len(ema_slow_s))
        macd_line    = [ema_fast_s[-n + i] - ema_slow_s[-n + i] for i in range(n)]
        signal_series = self.calculate_ema_series(macd_line, signal)
        return [macd_line[i] - signal_series[i] for i in range(len(signal_series))]

    # ─────────────────────────── DIVERGENCE ──────────────────────────────────

    def _detect_rsi_divergence(self, prices: List[float],
                                look_back: int = 30) -> str:
        """
        Swing-point RSI divergence.
        Returns  'BULLISH_DIV'  — price lower-low, RSI higher-low
                 'BEARISH_DIV'  — price higher-high, RSI lower-high
                 'NONE'
        """
        min_len = look_back + 15
        if len(prices) < min_len:
            return "NONE"
        seg = prices[-look_back:]

        def _rsi_at(offset_from_end: int) -> Optional[float]:
            """RSI at a position `offset_from_end` bars before the current bar."""
            end = len(prices) - offset_from_end
            if end < 15:
                return None
            return self.calculate_rsi(prices[:end], 14)

        # Find two most-recent local swing lows (bullish div check)
        lows_idx = [i for i in range(1, len(seg) - 1)
                    if seg[i] < seg[i - 1] and seg[i] < seg[i + 1]]
        if len(lows_idx) >= 2:
            i1, i2 = lows_idx[-2], lows_idx[-1]
            offset1 = look_back - i1  # bars from end
            offset2 = look_back - i2
            rsi_i1 = _rsi_at(max(offset1, 0))
            rsi_i2 = _rsi_at(max(offset2, 0))
            if rsi_i1 and rsi_i2:
                if seg[i2] < seg[i1] and rsi_i2 > rsi_i1 + 2:
                    return "BULLISH_DIV"

        # Find two most-recent local swing highs (bearish div check)
        highs_idx = [i for i in range(1, len(seg) - 1)
                     if seg[i] > seg[i - 1] and seg[i] > seg[i + 1]]
        if len(highs_idx) >= 2:
            i1, i2 = highs_idx[-2], highs_idx[-1]
            offset1 = look_back - i1
            offset2 = look_back - i2
            rsi_i1 = _rsi_at(max(offset1, 0))
            rsi_i2 = _rsi_at(max(offset2, 0))
            if rsi_i1 and rsi_i2:
                if seg[i2] > seg[i1] and rsi_i2 < rsi_i1 - 2:
                    return "BEARISH_DIV"
        return "NONE"

    def _detect_macd_divergence(self, prices: List[float],
                                 look_back: int = 30) -> str:
        """
        MACD-histogram divergence.
        Returns 'BULLISH_DIV', 'BEARISH_DIV', or 'NONE'.
        """
        hist = self._build_macd_histogram(prices)
        if len(hist) < look_back + 5:
            return "NONE"
        seg_p = prices[-look_back:]
        seg_h = hist[-look_back:]

        lows_idx = [i for i in range(1, len(seg_p) - 1)
                    if seg_p[i] < seg_p[i - 1] and seg_p[i] < seg_p[i + 1]]
        if len(lows_idx) >= 2:
            i1, i2 = lows_idx[-2], lows_idx[-1]
            if i2 < len(seg_h) and i1 < len(seg_h):
                if seg_p[i2] < seg_p[i1] and seg_h[i2] > seg_h[i1] + 1e-9:
                    return "BULLISH_DIV"

        highs_idx = [i for i in range(1, len(seg_p) - 1)
                     if seg_p[i] > seg_p[i - 1] and seg_p[i] > seg_p[i + 1]]
        if len(highs_idx) >= 2:
            i1, i2 = highs_idx[-2], highs_idx[-1]
            if i2 < len(seg_h) and i1 < len(seg_h):
                if seg_p[i2] > seg_p[i1] and seg_h[i2] < seg_h[i1] - 1e-9:
                    return "BEARISH_DIV"
        return "NONE"

    # ─────────────────────────── ICHIMOKU SNAPSHOT ───────────────────────────

    def calculate_ichimoku(self, prices: List[float]) -> Dict:
        """
        Ichimoku Cloud components (simplified, close-only).
          Tenkan-sen  = (max9 + min9) / 2
          Kijun-sen   = (max26 + min26) / 2
          Senkou A    = (Tenkan + Kijun) / 2  (projected 26 forward — here: current)
          Senkou B    = (max52 + min52) / 2   (projected 26 forward — here: current)
          Chikou span = current close vs close 26 periods ago
        """
        n = len(prices)
        def _midpoint(period):
            window = prices[-period:] if n >= period else prices
            return (max(window) + min(window)) / 2 if window else prices[-1]

        tenkan  = _midpoint(9)
        kijun   = _midpoint(26)
        senkou_a = (tenkan + kijun) / 2
        senkou_b = _midpoint(52)
        chikou_above = prices[-1] > prices[-26] if n >= 27 else None

        above_cloud = prices[-1] > max(senkou_a, senkou_b)
        below_cloud = prices[-1] < min(senkou_a, senkou_b)
        tk_cross_bull = tenkan > kijun   # TK cross bullish

        return {
            "tenkan": tenkan, "kijun": kijun,
            "senkou_a": senkou_a, "senkou_b": senkou_b,
            "above_cloud": above_cloud, "below_cloud": below_cloud,
            "tk_cross_bull": tk_cross_bull,
            "chikou_above": chikou_above,
        }

    # ─────────────────────────── MAIN ANALYSIS ───────────────────────────────

    def analyze_market(self, token: str, prices: List[float],
                       volumes: List[float]) -> Dict:
        """
        Run all indicators, score each, and derive a weighted BUY / SELL / HOLD
        signal with confidence.

        Scoring table (total pool = 24 points)
        ───────────────────────────────────────
        RSI          ± 0-3  (oversold/overbought + direction)
        MACD         ± 1    (histogram sign + crossover)
        SMA cross    ± 1    (20 vs 50)
        Price vs SMA ± 1    (price above/below SMA-20)
        EMA cross    ± 1    (12 vs 26)
        Bollinger    ± 2    (band touch + position)
        Stochastic   ± 2    (overbought / oversold)
        Williams %R  ± 1
        CCI          ± 1
        OBV slope    ± 1
        VWAP         ± 1
        Ichimoku     ± 3    (cloud + TK cross + chikou)
        RSI diverge  ± 3
        MACD diverge ± 2
        ATR filter   (gate only — blocks trades in ultra-low-volatility)
        """
        if len(prices) < 50:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient data"}

        current_price = prices[-1]
        N = len(prices)

        # ── Guard: stablecoins ────────────────────────────────────────────────
        # Use symbol whitelist ONLY — never price range (synthetic histories
        # default to price~1.0 which would falsely trigger a range check).
        _STABLES = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD", "USDH"}
        if token.upper() in _STABLES:
            return self._hold_result(current_price, prices, "Stablecoin — skipped")

        # ── Fixed periods ─────────────────────────────────────────────────────
        SMA_FAST_P = min(20, N);  SMA_SLOW_P = min(50, N)
        EMA_FAST_P = min(12, N);  EMA_SLOW_P = min(26, N)

        sma_fast = self.calculate_sma(prices, SMA_FAST_P)
        sma_slow = self.calculate_sma(prices, SMA_SLOW_P)
        ema_fast = self.calculate_ema(prices, EMA_FAST_P)
        ema_slow = self.calculate_ema(prices, EMA_SLOW_P)

        rsi      = self.calculate_rsi(prices, 14)
        if rsi is None or rsi < 5:
            label = "Insufficient data for RSI" if rsi is None else f"RSI={rsi:.1f} warmup artifact"
            return self._hold_result(current_price, prices, label)

        rsi_prev   = self.calculate_rsi(prices[:-1], 14) if N > 15 else None
        rsi_rising = (rsi_prev is not None) and (rsi > rsi_prev + 1.0)

        # MACD (proper histogram)
        macd_line  = ema_fast - ema_slow
        hist_series = self._build_macd_histogram(prices, EMA_FAST_P, EMA_SLOW_P)
        signal_line = self.calculate_ema(
            [ema_fast - ema_slow] * 9, 9)  # proxy if hist_series too short
        macd_hist_current = hist_series[-1] if hist_series else 0.0
        macd_hist_prev    = hist_series[-2] if len(hist_series) > 1 else 0.0
        macd_hist_growing = macd_hist_current > macd_hist_prev

        # Bollinger Bands
        bb_upper, bb_mid, bb_lower = self.calculate_bollinger_bands(prices, 20, 2.0)
        bb_width = (bb_upper - bb_lower) / bb_mid if bb_mid > 0 else 0

        # ATR — volatility gate
        atr = self.calculate_atr(prices, 14)
        atr_pct = (atr / current_price) if (atr and current_price > 0) else 0
        # Skip if volatility is essentially zero (dead market / bad data)
        if atr_pct < 0.0001:
            return self._hold_result(current_price, prices, "ATR near-zero — dead market")

        # Stochastic
        stoch_k, stoch_d = self.calculate_stochastic(prices, 14, 3)

        # Williams %R
        will_r = self.calculate_williams_r(prices, 14)

        # CCI
        cci = self.calculate_cci(prices, 20)

        # OBV slope
        obv_slope = self.calculate_obv_slope(prices, volumes, 10)

        # VWAP
        vwap = self.calculate_vwap(prices, volumes, 50)

        # Ichimoku
        ichi = self.calculate_ichimoku(prices)

        # Divergence
        rsi_div  = self._detect_rsi_divergence(prices, 30)
        macd_div = self._detect_macd_divergence(prices, 30)

        # Trend / momentum labels
        trend    = "BULLISH" if current_price > sma_fast else "BEARISH"
        momentum = "STRONG"  if abs(current_price - sma_fast) / max(sma_fast, 1e-9) > 0.03 else "WEAK"

        # ── SCORING ───────────────────────────────────────────────────────────
        buy_score = 0.0
        sell_score = 0.0
        reasons_buy  = []
        reasons_sell = []

        # ── Overbought / Oversold override ────────────────────────────────────
        # When oscillators are at extremes, trend-following indicators (SMA cross,
        # EMA cross, price>SMA, VWAP, Ichimoku) pile up on the SAME side and
        # overwhelm the oscillator sell signals. We zero out trend-buy votes when
        # price is severely overbought, and trend-sell votes when deeply oversold.
        stoch_k_val = stoch_k if stoch_k is not None else 50.0
        will_r_val  = will_r  if will_r  is not None else -50.0
        _overbought  = (rsi >= 75) or (stoch_k_val >= 80) or (will_r_val >= -15)
        _oversold    = (rsi <= 30) or (stoch_k_val <= 20) or (will_r_val <= -85)

        # 1. RSI (weight 3)
        if rsi < 20:
            if rsi_rising:
                buy_score += 3; reasons_buy.append(f"RSI={rsi:.1f} deeply oversold+turning")
        elif rsi < 35:
            if rsi_rising:
                buy_score += 2; reasons_buy.append(f"RSI={rsi:.1f} oversold+rising")
        elif rsi > 80:
            sell_score += 3; reasons_sell.append(f"RSI={rsi:.1f} deeply overbought")
        elif rsi > 65:
            sell_score += 2; reasons_sell.append(f"RSI={rsi:.1f} overbought")

        # 2. MACD histogram (weight 1)
        if macd_hist_current > 0 and macd_hist_growing:
            buy_score  += 1; reasons_buy.append("MACD histogram rising positive")
        elif macd_hist_current < 0 and not macd_hist_growing:
            sell_score += 1; reasons_sell.append("MACD histogram falling negative")

        # 3. SMA crossover (weight 1) — suppressed when overbought/oversold
        if not _overbought and sma_fast > sma_slow:
            buy_score  += 1; reasons_buy.append("SMA20 > SMA50 (golden cross)")
        elif not _oversold and sma_fast < sma_slow:
            sell_score += 1; reasons_sell.append("SMA20 < SMA50 (death cross)")

        # 4. Price vs SMA-20 (weight 1) — suppressed when overbought/oversold
        if not _overbought and current_price > sma_fast:
            buy_score  += 1; reasons_buy.append("Price above SMA20")
        elif not _oversold and current_price <= sma_fast:
            sell_score += 1; reasons_sell.append("Price below SMA20")

        # 5. EMA-12 vs EMA-26 crossover (weight 1) — suppressed when overbought/oversold
        if not _overbought and ema_fast > ema_slow:
            buy_score  += 1; reasons_buy.append("EMA12 > EMA26")
        elif not _oversold and ema_fast < ema_slow:
            sell_score += 1; reasons_sell.append("EMA12 < EMA26")

        # 6. Bollinger Bands (weight 2)
        if current_price <= bb_lower:
            buy_score  += 2; reasons_buy.append(f"Price at/below lower BB (${bb_lower:.4f})")
        elif current_price >= bb_upper:
            sell_score += 2; reasons_sell.append(f"Price at/above upper BB (${bb_upper:.4f})")
        elif current_price < bb_mid:
            sell_score += 0.5  # below midline — mild bearish
        else:
            buy_score  += 0.5  # above midline — mild bullish

        # 7. Stochastic (weight 2)
        if stoch_k is not None and stoch_d is not None:
            if stoch_k < 20 and stoch_d < 20 and stoch_k > stoch_d:
                buy_score  += 2; reasons_buy.append(f"Stoch %K={stoch_k:.1f} oversold+crossing up")
            elif stoch_k > 80 and stoch_d > 80 and stoch_k < stoch_d:
                sell_score += 2; reasons_sell.append(f"Stoch %K={stoch_k:.1f} overbought+crossing down")
            elif stoch_k < 30:
                buy_score  += 1; reasons_buy.append(f"Stoch %K={stoch_k:.1f} in oversold zone")
            elif stoch_k > 70:
                sell_score += 1; reasons_sell.append(f"Stoch %K={stoch_k:.1f} in overbought zone")

        # 8. Williams %R (weight 1)
        if will_r is not None:
            if will_r <= -80:
                buy_score  += 1; reasons_buy.append(f"Williams %R={will_r:.1f} oversold")
            elif will_r >= -20:
                sell_score += 1; reasons_sell.append(f"Williams %R={will_r:.1f} overbought")

        # 9. CCI (weight 1)
        if cci is not None:
            if cci < -100:
                buy_score  += 1; reasons_buy.append(f"CCI={cci:.1f} (<-100 oversold)")
            elif cci > 100:
                sell_score += 1; reasons_sell.append(f"CCI={cci:.1f} (>+100 overbought)")

        # 10. OBV slope (weight 1) — not suppressed (volume is independent confirmation)
        if obv_slope == 1:
            buy_score  += 1; reasons_buy.append("OBV rising — accumulation")
        elif obv_slope == -1:
            sell_score += 1; reasons_sell.append("OBV falling — distribution")

        # 11. VWAP (weight 1) — suppressed when overbought/oversold
        if vwap > 0:
            if not _overbought and current_price > vwap * 1.005:
                buy_score  += 1; reasons_buy.append(f"Price above VWAP (${vwap:.4f})")
            elif not _oversold and current_price < vwap * 0.995:
                sell_score += 1; reasons_sell.append(f"Price below VWAP (${vwap:.4f})")

        # 12. Ichimoku (weight 3) — suppressed when overbought/oversold
        if not _overbought and ichi["above_cloud"]:
            buy_score  += 1; reasons_buy.append("Price above Ichimoku cloud")
        elif not _oversold and ichi["below_cloud"]:
            sell_score += 1; reasons_sell.append("Price below Ichimoku cloud")
        if not _overbought and ichi["tk_cross_bull"]:
            buy_score  += 1; reasons_buy.append("Ichimoku TK bullish cross")
        elif not _oversold and not ichi["tk_cross_bull"]:
            sell_score += 1; reasons_sell.append("Ichimoku TK bearish cross")
        if not _overbought and ichi["chikou_above"] is True:
            buy_score  += 1; reasons_buy.append("Chikou above price 26 periods ago")
        elif not _oversold and ichi["chikou_above"] is False:
            sell_score += 1; reasons_sell.append("Chikou below price 26 periods ago")

        # 13. RSI Divergence (weight 3 — high importance)
        if rsi_div == "BULLISH_DIV":
            buy_score  += 3; reasons_buy.append("🔀 Bullish RSI divergence detected")
        elif rsi_div == "BEARISH_DIV":
            sell_score += 3; reasons_sell.append("🔀 Bearish RSI divergence detected")

        # 14. MACD Divergence (weight 2)
        if macd_div == "BULLISH_DIV":
            buy_score  += 2; reasons_buy.append("🔀 Bullish MACD divergence detected")
        elif macd_div == "BEARISH_DIV":
            sell_score += 2; reasons_sell.append("🔀 Bearish MACD divergence detected")

        # ── Final signal ──────────────────────────────────────────────────────
        total = buy_score + sell_score
        if buy_score > sell_score:
            signal     = "BUY"
            confidence = min(buy_score / max(total, 1), 1.0)
            reason     = "; ".join(reasons_buy[:4]) or "Multi-indicator BUY"
        elif sell_score > buy_score:
            signal     = "SELL"
            confidence = min(sell_score / max(total, 1), 1.0)
            reason     = "; ".join(reasons_sell[:4]) or "Multi-indicator SELL"
        else:
            signal     = "HOLD"
            confidence = 0.5
            reason     = "Indicators balanced — no clear edge"

        return {
            "signal":        signal,
            "confidence":    confidence,
            "reason":        reason,
            # Core
            "current_price": current_price,
            "trend":         trend,
            "momentum":      momentum,
            # MA
            "sma_20":        sma_fast,
            "sma_50":        sma_slow,
            "ema_12":        ema_fast,
            "ema_26":        ema_slow,
            # MACD
            "macd":          macd_line,
            "macd_signal":   signal_line,
            "macd_histogram": macd_hist_current,
            # Oscillators
            "rsi":           rsi,
            "stoch_k":       stoch_k,
            "stoch_d":       stoch_d,
            "williams_r":    will_r,
            "cci":           cci,
            # Volatility
            "bb_upper":      bb_upper,
            "bb_mid":        bb_mid,
            "bb_lower":      bb_lower,
            "bb_width":      bb_width,
            "atr":           atr,
            "atr_pct":       atr_pct,
            # Volume
            "obv_slope":     obv_slope,
            "vwap":          vwap,
            # Ichimoku
            "ichi_tenkan":   ichi["tenkan"],
            "ichi_kijun":    ichi["kijun"],
            "ichi_above_cloud": ichi["above_cloud"],
            "ichi_below_cloud": ichi["below_cloud"],
            # Divergence
            "rsi_divergence":  rsi_div,
            "macd_divergence": macd_div,
            # Score breakdown
            "buy_score":     buy_score,
            "sell_score":    sell_score,
            "overbought":    _overbought,
            "oversold":      _oversold,
        }

    # ─────────────────────────── HELPERS ─────────────────────────────────────

    def _hold_result(self, current_price: float, prices: List[float],
                     reason: str) -> Dict:
        """Return a HOLD result with safe defaults."""
        sma = self.calculate_sma(prices, min(20, len(prices))) if prices else current_price
        return {
            "signal": "HOLD", "confidence": 0.0, "reason": reason,
            "current_price": current_price, "trend": "NEUTRAL", "momentum": "WEAK",
            "sma_20": sma, "sma_50": sma, "ema_12": sma, "ema_26": sma,
            "macd": 0.0, "macd_signal": 0.0, "macd_histogram": 0.0,
            "rsi": 50.0, "stoch_k": None, "stoch_d": None,
            "williams_r": None, "cci": None,
            "bb_upper": current_price, "bb_mid": current_price,
            "bb_lower": current_price, "bb_width": 0.0,
            "atr": None, "atr_pct": 0.0,
            "obv_slope": 0, "vwap": current_price,
            "ichi_tenkan": current_price, "ichi_kijun": current_price,
            "ichi_above_cloud": None, "ichi_below_cloud": None,
            "rsi_divergence": "NONE", "macd_divergence": "NONE",
            "buy_score": 0.0, "sell_score": 0.0,
            "overbought": False, "oversold": False,
        }

    def _generate_reason(self, signal: str, rsi: float, trend: str,
                         macd: float, signal_line: float) -> str:
        """Legacy compatibility shim — kept for any external callers."""
        reasons = []
        if rsi < 30:
            reasons.append("RSI oversold")
        elif rsi > 70:
            reasons.append("RSI overbought")
        reasons.append("Bullish trend" if trend == "BULLISH" else "Bearish trend")
        reasons.append("MACD bullish" if macd > signal_line else "MACD bearish")
        return "; ".join(reasons)


class SolanaPortfolio:
    """Portfolio management for Solana tokens"""
    
    def __init__(self, initial_usdc: float = 1000.0):
        self.initial_usdc = initial_usdc
        self.usdc_balance = initial_usdc
        self.positions: Dict[str, Position] = {}
        self.trade_history: List[Trade] = []
    
    def add_trade(self, trade: Trade, token_price: float):
        """Record a trade"""
        self.trade_history.append(trade)
        
        if trade.side == "buy":
            # Buying token with USDC
            self.usdc_balance -= trade.amount_in
            
            token = trade.token_out
            # Guard: if we received zero tokens (bad quote / unknown price), skip
            if trade.amount_out <= 0:
                return

            if token in self.positions:
                pos = self.positions[token]
                total_cost = (pos.avg_price_usdc * pos.amount) + trade.amount_in
                pos.amount += trade.amount_out
                pos.avg_price_usdc = total_cost / pos.amount if pos.amount > 0 else 0.0
            else:
                self.positions[token] = Position(
                    token=token,
                    amount=trade.amount_out,
                    avg_price_usdc=trade.price,
                    current_price=token_price,
                    pnl=0.0,
                    pnl_percentage=0.0
                )
        else:  # sell
            # Selling token for USDC
            self.usdc_balance += trade.amount_out
            
            token = trade.token_in
            if token in self.positions:
                self.positions[token].amount -= trade.amount_in
                if self.positions[token].amount <= 0.000001:
                    del self.positions[token]
    
    def update_prices(self, prices: Dict[str, float]):
        """Update current prices for positions"""
        for token, price in prices.items():
            if token in self.positions:
                pos = self.positions[token]
                pos.current_price = price
                pos.pnl = (price - pos.avg_price_usdc) * pos.amount
                if pos.avg_price_usdc > 0:
                    pos.pnl_percentage = ((price - pos.avg_price_usdc) / pos.avg_price_usdc) * 100
                else:
                    pos.pnl_percentage = 0.0
    
    def get_total_value(self) -> float:
        """Calculate total portfolio value in USDC"""
        positions_value = sum(
            pos.amount * pos.current_price 
            for pos in self.positions.values()
        )
        return self.usdc_balance + positions_value
    
    def get_performance(self) -> Dict:
        """Get portfolio performance metrics"""
        total_value = self.get_total_value()
        total_return = total_value - self.initial_usdc
        total_return_pct = (total_return / self.initial_usdc) * 100
        
        total_trades = len([t for t in self.trade_history if t.status == "filled"])
        
        return {
            "initial_balance": self.initial_usdc,
            "current_value": total_value,
            "usdc_balance": self.usdc_balance,
            "total_return": total_return,
            "total_return_pct": total_return_pct,
            "total_trades": total_trades
        }


class SolanaTradingAgent:
    """Main Solana trading agent orchestrator"""
    
    def __init__(self, mode: TradingMode = TradingMode.PAPER,
                 wallet_address: str = "",
                 initial_usdc: float = 1000.0):
        self.mode = mode
        self.wallet_address = wallet_address
        self.client = JupiterClient(mode)
        self.ai = TradingAI()
        self.portfolio = SolanaPortfolio(initial_usdc)
        self.watched_tokens = []
        self.price_history = {}
        
    def add_token(self, token_symbol: str, token_address: str = ""):
        """Add a token to watch list. Accepts symbol (must be in TOKENS) or symbol+address."""
        if token_symbol not in TOKENS:
            if token_address:
                # Dynamically register a new token (e.g. from trending detector)
                TOKENS[token_symbol] = token_address
            else:
                print(f"✗ Unknown token: {token_symbol}")
                print(f"  Available: {', '.join(TOKENS.keys())}")
                return

        if token_symbol not in self.watched_tokens:
            self.watched_tokens.append(token_symbol)
            self.price_history[token_symbol] = {"prices": [], "volumes": []}
            print(f"✓ Added {token_symbol} to watch list")
    
    def remove_token(self, token_symbol: str):
        """Remove a token from watch list"""
        if token_symbol in self.watched_tokens:
            self.watched_tokens.remove(token_symbol)
            print(f"✓ Removed {token_symbol} from watch list")

    def prefill_price_history(self, token_symbol: str, prices: List[float]):
        """
        Pre-load a price history list into the buffer for a token.
        Allows instant analysis without waiting for live data accumulation.
        """
        if token_symbol not in self.price_history:
            self.price_history[token_symbol] = {"prices": [], "volumes": []}

        prices = prices[-1000:]
        self.price_history[token_symbol]["prices"] = list(prices)
        self.price_history[token_symbol]["volumes"] = [1_000_000] * len(prices)

        if prices:
            self.portfolio.update_prices({token_symbol: prices[-1]})
        print(f"  ✓ Pre-filled {len(prices)} historical prices for {token_symbol}")
    
    def update_market_data(self):
        """Fetch and update market data for all watched tokens"""
        for token_symbol in self.watched_tokens:
            try:
                token_mint = TOKENS[token_symbol]
                price = self.client.get_token_price(token_mint)
                
                if price > 0:
                    self.price_history[token_symbol]["prices"].append(price)
                    self.price_history[token_symbol]["volumes"].append(1000000)  # Placeholder
                    
                    # Keep last 1000 data points
                    if len(self.price_history[token_symbol]["prices"]) > 1000:
                        self.price_history[token_symbol]["prices"].pop(0)
                        self.price_history[token_symbol]["volumes"].pop(0)
                    
                    # Update portfolio prices
                    self.portfolio.update_prices({token_symbol: price})
                    
            except Exception as e:
                print(f"Error updating {token_symbol}: {e}")
    
    def analyze_token(self, token_symbol: str) -> Optional[Dict]:
        """Analyze a specific token"""
        if token_symbol not in self.price_history:
            print(f"No data for {token_symbol}")
            return None
        
        prices = self.price_history[token_symbol]["prices"]
        volumes = self.price_history[token_symbol]["volumes"]
        
        if len(prices) < 50:
            print(f"Insufficient data for {token_symbol} (need at least 50 data points, have {len(prices)})")
            return None
        
        analysis = self.ai.analyze_market(token_symbol, prices, volumes)
        return analysis
    
    def execute_swap(self, from_token: str, to_token: str, amount: float):
        """Execute a token swap"""
        try:
            # Get token mints
            from_mint = TOKENS.get(from_token)
            to_mint = TOKENS.get(to_token)
            
            if not from_mint or not to_mint:
                print("✗ Invalid token symbols")
                return None

            # ── Paper mode: compute amounts directly from real prices ─────────
            # This avoids the decimal-precision bug where _simulate_quote and
            # execute_swap both hardcode 1e6 divisor regardless of the token's
            # actual decimal count (SOL=9, most SPL=6, some=8).
            # By deriving out_amount from the real USD price we stay consistent
            # with the price history and P&L tracking.
            if self.mode == TradingMode.PAPER:
                from_price = self.client.get_token_price(from_mint)
                to_price   = self.client.get_token_price(to_mint)

                # Hard abort if either price is unknown — don't use a wrong fallback
                if from_price <= 0:
                    print(f"✗ Could not get price for {from_token} — swap aborted")
                    return None
                if to_price <= 0:
                    print(f"✗ Could not get price for {to_token} — swap aborted")
                    return None

                slippage    = 0.005   # 0.5%
                input_value = amount * from_price
                out_amount  = (input_value / to_price) * (1 - slippage)

                if to_token == "USDC":
                    side       = "sell"
                    price      = out_amount / amount  # USDC per token
                    amount_in  = amount
                    amount_out = out_amount
                    token_in   = from_token
                    token_out  = to_token
                else:
                    side       = "buy"
                    price      = amount / out_amount if out_amount > 0 else 0  # USDC per token
                    amount_in  = amount
                    amount_out = out_amount
                    token_in   = from_token
                    token_out  = to_token

                import random
                tx_signature = f"sim_{int(time.time())}_{random.randint(1000, 9999)}"

            else:
                # ── Live mode: use Jupiter quote ──────────────────────────────
                amount_lamports = int(amount * 1e9)
                quote = self.client.get_quote(from_mint, to_mint, amount_lamports)
                if not quote:
                    print("✗ Failed to get quote")
                    return None

                # Jupiter returns outAmount in the output token's native decimals.
                # For live trading, the correct divisor must be looked up per-token.
                # USDC = 6 decimals. Other tokens vary — query on-chain or use Jupiter metadata.
                out_decimals = 6  # USDC default; override for non-USDC output in live mode
                out_amount = int(quote["outAmount"]) / (10 ** out_decimals)

                tx_signature = self.client.execute_swap(quote, self.wallet_address)
                if not tx_signature:
                    print("✗ Swap execution failed")
                    return None

                if to_token == "USDC":
                    side       = "sell"
                    price      = out_amount / amount
                    amount_in  = amount
                    amount_out = out_amount
                    token_in   = from_token
                    token_out  = to_token
                else:
                    side       = "buy"
                    price      = amount / out_amount if out_amount > 0 else 0
                    amount_in  = amount
                    amount_out = out_amount
                    token_in   = from_token
                    token_out  = to_token
            
            # Create trade record
            trade = Trade(
                timestamp=datetime.now().isoformat(),
                token_in=token_in,
                token_out=token_out,
                side=side,
                price=price,
                amount_in=amount_in,
                amount_out=amount_out,
                tx_signature=tx_signature,
                status="filled"
            )
            
            # Update portfolio
            current_price = self.price_history[token_out]["prices"][-1] if token_out in self.price_history and self.price_history[token_out]["prices"] else price
            self.portfolio.add_trade(trade, current_price)
            
            print(f"\n✓ Swap executed: {from_token} → {to_token}")
            print(f"  Amount In: {amount_in:.6f} {token_in}")
            print(f"  Amount Out: {amount_out:.6f} {token_out}")
            print(f"  Price: ${price:.6f}")
            print(f"  TX: {tx_signature}")
            
            return trade
            
        except Exception as e:
            print(f"✗ Error executing swap: {e}")
            return None
    
    def show_portfolio(self):
        """Display current portfolio status"""
        print("\n" + "="*60)
        print("SOLANA PORTFOLIO STATUS")
        print("="*60)
        
        perf = self.portfolio.get_performance()
        print(f"\nUSDC Balance: ${self.portfolio.usdc_balance:,.2f}")
        print(f"Total Value: ${perf['current_value']:,.2f}")
        print(f"Total Return: ${perf['total_return']:,.2f} ({perf['total_return_pct']:.2f}%)")
        print(f"Total Swaps: {perf['total_trades']}")
        
        if self.portfolio.positions:
            print("\n" + "-"*60)
            print("OPEN POSITIONS")
            print("-"*60)
            print(f"{'Token':<10} {'Amount':<15} {'Avg Price':<12} {'Current':<12} {'P&L':<15}")
            print("-"*60)
            
            for token, pos in self.portfolio.positions.items():
                pnl_color = "+" if pos.pnl >= 0 else "-"
                print(f"{token:<10} {pos.amount:<15.6f} ${pos.avg_price_usdc:<11.4f} "
                      f"${pos.current_price:<11.4f} {pnl_color}${abs(pos.pnl):<7.2f} ({pos.pnl_percentage:+.2f}%)")
        
        print("="*60 + "\n")
    
    def show_analysis(self, token_symbol: str):
        """Display full multi-indicator technical analysis for a token"""
        analysis = self.analyze_token(token_symbol)
        if not analysis:
            return

        sig   = analysis['signal']
        conf  = analysis['confidence']
        price = analysis['current_price']

        sig_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}.get(sig, "⚪")

        print("\n" + "="*70)
        print(f"  📊 TECHNICAL ANALYSIS: {token_symbol}")
        print("="*70)
        print(f"\n  Price  : ${price:.6f}")
        print(f"  Signal : {sig_icon} {sig}  (Confidence: {conf:.1%})")
        print(f"  Trend  : {analysis['trend']}   Momentum: {analysis['momentum']}")
        print(f"  Scores : BUY={analysis['buy_score']:.1f}  SELL={analysis['sell_score']:.1f}")

        print(f"\n  ── Moving Averages ──────────────────────────────────────")
        print(f"  SMA-20  : ${analysis['sma_20']:.6f}   "
              f"SMA-50: ${analysis['sma_50']:.6f}")
        print(f"  EMA-12  : ${analysis['ema_12']:.6f}   "
              f"EMA-26: ${analysis['ema_26']:.6f}")

        print(f"\n  ── MACD ─────────────────────────────────────────────────")
        print(f"  MACD Line : {analysis['macd']:+.6f}")
        print(f"  Histogram : {analysis['macd_histogram']:+.6f}   "
              f"{'▲ growing' if analysis['macd_histogram'] > 0 else '▼ shrinking'}")

        print(f"\n  ── Oscillators ──────────────────────────────────────────")
        rsi_label = ("oversold" if analysis['rsi'] < 35 else
                     "overbought" if analysis['rsi'] > 65 else "neutral")
        print(f"  RSI-14    : {analysis['rsi']:.2f}  [{rsi_label}]")
        sk = analysis.get('stoch_k')
        sd = analysis.get('stoch_d')
        if sk is not None:
            sk_label = "oversold" if sk < 20 else "overbought" if sk > 80 else "neutral"
            print(f"  Stoch %K/D: {sk:.1f} / {sd:.1f}  [{sk_label}]")
        wr = analysis.get('williams_r')
        if wr is not None:
            wr_label = "oversold" if wr <= -80 else "overbought" if wr >= -20 else "neutral"
            print(f"  Williams %R: {wr:.1f}  [{wr_label}]")
        cci_val = analysis.get('cci')
        if cci_val is not None:
            cci_label = "oversold" if cci_val < -100 else "overbought" if cci_val > 100 else "neutral"
            print(f"  CCI-20    : {cci_val:.1f}  [{cci_label}]")

        print(f"\n  ── Bollinger Bands (20, 2σ) ─────────────────────────────")
        print(f"  Upper: ${analysis['bb_upper']:.6f}   "
              f"Mid: ${analysis['bb_mid']:.6f}   "
              f"Lower: ${analysis['bb_lower']:.6f}")
        bb_pos = ("AT/BELOW lower band ⚡" if price <= analysis['bb_lower'] else
                  "AT/ABOVE upper band ⚡" if price >= analysis['bb_upper'] else
                  "inside bands")
        print(f"  Width: {analysis['bb_width']:.4f}   Position: {bb_pos}")
        if analysis.get('atr') is not None:
            print(f"  ATR-14: {analysis['atr']:.6f}  ({analysis['atr_pct']*100:.2f}% of price)")

        print(f"\n  ── Volume Indicators ────────────────────────────────────")
        obv_label = {1: "rising (accumulation) ↑", -1: "falling (distribution) ↓", 0: "flat"
                     }.get(analysis['obv_slope'], "flat")
        print(f"  OBV slope : {obv_label}")
        print(f"  VWAP      : ${analysis['vwap']:.6f}   "
              f"Price {'above' if price > analysis['vwap'] else 'below'} VWAP")

        print(f"\n  ── Ichimoku Cloud ───────────────────────────────────────")
        print(f"  Tenkan-sen : ${analysis['ichi_tenkan']:.6f}   "
              f"Kijun-sen: ${analysis['ichi_kijun']:.6f}")
        cloud_pos = ("ABOVE cloud ☀️" if analysis['ichi_above_cloud'] else
                     "BELOW cloud ☁️" if analysis['ichi_below_cloud'] else "inside cloud")
        print(f"  Cloud pos  : {cloud_pos}")

        print(f"\n  ── Divergence ───────────────────────────────────────────")
        rsi_d  = analysis.get('rsi_divergence',  'NONE')
        macd_d = analysis.get('macd_divergence', 'NONE')
        print(f"  RSI  divergence : {rsi_d}")
        print(f"  MACD divergence : {macd_d}")

        print(f"\n  ── Reason ───────────────────────────────────────────────")
        print(f"  {analysis['reason']}")
        print("="*70 + "\n")


def main():
    """Main CLI interface"""
    parser = argparse.ArgumentParser(description="Solana DEX Trading AI Agent")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                       help="Trading mode (default: paper)")
    parser.add_argument("--wallet", default="", help="Solana wallet address")
    parser.add_argument("--balance", type=float, default=1000.0,
                       help="Initial USDC balance for paper trading (default: 1000)")
    
    args = parser.parse_args()
    
    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER
    
    if mode == TradingMode.LIVE and not args.wallet:
        print("⚠️  Warning: Live mode without wallet address")
        print("   Trades will be simulated")
    
    agent = SolanaTradingAgent(mode, args.wallet, args.balance)
    
    print("\n" + "="*60)
    print("SOLANA DEX TRADING AI AGENT")
    print("="*60)
    print(f"Mode: {mode.value.upper()}")
    print(f"DEX: Jupiter Aggregator")
    if mode == TradingMode.PAPER:
        print(f"Initial USDC: ${args.balance:,.2f}")
    print("="*60 + "\n")
    
    print("Available Tokens:")
    print(", ".join(TOKENS.keys()))
    print("\nCommands:")
    print("  add <token>           - Add token to watch list (e.g., add SOL)")
    print("  remove <token>        - Remove token from watch list")
    print("  list                  - Show watch list")
    print("  analyze <token>       - Analyze token")
    print("  buy <token> <usdc>    - Buy token with USDC (e.g., buy SOL 100)")
    print("  sell <token> <amount> - Sell token for USDC (e.g., sell SOL 0.5)")
    print("  portfolio             - Show portfolio status")
    print("  update                - Update market data")
    print("  help                  - Show this help")
    print("  quit                  - Exit\n")
    
    while True:
        try:
            command = input("solana-ai> ").strip().lower()
            
            if not command:
                continue
            
            parts = command.split()
            cmd = parts[0]
            
            if cmd == "quit" or cmd == "exit":
                print("Goodbye!")
                break
            
            elif cmd == "help":
                print("\nAvailable commands:")
                print("  add <token>           - Add token to watch list")
                print("  remove <token>        - Remove token from watch list")
                print("  list                  - Show watch list")
                print("  analyze <token>       - Analyze token")
                print("  buy <token> <usdc>    - Buy token with USDC")
                print("  sell <token> <amount> - Sell token for USDC")
                print("  portfolio             - Show portfolio status")
                print("  update                - Update market data")
                print("  quit                  - Exit\n")
            
            elif cmd == "add" and len(parts) > 1:
                agent.add_token(parts[1].upper())
            
            elif cmd == "remove" and len(parts) > 1:
                agent.remove_token(parts[1].upper())
            
            elif cmd == "list":
                if agent.watched_tokens:
                    print("\nWatched Tokens:")
                    for token in agent.watched_tokens:
                        print(f"  - {token}")
                    print()
                else:
                    print("\nNo tokens in watch list\n")
            
            elif cmd == "update":
                print("Updating market data...")
                agent.update_market_data()
                print("✓ Market data updated\n")
            
            elif cmd == "analyze" and len(parts) > 1:
                token = parts[1].upper()
                if token not in agent.watched_tokens:
                    print(f"Token {token} not in watch list. Adding it...")
                    agent.add_token(token)
                    agent.update_market_data()
                agent.show_analysis(token)
            
            elif cmd == "buy" and len(parts) >= 3:
                token = parts[1].upper()
                try:
                    usdc_amount = float(parts[2])
                    if token not in agent.watched_tokens:
                        agent.add_token(token)
                        agent.update_market_data()
                    agent.execute_swap("USDC", token, usdc_amount)
                except ValueError:
                    print("✗ Invalid amount\n")
            
            elif cmd == "sell" and len(parts) >= 3:
                token = parts[1].upper()
                try:
                    amount = float(parts[2])
                    agent.execute_swap(token, "USDC", amount)
                except ValueError:
                    print("✗ Invalid amount\n")
            
            elif cmd == "portfolio":
                agent.show_portfolio()
            
            else:
                print(f"✗ Unknown command: {cmd}")
                print("  Type 'help' for available commands\n")
        
        except KeyboardInterrupt:
            print("\n\nGoodbye!")
            break
        except Exception as e:
            print(f"✗ Error: {e}\n")


if __name__ == "__main__":
    main()