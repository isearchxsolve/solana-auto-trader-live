"""
labeler.py -- Real trade outcome labeler for model retraining.

Records every token the bot evaluates at evaluation time. After
OUTCOME_DELAY_S seconds, fetches the current price via DexScreener and
computes the actual outcome. Saves results to labeled_outcomes.csv which
can be fed directly to train.py --data to retrain on real market data.

Label thresholds (configurable via .env):
  PUMP_THRESHOLD: final_price >= entry * PUMP_THRESHOLD  → pump_label=1
  DUMP_THRESHOLD: final_price <= entry * DUMP_THRESHOLD  → dump_label=1
"""

import asyncio
import csv
import logging
import os
import time
from typing import Dict

logger = logging.getLogger(__name__)

# Config (can override via .env)
OUTCOME_DELAY_S = int(os.getenv("LABELER_DELAY_S",  str(30 * 60)))  # 30 min default
PUMP_THRESHOLD  = float(os.getenv("LABELER_PUMP_THR", "1.20"))       # +20% = pump
DUMP_THRESHOLD  = float(os.getenv("LABELER_DUMP_THR", "0.85"))       # -15% = dump
OUTCOMES_PATH   = os.getenv("LABELER_OUTPUT",       "labeled_outcomes.csv")

# In-memory queue: token → {eval_time, entry_price, features}
_pending: Dict[str, dict] = {}

_BASE_FIELDNAMES = [
    "token", "eval_time", "outcome_time",
    "entry_price", "final_price",
    "pct_change", "pump_label", "dump_label",
]


def record_evaluation(token_address: str, features: dict, current_price: float) -> None:
    """
    Call once per token per evaluation cycle. Idempotent -- if the token is
    already pending, we update the price (more recent is better) but don't
    reset the clock.
    """
    if current_price <= 0:
        return
    if token_address not in _pending:
        _pending[token_address] = {
            "eval_time":   time.time(),
            "entry_price": current_price,
            "features":    features.copy(),
        }
    else:
        # Refresh price so the 'entry' price reflects a fresh observation
        _pending[token_address]["entry_price"] = current_price
        _pending[token_address]["features"]    = features.copy()


def _ensure_csv(feature_cols) -> None:
    if not os.path.exists(OUTCOMES_PATH):
        with open(OUTCOMES_PATH, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_BASE_FIELDNAMES + feature_cols)
            w.writeheader()
        logger.info(f"[labeler] Created {OUTCOMES_PATH}")


async def _fetch_price(session, token_address: str) -> float:
    import aiohttp
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return 0.0
            data = await resp.json()
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            return 0.0
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0)))
        return float(best.get("priceUsd") or 0)
    except Exception:
        return 0.0


async def outcome_loop(session) -> None:
    """
    Background task. Every 60 s, checks for matured evaluations and records
    their outcomes. Run as an asyncio task alongside the main bot loop.
    """
    from features import FEATURE_COLUMNS
    _ensure_csv(FEATURE_COLUMNS)
    all_fields = _BASE_FIELDNAMES + FEATURE_COLUMNS

    logger.info(
        f"[labeler] Started | delay={OUTCOME_DELAY_S//60}min "
        f"| pump>={PUMP_THRESHOLD:.0%} dump<={DUMP_THRESHOLD:.0%} "
        f"| output={OUTCOMES_PATH}"
    )

    while True:
        await asyncio.sleep(60)
        now  = time.time()
        done = [t for t, v in list(_pending.items())
                if now - v["eval_time"] >= OUTCOME_DELAY_S]

        for token in done:
            rec = _pending.pop(token, None)
            if rec is None:
                continue
            try:
                final_price = await _fetch_price(session, token)
                entry_price = rec["entry_price"]

                if final_price <= 0 or entry_price <= 0:
                    logger.debug(f"[labeler] {token[:8]}.. no price data -- skipping")
                    continue

                pct       = (final_price - entry_price) / entry_price
                pump_lbl  = int(final_price >= entry_price * PUMP_THRESHOLD)
                dump_lbl  = int(final_price <= entry_price * DUMP_THRESHOLD)

                row: dict = {
                    "token":        token,
                    "eval_time":    round(rec["eval_time"], 2),
                    "outcome_time": round(now, 2),
                    "entry_price":  entry_price,
                    "final_price":  final_price,
                    "pct_change":   round(pct, 6),
                    "pump_label":   pump_lbl,
                    "dump_label":   dump_lbl,
                }
                for col in FEATURE_COLUMNS:
                    row[col] = rec["features"].get(col, 0.0)

                with open(OUTCOMES_PATH, "a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=all_fields)
                    w.writerow(row)

                logger.info(
                    f"[labeler] {token[:8]}.. outcome={pct:+.1%} "
                    f"pump={pump_lbl} dump={dump_lbl} "
                    f"entry={entry_price:.8f} final={final_price:.8f}"
                )
            except Exception as exc:
                logger.warning(f"[labeler] Error resolving {token[:8]}: {exc}")

    # unreachable -- task runs forever
