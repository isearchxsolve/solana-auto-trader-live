#!/usr/bin/env python3
"""
Solana Meme Coin Trader — v1.7
================================
Built on MemeSignalEngine v1.5 (engine updated in-place for v1.7 bug fixes).

Usage:
  python solana_meme_trader.py --mode paper --balance 1000
  python solana_meme_trader.py --mode live  --balance 500

THIS IS HIGH RISK. READ BEFORE USING:
  - Every trade risks up to 12% of the position (memes are volatile)
  - Rug pulls can take a token to zero instantly — position caps are HARD
  - The daily loss circuit breaker exists for a reason — respect it
  - Paper trade first. Seriously.

WHAT THIS BOT DOES:
  - Scans boosted + new meme listings every 60 seconds
  - Applies 8 meme-specific hard filters (age, mcap, rug flags, etc.)
  - Enters on volume explosion + momentum confirmation
  - Exits at ATR-based TP1/TP2 or trailing stop
  - Emergency dump detection: exits if price drops 8% from peak fast
  - Hard cap: max $150 per trade, max 5 concurrent positions
  - Daily loss limit: 8% of starting equity — halts for the day if hit

POSITION SIZING:
  - Base: 8% of portfolio per trade
  - Hard cap: $150 absolute maximum (regardless of portfolio size)
  - ATR scaled: high-volatility tokens get smaller allocation
  - Compounding: +3% after 3 consecutive wins, -5% on any loss

DISCOVERY:
  - Prioritises NEW LISTINGS and BOOSTED tokens (higher meme upside)
  - Requires: liq $50k-$10M, age >2h, mcap <$50M, volume >$20k/24h
  - Scans up to 15 tokens simultaneously
  - Refresh every 60s (meme opportunities come and go fast)

v1.7 changes vs v1.6  (8 bug fixes):
  FIX 1 — _can_enter() R:R gate uses effective ATR for consistency:
    • Previously, _can_enter() checked analysis["rr_ratio"] (raw ATR) while
      _execute_buy() and _monitor_exits() used effective_atr = max(ATR, 1.5%).
    • This was an inconsistency, not a negative-EV issue: because TP scales at
      2× the stop multiplier (6× vs 3×), R:R strictly increases with ATR — the
      floor always improves R:R, never degrades it.
    • The gate recompute now uses effective_atr for auditability: what _can_enter
      approves matches exactly what _execute_buy will set up.
    • The real sizing problem (dollar risk 3× wider than sizer expected) is fixed
      separately in FIX 3 (sizer now receives effective_atr).
    • Also wires the is_ignition flag from the engine (via FIX 2) so ignition
      setups correctly use the MEME_MIN_RR_IGNITION = 0.65 threshold instead of
      the 1.0 that was previously blocking all ignition entries.

  FIX 2 — Ignition mode R:R conflict resolved:
    • Engine's _check_rr() now returns a 4-tuple (ok, rr, msg, is_ignition).
    • is_ignition is stored in the analysis dict and read by _can_enter().
    • _can_enter() applies MEME_MIN_RR_IGNITION (0.65) when is_ignition=True
      instead of the hard-coded 1.0 that previously blocked all ignition setups.

  FIX 3 — Sizer uses effective ATR (fixes risk/size mismatch):
    • _execute_buy() passes effective_atr to sizer.get_size() instead of raw
      atr_pct. Position is now sized against the actual stop width (floor-aware).

  FIX 4 — Discovery liquidity filter aligned with engine gate ($35k → $50k):
    • Tokens with $35k–$49k liquidity previously passed discovery, wasted a
      GeckoTerminal API call, then hit a structural hard-fail every cycle until
      evicted. Discovery now pre-filters at $50k to match MEME_MIN_LIQ.

  FIX 5 — Session stats no longer double-count TP1+TP2 trades:
    • _record() was called at TP1 (partial exit) and again at TP2/TRAIL/DUMP.
      A single winning trade appeared as 2 wins. _record() is now only called
      at final exit; TP1 is tracked via tp1_hit but not recorded as a trade.

  FIX 6 — min_soft_score raised from 2 to 3 (29% → 43% confirmation):
    • 2/7 allowed entries on macd_cross + room_to_run alone with no volume
      activity — mean-reversion plays disguised as momentum. Raised to 3.
    • Added volume hard gate in _can_enter(): requires rv >= 1.2x average
      (or vol_explosion/vol_spike flag active) before proceeding to execute.
      This enforces the requirement stated in the startup banner.

  FIX 7 — Regime gate re-enabled (REGIME_MIN 0 → 30):
    • Score 0 allowed trading during full market sell-offs. Both v1.6 test
      trades happened in regime 47–51 with backdrop of tokens dumping 15–37%.
    • Raised to 30: blocks the worst conditions while still permitting trading
      in neutral/weak markets where genuine ignition setups can occur.

  FIX 8 — Score denominator corrected from /8 to /7:
    • vol_explosion and vol_spike are mutually exclusive by design, so max
      achievable score is 7. All display strings updated to /7.
    • Engine _check_rr docstring and _soft_score docstring updated to match.

v1.6 changes vs v1.5:
  FIX 1 — Watchlist health check: evict tokens that now fail safety filters:
    • Every discovery cycle, watched tokens are checked against the current
      trending data. Any watched token (not in an open position) whose 1h
      price change is < -10% OR 6h change < -20% is immediately removed from
      the watchlist and eviction-blacklisted (15m cooldown).
    • Prevents the KIMCHI pattern: flagged as "6h downtrend" in every discovery
      pass but remaining on the watchlist and being entered twice anyway.
    • Metadata (price_change_h1, price_change_h6) is refreshed from the latest
      trending snapshot each cycle so the check uses current data, not
      discovery-time snapshots that may be many minutes stale.

  FIX 2 — Synthetic-history entry block at execution gate:
    • _can_enter() now rejects entries on tokens with synthetic price history
      that have accumulated fewer than 30 live candles.
    • Previously the engine widened its RSI gate for synthetic tokens (Fix 2
      in v1.5) but still allowed execution. That's wrong: RSI, ATR, and MACD
      computed on generated data give false precision — they look like valid
      signals but are artefacts of the synthetic generation algorithm.
    • After 30 live candles the indicator data is reliable and entries proceed
      normally. The token stays on the watchlist during warmup.

  FIX 3 — ATR stop floor prevents whipsaw on quiet/young tokens:
    • MIN_EFFECTIVE_ATR_PCT = 1.5% introduced. When recording position_atr at
      entry, effective ATR = max(signal_engine_ATR, MIN_EFFECTIVE_ATR_PCT).
    • Minimum effective stop: 3x * 1.5% = 4.5% (well under the 12% hard cap).
    • Addresses UFO pattern: engine reported ATR=0.6% (compressed on 16h-old
      token with short history) → stop at -1.9% → whipsawed out on normal
      meme noise before the trade had any room to breathe.
    • ATR floor only applies to stop calculation (position_atr). Engine R:R
      display still shows the raw engine ATR so the signal report is honest.

v1.5 changes vs v1.4:
  FIX 1 — R:R gate no longer blocks all entries in meme-scale ATR markets:
    • _can_enter R:R threshold lowered 1.3 → 1.0 to match engine's updated gate.
      Engine (MemeSignalEngine v1.5) now uses 0.6% fee (was 1%) and separate
      thresholds for standard (1.0) vs ignition (0.65) setups.

  FIX 2 — Synthetic history no longer poisons RSI calculations:
    • Discovery thread now sets token_metadata["is_synthetic"] = True for tokens
      whose GeckoTerminal history had to be generated rather than fetched.
    • live_points is injected into meta dict before engine.analyze() so the
      engine's _check_rsi_entry can gate on it (see MemeSignalEngine v1.5).

  FIX 3 — Entry window widened for quiet-but-valid markets:
    • Combination of Fix 1 (lower fee + ignition TP2 targeting) and Fix 2
      (synthetic RSI no longer blocking real tokens) together open the entry
      window in sideways/low-ATR markets that were previously fully locked out.

  FIX 4 — Regime score now stable with thin watchlists:
    • _check_regime filters out synthetic-history tokens before calling
      analyzer.analyze(). Synthetic RSI swings (9→88→49 in 3 cycles) were
      tanking the regime score even with α=0.10 dampening when watching only
      1-2 tokens. Real-history tokens only → stable regime reading.
    • Fallback: if all watched tokens are synthetic, uses full set with warning.

v1.4 changes vs v1.3:
  - Lowered R:R minimum from 1.5 to 1.3 in _can_enter.
    Analysis of the 103-cycle session showed LOBSTAR (+47% move) and ONSING
    were both blocked by R:R 1.30-1.49 despite valid volume (1.4-1.7x) and
    genuine soft signals (4-5/8). ATR compression in choppy 50-70 regime
    markets pushes R:R below 1.5 on otherwise tradeable setups. 1.3 retains
    meaningful edge filter while capturing these setups. NVIDIA, COIN, GROKIUS
    (R:R 0.5-1.0) remain correctly blocked at this threshold.

v1.3 changes vs v1.2:
  - Added stale position exit rule: if a position has been open >= 4 hours
    without hitting TP1, P&L is drifting flat (-5% to +10%), and volume has
    faded below 0.8x average, the position is exited as dead capital.
    Constants: STALE_HOURS=4, STALE_MAX_PNL=10%, STALE_MIN_PNL=-5%,
    STALE_MAX_RV=0.8x. Logged as "STALE" exit type in session stats.
    position_entry_ts recorded at buy and cleaned up on all exit paths.

v1.2 fixes vs v1.1:
  - eviction_fails counter no longer reset on transient (RSI/RR) failures or
    HOLD signals — only a genuine BUY clears it. Previously a single quiet cycle
    would pardon up to 4 accumulated structural failures, preventing eviction.
  - Removed redundant partial day-reset inside _can_enter (only reset
    trades_today, missing halted_today / sl_blacklist / tracker.reset_day).
    _check_daily_reset already handles a full, correct daily reset every cycle.

v1.1 fixes vs v1.0:
  - Fixed DUMP_DROP_PCT docstring: constant is 8%, not 12%
  - Fixed MAX_CONCURRENT docstring and print: now correctly says 5
  - Fixed stale "2% fees" print in _can_enter (fee is 1%)
  - position_entry dict is now written at buy time (was dead field)
  - sl_blacklist resets daily alongside trades_today
  - Removed _quick_rsi (duplicate of _calc_rsi) — uses _calc_rsi directly
  - Eviction blacklist pruned every cycle via MarketConditionAnalyzer helper
  - Startup banner reads min_soft_score from trader instance (not engine class)
  - Background discovery thread prevents scan from stalling exit monitor
"""

