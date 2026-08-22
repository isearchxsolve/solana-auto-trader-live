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
    """AI-powered trading decision engine for Solana tokens"""
    
    def __init__(self):
        self.indicators_cache = {}
    
    def calculate_rsi(self, prices: List[float], period: int = 14) -> Optional[float]:
        """Calculate Relative Strength Index. Returns None if insufficient data."""
        if len(prices) < period + 1:
            return None
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def calculate_sma(self, prices: List[float], period: int) -> float:
        """Calculate Simple Moving Average"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        return sum(prices[-period:]) / period
    
    def calculate_ema(self, prices: List[float], period: int) -> float:
        """Calculate Exponential Moving Average"""
        if len(prices) < period:
            return self.calculate_sma(prices, len(prices))
        
        multiplier = 2 / (period + 1)
        ema = self.calculate_sma(prices[:period], period)
        
        for price in prices[period:]:
            ema = (price * multiplier) + (ema * (1 - multiplier))
        
        return ema
    
    def analyze_market(self, token: str, prices: List[float], volumes: List[float]) -> Dict:
        """Perform comprehensive market analysis over 3600 data points"""
        if len(prices) < 50:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Insufficient data"}
        
        current_price = prices[-1]

        # Fixed indicator periods — consistent regardless of history length.
        # Previously periods scaled with len(prices), so a 1000-candle synthetic
        # token used SMA(250)/SMA(500) while a 105-candle real token used
        # SMA(26)/SMA(52) — completely different systems for different tokens.
        # Synthetic tokens were permanently BEARISH because their 250-period SMA
        # lagged far behind the synthetic pump history.
        N = len(prices)
        SMA_FAST_P  = min(20,  N)
        SMA_SLOW_P  = min(50,  N)
        EMA_FAST_P  = min(12,  N)
        EMA_SLOW_P  = min(26,  N)

        rsi        = self.calculate_rsi(prices, period=14)
        sma_fast   = self.calculate_sma(prices, SMA_FAST_P)
        sma_slow   = self.calculate_sma(prices, SMA_SLOW_P)
        ema_fast   = self.calculate_ema(prices, EMA_FAST_P)
        ema_slow   = self.calculate_ema(prices, EMA_SLOW_P)

        # RSI None means truly insufficient data — hold
        if rsi is None:
            return {"signal": "HOLD", "confidence": 0.0,
                    "reason": "Insufficient data for RSI",
                    "rsi": 50.0, "trend": "NEUTRAL", "momentum": "WEAK",
                    "current_price": current_price, "sma_20": sma_fast,
                    "sma_50": sma_slow, "macd": 0.0}

        # MACD — signal line = EMA-9 of the MACD series (not EMA of raw prices).
        # Previously signal_line was EMA(prices) ≈ current_price, so macd
        # (near 0) was always < signal_line, adding a permanent bearish bias.
        macd = ema_fast - ema_slow
        window = min(50, len(prices))
        macd_series = []
        for i in range(window):
            sub = prices[-(window - i):]
            _ef = self.calculate_ema(sub, min(100, max(1, len(sub) // 8)))
            _es = self.calculate_ema(sub, min(200, max(1, len(sub) // 4)))
            macd_series.append(_ef - _es)
        signal_line = self.calculate_ema(macd_series, min(9, len(macd_series)))

        # Trend analysis
        trend    = "BULLISH" if sma_fast < current_price else "BEARISH"
        momentum = "STRONG"  if abs(current_price - sma_fast) / sma_fast > 0.03 else "WEAK"
        
        # ── Stablecoin guard — never trade near-$1 tokens ───────────────────────
        if 0.95 <= current_price <= 1.05:
            return {"signal": "HOLD", "confidence": 0.0, "reason": "Stablecoin — skipped",
                    "rsi": rsi, "trend": "NEUTRAL", "momentum": "WEAK",
                    "current_price": current_price, "sma_20": sma_fast,
                    "sma_50": sma_slow, "macd": macd}

        # ── Synthetic-history guard ───────────────────────────────────────────────
        # RSI values below ~5 are a mathematical artifact of synthetic/warmup data:
        # every candle trending the same direction → RS → infinity → RSI = 100 or 0.
        # Real markets never sustain RSI < 5. Block BUY signals in this case so we
        # don't chase a fake oversold signal with real (paper) money.
        if rsi < 5:
            return {"signal": "HOLD", "confidence": 0.0,
                    "reason": f"RSI={rsi:.1f} — synthetic warmup artifact, skip",
                    "rsi": rsi, "trend": trend, "momentum": momentum,
                    "current_price": current_price, "sma_20": sma_fast,
                    "sma_50": sma_slow, "macd": macd}

        # ── RSI momentum direction ───────────────────────────────────────────────
        # Compute RSI one period back to determine if RSI is rising or falling.
        # This prevents "catching a falling knife": RSI=30 falling is NOT the same
        # as RSI=30 rising. Only award oversold BUY points when RSI has turned upward.
        # If rsi_prev is None (insufficient data), treat direction as unknown → no
        # oversold BUY points awarded (conservative default).
        rsi_prev = self.calculate_rsi(prices[:-1], period=14) if len(prices) > 15 else None
        rsi_rising = (rsi_prev is not None) and (rsi > rsi_prev + 1.0)

        # ── Signal scoring ───────────────────────────────────────────────────────
        buy_signals = 0
        sell_signals = 0

        # RSI — extreme readings (< 20 or > 80) get triple weight: strong mean-reversion
        # BUY signals from RSI only fire when RSI is actually reversing upward.
        if rsi < 20:
            if rsi_rising:
                buy_signals += 3      # deeply oversold + reversing → strong BUY
            # else: RSI still falling — do not award buy points (knife catch)
        elif rsi < 35:
            if rsi_rising:
                buy_signals += 2      # oversold + reversing → BUY
            # else: RSI still falling — skip
        elif rsi > 80:
            sell_signals += 3     # deeply overbought → strong SELL
        elif rsi > 65:
            sell_signals += 2     # overbought → SELL

        # Moving average crossover (trend direction)
        if sma_fast > sma_slow:
            buy_signals += 1
        elif sma_fast < sma_slow:
            sell_signals += 1

        # Price vs fast SMA (momentum)
        if current_price > sma_fast:
            buy_signals += 1
        else:
            sell_signals += 1

        # MACD crossover direction
        if macd > signal_line:
            buy_signals += 1
        else:
            sell_signals += 1

        # ── Final signal — majority wins ─────────────────────────────────────────
        total_signals = buy_signals + sell_signals
        if buy_signals > sell_signals:
            signal = "BUY"
            confidence = min(buy_signals / max(total_signals, 1), 1.0)
        elif sell_signals > buy_signals:
            signal = "SELL"
            confidence = min(sell_signals / max(total_signals, 1), 1.0)
        else:
            signal = "HOLD"
            confidence = 0.5
        
        return {
            "signal": signal,
            "confidence": confidence,
            "rsi": rsi,
            "trend": trend,
            "momentum": momentum,
            "current_price": current_price,
            "sma_20": sma_fast,
            "sma_50": sma_slow,
            "macd": macd,
            "reason": self._generate_reason(signal, rsi, trend, macd, signal_line)
        }
    
    def _generate_reason(self, signal: str, rsi: float, trend: str, 
                        macd: float, signal_line: float) -> str:
        """Generate human-readable reasoning for the signal"""
        reasons = []
        
        if rsi < 30:
            reasons.append("RSI indicates oversold condition")
        elif rsi > 70:
            reasons.append("RSI indicates overbought condition")
        
        if trend == "BULLISH":
            reasons.append("Price above moving averages (bullish trend)")
        else:
            reasons.append("Price below moving averages (bearish trend)")
        
        if macd > signal_line:
            reasons.append("MACD shows bullish momentum")
        else:
            reasons.append("MACD shows bearish momentum")
        
        return "; ".join(reasons) if reasons else "Mixed signals"


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
            if token in self.positions:
                pos = self.positions[token]
                total_cost = (pos.avg_price_usdc * pos.amount) + trade.amount_in
                pos.amount += trade.amount_out
                pos.avg_price_usdc = total_cost / pos.amount
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
                pos.pnl_percentage = ((price - pos.avg_price_usdc) / pos.avg_price_usdc) * 100
    
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
                from_price = self.client.get_token_price(from_mint) or 1.0
                to_price   = self.client.get_token_price(to_mint)   or 1.0
                if to_price == 0:
                    print("✗ Could not get output token price")
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
        """Display technical analysis for a token"""
        analysis = self.analyze_token(token_symbol)
        if not analysis:
            return
        
        print("\n" + "="*60)
        print(f"TECHNICAL ANALYSIS: {token_symbol}")
        print("="*60)
        print(f"\nCurrent Price: ${analysis['current_price']:.6f}")
        print(f"Signal: {analysis['signal']} (Confidence: {analysis['confidence']:.1%})")
        print(f"Trend: {analysis['trend']} - {analysis['momentum']}")
        print(f"\nIndicators:")
        print(f"  RSI: {analysis['rsi']:.2f}")
        print(f"  SMA(20): ${analysis['sma_20']:.6f}")
        print(f"  SMA(50): ${analysis['sma_50']:.6f}")
        print(f"  MACD: {analysis['macd']:.6f}")
        print(f"\nReason: {analysis['reason']}")
        print("="*60 + "\n")


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