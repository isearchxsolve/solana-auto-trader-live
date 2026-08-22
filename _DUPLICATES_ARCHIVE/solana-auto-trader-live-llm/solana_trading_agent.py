#!/usr/bin/env python3
"""
Solana DEX Trading AI Agent
Supports Jupiter Aggregator integration with paper and live trading modes.

Paper mode  — uses real DexScreener prices, simulates execution instantly.
Live mode   — gets real Jupiter quotes, signs and broadcasts via SolanaWallet.
"""

import time
import random
import requests
from datetime import datetime
from typing import Dict, List, Optional
import argparse
import sys
from dataclasses import dataclass
from enum import Enum


# ── Enums ──────────────────────────────────────────────────────────────────────

class TradingMode(Enum):
    PAPER = "paper"
    LIVE  = "live"


class OrderSide(Enum):
    BUY  = "buy"
    SELL = "sell"


# ── Data classes ───────────────────────────────────────────────────────────────

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

TOKENS: Dict[str, str] = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "RAY":  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JTO":  "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
}

# Native decimal places — needed for correct lamport conversion in live mode
TOKEN_DECIMALS: Dict[str, int] = {
    "SOL":  9, "USDC": 6, "USDT": 6, "RAY":  6,
    "BONK": 5, "JUP":  6, "WIF":  6, "JTO":  9, "PYTH": 6,
}


# ── Jupiter Client ─────────────────────────────────────────────────────────────

class JupiterClient:
    """
    Jupiter Aggregator v6 API client.
    Handles price fetching (DexScreener) and swap quotes (Jupiter).
    """

    QUOTE_URL = "https://quote-api.jup.ag/v6/quote"

    def __init__(self, mode: TradingMode = TradingMode.PAPER):
        self.mode         = mode
        self._price_cache: Dict[str, float] = {}
        self._price_ts:    Dict[str, float] = {}
        self._CACHE_TTL   = 10  # seconds

    # ── Price ──────────────────────────────────────────────────────────────────

    def get_token_price(self, token_mint: str) -> float:
        """
        Real-time price from DexScreener (both paper and live mode).
        Cached per mint for 10 s; returns stale value on network error.
        """
        now          = time.time()
        cached_price = self._price_cache.get(token_mint, 0.0)
        last_fetch   = self._price_ts.get(token_mint, 0.0)

        if cached_price > 0 and (now - last_fetch) < self._CACHE_TTL:
            return cached_price * (1 + random.uniform(-0.001, 0.001))

        try:
            r = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                timeout=6,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                pairs     = r.json().get("pairs", [])
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                best      = sol_pairs[0] if sol_pairs else (pairs[0] if pairs else None)
                if best:
                    price = float(best.get("priceUsd", 0) or 0)
                    if price > 0:
                        self._price_cache[token_mint] = price
                        self._price_ts[token_mint]    = now
                        return price * (1 + random.uniform(-0.001, 0.001))
        except Exception:
            pass

        return cached_price * (1 + random.uniform(-0.001, 0.001)) if cached_price > 0 else 0.0

    # ── Quote ──────────────────────────────────────────────────────────────────

    def get_quote(self, input_mint: str, output_mint: str,
                  amount_raw: int, slippage_bps: int = 100) -> Optional[Dict]:
        """
        Paper  -> synthetic quote from cached prices.
        Live   -> real Jupiter v6 quote API.
        amount_raw is in the input token's native units (lamports / smallest unit).
        """
        if self.mode == TradingMode.PAPER:
            return self._simulate_quote(input_mint, output_mint, amount_raw)

        try:
            r = requests.get(
                self.QUOTE_URL,
                params={
                    "inputMint":        input_mint,
                    "outputMint":       output_mint,
                    "amount":           amount_raw,
                    "slippageBps":      slippage_bps,
                    "onlyDirectRoutes": False,
                },
                timeout=10,
            )
            if r.status_code == 200:
                return r.json()
            print(f"  [Jupiter] Quote HTTP {r.status_code}: {r.text[:200]}")
            return None
        except Exception as e:
            print(f"  [Jupiter] Quote error: {e}")
            return None

    def _simulate_quote(self, input_mint: str, output_mint: str,
                        amount_raw: int) -> Dict:
        """Build a quote from cached DexScreener prices."""
        def _p(mint: str) -> float:
            c = self._price_cache.get(mint, 0.0)
            if c > 0:
                return c
            fallback = {
                TOKENS["SOL"]:  110.0, TOKENS["USDC"]: 1.0,
                TOKENS["USDT"]: 1.0,   TOKENS["RAY"]:  1.5,
                TOKENS["BONK"]: 0.00002, TOKENS["JUP"]: 0.85,
                TOKENS["WIF"]:  2.3,   TOKENS["JTO"]:  3.1,
                TOKENS["PYTH"]: 0.42,
            }
            return fallback.get(mint, 1.0)

        sym_in  = next((s for s, m in TOKENS.items() if m == input_mint),  None)
        sym_out = next((s for s, m in TOKENS.items() if m == output_mint), None)
        dec_in  = TOKEN_DECIMALS.get(sym_in,  9) if sym_in  else 9
        dec_out = TOKEN_DECIMALS.get(sym_out, 6) if sym_out else 6

        in_human  = amount_raw / (10 ** dec_in)
        in_usd    = in_human * _p(input_mint)
        out_human = (in_usd / _p(output_mint)) * 0.995   # 0.5% slippage
        out_raw   = int(out_human * (10 ** dec_out))

        return {
            "inputMint":      input_mint,
            "outputMint":     output_mint,
            "inAmount":       str(amount_raw),
            "outAmount":      str(out_raw),
            "priceImpactPct": 0.1,
            "routePlan":      [{"swapInfo": {"label": "Simulated"}}],
            "_dec_out":       dec_out,
        }