import time
import threading
import argparse
from datetime import datetime, date
from typing import Dict, Optional, List, Tuple

from solana_trading_agent import SolanaTradingAgent, TradingMode, TOKENS
from trending_tokens import EnhancedTrendingTokenDetector, AutoTokenSelector
from meme_signal_engine import MemeSignalEngine, _calc_rsi


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_POSITION_USD      = 150.0   # absolute hard cap per trade
BASE_POSITION_PCT     = 0.08    # 8% of portfolio base size
MAX_ATR_FOR_FULL_SIZE = 0.08    # ATR above 8% = scale down position
DAILY_LOSS_LIMIT_PCT  = 8.0     # halt if daily loss >= 8%
MAX_CONCURRENT        = 5       # max 5 meme positions at once
WIN_STREAK_REQ        = 3       # wins before compounding up
WIN_STEP              = 0.03    # +3% per compounding step
LOSS_STEP             = 0.05    # -5% on any loss
MAX_MULT              = 1.80
MIN_MULT              = 0.40
MIN_PROB              = 55.0    # minimum probability to trade
COOLDOWN_AFTER_STOP   = 600     # 10 min cooldown after stop on a token
EVICTION_FAIL_LIMIT   = 5       # evict a token after this many consecutive hard-fail cycles
EVICTION_COOLDOWN     = 900     # 15 min before an evicted token can re-enter watchlist

# ATR floor — prevents stops from being too tight on quiet or young tokens.
# Minimum effective ATR = 1.5%  →  minimum stop = 3x * 1.5% = 4.5%.
# Applies only to the recorded position_atr at entry; engine R:R display
# still shows the raw ATR so the signal report remains honest.
MIN_EFFECTIVE_ATR_PCT = 0.015

# Watchlist health-check thresholds — evict watched tokens whose price action
# has deteriorated to the point where the original entry thesis no longer holds.
# Uses same logic as AutoTokenSelector safety filters (just expressed here for
# the watchlist purge path; actual selector thresholds live in trending_tokens.py).
WATCHLIST_DUMP_H1_PCT       = -10.0   # 1h drop  worse than this → dump, evict
WATCHLIST_DOWNTREND_H6_PCT  = -20.0   # 6h change worse than this → downtrend, evict

# Minimum live candles before a synthetic-history token may be entered.
# Below this threshold the RSI/ATR/MACD readings come mostly from generated
# data and are not reliable enough to base a real-money entry on.
SYNTHETIC_MIN_LIVE_CANDLES  = 30

# Stale position exit — kill dead-money trades where the thesis has expired
STALE_HOURS           = 4.0     # exit if open this long without hitting TP1
STALE_MAX_PNL         = 10.0    # only exit if P&L < this % (don't cut runners)
STALE_MIN_PNL         = -5.0    # don't exit if already near stop (let stop handle it)
STALE_MAX_RV          = 0.8     # require volume confirmation: rv < 0.8x avg = dead move


# ─────────────────────────────────────────────────────────────────────────────
# POSITION SIZER
# ─────────────────────────────────────────────────────────────────────────────

class MemeSizer:
    """
    Meme-specific position sizer.
    Enforces hard $ cap, ATR scaling, and win-streak compounding.
    """

    def __init__(self):
        self._mult             = 1.0
        self._consecutive_wins = 0

    def get_size(self, prob: float, atr_pct: float,
                 total: float, available_usdc: float) -> Tuple[float, str]:
        """Returns (usdc_amount, label). 0.0 if below minimum probability."""
        if prob < MIN_PROB:
            return 0.0, "SKIP"

        # Base: fixed % of portfolio, adjusted by multiplier
        base_usdc = total * BASE_POSITION_PCT * self._mult

        # ATR scale: reduce size proportionally if volatility is extreme
        atr_factor = 1.0
        if atr_pct > MAX_ATR_FOR_FULL_SIZE:
            atr_factor = MAX_ATR_FOR_FULL_SIZE / atr_pct
            atr_factor = max(atr_factor, 0.35)   # floor at 35% of base

        scaled_usdc = base_usdc * atr_factor

        # Hard $ cap — protect against portfolio concentration
        capped_usdc = min(scaled_usdc, MAX_POSITION_USD)

        # Never use more than 90% of available USDC
        final_usdc = min(capped_usdc, available_usdc * 0.90)

        label = "CAP" if scaled_usdc > MAX_POSITION_USD else "STD"
        return final_usdc, label

    def record_win(self):
        self._consecutive_wins += 1
        if self._consecutive_wins >= WIN_STREAK_REQ:
            old = self._mult
            self._mult = min(self._mult + WIN_STEP, MAX_MULT)
            self._consecutive_wins = 0
            if self._mult != old:
                print(f"  [compound] {WIN_STREAK_REQ} wins → "
                      f"sizer {old:.2f}x → {self._mult:.2f}x 🚀")

    def record_loss(self):
        self._consecutive_wins = 0
        old = self._mult
        self._mult = max(self._mult - LOSS_STEP, MIN_MULT)
        if self._mult != old:
            print(f"  [compound] Loss → sizer {old:.2f}x → {self._mult:.2f}x")

    @property
    def multiplier(self) -> float:
        return self._mult

    @property
    def consecutive_wins(self) -> int:
        return self._consecutive_wins


# ─────────────────────────────────────────────────────────────────────────────
# EQUITY TRACKER
# ─────────────────────────────────────────────────────────────────────────────

class EquityTracker:
    def __init__(self, starting: float):
        self._hwm              = starting
        self._day_start_equity = starting

    def update(self, equity: float):
        if equity > self._hwm:
            self._hwm = equity

    def reset_day(self, equity: float):
        self._day_start_equity = equity

    def daily_loss_pct(self, equity: float) -> float:
        if self._day_start_equity <= 0:
            return 0.0
        return max(0.0, (self._day_start_equity - equity) / self._day_start_equity * 100)

    def drawdown_pct(self, equity: float) -> float:
        if self._hwm <= 0:
            return 0.0
        return max(0.0, (self._hwm - equity) / self._hwm * 100)

    @property
    def hwm(self) -> float:
        return self._hwm


# ─────────────────────────────────────────────────────────────────────────────
# MAIN TRADER
# ─────────────────────────────────────────────────────────────────────────────

