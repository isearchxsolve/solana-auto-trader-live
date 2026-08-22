#!/usr/bin/env python3
"""
Solana Auto Trader — Established Tokens Edition
Trades SOL, JUP, RAY, BONK, WIF using real 5-minute candles.

Usage:
  python solana_auto_trader.py                       # paper, $1000, all tokens
  python solana_auto_trader.py --balance 500         # paper, $500
  python solana_auto_trader.py --tokens SOL JUP RAY  # specific tokens
  python solana_auto_trader.py --mode live           # real money
"""

import time, math, argparse, random
from datetime import datetime
from typing import Dict, Optional

from solana_trading_agent import (
    TradingMode, TradingAI, Portfolio, PriceHistory, TOKENS
)
from market_condition_analyzer import MarketConditionAnalyzer

# ── Strategy constants (edit these to tune) ──────────────────────────────────
STOP_LOSS_PCT   = 0.07   # 7 %   hard stop
TAKE_PROFIT_PCT = 0.15   # 15%   hard take-profit
TRAIL_TRIGGER   = 0.08   # trailing stop activates after +8%
TRAIL_DIST      = 0.04   # trail sits 4% below the peak
POSITION_PCT    = 0.20   # 20% of portfolio per trade
MAX_POSITIONS   = 2      # never hold more than 2 tokens at once
MIN_CANDLES     = 80     # need 80 real candles before first trade (~6.7 hrs)
TRADE_COOLDOWN  = 300    # 5-minute cooldown per token after any trade
UPDATE_EVERY_S  = 60     # price refresh interval (seconds)
DEFAULT_TOKENS  = ["SOL", "JUP", "RAY", "BONK", "WIF"]