# ── Technical indicators ───────────────────────────────────────────────────────

class TradingAI:
    """Multi-indicator analysis engine."""

    def __init__(self):
        self.indicators_cache = {}

    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(prices)):
            d = prices[i] - prices[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        ag = sum(gains[-period:])  / period
        al = sum(losses[-period:]) / period
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    def calculate_sma(self, prices: List[float], period: int) -> float:
        if not prices:
            return 0.0
        if len(prices) < period:
            return sum(prices) / len(prices)
        return sum(prices[-period:]) / period

    def calculate_ema(self, prices: List[float], period: int) -> float:
        if not prices:
            return 0.0
        if len(prices) < period:
            return self.calculate_sma(prices, len(prices))
        mult = 2.0 / (period + 1)
        ema  = self.calculate_sma(prices[:period], period)
        for p in prices[period:]:
            ema = p * mult + ema * (1 - mult)
        return ema

    def analyze_market(self, token: str,
                       prices: List[float],
                       volumes: List[float]) -> Dict:
        """
        Full technical analysis.
        Returns a dict compatible with TrendingSolanaAutoTrader and the CLI.
        """
        N   = len(prices)
        cur = prices[-1] if prices else 0.0

        _base = {
            "signal": "HOLD", "confidence": 0.0, "reason": "Insufficient data",
            "rsi": 50.0, "rsi_rising": False, "trend": "NEUTRAL", "momentum": "WEAK",
            "current_price": cur, "sma_20": cur, "sma_50": cur, "macd": 0.0,
            "ema_slope": 0.0, "relative_volume": 1.0, "rel_vol": 1.0,
            "buy_pts": 0, "buy_pts_detail": {}, "sell_pts": 0,
            "bb": None, "bb_squeeze": False,
        }

        if N < 50:
            return _base

        sma_fast = self.calculate_sma(prices, min(20, N))
        sma_slow = self.calculate_sma(prices, min(50, N))
        ema_fast = self.calculate_ema(prices, min(12, N))
        ema_slow = self.calculate_ema(prices, min(26, N))
        rsi      = self.calculate_rsi(prices, 14)

        if rsi is None:
            _base.update({"reason": "RSI not ready", "sma_20": sma_fast, "sma_50": sma_slow})
            return _base

        # Stablecoin guard
        if 0.95 <= cur <= 1.05:
            _base.update({"reason": "Stablecoin — skipped", "rsi": rsi,
                          "sma_20": sma_fast, "sma_50": sma_slow})
            return _base

        # Synthetic warmup guard (RSI < 5 is a mathematical artefact)
        if rsi < 5:
            _base.update({"reason": f"RSI={rsi:.1f} synthetic artefact", "rsi": rsi,
                          "sma_20": sma_fast, "sma_50": sma_slow})
            return _base

        # MACD with proper signal line
        macd        = ema_fast - ema_slow
        window      = min(50, N)
        macd_series = []
        for i in range(window):
            sub = prices[-(window - i):]
            _ef = self.calculate_ema(sub, min(100, max(1, len(sub) // 8)))
            _es = self.calculate_ema(sub, min(200, max(1, len(sub) // 4)))
            macd_series.append(_ef - _es)
        signal_line = self.calculate_ema(macd_series, min(9, len(macd_series)))

        # Trend / momentum
        trend    = "BULLISH" if cur > sma_fast else "BEARISH"
        momentum = "STRONG"  if abs(cur - sma_fast) / sma_fast > 0.03 else "WEAK"

        # RSI direction
        rsi_prev   = self.calculate_rsi(prices[:-3], 14) if N > 17 else None
        rsi_rising = (rsi_prev is not None) and (rsi > rsi_prev + 1.0)

        # Relative volume
        vol_ma  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1.0
        rel_vol = (volumes[-1] / vol_ma) if vol_ma > 0 else 1.0

        # EMA slope (6-bar)
        ema_slope = 0.0
        if N >= 27:
            e_now  = self.calculate_ema(prices,      20)
            e_prev = self.calculate_ema(prices[:-6], 20)
            ema_slope = (e_now - e_prev) / e_prev if e_prev else 0.0

        # Scoring
        buy_sig = sell_sig = 0
        if   rsi < 20 and rsi_rising: buy_sig  += 3
        elif rsi < 35 and rsi_rising: buy_sig  += 2
        elif rsi > 80:                sell_sig += 3
        elif rsi > 65:                sell_sig += 2

        if sma_fast > sma_slow: buy_sig  += 1
        else:                   sell_sig += 1

        if cur > sma_fast: buy_sig  += 1
        else:              sell_sig += 1

        if macd > signal_line: buy_sig  += 1
        else:                  sell_sig += 1

        # total = buy_sig + sell_sig
        # if buy_sig > sell_sig:
        #     signal, conf = "BUY",  min(buy_sig  / max(total, 1), 1.0)
        # elif sell_sig > buy_sig:
        #     signal, conf = "SELL", min(sell_sig / max(total, 1), 1.0)
        # else:
        #     signal, conf = "HOLD", 0.5

        # --- Balanced Continuation Logic ---

        signal = "HOLD"
        conf   = 0.5
        total  = buy_sig + sell_sig

        # Strong SELL only if clearly dominant
        if sell_sig >= 3 and sell_sig > buy_sig:
            signal = "SELL"
            conf   = min(sell_sig / max(total, 1), 1.0)

        # Favor continuation in bullish trend
        elif (
            buy_sig >= 3
            and trend == "BULLISH"
            and 40 <= rsi <= 65
        ):
            signal = "BUY"
            conf   = min(buy_sig / max(total, 1), 1.0)

        # Mild buy bias in bullish structure
        elif buy_sig > sell_sig and trend == "BULLISH":
            signal = "BUY"
            conf   = min(buy_sig / max(total, 1), 1.0)

        # Fallback SELL if bearish and dominant
        elif sell_sig > buy_sig and trend == "BEARISH":
            signal = "SELL"
            conf   = min(sell_sig / max(total, 1), 1.0)


        # ── Bollinger Bands (used both for buy_pts and bb_squeeze signal) ──────
        bb_result   = None
        bb_squeeze  = False
        if N >= 20:
            bb_period = 20
            bb_mid    = self.calculate_sma(prices, bb_period)
            variance  = sum((p - bb_mid) ** 2 for p in prices[-bb_period:]) / bb_period
            bb_std    = variance ** 0.5
            bb_upper  = bb_mid + 2 * bb_std
            bb_lower  = bb_mid - 2 * bb_std
            bb_range  = bb_upper - bb_lower
            pct_b     = (cur - bb_lower) / bb_range if bb_range > 0 else 0.5

            # Squeeze: bands narrow relative to recent average band width
            if N >= 40:
                widths = []
                for i in range(-40, -20):
                    sub    = prices[i:i+20]
                    s_mid  = sum(sub) / 20
                    s_std  = (sum((p - s_mid)**2 for p in sub) / 20) ** 0.5
                    widths.append(s_std * 4)
                avg_width = sum(widths) / len(widths) if widths else bb_range
                bb_squeeze = bb_range < avg_width * 0.85

            bb_result = {
                "upper":  bb_upper,
                "mid":    bb_mid,
                "lower":  bb_lower,
                "pct_b":  pct_b,
                "squeeze": bb_squeeze,
            }

        # buy_pts used by TrendingSolanaAutoTrader trade gate
        # Each condition is stored individually for debug display.
        # Max 7 pts — gate requires 4-6 depending on regime.
        pt_trend    = trend == "BULLISH"
        pt_rsi_rise = bool(rsi_rising)
        pt_macd     = macd > signal_line
        pt_rsi_zone = 42 <= rsi <= 62
        pt_vol      = rel_vol >= 1.2
        pt_slope    = ema_slope > 0.0005
        # Bonus: price near lower BB (pullback into support) — valid even with
        # flat volume, useful for short-history tokens
        pt_bb_low   = (bb_result is not None and bb_result["pct_b"] < 0.35)

        buy_pts = sum([pt_trend, pt_rsi_rise, pt_macd, pt_rsi_zone,
                       pt_vol, pt_slope, pt_bb_low])

        # Store breakdown for display in trader loop
        buy_pts_detail = {
            "trend":    pt_trend,
            "rsi_rise": pt_rsi_rise,
            "macd":     pt_macd,
            "rsi_zone": pt_rsi_zone,
            "vol":      pt_vol,
            "slope":    pt_slope,
            "bb_low":   pt_bb_low,
        }


        sell_pts = sum([rsi > 72, macd < 0])

        # Reason string
        parts = []
        if rsi < 30:   parts.append("RSI oversold")
        elif rsi > 70: parts.append("RSI overbought")
        parts.append("bullish trend" if trend == "BULLISH" else "bearish trend")
        parts.append("MACD↑" if macd > signal_line else "MACD↓")
        reason = "; ".join(parts)

        return {
            "signal":          signal,
            "confidence":      conf,
            "reason":          reason,
            "rsi":             rsi,
            "rsi_rising":      rsi_rising,
            "trend":           trend,
            "momentum":        momentum,
            "current_price":   cur,
            "sma_20":          sma_fast,
            "sma_50":          sma_slow,
            "macd":            macd,
            "ema_slope":       ema_slope,
            "relative_volume": rel_vol,
            "rel_vol":         rel_vol,
            "buy_pts":         buy_pts,
            "buy_pts_detail":  buy_pts_detail,
            "sell_pts":        sell_pts,
            "bb":              bb_result,
            "bb_squeeze":      bb_squeeze,
        }


# ── Portfolio ──────────────────────────────────────────────────────────────────

class SolanaPortfolio:
    """USDC balance, token positions, and trade history."""

    def __init__(self, initial_usdc: float = 1000.0):
        self.initial_usdc   = initial_usdc
        self.usdc_balance   = initial_usdc
        self.positions:     Dict[str, Position] = {}
        self.trade_history: List[Trade]         = []

    def add_trade(self, trade: Trade, token_price: float):
        self.trade_history.append(trade)
        if trade.side == "buy":
            self.usdc_balance -= trade.amount_in
            tok = trade.token_out
            if tok in self.positions:
                pos = self.positions[tok]
                total_cost        = pos.avg_price_usdc * pos.amount + trade.amount_in
                pos.amount       += trade.amount_out
                pos.avg_price_usdc = total_cost / pos.amount
            else:
                self.positions[tok] = Position(
                    token=tok, amount=trade.amount_out,
                    avg_price_usdc=trade.price, current_price=token_price,
                    pnl=0.0, pnl_percentage=0.0,
                )
        else:
            self.usdc_balance += trade.amount_out
            tok = trade.token_in
            if tok in self.positions:
                self.positions[tok].amount -= trade.amount_in
                if self.positions[tok].amount <= 1e-6:
                    del self.positions[tok]

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

    def get_total_value(self) -> float:
        return self.usdc_balance + sum(
            p.amount * p.current_price for p in self.positions.values()
        )

    def get_performance(self) -> Dict:
        tv  = self.get_total_value()
        ret = tv - self.initial_usdc
        return {
            "initial_balance":  self.initial_usdc,
            "current_value":    tv,
            "usdc_balance":     self.usdc_balance,
            "total_return":     ret,
            "total_return_pct": ret / self.initial_usdc * 100 if self.initial_usdc else 0,
            "total_trades":     len([t for t in self.trade_history if t.status == "filled"]),
        }


# ── Main agent ─────────────────────────────────────────────────────────────────

class SolanaTradingAgent:
    """
    Orchestrates price data, technical analysis, and trade execution.

    Paper mode:  real prices from DexScreener, instant simulated execution.
    Live mode:   real prices + real Jupiter v6 swaps signed by SolanaWallet.

    Usage — paper:
        agent = SolanaTradingAgent(TradingMode.PAPER, initial_usdc=1000)

    Usage — live:
        from wallet_integration import load_wallet_config, SolanaWallet
        wallet = SolanaWallet(load_wallet_config())
        agent  = SolanaTradingAgent(TradingMode.LIVE, wallet=wallet)
    """

    def __init__(
        self,
        mode:           TradingMode = TradingMode.PAPER,
        wallet_address: str         = "",
        initial_usdc:   float       = 1000.0,
        wallet                      = None,   # SolanaWallet for live mode
    ):
        self.mode           = mode
        self.wallet_address = wallet_address
        self.wallet         = wallet

        self.client    = JupiterClient(mode)
        self.ai        = TradingAI()
        self.portfolio = SolanaPortfolio(initial_usdc)

        self.price_history:  Dict[str, Dict] = {}
        self.watched_tokens: List[str]       = []

        if mode == TradingMode.LIVE and wallet is None:
            print("⚠️  Live mode: no SolanaWallet supplied — swaps will fail.")

    # ── Token management ──────────────────────────────────────────────────────

    def add_token(self, token_symbol: str, token_address: str = ""):
        token_symbol = token_symbol.upper()
        if token_symbol not in TOKENS:
            if token_address:
                TOKENS[token_symbol] = token_address
            else:
                print(f"✗ Unknown token: {token_symbol}")
                return
        if token_symbol not in self.watched_tokens:
            self.watched_tokens.append(token_symbol)
            self.price_history[token_symbol] = {
                "prices": [], "volumes": [], "live_points": 0
            }
            print(f"✓ Added {token_symbol} to watchlist")

    def remove_token(self, token_symbol: str):
        token_symbol = token_symbol.upper()
        if token_symbol in self.watched_tokens:
            self.watched_tokens.remove(token_symbol)
            print(f"✓ Removed {token_symbol}")

    # ── Price history pre-fill ────────────────────────────────────────────────

    def prefill_price_history(self, token_symbol: str, candles):
        """
        Seed history before live ticks arrive.
        candles: list of dicts {"close", "vol"} or list of floats.
        live_points is preserved so warmup gating is not reset.
        """
        token_symbol = token_symbol.upper()
        buf = self.price_history.setdefault(
            token_symbol, {"prices": [], "volumes": [], "live_points": 0}
        )
        prices, volumes = [], []
        for c in candles:
            if isinstance(c, dict):
                prices.append(float(c.get("close", c.get("price", 0))))
                volumes.append(float(c.get("vol",   c.get("volume", 1_000_000))))
            else:
                prices.append(float(c))
                volumes.append(1_000_000)

        buf["prices"]  = (prices  + buf["prices"])[-1000:]
        buf["volumes"] = (volumes + buf["volumes"])[-1000:]

        if buf["prices"]:
            self.portfolio.update_prices({token_symbol: buf["prices"][-1]})
        print(f"  ✓ Pre-filled {len(prices)} prices for {token_symbol} "
              f"(total {len(buf['prices'])})")

    # ── Market data ───────────────────────────────────────────────────────────

    def update_market_data(self):
        """Append latest live price for every watched token."""
        STABLES   = {"USDC", "USDT", "USDS", "DAI"}
        price_map: Dict[str, float] = {}

        for sym in list(self.watched_tokens):
            mint = TOKENS.get(sym)
            if not mint:
                continue
            price = 1.0 if sym in STABLES else self.client.get_token_price(mint)
            if price > 0:
                buf = self.price_history.setdefault(
                    sym, {"prices": [], "volumes": [], "live_points": 0}
                )
                buf["prices"].append(price)
                buf["volumes"].append(1_000_000)
                buf["live_points"] = buf.get("live_points", 0) + 1
                if len(buf["prices"]) > 1000:
                    buf["prices"].pop(0)
                    buf["volumes"].pop(0)
                price_map[sym] = price

        if price_map:
            self.portfolio.update_prices(price_map)

    # ── Analysis ──────────────────────────────────────────────────────────────

    def analyze_token(self, token_symbol: str) -> Optional[Dict]:
        """Full technical analysis. Returns None if insufficient data."""
        token_symbol = token_symbol.upper()
        buf = self.price_history.get(token_symbol)
        if not buf:
            return None
        prices  = buf.get("prices",  [])
        volumes = buf.get("volumes", [])
        if len(prices) < 50:
            return None
        return self.ai.analyze_market(token_symbol, prices, volumes)

    # ── Trade execution ───────────────────────────────────────────────────────

    def execute_swap(self, from_token: str, to_token: str,
                     amount: float) -> Optional[Trade]:
        """
        Execute a swap. Returns Trade on success, None on failure.
        Paper  -> instant simulated trade at real market prices.
        Live   -> Jupiter quote + SolanaWallet signing + on-chain broadcast.
        """
        from_token = from_token.upper()
        to_token   = to_token.upper()

        from_mint = TOKENS.get(from_token)
        to_mint   = TOKENS.get(to_token)
        if not from_mint or not to_mint:
            print(f"✗ execute_swap: unknown token(s): {from_token}, {to_token}")
            return None

        try:
            if self.mode == TradingMode.PAPER:
                return self._paper_swap(from_token, to_token,
                                        from_mint,  to_mint, amount)
            return self._live_swap(from_token, to_token,
                                   from_mint,  to_mint, amount)
        except Exception as e:
            print(f"✗ execute_swap exception: {e}")
            return None

    # ── Paper ─────────────────────────────────────────────────────────────────

    def _paper_swap(self, from_token: str, to_token: str,
                    from_mint: str, to_mint: str,
                    amount: float) -> Optional[Trade]:
        from_price = self.client.get_token_price(from_mint) or 1.0
        to_price   = self.client.get_token_price(to_mint)   or 1.0
        if to_price == 0:
            print("✗ Paper swap: output token price unavailable")
            return None

        usd_value  = amount * from_price
        out_amount = (usd_value / to_price) * (1 - 0.005)  # 0.5% slippage

        if to_token == "USDC":
            side  = "sell"
            price = out_amount / amount if amount > 0 else 0.0
        else:
            side  = "buy"
            price = usd_value / out_amount if out_amount > 0 else 0.0

        tx = f"sim_{int(time.time())}_{random.randint(1000, 9999)}"
        return self._record_trade(from_token, to_token, side,
                                  amount, out_amount, price, tx)

    # ── Live ──────────────────────────────────────────────────────────────────

    def _live_swap(self, from_token: str, to_token: str,
                   from_mint: str, to_mint: str,
                   amount: float) -> Optional[Trade]:
        if self.wallet is None:
            print("✗ Live swap: SolanaWallet not provided to SolanaTradingAgent")
            return None

        # Convert to smallest native units
        dec_in  = TOKEN_DECIMALS.get(from_token, 9)
        dec_out = TOKEN_DECIMALS.get(to_token,   6)
        raw_in  = int(amount * (10 ** dec_in))

        # Jupiter quote
        quote = self.client.get_quote(from_mint, to_mint, raw_in, slippage_bps=100)
        if not quote:
            print("✗ Live swap: Jupiter quote failed")
            return None

        # Sign and broadcast
        print(f"  [live] Signing {from_token} → {to_token} swap ...")
        tx = self.wallet.execute_jupiter_swap(quote)
        if not tx:
            print("✗ Live swap: transaction not confirmed")
            return None

        # Derive human amounts from quote
        raw_out    = int(quote.get("outAmount", 0))
        out_amount = raw_out / (10 ** dec_out)
        if out_amount <= 0:
            print("✗ Live swap: outAmount is zero")
            return None

        if to_token == "USDC":
            side  = "sell"
            price = out_amount / amount if amount > 0 else 0.0
        else:
            side      = "buy"
            usd_in    = amount * (self.client.get_token_price(from_mint) or 1.0)
            price     = usd_in / out_amount if out_amount > 0 else 0.0

        return self._record_trade(from_token, to_token, side,
                                  amount, out_amount, price, tx)

    # ── Shared ────────────────────────────────────────────────────────────────

    def _record_trade(self, from_token: str, to_token: str,
                      side: str, amount_in: float, amount_out: float,
                      price: float, tx: str) -> Trade:
        trade = Trade(
            timestamp=datetime.now().isoformat(),
            token_in=from_token, token_out=to_token,
            side=side, price=price,
            amount_in=amount_in, amount_out=amount_out,
            tx_signature=tx, status="filled",
        )
        buf        = self.price_history.get(to_token, {})
        cur_price  = (buf.get("prices") or [price])[-1]
        self.portfolio.add_trade(trade, cur_price)

        tag = "🟢 BUY " if side == "buy" else "🔴 SELL"
        print(f"\n  {tag}  {from_token} → {to_token}")
        print(f"    Amount In:  {amount_in:.6f} {from_token}")
        print(f"    Amount Out: {amount_out:.6f} {to_token}")
        print(f"    Price:      ${price:.6f}")
        print(f"    TX:         {tx}")
        return trade

    # ── Display ───────────────────────────────────────────────────────────────

    def show_portfolio(self):
        print("\n" + "=" * 60)
        print("  PORTFOLIO STATUS")
        print("=" * 60)
        perf = self.portfolio.get_performance()
        sign = "+" if perf["total_return"] >= 0 else ""
        print(f"  USDC Balance: ${self.portfolio.usdc_balance:,.2f}")
        print(f"  Total Value:  ${perf['current_value']:,.2f}")
        print(f"  Total Return: {sign}${perf['total_return']:.2f} "
              f"({sign}{perf['total_return_pct']:.2f}%)")
        print(f"  Total Trades: {perf['total_trades']}")
        if self.portfolio.positions:
            print(f"\n  {'Token':<10} {'Amount':>14} {'Entry':>12} "
                  f"{'Current':>12} {'P&L':>14}")
            print("  " + "-" * 64)
            for tok, pos in self.portfolio.positions.items():
                s = "+" if pos.pnl >= 0 else ""
                print(f"  {tok:<10} {pos.amount:>14.6f} "
                      f"${pos.avg_price_usdc:>11.4f} "
                      f"${pos.current_price:>11.4f} "
                      f"{s}${abs(pos.pnl):>6.2f} ({pos.pnl_percentage:+.2f}%)")
        print("=" * 60 + "\n")

    def show_analysis(self, token_symbol: str):
        a = self.analyze_token(token_symbol)
        if not a:
            print(f"  No analysis yet for {token_symbol} "
                  f"(need 50 data points).")
            return
        print("\n" + "=" * 60)
        print(f"  ANALYSIS: {token_symbol}")
        print("=" * 60)
        print(f"  Price:      ${a['current_price']:.6f}")
        print(f"  Signal:     {a['signal']}  (conf {a['confidence']:.0%})")
        print(f"  Trend:      {a['trend']}  {a.get('momentum','')}")
        print(f"  RSI:        {a['rsi']:.1f}  "
              f"({'rising' if a.get('rsi_rising') else 'flat/falling'})")
        print(f"  MACD:       {a['macd']:.6f}")
        print(f"  EMA slope:  {a.get('ema_slope', 0):+.5f}")
        print(f"  Buy pts:    {a.get('buy_pts', 0)}   "
              f"Sell pts: {a.get('sell_pts', 0)}")
        print(f"  Reason:     {a['reason']}")
        print("=" * 60 + "\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solana DEX Trading AI Agent")
    parser.add_argument("--mode",    choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",  default="")
    parser.add_argument("--balance", type=float, default=1000.0)
    args = parser.parse_args()

    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER

    wallet_instance = None
    if mode == TradingMode.LIVE:
        try:
            from wallet_integration import load_wallet_config, SolanaWallet
            cfg = load_wallet_config()
            if not cfg:
                print("✗ No wallet config. Run: python setup_wallet.py --setup")
                sys.exit(1)
            wallet_instance = SolanaWallet(cfg)
        except ImportError:
            print("✗ Install deps: pip install solana solders base58")
            sys.exit(1)

    agent = SolanaTradingAgent(mode, args.wallet, args.balance,
                               wallet=wallet_instance)

    print(f"\n{'='*60}\n  SOLANA DEX TRADING AI AGENT  ({mode.value.upper()})")
    if mode == TradingMode.PAPER:
        print(f"  Starting balance: ${args.balance:,.2f} USDC")
    print(f"{'='*60}\n")
    print("Commands: add / remove / list / update / analyze / buy / sell / portfolio / quit\n")

    while True:
        try:
            parts = input("solana-ai> ").strip().split()
            if not parts:
                continue
            cmd = parts[0].lower()

            if cmd in ("quit", "exit"):
                print("Goodbye!")
                break
            elif cmd == "add"     and len(parts) > 1: agent.add_token(parts[1].upper())
            elif cmd == "remove"  and len(parts) > 1: agent.remove_token(parts[1].upper())
            elif cmd == "list":
                print("Watching:", ", ".join(agent.watched_tokens) or "nothing")
            elif cmd == "update":
                agent.update_market_data(); print("✓ Updated")
            elif cmd == "analyze" and len(parts) > 1:
                tok = parts[1].upper()
                if tok not in agent.watched_tokens:
                    agent.add_token(tok); agent.update_market_data()
                agent.show_analysis(tok)
            elif cmd == "buy"  and len(parts) >= 3:
                tok = parts[1].upper()
                if tok not in agent.watched_tokens:
                    agent.add_token(tok); agent.update_market_data()
                agent.execute_swap("USDC", tok, float(parts[2]))
            elif cmd == "sell" and len(parts) >= 3:
                agent.execute_swap(parts[1].upper(), "USDC", float(parts[2]))
            elif cmd == "portfolio": agent.show_portfolio()
            elif cmd == "help":
                print("  add/remove/list/update/analyze/buy/sell/portfolio/quit")
            else:
                print(f"✗ Unknown: {cmd}")
        except KeyboardInterrupt:
            print("\nGoodbye!")
            break
        except Exception as e:
            print(f"✗ Error: {e}")


if __name__ == "__main__":
    main()