class MemeCoinTrader:

    REGIME_MIN = 30  # memes can trade in weaker markets, but block the worst downtrends.
                     # Score < 30 = broad market in heavy sell-off; ignition setups also
                     # fail in this environment so there is no upside to staying active.

    def __init__(self, agent: SolanaTradingAgent, auto_discover: bool = True):
        self.agent         = agent
        self.auto_discover = auto_discover
        self.running       = False
        self.engine        = MemeSignalEngine()

        starting       = agent.portfolio.get_total_value()
        self.sizer     = MemeSizer()
        self.tracker   = EquityTracker(starting)

        # min_soft_score: 3/7 = 43% of signals required. The engine default is also 3.
        # We previously lowered to 2 for low-vol markets, but 2/7 = 28% allows entries
        # on non-volume signals alone (MACD + room_to_run is sufficient), which generates
        # mean-reversion entries in downtrends with no real momentum. 3 is the minimum
        # that forces at least some signal confluence before committing capital.
        self.min_soft_score = 3

        # Exit state
        self.position_peaks:    Dict[str, float] = {}
        self.position_entry:    Dict[str, float] = {}  # entry price per token (USD)
        self.position_atr:      Dict[str, float] = {}
        self.position_peak_ts:  Dict[str, float] = {}  # timestamp of peak
        self.tp1_hit:           Dict[str, bool]  = {}
        self.tp2_hit:           Dict[str, bool]  = {}
        self.tp_lockout:        Dict[str, float] = {}
        self.tp_lockout_secs    = 120
        self.position_entry_ts: Dict[str, float] = {}  # when position was opened (for stale exit)

        # Risk controls
        self.max_trades_per_day  = 60
        self.trade_cooldown_secs = 30    # 30s between trades on same token
        self.min_live_candles    = 15    # lower than established — memes are newer

        self.trades_today  = 0
        self.day_start     = date.today()
        self.halted_today  = False

        self.last_trades:   Dict[str, datetime] = {}
        self.sl_strikes:    Dict[str, int]      = {}
        self.sl_blacklist:  set                 = set()
        self.sl_cooldown:   Dict[str, float]    = {}  # per-token cooldown after stop

        # Auto-eviction: remove dead/quiet tokens from watchlist
        self.eviction_fails:     Dict[str, int]   = {}  # consecutive hard-fail cycles
        self.eviction_blacklist: Dict[str, float] = {}  # sym -> eviction timestamp

        # RSI skip cooldown at discovery — prevents re-fetching GeckoTerminal history
        # every 60s for tokens that are persistently overbought or oversold.
        self.discovery_rsi_skip:     Dict[str, float] = {}
        self.DISCOVERY_RSI_SKIP_SECS = 300  # 5 min cooldown before trying again

        # Discovery — runs in background thread to avoid stalling exit monitor
        self.refresh_interval   = 60     # seconds between discovery scans
        self.last_refresh       = 0
        self._discovery_lock    = threading.Lock()
        self._discovery_running = False
        self.detector           = EnhancedTrendingTokenDetector()
        self.selector           = AutoTokenSelector(self.detector)
        self.token_metadata:     Dict[str, Dict] = {}

        self._current_score     = 50.0
        self.session_trades:    List[Dict] = []

    # ─────────────────────────────────────────────────────────────────────────
    # REGIME CHECK
    # ─────────────────────────────────────────────────────────────────────────

    def _check_regime(self, tokens_analysis: Dict) -> Tuple[bool, float, str]:
        analyzer = getattr(self.agent, "market_analyzer", None)
        if not analyzer:
            return True, 50.0, "no analyzer"

        if not tokens_analysis:
            cached = getattr(analyzer, "_smoothed_score", None)
            if cached is not None:
                self._current_score = cached
                lbl = "🟢" if cached >= 70 else ("🟡" if cached >= 50 else "🔴")
                return cached >= self.REGIME_MIN, cached, f"  [regime] {lbl} ({cached:.0f})"
            print(f"  [regime] ⏳ warming up (no watched tokens yet)")
            return True, 50.0, "  [regime] no data"

        # Fix 4: Exclude tokens with synthetic history from regime analysis.
        # Synthetic RSI can swing 9→88→49 in minutes (generated data artefact),
        # which tanks the regime score even though no real market deterioration
        # has occurred — especially damaging when watching only 1-2 tokens.
        real_analysis = {
            tok: ana for tok, ana in tokens_analysis.items()
            if not self.token_metadata.get(tok, {}).get("is_synthetic")
        }
        # Fallback: if every watched token is synthetic, use all of them rather
        # than passing an empty dict (which would return stale cached regime).
        regime_input = real_analysis if real_analysis else tokens_analysis
        if not real_analysis and tokens_analysis:
            # All synthetic — add a note so operator knows
            print(f"  [regime] ⚠  all watched tokens have synthetic history — "
                  f"regime computed on synthetic RSI (less reliable)")

        status, reason, details = analyzer.analyze(regime_input)
        score = details["overall_score"]
        self._current_score = score
        self.selector.set_regime(score)

        if score < self.REGIME_MIN:
            return False, score, f"⏸  regime {score:.0f} < {self.REGIME_MIN} — cash only"

        lbl = "🟢 BULLISH" if score >= 70 else "🟡 NEUTRAL"
        print(f"  [regime] {lbl} ({score:.0f})  sizer×{self.sizer.multiplier:.2f}")
        return True, score, f"✅ regime {score:.0f}"

    # ─────────────────────────────────────────────────────────────────────────
    # DAILY RESET + CIRCUIT BREAKER
    # ─────────────────────────────────────────────────────────────────────────

    def _check_daily_reset(self, equity: float):
        today = date.today()
        if today != self.day_start:
            self.day_start    = today
            self.trades_today = 0
            self.halted_today = False
            self.sl_blacklist = set()   # clear blacklist each day
            self.tracker.reset_day(equity)
            print(f"\n  [day] New day — counters and blacklist reset")

    def _check_circuit_breaker(self, equity: float) -> bool:
        if self.halted_today:
            return True
        loss = self.tracker.daily_loss_pct(equity)
        if loss >= DAILY_LOSS_LIMIT_PCT:
            self.halted_today = True
            print(f"\n  ⛔ CIRCUIT BREAKER: Daily loss {loss:.1f}% — halted for today")
            return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # DISCOVERY — meme-focused, runs in background thread
    # ─────────────────────────────────────────────────────────────────────────

    def _trigger_discovery(self):
        """
        Spawn a background thread for the discovery scan so the main loop
        (and critically, the exit monitor) is never blocked during HTTP calls
        to GeckoTerminal / DexScreener which can take 30-60 seconds.
        """
        with self._discovery_lock:
            if self._discovery_running:
                return  # previous scan still in progress — skip this cycle
            self._discovery_running = True

        t = threading.Thread(target=self._discovery_thread, daemon=True)
        t.start()

    def _discovery_thread(self):
        """Worker — called from background thread only."""
        try:
            self.refresh_tokens()
        finally:
            with self._discovery_lock:
                self._discovery_running = False

    def refresh_tokens(self):
        try:
            print("\n[discover] Scanning for meme setups...")
            # Prioritise NEW LISTINGS and BOOSTED tokens for meme plays
            self.selector.preferences.update({
                "min_liquidity":        50_000,
                "min_volume":           20_000,
                "max_tokens":           15,
                "prefer_rising":        True,       # memes: catch momentum
                "max_price_change_24h": 9999,       # no cap — 5000% moves are the point
                "prioritize_boosted":   True,       # boosted = project spending money = signal
                "min_score":            25.0,
            })

            selected = self.selector.get_token_symbols()

            # Always show full trending table including new listings
            trending = self.detector.get_trending_tokens(
                min_liquidity=50_000, min_volume_24h=20_000,
                max_tokens=20, prioritize_boosted=True)
            if trending:
                self.detector.display_trending(trending)

            # ── Watchlist health check (Fix 1 v1.6) ──────────────────────────
            # Refresh h1/h6 metadata for watched tokens from the current
            # trending snapshot, then evict any that now fail safety filters.
            # This prevents dead/dumping tokens (e.g. flagged "6h downtrend"
            # in every discovery pass) from staying on the watchlist and
            # being entered again on the next qualifying signal.
            if trending:
                trending_by_sym = {t["symbol"]: t for t in trending}
                to_evict = []
                for sym in list(self.agent.watched_tokens):
                    if sym in self.agent.portfolio.positions:
                        continue  # never evict while holding — exit logic needs it
                    if sym not in trending_by_sym:
                        continue  # not in current scan — can't assess, leave it
                    tdata = trending_by_sym[sym]
                    h1 = tdata.get("price_change_h1", 0.0)
                    h6 = tdata.get("price_change_h6", 0.0)
                    # Refresh stored metadata with current price data
                    if sym in self.token_metadata:
                        self.token_metadata[sym]["price_change_h1"] = h1
                        self.token_metadata[sym]["price_change_h6"] = h6
                    if h1 < WATCHLIST_DUMP_H1_PCT:
                        to_evict.append((sym, f"1h dump ({h1:+.1f}%)"))
                    elif h6 < WATCHLIST_DOWNTREND_H6_PCT:
                        to_evict.append((sym, f"6h downtrend ({h6:+.1f}%)"))
                for sym, reason in to_evict:
                    if sym in self.agent.watched_tokens:
                        self.agent.watched_tokens.remove(sym)
                        self.token_metadata.pop(sym, None)
                        self.eviction_blacklist[sym] = time.time()
                        self.eviction_fails.pop(sym, None)
                        print(f"  ⚠  [health] Evicting {sym}: {reason} — "
                              f"thesis dead, blacklisted {EVICTION_COOLDOWN//60}m")
            # ── end watchlist health check ────────────────────────────────────

            if not selected:
                print("[discover] No tokens pass meme criteria — waiting")
                self.last_refresh = time.time()
                return

            print("\n[discover] MEME CANDIDATES:")
            print("-" * 65)
            added = skipped_rsi = skipped_liq = 0

            for token in selected:
                sym       = token["symbol"]
                addr      = token["address"]
                pair_addr = token.get("pair_address", "")
                liq       = token.get("liquidity_usd", 0)
                ch24      = token.get("price_change_24h", 0)
                ch1       = token.get("price_change_h1", 0)
                score_t   = token.get("score", 0)
                quality   = token.get("quality_indicator", "")
                boosted   = token.get("is_boosted", False)
                mcap      = token.get("market_cap_usd", 0)
                age_h     = token.get("age_hours", None)

                if sym in self.agent.watched_tokens:
                    continue

                # Skip eviction-blacklisted tokens until cooldown expires
                evict_ts = self.eviction_blacklist.get(sym)
                if evict_ts is not None:
                    remaining = EVICTION_COOLDOWN - (time.time() - evict_ts)
                    if remaining > 0:
                        print(f"  [skip] {sym}: evicted — cooldown {remaining/60:.0f}m remaining")
                        continue
                    else:
                        del self.eviction_blacklist[sym]

                # RSI skip cooldown — don't re-fetch GeckoTerminal history for tokens
                # that were overbought/oversold at last check (wastes API calls).
                rsi_skip_ts = self.discovery_rsi_skip.get(sym)
                if rsi_skip_ts is not None:
                    remaining_rsi = self.DISCOVERY_RSI_SKIP_SECS - (time.time() - rsi_skip_ts)
                    if remaining_rsi > 0:
                        continue  # silent — no console spam
                    else:
                        del self.discovery_rsi_skip[sym]

                # Meme-specific liq bounds — aligned with engine's MEME_MIN_LIQ=$50k hard gate.
                # Previously $35k, which caused tokens in the $35k–$49k band to be fetched
                # and watched but hit a structural liquidity hard-fail every single cycle
                # until evicted (wasted GeckoTerminal calls, watchlist clutter).
                if liq < 50_000:
                    print(f"  [skip] {sym}: liq ${liq/1000:.0f}k — rug risk (< $50k)")
                    skipped_liq += 1
                    continue
                if liq > 10_000_000:
                    print(f"  [skip] {sym}: liq ${liq/1e6:.1f}M — pump already in")
                    skipped_liq += 1
                    continue
                if mcap and mcap > 50_000_000:
                    print(f"  [skip] {sym}: mcap ${mcap/1e6:.0f}M — too big for meme play")
                    skipped_liq += 1
                    continue

                # Fetch history BEFORE adding to watchlist.
                print(f"  Fetching {sym} {'🚀[BOOST]' if boosted else ''}...")
                hist, is_real = self.detector.fetch_historical_prices(
                    pair_addr, n_points=500, token_info=token)

                if not hist:
                    print(f"  [skip] {sym}: no price history")
                    continue

                # Extract closes
                closes = [c["close"] if isinstance(c, dict) else c for c in hist]

                # RSI gate at discovery: skip overbought (already pumped) or
                # deeply oversold (dying — not a bounce setup, high rug risk).
                rsi_val = _calc_rsi(closes)
                if rsi_val is not None:
                    if rsi_val > 72:
                        print(f"  [skip] {sym}: RSI {rsi_val:.0f} > 72 — already pumped")
                        self.discovery_rsi_skip[sym] = time.time()
                        skipped_rsi += 1
                        continue
                    if rsi_val < 28:
                        print(f"  [skip] {sym}: RSI {rsi_val:.0f} < 28 — oversold/dying")
                        self.discovery_rsi_skip[sym] = time.time()
                        skipped_rsi += 1
                        continue

                # All checks passed — add to watchlist and prefill
                if sym not in TOKENS:
                    TOKENS[sym] = addr
                self.agent.add_token(sym, addr)

                self.token_metadata[sym] = {
                    "is_boosted":           boosted,
                    "is_synthetic":         not is_real,   # Fix 2: flag synthetic history
                    "quality_indicator":    quality,
                    "score":                score_t,
                    "price_change_24h":     ch24,
                    "price_change_h1":      ch1,
                    "liquidity_usd":        liq,
                    "liquidity_change_pct": token.get("liquidity_change_pct", 0.0),
                    "market_cap_usd":       mcap,
                    "age_hours":            age_h,
                    "volume_24h_usd":       token.get("volume_24h_usd", 0),
                }

                self.agent.prefill_price_history(sym, hist)
                buf = self.agent.price_history[sym]
                buf["live_points"] = len(hist) if is_real else 0
                status_s = f"[OK] {len(hist)} candles" if is_real else "[~] synthetic"

                age_s  = f"{age_h:.1f}h" if age_h else "age?"
                mcap_s = f"${mcap/1000:.0f}k" if mcap else "mcap?"
                rsi_s  = f"{rsi_val:.0f}" if rsi_val else "?"
                print(f"  → {added+1}. {sym:<10} {quality:<10}  "
                      f"24h={ch24:+.0f}%  liq=${liq/1000:.0f}k  "
                      f"age={age_s}  mcap={mcap_s}  RSI={rsi_s}  {status_s}")
                added += 1

            print("-" * 65)
            print(f"[discover] Added:{added}  SkippedRSI:{skipped_rsi}  "
                  f"SkippedLiq:{skipped_liq}  "
                  f"Watching:{len(self.agent.watched_tokens)}\n")
            self.last_refresh = time.time()

        except Exception as e:
            import traceback
            print(f"[discover] Error: {e}")
            traceback.print_exc()
            self.last_refresh = time.time() - self.refresh_interval + 30

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY GATE
    # ─────────────────────────────────────────────────────────────────────────

    def _can_enter(self, token: str, analysis: Dict) -> bool:
        if token in self.sl_blacklist:
            return False
        if analysis.get("hard_fail"):
            return False

        # Fix 2 (v1.6): Block entries on tokens with synthetic price history
        # that haven't accumulated enough real candles yet.  Indicators computed
        # on generated data (RSI, ATR, MACD) look legitimate but are artefacts
        # of the generation algorithm — they do not reflect actual order flow.
        meta = self.token_metadata.get(token, {})
        if meta.get("is_synthetic"):
            buf      = self.agent.price_history.get(token, {})
            live_pts = buf.get("live_points", 0)
            if live_pts < SYNTHETIC_MIN_LIVE_CANDLES:
                print(f"  [skip] {token}: synthetic history "
                      f"({live_pts}/{SYNTHETIC_MIN_LIVE_CANDLES} live pts) "
                      f"— waiting for real order flow data")
                return False

        # Per-token stop cooldown (10 min after a stop on this specific token)
        if token in self.sl_cooldown:
            if time.time() < self.sl_cooldown[token]:
                rem = int(self.sl_cooldown[token] - time.time())
                print(f"  [skip] stop cooldown {rem}s")
                return False
            del self.sl_cooldown[token]

        prob = analysis.get("prob_score", 0.0)
        if prob < MIN_PROB:
            print(f"  [skip] prob {prob:.0f} < {MIN_PROB:.0f}")
            return False

        if token in self.tp_lockout:
            if time.time() < self.tp_lockout[token]:
                rem = int(self.tp_lockout[token] - time.time())
                print(f"  [skip] TP lockout {rem}s")
                return False
            del self.tp_lockout[token]

        if token in self.last_trades:
            elapsed = (datetime.now() - self.last_trades[token]).total_seconds()
            if elapsed < self.trade_cooldown_secs:
                print(f"  [skip] cooldown {int(self.trade_cooldown_secs - elapsed)}s")
                return False

        if self.trades_today >= self.max_trades_per_day:
            print(f"  [skip] daily limit {self.trades_today}/{self.max_trades_per_day}")
            return False

        if token in self.agent.portfolio.positions:
            print(f"  [skip] already holding")
            return False

        if len(self.agent.portfolio.positions) >= MAX_CONCURRENT:
            print(f"  [skip] max {MAX_CONCURRENT} concurrent meme positions")
            return False

        total = self.agent.portfolio.get_total_value()
        if self.agent.portfolio.usdc_balance < 15:
            print(f"  [skip] insufficient USDC")
            return False

        if analysis.get("buy_pts", 0) < self.min_soft_score:
            print(f"  [skip] soft {analysis.get('buy_pts',0)}/{self.min_soft_score}")
            return False

        # ── Volume hard gate ─────────────────────────────────────────────────
        # Require at least 1.2x average volume — the banner states this requirement
        # and it exists for good reason: entries without volume confirmation are
        # mean-reversion plays in disguise, not momentum setups.
        rv     = analysis.get("relative_volume", 1.0)
        detail = analysis.get("buy_pts_detail", {})
        has_vol_signal = detail.get("vol_explosion") or detail.get("vol_spike")
        if rv < 1.2 and not has_vol_signal:
            print(f"  [skip] vol {rv:.1f}x < 1.2x — no volume confirmation")
            return False

        # ── R:R gate — recomputed with effective ATR for consistency ────────
        # RATIONALE: analysis["rr_ratio"] was computed by the engine with raw ATR.
        # _execute_buy() and _monitor_exits() use effective_atr = max(ATR, 1.5%).
        # R:R actually INCREASES with ATR (tp_mult > stop_mult so fee matters less),
        # so the floor never degrades R:R — this recompute is purely for consistency:
        # what _can_enter approves should match what _execute_buy will set up.
        # The real protection for the floor mismatch is the sizer fix (BUG 3):
        # effective_atr is now also passed to sizer.get_size() so dollar risk
        # reflects the actual stop width, not the raw ATR.
        # BUG 2 fix: is_ignition from the engine drives the correct RR threshold.
        from meme_signal_engine import (MEME_FEE_PCT, MEME_STOP_MULT, MEME_MAX_STOP_PCT,
                                        MEME_TP1_MULT, MEME_TP2_MULT,
                                        MEME_MIN_RR, MEME_MIN_RR_IGNITION)
        raw_atr      = analysis.get("atr_pct", 0.0)
        effective_atr = max(raw_atr, MIN_EFFECTIVE_ATR_PCT)
        is_ignition  = analysis.get("is_ignition", False)

        fee      = MEME_FEE_PCT
        stop_pct = min(MEME_STOP_MULT * effective_atr, MEME_MAX_STOP_PCT)
        tp_pct   = (MEME_TP2_MULT if is_ignition else MEME_TP1_MULT) * effective_atr
        risk     = stop_pct + fee
        reward   = tp_pct - fee
        effective_rr = (reward / risk) if (risk > 0 and reward > 0) else 0.0
        min_rr   = MEME_MIN_RR_IGNITION if is_ignition else MEME_MIN_RR

        if effective_rr < min_rr:
            mode = " [ignition→TP2]" if is_ignition else ""
            atr_note = (f" (raw {raw_atr*100:.1f}% → floor {effective_atr*100:.1f}%)"
                        if effective_atr > raw_atr else f" (ATR {raw_atr*100:.1f}%)")
            print(f"  [skip] effective R:R {effective_rr:.2f} < {min_rr}{mode}{atr_note}")
            return False

        # Reject if sell divergence just detected
        if analysis.get("sell_divergence", False):
            print(f"  [skip] sell divergence — insiders distributing")
            return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT MONITORING — meme-adapted
    # ─────────────────────────────────────────────────────────────────────────

    def _monitor_exits(self, current_analyses: Dict):
        for token, position in list(self.agent.portfolio.positions.items()):
            pnl      = position.pnl_percentage
            analysis = current_analyses.get(token, {})

            # Track peak
            if token not in self.position_peaks or pnl > self.position_peaks[token]:
                self.position_peaks[token]   = pnl
                self.position_peak_ts[token] = time.time()
            peak    = self.position_peaks[token]
            peak_ts = self.position_peak_ts.get(token, time.time())

            # ATR levels (locked at entry, recomputed as fallback)
            atr_pct    = self.position_atr.get(token) or analysis.get("atr_pct", 0.05)
            stop_pct   = min(MemeSignalEngine.STOP_ATR_MULT  * atr_pct, MemeSignalEngine.MAX_STOP_PCT) * 100
            tp1_pct    = MemeSignalEngine.TP1_ATR_MULT   * atr_pct * 100
            tp2_pct    = MemeSignalEngine.TP2_ATR_MULT   * atr_pct * 100
            trail_pct  = MemeSignalEngine.TRAIL_ATR_MULT * atr_pct * 100

            # ── DUMP DETECTION — fast 8% drop from peak ───────────────────────
            # If price drops >8% from peak AND it happened fast = dump, exit NOW
            drop_from_peak = peak - pnl
            time_at_peak   = time.time() - peak_ts
            if (peak > tp1_pct * 0.5   # only check after token has moved meaningfully
                    and drop_from_peak >= MemeSignalEngine.DUMP_DROP_PCT * 100
                    and time_at_peak < 180):   # happened in < 3 minutes
                print(f"\n  🚨 DUMP DETECTED {token}  "
                      f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                      f"Drop:{drop_from_peak:.1f}% in {time_at_peak:.0f}s — EXIT NOW")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self._record(token, "DUMP_EXIT", pnl, position.pnl)
                    self.sizer.record_loss() if pnl < 0 else self.sizer.record_win()
                    self._cleanup_position(token)
                continue

            # ── HARD STOP ────────────────────────────────────────────────────
            if pnl <= -stop_pct:
                print(f"\n  ❌ STOP {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-stop:{-stop_pct:.1f}%  ATR={atr_pct*100:.1f}%")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token]   = datetime.now()
                    self.sl_strikes[token]    = self.sl_strikes.get(token, 0) + 1
                    self.sl_cooldown[token]   = time.time() + COOLDOWN_AFTER_STOP
                    if self.sl_strikes[token] >= 2:
                        self.sl_blacklist.add(token)
                        print(f"  🚫 Blacklisted {token} (2 stops)")
                    self._record(token, "STOP", pnl, position.pnl)
                    self.sizer.record_loss()
                    self._cleanup_position(token)
                continue

            # ── TP1 — sell 60% ───────────────────────────────────────────────
            if pnl >= tp1_pct and not self.tp1_hit.get(token, False):
                sell_amt = position.amount * MemeSignalEngine.TP1_AMOUNT
                print(f"\n  💰 TP1 {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-TP1:{tp1_pct:.1f}%  Locking {MemeSignalEngine.TP1_AMOUNT*100:.0f}%")
                trade = self.agent.execute_swap(token, "USDC", sell_amt)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp1_hit[token]     = True
                    # NOTE: _record() is intentionally NOT called here.
                    # The trade is recorded at the final exit (TP2, TRAIL, or DUMP_EXIT)
                    # to avoid double-counting in session stats. TP1 is a partial exit,
                    # not a closed trade.
                continue

            # ── TP2 — sell remainder ─────────────────────────────────────────
            if (pnl >= tp2_pct and self.tp1_hit.get(token)
                    and not self.tp2_hit.get(token, False)):
                print(f"\n  🚀💰 TP2 {token}  P&L:{pnl:+.1f}%  "
                      f"ATR-TP2:{tp2_pct:.1f}%  Exiting remainder")
                trade = self.agent.execute_swap(token, "USDC", position.amount)
                if trade:
                    self.last_trades[token] = datetime.now()
                    self.tp2_hit[token]     = True
                    self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                    self._record(token, "TP2", pnl, position.pnl)
                    self.sizer.record_win()
                    self._cleanup_position(token)
                continue

            # ── TRAILING STOP (ATR-based, after TP1) ─────────────────────────
            if peak >= tp1_pct:
                floor = peak - trail_pct
                if pnl <= floor:
                    print(f"\n  🏁 TRAIL {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%  Trail:{trail_pct:.1f}%")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self.tp_lockout[token]  = time.time() + self.tp_lockout_secs
                        self._record(token, "TRAIL", pnl, position.pnl)
                        self.sizer.record_win() if pnl > 0 else self.sizer.record_loss()
                        self._cleanup_position(token)
                else:
                    print(f"  🏃 trail {token}  "
                          f"Peak:{peak:+.1f}%  Now:{pnl:+.1f}%  "
                          f"Floor:{floor:+.1f}%")

            # ── STALE EXIT — thesis expired, dead capital ─────────────────────
            # If a position hasn't hit TP1 within STALE_HOURS AND P&L is
            # drifting flat (not near stop, not a runner), exit to free capital.
            # Volume confirmation required — if vol is still elevated, wait.
            if not self.tp1_hit.get(token, False):
                open_hours = (time.time() - self.position_entry_ts.get(token, time.time())) / 3600
                rv         = analysis.get("relative_volume", 1.0)
                if (open_hours >= STALE_HOURS
                        and STALE_MIN_PNL < pnl < STALE_MAX_PNL
                        and rv < STALE_MAX_RV):
                    print(f"\n  ⏳ STALE {token}  "
                          f"Open:{open_hours:.1f}h  P&L:{pnl:+.1f}%  "
                          f"Vol:{rv:.1f}x — thesis expired, exiting")
                    trade = self.agent.execute_swap(token, "USDC", position.amount)
                    if trade:
                        self.last_trades[token] = datetime.now()
                        self._record(token, "STALE", pnl, position.pnl)
                        self.sizer.record_loss() if pnl < 0 else self.sizer.record_win()
                        self._cleanup_position(token)

    def _cleanup_position(self, token: str):
        self.position_peaks.pop(token, None)
        self.position_peak_ts.pop(token, None)
        self.position_entry_ts.pop(token, None)
        self.position_entry.pop(token, None)
        self.position_atr.pop(token, None)
        self.tp1_hit.pop(token, None)
        self.tp2_hit.pop(token, None)

    def _record(self, token: str, exit_type: str, pnl_pct: float, pnl_usd: float):
        self.session_trades.append({
            "token":     token,
            "exit":      exit_type,
            "pnl_pct":   pnl_pct,
            "pnl_usd":   pnl_usd,
            "timestamp": datetime.now(),
            "win":       pnl_pct > 0,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # EXECUTE BUY
    # ─────────────────────────────────────────────────────────────────────────

    def _execute_buy(self, token: str, analysis: Dict):
        prob    = analysis.get("prob_score", 0.0)
        cur     = analysis.get("current_price", 0.0)
        atr_pct = analysis.get("atr_pct", 0.05)
        rr      = analysis.get("rr_ratio", 0.0)
        rv      = analysis.get("relative_volume", 1.0)
        vol_lbl = analysis.get("volume_state", "?")
        vel     = analysis.get("velocity_5", 0.0)
        mult    = analysis.get("multiple_from_low", 1.0)
        if cur == 0:
            return

        # Fix 3 (v1.6): ATR floor — prevents stops from being too tight on
        # quiet or young tokens whose history is too short to give a reliable
        # ATR reading.  Floor = MIN_EFFECTIVE_ATR_PCT (1.5%) → min stop 4.5%.
        # We record the FLOORED value in position_atr (which drives the stop).
        # The raw engine ATR is still shown in the buy banner for transparency.
        effective_atr = max(atr_pct, MIN_EFFECTIVE_ATR_PCT)
        if effective_atr > atr_pct:
            atr_floor_applied = True
        else:
            atr_floor_applied = False

        total    = self.agent.portfolio.get_total_value()
        # BUG 3 FIX: use effective_atr (floored) for sizing — position size should
        # reflect the actual stop width (4.5% min), not the raw ATR (which may be
        # 0.4%) that would imply a 1.2% stop that doesn't exist post-floor.
        usdc_amt, size_label = self.sizer.get_size(
            prob, effective_atr, total, self.agent.portfolio.usdc_balance)

        if usdc_amt < 5:
            print(f"  [skip] size ${usdc_amt:.2f} too small")
            return

        detail = analysis.get("buy_pts_detail", {})
        flags  = "".join([
            "V" if detail.get("vol_explosion") else ("v" if detail.get("vol_spike") else "."),
            "P" if detail.get("velocity")  else ".",
            "R" if detail.get("rsi_rise")  else ".",
            "M" if detail.get("macd")      else ".",
            "X" if detail.get("room")      else ".",
            "E" if detail.get("ema_vel")   else ".",
            "A" if detail.get("vol_build") else ".",
        ])

        stop_pct = min(MemeSignalEngine.STOP_ATR_MULT  * effective_atr, MemeSignalEngine.MAX_STOP_PCT) * 100
        tp1_pct  = MemeSignalEngine.TP1_ATR_MULT   * effective_atr * 100
        tp2_pct  = MemeSignalEngine.TP2_ATR_MULT   * effective_atr * 100

        boosted = self.token_metadata.get(token, {}).get("is_boosted", False)
        age_h   = self.token_metadata.get(token, {}).get("age_hours", None)

        print(f"\n  ══ 🚀 BUY MEME {token} {'🚀[BOOST]' if boosted else ''} ══")
        print(f"     Prob:       {prob:.0f}%  |  Score: {analysis.get('buy_pts',0)}/7  [{flags}]")
        print(f"     Size:       ${usdc_amt:.2f}  ({size_label})  sizer×{self.sizer.multiplier:.2f}")
        print(f"     Wins:       {self.sizer.consecutive_wins}/{WIN_STREAK_REQ}")
        print(f"     Price:      ${cur:.8f}")
        print(f"     Vol:        {vol_lbl}  Velocity: {vel:+.1f}%/5bars")
        print(f"     From low:   {mult:.1f}x  R:R: {rr:.2f}")
        if atr_floor_applied:
            print(f"     ATR:        {atr_pct*100:.1f}% raw → {effective_atr*100:.1f}% (floored)  →  "
                  f"Stop:{-stop_pct:.1f}%  TP1:+{tp1_pct:.1f}%  TP2:+{tp2_pct:.1f}%")
        else:
            print(f"     ATR:        {atr_pct*100:.1f}%  →  "
                  f"Stop:{-stop_pct:.1f}%  TP1:+{tp1_pct:.1f}%  TP2:+{tp2_pct:.1f}%")
        if age_h:
            print(f"     Age:        {age_h:.1f}h")

        trade = self.agent.execute_swap("USDC", token, usdc_amt)
        if trade:
            self.last_trades[token]       = datetime.now()
            self.trades_today            += 1
            self.position_peaks[token]    = 0.0
            self.position_peak_ts[token]  = time.time()
            self.position_entry_ts[token] = time.time()   # for stale exit
            self.position_entry[token]    = cur           # record entry price
            self.position_atr[token]      = effective_atr  # floored ATR drives stop/TP
            self.tp1_hit[token]           = False
            self.tp2_hit[token]           = False
            print(f"  ✅ Executed")
        else:
            print(f"  ❌ Failed")

    # ─────────────────────────────────────────────────────────────────────────
    # SESSION STATS
    # ─────────────────────────────────────────────────────────────────────────

    def _print_stats(self):
        if not self.session_trades:
            return
        wins   = [t for t in self.session_trades if t["win"]]
        losses = [t for t in self.session_trades if not t["win"]]
        wr     = len(wins) / len(self.session_trades) * 100
        pnl    = sum(t["pnl_usd"] for t in self.session_trades)
        by_exit = {}
        for t in self.session_trades:
            by_exit[t["exit"]] = by_exit.get(t["exit"], 0) + 1

        print(f"\n  ── Meme Session Stats ────────────────────────────────")
        print(f"     Trades: {len(self.session_trades)}  "
              f"WR: {wr:.0f}%  ({len(wins)}W/{len(losses)}L)  "
              f"Net: ${pnl:+.2f}")
        if wins:
            print(f"     Avg win:  +{sum(t['pnl_pct'] for t in wins)/len(wins):.1f}%")
        if losses:
            print(f"     Avg loss:  {sum(t['pnl_pct'] for t in losses)/len(losses):.1f}%")
        if wins and losses:
            avg_w = sum(t["pnl_pct"] for t in wins) / len(wins)
            avg_l = abs(sum(t["pnl_pct"] for t in losses) / len(losses))
            if avg_l > 0:
                print(f"     Realised R:R: {avg_w/avg_l:.2f}")
        exits_str = "  ".join(f"{k}:{v}" for k, v in sorted(by_exit.items()))
        print(f"     Exits:  {exits_str}")
        print(f"     Sizer ×{self.sizer.multiplier:.2f}  "
              f"Win streak: {self.sizer.consecutive_wins}/{WIN_STREAK_REQ}  "
              f"EqHWM: ${self.tracker.hwm:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # MAIN LOOP
    # ─────────────────────────────────────────────────────────────────────────

    def run(self, update_interval: int = 15):
        self.running = True
        SKIP = {"USDC", "USDT", "USDS", "DAI", "USD1", "BUSD", "TUSD"}

        print("\n" + "=" * 70)
        print("  🚀 SOLANA MEME COIN TRADER  —  v1.7")
        print("=" * 70)
        print("\n⚠  HIGH RISK — read the docstring before live trading")
        print(f"\nRegime gate:      score >= {self.REGIME_MIN}  (meme floor — blocks heavy sell-off markets)")
        print(f"Fee assumption:   0.6% round trip (0.3% per leg — Jupiter/Raydium actual)")
        print(f"Circuit breaker:  halt if daily loss >= {DAILY_LOSS_LIMIT_PCT}%")
        # Import here so constants are available for the summary block below
        from meme_signal_engine import MEME_MIN_RSI, MEME_MAX_RSI_ENTRY

        print(f"\nEntry filters:")
        print(f"  Liq:     $50k - $10M  (sweet spot for meme moves)")
        print(f"  Age:     > 2h  (skip brand-new = rug risk)")
        print(f"  Mcap:    < $50M  (skip already-pumped)")
        print(f"  RSI:     {MEME_MIN_RSI}-{MEME_MAX_RSI_ENTRY}  (early stage only)")
        print(f"  Vol:     > 1.2x average (some interest required)")
        print(f"  R:R:     > 1.0 after 0.6% fees (ignition setups: > 0.65 vs TP2; effective ATR used)")
        print(f"  Soft score: >= {self.min_soft_score}/7 signals")
        print(f"\nATR-based exits:")
        print(f"  Stop:    {MemeSignalEngine.STOP_ATR_MULT}x ATR  (max {MemeSignalEngine.MAX_STOP_PCT*100:.0f}%)")
        print(f"  TP1:     {MemeSignalEngine.TP1_ATR_MULT}x ATR  (sell {MemeSignalEngine.TP1_AMOUNT*100:.0f}%)")
        print(f"  TP2:     {MemeSignalEngine.TP2_ATR_MULT}x ATR  (sell remainder)")
        print(f"  Trail:   {MemeSignalEngine.TRAIL_ATR_MULT}x ATR below peak  (after TP1)")
        print(f"  Dump:    exit if -{MemeSignalEngine.DUMP_DROP_PCT*100:.0f}% from peak in <3 min")
        print(f"  Stale:   exit if open >{STALE_HOURS:.0f}h, P&L {STALE_MIN_PNL:.0f}%→{STALE_MAX_PNL:.0f}%, vol <{STALE_MAX_RV}x")
        print(f"\nPosition sizing:")
        print(f"  Base:    {BASE_POSITION_PCT*100:.0f}% of portfolio")
        print(f"  Cap:     ${MAX_POSITION_USD:.0f} absolute maximum")
        print(f"  Concurrent: max {MAX_CONCURRENT}")
        print(f"  Compounding: {WIN_STREAK_REQ} wins → +{WIN_STEP*100:.0f}% size")
        print(f"\nCycle: {update_interval}s  Discovery: every {self.refresh_interval}s (background thread)")
        print(f"\nCtrl+C to stop")
        print("=" * 70)

        analyzer = getattr(self.agent, "market_analyzer", None)
        iteration = 0

        try:
            while self.running:
                iteration += 1
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"\n{'='*70}")
                print(f"Cycle #{iteration} — {now_str}")
                print(f"{'='*70}")

                # Invalidate regime cache so this cycle gets a fresh compute
                if analyzer:
                    analyzer.invalidate_cache()
                    # Prune stale eviction blacklist entries to prevent unbounded growth
                    pruned = analyzer.prune_eviction_blacklist(
                        self.eviction_blacklist, EVICTION_COOLDOWN)
                    if pruned:
                        print(f"  [evict] Pruned {len(pruned)} expired blacklist entries: "
                              f"{', '.join(pruned)}")

                # Discovery — trigger in background thread if interval elapsed
                if self.auto_discover:
                    if time.time() - self.last_refresh > self.refresh_interval:
                        self._trigger_discovery()

                # Market update
                print("\n[market] Updating prices...")
                self.agent.update_market_data()

                # Build analysis
                tokens_analysis: Dict[str, Dict] = {}
                for token in list(self.agent.watched_tokens):
                    if token in SKIP:
                        continue
                    buf      = self.agent.price_history.get(token, {})
                    live_pts = buf.get("live_points", 0)
                    prices   = buf.get("prices",  [])
                    volumes  = buf.get("volumes", [])
                    if live_pts < self.min_live_candles or not prices:
                        continue
                    meta = self.token_metadata.get(token, {})
                    # Pass live_points into meta so engine can gate synthetic RSI trust
                    meta = {**meta, "live_points": live_pts}
                    analysis = self.engine.analyze(
                        token, prices, volumes,
                        token_meta=meta,
                        min_soft_score=self.min_soft_score,
                    )
                    tokens_analysis[token] = analysis

                # Equity
                equity = self.agent.portfolio.get_total_value()
                self._check_daily_reset(equity)
                self.tracker.update(equity)
                dd         = self.tracker.drawdown_pct(equity)
                daily_loss = self.tracker.daily_loss_pct(equity)
                halted     = self._check_circuit_breaker(equity)

                # Regime
                allowed, regime_score, regime_msg = self._check_regime(tokens_analysis)
                print(f"\n{regime_msg}")

                if halted:
                    print("  ⛔ Circuit breaker — monitoring exits only")
                    if self.agent.portfolio.positions:
                        self._monitor_exits(tokens_analysis)
                elif not allowed:
                    if self.agent.portfolio.positions:
                        print("[positions] Monitoring stops (regime paused)...")
                        self._monitor_exits(tokens_analysis)
                else:
                    if self.agent.portfolio.positions:
                        print("\n[positions] Monitoring exits...")
                        self._monitor_exits(tokens_analysis)

                    print("\n[analysis] Scanning meme tokens...")
                    qualified = []

                    for token, analysis in tokens_analysis.items():
                        meta      = self.token_metadata.get(token, {})
                        boosted   = meta.get("is_boosted", False)
                        hard_fail = analysis.get("hard_fail", False)
                        hard_msg  = analysis.get("hard_fail_reason", "")
                        soft      = analysis.get("buy_pts", 0)
                        prob      = analysis.get("prob_score", 0.0)
                        rv        = analysis.get("relative_volume", 1.0)
                        vel       = analysis.get("velocity_5", 0.0)
                        mult      = analysis.get("multiple_from_low", 1.0)
                        rr        = analysis.get("rr_ratio", 0.0)
                        atr       = analysis.get("atr_pct", 0.0) * 100
                        sell_div  = analysis.get("sell_divergence", False)
                        ts        = datetime.now().strftime("%H:%M:%S")

                        detail = analysis.get("buy_pts_detail", {})
                        flags  = "".join([
                            "V" if detail.get("vol_explosion") else ("v" if detail.get("vol_spike") else "."),
                            "P" if detail.get("velocity")  else ".",
                            "R" if detail.get("rsi_rise")  else ".",
                            "M" if detail.get("macd")      else ".",
                            "X" if detail.get("room")      else ".",
                            "E" if detail.get("ema_vel")   else ".",
                            "A" if detail.get("vol_build") else ".",
                        ])

                        boost_tag = "🚀" if boosted else "  "
                        sig       = analysis["signal"]
                        sd        = {"BUY": "BUY ", "SELL": "SELL", "HOLD": "HOLD"}.get(sig, "HOLD")
                        score_str = ("✗HARD" if hard_fail else
                                     f"{soft}/7[{flags}] p{prob:.0f}% R{rr:.1f}")
                        div_warn  = " ⚠DIV" if sell_div else ""

                        print(f"  [{ts}] {boost_tag}{token:<12}  "
                              f"${analysis['current_price']:<12.8f}  "
                              f"RSI:{analysis['rsi']:.0f}  {sd}  "
                              f"vol:{rv:.1f}x  vel:{vel:+.1f}%  "
                              f"{mult:.1f}x-low  ATR:{atr:.1f}%  "
                              f"{score_str}{div_warn}")

                        if hard_fail:
                            print(f"           ↳ {hard_msg[:90]}")
                            # Auto-eviction: only count STRUCTURAL failures.
                            # Transient failures (rsi_entry, rr_gate) mean the token is
                            # oversold/quiet but may recover.
                            # Structural failures (liquidity, age, mcap, parabolic,
                            # distribution, volume) mean the token fundamentally
                            # doesn't belong and should be removed.
                            # Match filter-type PREFIXES only (e.g. "liquidity:")
                            # NOT arbitrary substrings — "exit liquidity" in an rsi_entry
                            # message would otherwise falsely match the "liquidity" key.
                            structural_keys = (
                                "liquidity:", "age:", "mcap:",
                                "parabolic:", "distribution:", "volume:",
                            )
                            is_structural = any(k in hard_msg for k in structural_keys)
                            if is_structural:
                                # Never evict a token we're currently holding — the
                                # position exit logic needs it to stay on the watchlist.
                                if token in self.agent.portfolio.positions:
                                    print(f"           ↳ structural fail ignored — holding position")
                                else:
                                    self.eviction_fails[token] = self.eviction_fails.get(token, 0) + 1
                                    fails = self.eviction_fails[token]
                                    if fails >= EVICTION_FAIL_LIMIT:
                                        if token in self.agent.watched_tokens:
                                            self.agent.watched_tokens.remove(token)
                                        self.token_metadata.pop(token, None)
                                        self.eviction_blacklist[token] = time.time()
                                        self.eviction_fails.pop(token, None)
                                        print(f"           ⏏  {token} evicted after "
                                              f"{EVICTION_FAIL_LIMIT} structural hard-fails "
                                              f"— blacklisted {EVICTION_COOLDOWN//60}m")
                                    else:
                                        print(f"           ↳ [{fails}/{EVICTION_FAIL_LIMIT}] "
                                              f"structural hard-fails (evict at {EVICTION_FAIL_LIMIT})")
                            else:
                                # Transient failure (RSI/RR) — token may recover.
                                # Do NOT reset the structural fail counter; a quiet
                                # cycle should not pardon accumulated structural failures.
                                print(f"           ↳ transient (RSI/RR) — watching for recovery")
                        elif sig == "BUY":
                            self.eviction_fails.pop(token, None)  # genuine pass — reset counter
                            # Only add to qualified if not already in a position
                            if token not in self.agent.portfolio.positions:
                                qualified.append((token, analysis))
                        else:
                            pass  # HOLD — passed hard filters but soft score low; keep counter

                    # Execute — highest probability first
                    qualified.sort(key=lambda x: x[1].get("prob_score", 0), reverse=True)
                    for token, analysis in qualified:
                        if self._can_enter(token, analysis):
                            self._execute_buy(token, analysis)

                # Portfolio summary
                perf = self.agent.portfolio.get_performance()
                print(f"\n[portfolio]")
                print(f"  USDC:         ${self.agent.portfolio.usdc_balance:.2f}")
                print(f"  Total:        ${perf['current_value']:.2f}")
                print(f"  Return:       ${perf['total_return']:+.2f} ({perf['total_return_pct']:+.2f}%)")
                print(f"  HWM:          ${self.tracker.hwm:.2f}  DD: {dd:.1f}%")
                print(f"  Daily P&L:    {-daily_loss:+.2f}%  "
                      f"({'⛔ HALTED' if halted else f'limit {DAILY_LOSS_LIMIT_PCT}%'})")
                print(f"  Sizer ×:      {self.sizer.multiplier:.2f}  "
                      f"wins: {self.sizer.consecutive_wins}/{WIN_STREAK_REQ}")
                print(f"  Trades today: {self.trades_today}/{self.max_trades_per_day}")
                print(f"  Regime:       {self._current_score:.0f}/100")
                print(f"  Watching:     {len(self.agent.watched_tokens)} meme tokens")

                if self.agent.portfolio.positions:
                    print(f"\n  Open positions:")
                    for tok, pos in self.agent.portfolio.positions.items():
                        peak    = self.position_peaks.get(tok, 0)
                        atr_e   = self.position_atr.get(tok, 0.05)
                        stop_l  = -min(MemeSignalEngine.STOP_ATR_MULT * atr_e, MemeSignalEngine.MAX_STOP_PCT) * 100
                        tp1_l   =  MemeSignalEngine.TP1_ATR_MULT  * atr_e * 100
                        entry   = self.position_entry.get(tok, pos.avg_price_usdc)
                        tp_flag = ("✓TP2" if self.tp2_hit.get(tok) else
                                   "✓TP1" if self.tp1_hit.get(tok) else "    ")
                        boosted_t = "🚀" if self.token_metadata.get(tok, {}).get("is_boosted") else "  "
                        print(f"    {boosted_t}{tok:<12}  "
                              f"P&L:{pos.pnl_percentage:+.2f}%  "
                              f"Peak:{peak:+.2f}%  "
                              f"{tp_flag}  "
                              f"Entry:${entry:.8f}  "
                              f"Stop:{stop_l:.1f}%  TP1:+{tp1_l:.1f}%  "
                              f"ATR:{atr_e*100:.1f}%")

                if self.sl_blacklist:
                    print(f"\n  🚫 Blacklisted: {', '.join(self.sl_blacklist)}")

                self._print_stats()

                nxt = max(0, int(self.refresh_interval - (time.time() - self.last_refresh)))
                print(f"\n[wait] {update_interval}s  |  Discovery in {nxt//60}m{nxt%60}s")
                time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\n[stop] Shutting down...")
            self.running = False
            print("\n" + "=" * 70)
            print("  🏁 MEME SESSION COMPLETE")
            print("=" * 70)
            self._print_stats()
            self.agent.show_portfolio()
            print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Solana Meme Coin Trader v1.6")
    parser.add_argument("--mode",             choices=["paper", "live"], default="paper")
    parser.add_argument("--wallet",           default="")
    parser.add_argument("--balance",          type=float, default=1000.0)
    parser.add_argument("--interval",         type=int,   default=15)   # 15s cycles
    parser.add_argument("--refresh-interval", type=int,   default=60)   # 60s discovery
    parser.add_argument("--no-auto-discover", action="store_true")
    parser.add_argument("--manual-tokens",    nargs="+")

    args = parser.parse_args()
    mode = TradingMode.LIVE if args.mode == "live" else TradingMode.PAPER

    wallet_instance = None
    if mode == TradingMode.LIVE:
        try:
            from wallet_integration import load_wallet_config, SolanaWallet
            cfg = load_wallet_config()
            if not cfg:
                print("[ERROR] No wallet config. Run: python setup_wallet.py --setup")
                return
            wallet_instance = SolanaWallet(cfg)
            sol_bal = wallet_instance.get_balance()
            if sol_bal < 0.01:
                if input(f"Low SOL ({sol_bal:.4f}). Continue? (yes/no): ").lower() not in ("yes", "y"):
                    return
            print("\n⚠  MEME COIN TRADING IS EXTREMELY HIGH RISK")
            print("   Rug pulls, dumps, and 100% losses can and do happen.")
            print("   Only trade what you can afford to lose entirely.\n")
            if input("Type 'MEME' to confirm live meme trading: ") != "MEME":
                print("Cancelled.")
                return
        except ImportError:
            print("[ERROR] pip install solana solders base58")
            return
        except Exception as e:
            print(f"[ERROR] {e}")
            return

    agent = SolanaTradingAgent(mode, args.wallet, args.balance, wallet=wallet_instance)

    from market_condition_analyzer import MarketConditionAnalyzer
    agent.market_analyzer = MarketConditionAnalyzer()
    print("[market] MarketConditionAnalyzer v9 attached.")

    auto_discover = not args.no_auto_discover
    if not auto_discover and args.manual_tokens:
        for tok in args.manual_tokens:
            agent.add_token(tok.upper())

    trader = MemeCoinTrader(agent, auto_discover=auto_discover)
    trader.refresh_interval = args.refresh_interval

    if auto_discover:
        print("\n[discover] Initial meme token scan...")
        trader.refresh_tokens()

    print("\n[market] Warming up (15 ticks)...")
    for i in range(15):
        agent.update_market_data()
        if (i + 1) % 5 == 0:
            print(f"  {i+1}/15")
        time.sleep(1)
    print("[OK] Ready\n")

    trader.run(update_interval=args.interval)


if __name__ == "__main__":
    main()