# ── Main trader ───────────────────────────────────────────────────────────────
class SolanaAutoTrader:

    def __init__(self, mode: TradingMode, tokens: list, initial_usdc: float):
        self.mode      = mode
        self.tokens    = [t.upper() for t in tokens if t.upper() in TOKENS]
        self.portfolio = Portfolio(initial_usdc)
        self.ai        = TradingAI()
        self.prices    = PriceHistory()
        self.market_analyzer = MarketConditionAnalyzer()

        # Per-token candle history
        self.closes:   Dict[str, list] = {t: [] for t in self.tokens}
        self.volumes:  Dict[str, list] = {t: [] for t in self.tokens}

        # State tracking
        self.last_trade: Dict[str, datetime] = {}
        self.pos_peaks:  Dict[str, float]    = {}
        self.trades_today = 0
        self.day_start    = datetime.now().date()
        self.sl_strikes:  Dict[str, int] = {}
        self.blacklist:   set            = set()
        
        # Trading pause state
        self.trading_paused = False
        self.pause_reason = ""

    # ── Startup ───────────────────────────────────────────────────────────────
    def initialize(self):
        """Discover pair addresses and load historical candles."""
        print("\n[init] Step 1 — Discovering pool addresses...")
        self.prices.discover_all(self.tokens)

        print("\n[init] Step 2 — Loading 5-min candle history (up to 1000 candles)...")
        for sym in self.tokens:
            if sym not in self.prices.pair_map:
                print(f"  {sym}: no pool address found, skipping")
                continue
            print(f"  Fetching {sym}...", end=" ", flush=True)
            candles = self.prices.fetch_candles(sym, timeframe="minute",
                                                aggregate=5, limit=1000)
            if candles:
                self.closes[sym]  = [c["close"] for c in candles]
                self.volumes[sym] = [c["vol"]   for c in candles]
                print(f"✓ {len(candles)} candles  "
                      f"(latest close ${candles[-1]['close']:.4f})")
            else:
                print("✗ failed — will trade once enough live prices accumulate")

    # ── Market data ───────────────────────────────────────────────────────────
    def update_prices(self):
        """Append current live price to each token's history."""
        updated = []
        for sym in self.tokens:
            price = self.prices.get_live_price(sym)
            if price > 0:
                self.closes[sym].append(price)
                self.volumes[sym].append(1_000_000)
                # Cap at 1000 candles
                if len(self.closes[sym]) > 1000:
                    self.closes[sym].pop(0)
                    self.volumes[sym].pop(0)
                self.portfolio.update_prices({sym: price})
                updated.append(f"{sym}=${price:.4f}")
        if updated:
            print("  " + "  ".join(updated))

    # ── Exit management ───────────────────────────────────────────────────────
    def monitor_positions(self):
        for sym, pos in list(self.portfolio.positions.items()):
            pnl = pos.pnl_percentage

            # Peak tracking
            if sym not in self.pos_peaks or pnl > self.pos_peaks[sym]:
                self.pos_peaks[sym] = pnl
            peak = self.pos_peaks[sym]

            # Stop loss
            if pnl <= -STOP_LOSS_PCT * 100:
                print(f"\n  🛑 STOP LOSS   {sym}  P&L: {pnl:+.2f}%")
                self._sell(sym, "stop_loss")
                self.sl_strikes[sym] = self.sl_strikes.get(sym, 0) + 1
                if self.sl_strikes[sym] >= 3:
                    self.blacklist.add(sym)
                    print(f"  ⛔ {sym} blacklisted after 3 stop losses")
                continue

            # Take profit
            if pnl >= TAKE_PROFIT_PCT * 100:
                print(f"\n  🎯 TAKE PROFIT  {sym}  P&L: {pnl:+.2f}%")
                self._sell(sym, "take_profit")
                continue

            # Trailing stop
            if peak >= TRAIL_TRIGGER * 100:
                floor = peak - TRAIL_DIST * 100
                if pnl <= floor:
                    print(f"\n  📉 TRAIL STOP  {sym}  "
                          f"Peak:{peak:+.2f}%  Now:{pnl:+.2f}%  Floor:{floor:+.2f}%")
                    self._sell(sym, "trail_stop")
                else:
                    print(f"  📌 Trailing  {sym}  "
                          f"Peak:{peak:+.2f}%  Now:{pnl:+.2f}%  Floor:{floor:+.2f}%")

    # ── Trade execution ───────────────────────────────────────────────────────
    def _buy(self, sym: str, price: float, analysis: Dict):
        total    = self.portfolio.total_value()
        usdc_amt = min(total * POSITION_PCT, self.portfolio.usdc * 0.98)
        if usdc_amt < 10:
            print(f"  [skip] Trade too small ${usdc_amt:.2f}")
            return

        tx = self._exec_tx("buy", sym, usdc_amt)
        if not tx:
            return

        self.portfolio.buy(sym, usdc_amt, price, tx)
        self.last_trade[sym]  = datetime.now()
        self.pos_peaks[sym]   = 0.0
        self.trades_today    += 1

        print(f"\n  ✅ BUY  {sym}")
        print(f"     USDC:   ${usdc_amt:.2f}")
        print(f"     Price:  ${price:.6f}")
        print(f"     RSI:    {analysis['rsi']:.0f}  "
              f"Trend: {analysis['trend']}  "
              f"Slope: {analysis['ema_slope']:+.4f}")
        print(f"     Reason: {analysis['reason']}")
        print(f"     TX: {tx}")

    def _sell(self, sym: str, reason: str):
        pos = self.portfolio.positions.get(sym)
        if not pos:
            return
        price = pos.current_price

        tx = self._exec_tx("sell", sym, pos.amount)
        if not tx:
            return

        trade = self.portfolio.sell(sym, price, tx)
        self.last_trade[sym] = datetime.now()
        self.pos_peaks.pop(sym, None)
        self.trades_today += 1

        pnl_usd = trade.amount_out - (pos.avg_price_usdc * trade.amount_in)
        print(f"     Sold: {trade.amount_in:.4f} {sym}  "
              f"→  ${trade.amount_out:.2f} USDC  "
              f"P&L: ${pnl_usd:+.2f}  TX: {tx}")

    def _exec_tx(self, side: str, sym: str, amount: float) -> Optional[str]:
        """Paper: return sim TX. Live: call Jupiter via wallet_integration."""
        if self.mode == TradingMode.PAPER:
            return f"sim_{int(time.time())}_{random.randint(1000, 9999)}"
        # Live trading
        try:
            from wallet_integration import load_wallet_config, SolanaWallet
            import requests as _req
            cfg    = load_wallet_config()
            wallet = SolanaWallet(cfg)
            usdc_m = TOKENS["USDC"]
            tok_m  = TOKENS[sym]

            if side == "buy":
                amt_in = int(amount * 1e6)          # USDC has 6 decimals
                quote_r = _req.get("https://quote-api.jup.ag/v6/quote",
                    params={"inputMint": usdc_m, "outputMint": tok_m,
                            "amount": amt_in, "slippageBps": 100}, timeout=10)
            else:
                amt_in = int(amount * 1e9)           # most SPL tokens: 9 decimals
                quote_r = _req.get("https://quote-api.jup.ag/v6/quote",
                    params={"inputMint": tok_m, "outputMint": usdc_m,
                            "amount": amt_in, "slippageBps": 100}, timeout=10)

            if quote_r.status_code != 200:
                print(f"  Quote error: {quote_r.status_code}")
                return None
            return wallet.execute_jupiter_swap(quote_r.json())
        except Exception as e:
            print(f"  Live TX error: {e}")
            return None

    # ── Market condition check ───────────────────────────────────────────────
    def _check_market_conditions(self):
        """Check if market conditions are suitable for trading"""
        # Build analysis dict for all tokens
        tokens_analysis = {}
        for sym in self.tokens:
            closes = self.closes[sym]
            volumes = self.volumes[sym]
            if len(closes) >= 30:
                analysis = self.ai.analyze(sym, closes, volumes)
                tokens_analysis[sym] = analysis
        
        if not tokens_analysis:
            return  # Not enough data yet
        
        # Check if we should trade
        should_trade, reason = self.market_analyzer.should_trade(
            tokens_analysis, 
            min_score=70  # Require HEALTHY conditions only (no trading in MARGINAL)
        )
        
        # Update pause state
        was_paused = self.trading_paused
        self.trading_paused = not should_trade
        self.pause_reason = reason
        
        # Log status changes
        if self.trading_paused and not was_paused:
            print("\n" + "!"*65)
            print("  ⏸  TRADING PAUSED - Poor market conditions detected")
            print("!"*65)
            self.market_analyzer.print_status(tokens_analysis)
        elif not self.trading_paused and was_paused:
            print("\n" + "!"*65)
            print("  ▶️  TRADING RESUMED - Market conditions improved")
            print("!"*65)
            self.market_analyzer.print_status(tokens_analysis)
        elif not self.trading_paused:
            # Print condensed status when trading is active
            status, _, details = self.market_analyzer.analyze(tokens_analysis)
            score = details.get('overall_score', 0)
            stats = details.get('stats', {})
            
            emoji = "🟢" if status == "HEALTHY" else "🟡"
            print(f"\n[conditions] {emoji} {status} (score: {score:.0f}/100) | "
                  f"{stats.get('bullish', 0)}B {stats.get('bearish', 0)}Be {stats.get('neutral', 0)}N | "
                  f"RSI:{stats.get('avg_rsi', 0):.0f}")

    # ── Entry gate ────────────────────────────────────────────────────────────
    def _can_buy(self, sym: str) -> tuple:
        """Returns (ok: bool, reason: str)."""
        if self.trading_paused:
            return False, "trading paused (poor market conditions)"
        if sym in self.blacklist:
            return False, "blacklisted"
        if sym in self.portfolio.positions:
            return False, "already holding"
        if len(self.portfolio.positions) >= MAX_POSITIONS:
            return False, f"max {MAX_POSITIONS} positions open"
        if self.portfolio.usdc < self.portfolio.total_value() * 0.05:
            return False, "USDC too low"
        if sym in self.last_trade:
            elapsed = (datetime.now() - self.last_trade[sym]).total_seconds()
            if elapsed < TRADE_COOLDOWN:
                return False, f"cooldown {int(TRADE_COOLDOWN - elapsed)}s"
        n = len(self.closes[sym])
        if n < MIN_CANDLES:
            return False, f"warming up ({n}/{MIN_CANDLES} candles)"
        return True, ""

    # ── Analyse + decide ──────────────────────────────────────────────────────
    def _analyse(self, sym: str):
        closes  = self.closes[sym]
        volumes = self.volumes[sym]

        if len(closes) < 30:
            print(f"  {sym:<6}: warming up ({len(closes)} candles)")
            return

        a   = self.ai.analyze(sym, closes, volumes)
        cur = a["current_price"]

        def _fp(p):
            if p >= 1:      return f"${p:.4f}"
            if p >= 0.0001: return f"${p:.6f}"
            d = max(8, -int(math.floor(math.log10(p))) + 2) if p > 0 else 8
            return f"${p:.{d}f}"

        ts = datetime.now().strftime("%H:%M:%S")
        candles_str = f"({len(closes)} candles)"
        print(f"  [{ts}] {sym:<6}  {_fp(cur):<14}  "
              f"RSI:{a['rsi']:.0f}  "
              f"{a['signal']:<4}  "
              f"Trend:{a['trend']:<8}  "
              f"Slope:{a['ema_slope']:+.4f}  "
              f"MACD:{a['macd_hist']:+.6f}  "
              f"{candles_str}")

        if a["signal"] == "BUY":
            # STRICT FILTER: Only buy BULLISH trends, never NEUTRAL
            if a["trend"] != "BULLISH":
                print(f"  [skip] Trend is {a['trend']}, require BULLISH")
            else:
                ok, reason = self._can_buy(sym)
                if ok:
                    self._buy(sym, cur, a)
                else:
                    print(f"  [skip] {reason}")

        elif a["signal"] == "SELL":
            if sym in self.portfolio.positions:
                print(f"\n  📊 SIGNAL SELL  {sym}  ({a['reason']})")
                self._sell(sym, "signal")
            else:
                print(f"  [skip] No {sym} position to sell")

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self):
        self._print_header()
        self.initialize()

        cycle = 0
        try:
            while True:
                cycle += 1
                if datetime.now().date() != self.day_start:
                    self.trades_today = 0
                    self.day_start    = datetime.now().date()

                print(f"\n{'='*65}")
                print(f"  Cycle #{cycle}  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*65}")

                print("\n[market] Fetching live prices...")
                self.update_prices()

                # Check market conditions
                self._check_market_conditions()

                if self.portfolio.positions:
                    print("\n[positions] Checking exits...")
                    self.monitor_positions()

                if not self.trading_paused:
                    print("\n[analysis] Scanning tokens...")
                    for sym in self.tokens:
                        self._analyse(sym)
                else:
                    print(f"\n[analysis] ⏸ Trading paused: {self.pause_reason}")
                    print("           Will auto-resume when conditions improve")

                self._print_portfolio()

                print(f"\n[wait] Sleeping {UPDATE_EVERY_S}s...")
                time.sleep(UPDATE_EVERY_S)

        except KeyboardInterrupt:
            print("\n\n[stop] Shutting down...")
            self._print_summary()

    # ── Display helpers ───────────────────────────────────────────────────────
    def _print_header(self):
        print("\n" + "="*65)
        print("  SOLANA AUTO TRADER  —  Smart Edition with Auto-Pause")
        print("="*65)
        print(f"  Mode:          {self.mode.value.upper()}")
        print(f"  Tokens:        {', '.join(self.tokens)}")
        print(f"  Candle source: GeckoTerminal  (5-min real OHLCV)")
        print(f"  Update every:  {UPDATE_EVERY_S}s")
        print(f"  Min candles:   {MIN_CANDLES}  before first trade")
        print(f"\n  🧠 SMART FEATURES:")
        print(f"    Auto-pause when market conditions are poor (score < 50/100)")
        print(f"    Auto-resume when conditions improve")
        print(f"    Monitors: trend strength, volatility, momentum, volume")
        print(f"\n  ENTRY (all must pass):")
        print(f"    EMA20 > EMA50 AND slope > 0  (confirmed uptrend)")
        print(f"    RSI 38–62  (pullback, not at top)")
        print(f"    RSI rising vs 3 bars ago  (momentum turning)")
        print(f"    MACD histogram improving")
        print(f"\n  EXIT:")
        print(f"    Stop loss:    {STOP_LOSS_PCT:.0%}")
        print(f"    Take profit:  {TAKE_PROFIT_PCT:.0%}")
        print(f"    Trailing:     from +{TRAIL_TRIGGER:.0%}, trails {TRAIL_DIST:.0%} below peak")
        print(f"\n  SIZING:   {POSITION_PCT:.0%} per trade  |  max {MAX_POSITIONS} open")
        print(f"  COOLDOWN: {TRADE_COOLDOWN}s per token after any trade")
        print(f"\n  Press Ctrl+C to stop")
        print("="*65)

    def _print_portfolio(self):
        s = self.portfolio.stats()
        sign = "+" if s["return_usd"] >= 0 else ""
        print(f"\n[portfolio]")
        print(f"  USDC:        ${self.portfolio.usdc:,.2f}")
        print(f"  Total Value: ${s['total_value']:,.2f}")
        print(f"  Return:      {sign}${s['return_usd']:.2f}  ({sign}{s['return_pct']:.2f}%)")
        print(f"  Trades:      {self.trades_today} today  |  {s['n_trades']} total")

        if self.portfolio.positions:
            print(f"  Open Positions:")
            for sym, pos in self.portfolio.positions.items():
                def _fp(p):
                    if p >= 1:      return f"${p:.4f}"
                    if p >= 0.0001: return f"${p:.6f}"
                    d = max(8, -int(math.floor(math.log10(p)))+2) if p > 0 else 8
                    return f"${p:.{d}f}"
                peak = self.pos_peaks.get(sym, pos.pnl_percentage)
                print(f"    {sym:<6}  entry:{_fp(pos.avg_price_usdc):<14}"
                      f"now:{_fp(pos.current_price):<14}"
                      f"P&L:{pos.pnl_percentage:+.2f}%  peak:{peak:+.2f}%")

        if self.blacklist:
            print(f"  Blacklisted: {', '.join(self.blacklist)}")

    def _print_summary(self):
        s = self.portfolio.stats()
        sign = "+" if s["return_usd"] >= 0 else ""
        print("\n" + "="*65)
        print("  SESSION SUMMARY")
        print("="*65)
        print(f"  Starting:  ${self.portfolio.initial_usdc:,.2f}")
        print(f"  Final:     ${s['total_value']:,.2f}")
        print(f"  Return:    {sign}${s['return_usd']:.2f}  ({sign}{s['return_pct']:.2f}%)")
        print(f"  Trades:    {s['n_trades']}")

        sells = [t for t in self.portfolio.trades if t.side == "sell"]
        buys  = [t for t in self.portfolio.trades if t.side == "buy"]
        if sells and buys:
            pnls = []
            for sell in sells:
                tok = sell.token_in
                blist = [b for b in buys if b.token_out == tok]
                if blist:
                    cost = blist[-1].amount_in
                    pnls.append(sell.amount_out - cost)
            if pnls:
                wins   = [p for p in pnls if p > 0]
                losses = [p for p in pnls if p <= 0]
                print(f"  Win rate:  {len(wins)}/{len(pnls)} "
                      f"({len(wins)/len(pnls)*100:.0f}%)")
                if wins:   print(f"  Avg win:   +${sum(wins)/len(wins):.2f}")
                if losses: print(f"  Avg loss:  ${sum(losses)/len(losses):.2f}")
        print("="*65)
        print("Goodbye!\n")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Solana Auto Trader — Established Tokens")
    p.add_argument("--mode",     choices=["paper", "live"], default="paper")
    p.add_argument("--balance",  type=float, default=1000.0,
                   help="Starting USDC (paper mode)")
    p.add_argument("--tokens",   nargs="+", default=DEFAULT_TOKENS,
                   help="Tokens to trade")
    p.add_argument("--interval", type=int, default=UPDATE_EVERY_S,
                   help="Update interval in seconds")
    args = p.parse_args()

    mode   = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER
    valid  = [t.upper() for t in args.tokens if t.upper() in TOKENS]

    if not valid:
        print(f"No valid tokens. Available: {', '.join(TOKENS.keys())}")
        return

    if mode == TradingMode.LIVE:
        print("\n⚠️  LIVE MODE — real money will be used.")
        if input("Type START to continue: ").strip() != "START":
            print("Cancelled.")
            return

    # Allow --interval override
    import solana_auto_trader as _m
    _m.UPDATE_EVERY_S = args.interval

    SolanaAutoTrader(mode, valid, args.balance).run()


if __name__ == "__main__":
    main()
