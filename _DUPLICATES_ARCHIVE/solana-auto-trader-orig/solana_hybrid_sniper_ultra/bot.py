"""
bot.py -- Main async bot engine. Flat layout, no sub-packages.

Live-trading additions vs original:
  • Take-profit at TAKE_PROFIT_MULT x entry (default 2x), then trailing stop
  • Daily loss circuit breaker (DAILY_LOSS_LIMIT, default 15%)
  • Dynamic position sizing scaled by pump_prob and volatility
  • Real outcome labeler writes labeled_outcomes.csv for model retraining
  • SNIPER lp_locked contradiction fixed (now uses liquidity_usd > 500)
"""

import os, sys, asyncio, logging, time, datetime, datetime as _dt
from datetime import timezone as _tz

import aiohttp
from dotenv import load_dotenv
from typing import Dict, List, Optional, Tuple, Any

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
load_dotenv(os.path.join(_ROOT, ".env"))

# Auto-train if model.pkl is missing
_MODEL_PATH = os.path.join(_ROOT, "model.pkl")
if not os.path.exists(_MODEL_PATH):
    print("[bot] model.pkl not found -- training now...")
    from train import train; train()
    print("[bot] Training complete.\n")

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
import sys as _sys
_stdout_handler = logging.StreamHandler(
    stream=open(_sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    if hasattr(_sys.stdout, 'fileno') else _sys.stdout
)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        _stdout_handler,
        logging.FileHandler(os.path.join("logs", "bot.log"), mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")

# ── Config ────────────────────────────────────────────────────────────────────
MODE             = os.getenv("MODE", "paper")
PRIVATE_KEY      = os.getenv("PRIVATE_KEY", "")
RPC_URL          = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY", "")
TRADE_AMOUNT_SOL = float(os.getenv("TRADE_AMOUNT_SOL", "0.05"))
MAX_POSITIONS    = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
# Minimum seconds to wait between scans. In practice each scan takes 75-150s
# (GeckoTerminal rate limit: 15s/call × 5 tokens = 75s steady-state, 150s cold start).
# Set to 0 so the bot starts the next scan immediately when the current one finishes
# rather than adding an additional artificial wait on top of the already-slow API calls.
LOOP_INTERVAL    = int(os.getenv("LOOP_INTERVAL", "60"))
SNIPER_SLIPPAGE  = 500

# Take-profit / trailing stop
TAKE_PROFIT_MULT  = float(os.getenv("TAKE_PROFIT_MULT",  "2.0"))   # 2x entry = TP
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.20"))  # 20% below peak

# Circuit breaker
DAILY_LOSS_LIMIT  = float(os.getenv("DAILY_LOSS_LIMIT",  "0.15"))  # 15% of day-start

# Labeler
ENABLE_LABELER    = os.getenv("ENABLE_LABELER", "true").lower() == "true"

# ── Shared state ──────────────────────────────────────────────────────────────
open_positions:    Dict[str, dict] = {}
predictions_cache: Dict[str, dict] = {}
mode_history:      List[dict]      = []

circuit_breaker: dict = {
    "active":            False,
    "day_start_balance": None,
    "day_start_ts":      None,
    "triggered_at":      None,
}

# ── Position persistence ───────────────────────────────────────────────────────
import json as _json

POSITIONS_FILE = os.path.join(_ROOT, "open_positions.json")


def _save_positions(paper_exec=None) -> None:
    """Persist open_positions (and paper balance) to disk so restarts don't orphan open trades."""
    try:
        data = dict(open_positions)
        if paper_exec is not None:
            data["__balance__"] = paper_exec.balance
        with open(POSITIONS_FILE, "w") as f:
            _json.dump(data, f, indent=2)
    except Exception as exc:
        logger.warning(f"[persist] Failed to save positions: {exc}")


def _load_positions() -> None:
    """Restore open_positions from disk on startup."""
    if not os.path.exists(POSITIONS_FILE):
        return
    try:
        with open(POSITIONS_FILE) as f:
            data = _json.load(f)
        if isinstance(data, dict):
            # Strip internal sentinel keys before populating open_positions
            positions = {k: v for k, v in data.items() if not k.startswith("__")}
            if positions:
                open_positions.update(positions)
                logger.info(
                    f"[persist] Restored {len(positions)} open position(s) from disk: "
                    + ", ".join(f"{k[:8]}.." for k in positions)
                )
    except Exception as exc:
        logger.warning(f"[persist] Failed to load positions: {exc}")


# ── Circuit breaker ───────────────────────────────────────────────────────────

def _update_circuit_breaker(current_balance: float) -> None:
    cb = circuit_breaker
    today_start_ts = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()

    if cb["day_start_ts"] is None:
        cb["day_start_balance"] = current_balance
        cb["day_start_ts"]      = time.time()
        logger.info(f"[CircuitBreaker] Day-start balance = {current_balance:.4f} SOL")
        return

    if cb["day_start_ts"] < today_start_ts:
        logger.info(f"[CircuitBreaker] UTC day reset | balance={current_balance:.4f} SOL")
        cb.update(active=False, day_start_balance=current_balance,
                  day_start_ts=time.time(), triggered_at=None)
        return

    start_bal = cb["day_start_balance"] or current_balance
    loss_pct  = (start_bal - current_balance) / (start_bal + 1e-9)
    if loss_pct >= DAILY_LOSS_LIMIT:
        if not cb["active"]:
            cb["active"] = True; cb["triggered_at"] = time.time()
            logger.warning(
                f"[CircuitBreaker] *** ACTIVATED *** "
                f"loss {loss_pct:.1%} >= {DAILY_LOSS_LIMIT:.0%} | "
                f"start={start_bal:.4f} now={current_balance:.4f} | "
                f"No new entries until UTC midnight."
            )
    else:
        if cb["active"]:
            logger.info("[CircuitBreaker] Deactivated")
        cb["active"] = False


# ── Dynamic position sizing ───────────────────────────────────────────────────

def _position_size(pump_prob: float, features: dict, pyramid: bool = False) -> float:
    """Scale 25-100% of TRADE_AMOUNT_SOL by ML confidence and volatility."""
    conf_mult = 0.5 + pump_prob * 0.5          # 0.5-1.0
    vol       = features.get("volatility", 0.02)
    vol_mult  = 0.60 if vol > 0.05 else (0.80 if vol > 0.03 else 1.00)
    size      = TRADE_AMOUNT_SOL * conf_mult * vol_mult
    if pyramid:
        size *= 0.5
    return float(max(TRADE_AMOUNT_SOL * 0.25, min(TRADE_AMOUNT_SOL, size)))


# ── Take-profit & trailing stop ───────────────────────────────────────────────

async def _handle_take_profit(token_address, pos_info, current_price, paper_exec, live_exec) -> bool:
    """
    Update peak price, fire take-profit at TAKE_PROFIT_MULT x entry (sell 50%),
    then trail remaining 50% with TRAILING_STOP_PCT below peak.
    Returns True if position was fully closed.
    """
    avg_ep        = pos_info.get("avg_entry_price", 0.0)
    tp_taken      = pos_info.get("take_profit_taken", False)
    peak_price    = pos_info.get("peak_price", current_price)
    trailing_stop = pos_info.get("trailing_stop", 0.0)

    if avg_ep <= 0:
        return False

    # Update peak / ratchet trailing stop
    if current_price > peak_price:
        pos_info["peak_price"] = current_price
        peak_price = current_price
        if tp_taken:
            new_stop = peak_price * (1 - TRAILING_STOP_PCT)
            if new_stop > trailing_stop:
                pos_info["trailing_stop"] = new_stop

    # Trailing stop hit (only after TP)
    if tp_taken and trailing_stop > 0 and current_price < trailing_stop:
        held_s = time.time() - pos_info.get("entry_time", time.time())
        pct    = (current_price - avg_ep) / avg_ep * 100
        reason = f"Trailing stop | price={current_price:.8f} < stop={trailing_stop:.8f}"
        logger.info(f"[{token_address[:8]}..] {reason}")
        if MODE == "paper":
            pnl = paper_exec.sell(token_address, current_price, note=reason)
            logger.info(f"[{token_address[:8]}..] CLOSED (trail) | PnL={pnl:+.4f} SOL ({pct:+.1f}%) | held={held_s/60:.1f}m")
        elif MODE == "live" and live_exec:
            half = TRADE_AMOUNT_SOL * pos_info.get("entries", 1) * 0.5
            sig  = await live_exec.sell(token_address, half)
            logger.info(f"[{token_address[:8]}..] Live trail stop sig={sig}")
        open_positions.pop(token_address, None)
        _save_positions()
        return True

    # Take-profit trigger
    if not tp_taken and avg_ep > 0:
        gain = (current_price - avg_ep) / avg_ep
        if gain >= (TAKE_PROFIT_MULT - 1.0):
            logger.info(f"[{token_address[:8]}..] TAKE PROFIT | gain={gain:.1%} | selling 50%, trailing rest")
            if MODE == "paper":
                pnl = paper_exec.sell_partial(token_address, 0.5, current_price, note="Take profit 50%")
                logger.info(f"[{token_address[:8]}..] Partial PnL={pnl:+.4f} SOL")
            elif MODE == "live" and live_exec:
                half = TRADE_AMOUNT_SOL * pos_info.get("entries", 1) * 0.5
                sig  = await live_exec.sell(token_address, half)
                logger.info(f"[{token_address[:8]}..] Live TP sig={sig}")
            pos_info["take_profit_taken"] = True
            pos_info["trailing_stop"]     = peak_price * (1 - TRAILING_STOP_PCT)

    return False


# ── Token processor ───────────────────────────────────────────────────────────

async def process_token(token_address, dex, gecko, paper_exec, live_exec,
                        dex_pair=None, use_gecko=True, session=None):
    from data_fetcher import fetch_token_info
    from features     import compute_sniper_features, build_feature_vector
    from predict      import predict
    from strategy     import evaluate, PositionState, TradingMode

    try:
        # OHLCV waterfall: gecko (top N only) → pump.fun → Birdeye.
        # use_gecko=False tokens skip the slow gecko OHLCV call (~15s) and go
        # directly to pump.fun/Birdeye, keeping bulk scans fast.
        info = await fetch_token_info(
            token_address, dex, gecko,
            dex_pair=dex_pair,
            session=session,
            use_gecko_ohlcv=use_gecko,
        )

        if "error" in info:
            return None
        ohlcv = info["ohlcv"]
        if ohlcv.empty or len(ohlcv) < 3:
            return None

        features = compute_sniper_features(
            df=ohlcv,
            token_age_seconds=info.get("token_age_seconds", 9999),
            active_traders=info.get("active_traders", 0),
            liquidity_usd=info.get("liquidity_usd", 0),
            lp_locked=info.get("lp_locked", False),
        )
        fvec = build_feature_vector(features)
        pump_prob, dump_risk = predict(fvec)

        ohlcv_source = info.get("ohlcv_source", "unknown")

        # FIX #1: Pre-compute effective probs so we can log them.
        # The strategy blends raw ML with rule-based scores (max of the two) before
        # making decisions, but previously only raw ML values were logged -- making it
        # look like the model was broken (pump=0.00) even when rule-based scoring was
        # successfully driving entries.  Now both are logged for transparency.
        from strategy import effective_probs as _eff_probs
        eff_pump, eff_dump = _eff_probs(features, pump_prob, dump_risk)

        predictions_cache[token_address] = {
            "pump_prob": pump_prob, "dump_risk": dump_risk,
            "features":  features,  "ts":        time.time(),
            "ohlcv_source": ohlcv_source,
        }

        current_price = float(ohlcv["close"].iloc[-1])

        # Feed labeler (non-blocking)
        if ENABLE_LABELER:
            try:
                from labeler import record_evaluation
                record_evaluation(token_address, features, current_price)
            except Exception:
                pass

        # Take-profit / trailing stop -- runs before signal evaluation
        pos_info = open_positions.get(token_address)
        if pos_info:
            closed = await _handle_take_profit(
                token_address, pos_info, current_price, paper_exec, live_exec
            )
            if closed:
                return

        # Build PositionState
        pos_data  = open_positions.get(token_address)
        pos_state = None
        if pos_data:
            from strategy import PositionState, TradingMode
            pos_state = PositionState(
                token=token_address,
                mode=TradingMode(pos_data["mode"]),
                entries=pos_data["entries"],
                entry_prices=pos_data.get("entry_prices", []),
                entry_time=pos_data.get("entry_time", 0.0),
                avg_entry_price=pos_data.get("avg_entry_price", 0.0),
            )

        decision = evaluate(features, pump_prob, dump_risk, pos_state, current_price)

        mode_history.append({"ts": time.time(), "token": token_address,
                              "mode": decision.mode.value, "reason": decision.reason})
        if len(mode_history) > 500:
            mode_history.pop(0)

        logger.info(
            f"[{token_address[:8]}..] mode={decision.mode.value:<7} "
            f"pump={pump_prob:.2f}(eff={eff_pump:.2f}) dump={dump_risk:.2f}(eff={eff_dump:.2f}) | "
            f"{decision.reason} | "
            f"rsi={features.get('rsi', 0):.0f} "
            f"mom1m={features.get('momentum_1m', 0):+.3f} "
            f"vspike={features.get('vol_spike', 0):.1f} "
            f"pve5={features.get('price_vs_ema5', features.get('price_gt_ema5', 0)):+.3f} "
            f"src={ohlcv_source}"
        )

        # ── ENTRY ─────────────────────────────────────────────────────────────
        if decision.enter and len(open_positions) < MAX_POSITIONS:
            # SAFETY: never open new positions on synthetic DexScreener OHLCV in live mode.
            # In paper mode we allow dexscreener entries to collect trade execution data,
            # but we tag them so we can analyse gecko-only vs dexscreener P&L separately.
            # Features derived from synthetic data are unreliable (random noise,
            # vol_spike artificially capped). Only trade on real GeckoTerminal candles in live.
            # All three OHLCV sources (gecko, pumpfun, birdeye) are real candles -- allow entry.
            # "none" / "unknown" means no real data was available -- always block.
            # SAFETY: only open new positions on real OHLCV sources.
            # "dex_synth" is estimated from DexScreener price summaries -- accurate enough
            # to monitor open positions and feed the labeler, but not reliable enough
            # to justify a new entry.  "none" / "unknown" = no data at all -- always block.
            _REAL_SOURCES       = {"gecko", "dex_chart", "pumpfun", "birdeye"}
            _MONITOR_SOURCES    = {"dex_synth"}   # allowed for hold/exit, blocked for new entry
            _is_real_data       = ohlcv_source in _REAL_SOURCES
            _is_monitor_data    = ohlcv_source in _MONITOR_SOURCES
            if not _is_real_data and not _is_monitor_data:
                logger.info(
                    f"[{token_address[:8]}..] Entry BLOCKED -- no OHLCV data "
                    f"(src={ohlcv_source})."
                )
            elif _is_monitor_data:
                # dex_synth = synthesised from DexScreener price snapshots only.
                # vol_spike is always 0.5 (no real volume data), making entry signals
                # meaningless noise.  Block all new entries; allow monitoring of
                # existing positions so stop-losses and exits still fire correctly.
                logger.info(
                    f"[{token_address[:8]}..] Entry BLOCKED -- synthetic OHLCV only "
                    f"(src=dex_synth). Monitoring open positions only."
                )
            elif circuit_breaker["active"]:
                logger.warning(f"[{token_address[:8]}..] Entry BLOCKED -- circuit breaker active")
            else:
                if ohlcv_source != "gecko":
                    logger.info(
                        f"[{token_address[:8]}..] Entry on {ohlcv_source} candles."
                    )
                from strategy import TradingMode as TM
                slip   = SNIPER_SLIPPAGE if decision.mode == TM.SNIPER else 300
                amount = _position_size(pump_prob, features, pyramid=decision.pyramid_allowed)

                if MODE == "paper":
                    _src_tag = ohlcv_source if ohlcv_source != "gecko" else None
                    _note    = f"[{_src_tag}] {decision.reason}" if _src_tag else decision.reason
                    success = paper_exec.buy(
                        token=token_address, mode=decision.mode.value,
                        price=current_price, amount_sol=amount,
                        slippage=slip / 10_000, note=_note,
                    )
                    if success:
                        now_ts = time.time()
                        if token_address not in open_positions:
                            open_positions[token_address] = {
                                "mode":              decision.mode.value,
                                "entries":           1,
                                "entry_prices":      [current_price],
                                "entry_time":        now_ts,
                                "avg_entry_price":   current_price,
                                "peak_price":        current_price,
                                "take_profit_taken": False,
                                "trailing_stop":     0.0,
                            }
                        else:
                            op = open_positions[token_address]
                            op["entries"] += 1
                            op["entry_prices"].append(current_price)
                            op["avg_entry_price"] = sum(op["entry_prices"]) / len(op["entry_prices"])
                        _save_positions()

                elif MODE == "live" and live_exec:
                    sig = await live_exec.buy(token_address, amount, slip)
                    if sig:
                        open_positions[token_address] = {
                            "mode":              decision.mode.value,
                            "entries":           1,
                            "entry_prices":      [current_price],
                            "last_sig":          sig,
                            "entry_time":        time.time(),
                            "avg_entry_price":   current_price,
                            "peak_price":        current_price,
                            "take_profit_taken": False,
                            "trailing_stop":     0.0,
                        }
                        _save_positions()

        # ── EXIT (stop-loss / time / dump-risk via strategy.evaluate) ─────────
        elif decision.exit and token_address in open_positions:
            pos_info = open_positions[token_address]
            if MODE == "paper":
                pnl    = paper_exec.sell(token_address, current_price, note=decision.reason)
                held_s = time.time() - pos_info.get("entry_time", time.time())
                avg_ep = pos_info.get("avg_entry_price", current_price)
                pct    = (current_price - avg_ep) / (avg_ep + 1e-12) * 100
                logger.info(
                    f"[{token_address[:8]}..] CLOSED | "
                    f"PnL={pnl:+.4f} SOL ({pct:+.1f}%) | "
                    f"held={held_s/60:.1f}m | reason={decision.reason}"
                )
            elif MODE == "live" and live_exec:
                entries  = pos_info.get("entries", 1)
                fraction = 0.5 if pos_info.get("take_profit_taken") else 1.0
                sig = await live_exec.sell(token_address, TRADE_AMOUNT_SOL * entries * fraction)
                logger.info(f"[{token_address[:8]}..] Live exit sig={sig}")
            open_positions.pop(token_address, None)
            _save_positions()

    except Exception as exc:
        logger.error(f"[process_token] {token_address}: {exc}", exc_info=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

async def main_loop():
    connector = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:

        from data_fetcher   import DexScreenerClient, GeckoTerminalClient, get_candidate_tokens
        from executor_paper import PaperExecutor

        dex        = DexScreenerClient(session)
        gecko      = GeckoTerminalClient(session)
        paper_exec = PaperExecutor(
            initial_balance_sol=float(os.getenv("PAPER_BALANCE_SOL", "1.0")),
            trade_amount_sol=TRADE_AMOUNT_SOL,
        )

        live_exec = None
        if MODE == "live":
            if not PRIVATE_KEY:
                logger.error("MODE=live but PRIVATE_KEY is not set in .env. Aborting.")
                return
            from solana_client import load_keypair, SolanaRpcClient
            from helius        import HeliusClient
            from swap_executor import SwapExecutor
            keypair   = load_keypair(PRIVATE_KEY)
            rpc       = SolanaRpcClient(RPC_URL, session)
            helius    = HeliusClient(HELIUS_API_KEY, session)
            live_exec = SwapExecutor(keypair, rpc, helius, session)
            logger.info(f"[bot] LIVE MODE | wallet={keypair.pubkey()}")
        else:
            logger.info("[bot] PAPER MODE -- no real funds at risk")

        # Restore open positions from previous session
        _load_positions()
        # Restore paper balance if state file exists
        if MODE == "paper" and os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as _f:
                    _state = _json.load(_f)
                _saved_bal = _state.get("__balance__")
                if _saved_bal and isinstance(_saved_bal, float):
                    paper_exec.balance = _saved_bal
                    logger.info(f"[persist] Restored paper balance: {_saved_bal:.4f} SOL")
            except Exception:
                pass

        if ENABLE_LABELER:
            import labeler as _labeler
            asyncio.create_task(_labeler.outcome_loop(session), name="labeler")
            logger.info("[bot] Outcome labeler task started")

        iteration = 0
        while True:
            iteration += 1
            start = time.time()
            logger.info(f"\n{'='*60}")
            logger.info(f"[bot] Scan #{iteration} | open_positions={len(open_positions)}")
            if circuit_breaker["active"]:
                logger.warning("[bot] Circuit breaker ACTIVE -- no new entries this scan")

            try:
                all_addresses, gecko_pairs, all_candidate_pairs = await get_candidate_tokens(
                    dex, max_trending=15, max_new=25
                )
                # FIX #4: Build all_pairs from the full pairs_by_addr dict so every
                # non-gecko token still carries its real dex_pair for dex_chart lookups.
                # Previously non-gecko slots were filled with {}, which evaluated falsy
                # via `{} or None` and silently dropped dex_chart OHLCV for ~90% of tokens.
                from data_fetcher import _pool_cache as _pc
                all_pairs = dict(all_candidate_pairs)   # full pair data for all candidates
                for addr in all_addresses:
                    if addr not in all_pairs:
                        all_pairs[addr] = {}

                # Ensure open positions are always evaluated
                for addr in list(open_positions.keys()):
                    if addr not in all_pairs:
                        cached = _pc.get(addr)
                        all_pairs[addr] = cached[0] if isinstance(cached, tuple) else (cached or {})

                # Top GECKO_CANDIDATES tokens → GeckoTerminal OHLCV (rate-limited, best quality)
                # Remaining tokens     → DexScreener chart OHLCV (real candles, instant, no limit)
                dex_only = [a for a in all_pairs if a not in gecko_pairs]

                logger.info(
                    f"[bot] Evaluating {len(all_pairs)} tokens "
                    f"({len(gecko_pairs)} gecko, {len(dex_only)} dex_chart/pumpfun)"
                )

                # gecko tokens: sequential (rate-limited)
                for addr in gecko_pairs:
                    await process_token(addr, dex, gecko, paper_exec, live_exec,
                                        dex_pair=all_pairs.get(addr) or None,
                                        use_gecko=True, session=session)

                # remaining tokens: parallel (no rate limit on dex_chart)
                dex_results = []
                for i in range(0, len(dex_only), 10):
                    batch = dex_only[i:i + 10]
                    results = await asyncio.gather(
                        *[process_token(a, dex, gecko, paper_exec, live_exec,
                                        dex_pair=all_pairs.get(a) or None,
                                        use_gecko=False, session=session)
                          for a in batch],
                        return_exceptions=True,
                    )
                    dex_results.extend(results)
                dex_ok   = sum(1 for r in dex_results if r is not None and not isinstance(r, Exception))
                dex_fail = len(dex_results) - dex_ok
                if dex_fail > 0:
                    logger.info(f"[bot] dex_chart: {dex_ok} evaluated, {dex_fail} skipped (no OHLCV)")

                if MODE == "paper":
                    s = paper_exec.summary()
                    unrealized = 0.0
                    for addr, pi in open_positions.items():
                        cached = predictions_cache.get(addr, {})
                        last_p = cached.get("features", {}).get("last_close", 0.0)
                        avg_ep = pi.get("avg_entry_price", 0.0)
                        if avg_ep > 0 and last_p > 0:
                            cost        = TRADE_AMOUNT_SOL * pi["entries"]
                            tok_held    = cost / avg_ep
                            if pi.get("take_profit_taken"):
                                tok_held *= 0.5; cost *= 0.5
                            unrealized += tok_held * last_p - cost
                    _update_circuit_breaker(s["balance_sol"])
                    cb_str = " | CIRCUIT BREAKER ACTIVE" if circuit_breaker["active"] else ""
                    logger.info(
                        f"[bot] Balance={s['balance_sol']:.4f} SOL "
                        f"| Realized={s['total_pnl']:+.4f} "
                        f"| Unrealized~{unrealized:+.4f} "
                        f"| trades={s['total_trades']} "
                        f"| open={s['open_positions']}"
                        f"{cb_str}"
                    )
                    _save_positions(paper_exec)

            except Exception as exc:
                logger.error(f"[bot] Loop error: {exc}", exc_info=True)

            elapsed = time.time() - start
            wait    = max(0, LOOP_INTERVAL - elapsed)
            logger.info(f"[bot] Scan took {elapsed:.1f}s, next scan in {wait:.0f}s")
            await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(main_loop())
