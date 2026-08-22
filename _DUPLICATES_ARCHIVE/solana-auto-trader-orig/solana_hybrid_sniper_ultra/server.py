"""
server.py -- FastAPI monitoring dashboard. Flat layout.

Run alongside bot.py:
    uvicorn server:app --host 0.0.0.0 --port 8000
"""

import os
import csv
import sys
import time

from fastapi import FastAPI

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

app = FastAPI(title="Solana Hybrid Sniper Ultra", version="1.0.0")

# Import shared state from bot (when running in same process or importable)
try:
    from bot import open_positions, predictions_cache, mode_history
except ImportError:
    open_positions    = {}
    predictions_cache = {}
    mode_history      = []

PAPER_TRADES_PATH = os.path.join(_ROOT, "paper_trades.csv")


@app.get("/")
async def root():
    return {"status": "ok", "time": time.time()}


@app.get("/status")
async def status():
    total_pnl, trade_count = 0.0, 0
    if os.path.exists(PAPER_TRADES_PATH):
        with open(PAPER_TRADES_PATH) as f:
            rows = list(csv.DictReader(f))
            trade_count = len(rows)
            total_pnl = sum(float(r.get("pnl", 0)) for r in rows)
    return {
        "open_positions":  len(open_positions),
        "total_pnl_sol":   round(total_pnl, 6),
        "total_trades":    trade_count,
        "tokens_tracked":  len(predictions_cache),
    }


@app.get("/mode")
async def get_mode():
    recent  = mode_history[-20:] if mode_history else []
    current = recent[-1] if recent else {"mode": "NONE", "reason": "no data"}
    return {
        "current_mode":   current.get("mode"),
        "reason":         current.get("reason"),
        "recent_history": list(reversed(recent)),
    }


@app.get("/positions")
async def get_positions():
    return {
        "open_count": len(open_positions),
        "positions":  [{"token": k, **v} for k, v in open_positions.items()],
    }


@app.get("/predictions")
async def get_predictions():
    out = [
        {
            "token":     token,
            "pump_prob": round(pred.get("pump_prob", 0), 4),
            "dump_risk": round(pred.get("dump_risk", 0), 4),
            "age_s":     int(pred.get("features", {}).get("token_age_seconds", -1)),
            "ts":        pred.get("ts"),
        }
        for token, pred in predictions_cache.items()
    ]
    out.sort(key=lambda x: x["pump_prob"], reverse=True)
    return {"count": len(out), "predictions": out[:50]}


@app.get("/signals")
async def get_signals():
    from strategy import evaluate
    results = []
    for token, pred in predictions_cache.items():
        features  = pred.get("features", {})
        pump_prob = pred.get("pump_prob", 0.5)
        dump_risk = pred.get("dump_risk", 0.5)
        decision  = evaluate(features, pump_prob, dump_risk)
        results.append({
            "token":         token,
            "mode":          decision.mode.value,
            "enter":         decision.enter,
            "exit":          decision.exit,
            "reason":        decision.reason,
            "pump_prob":     round(pump_prob, 4),
            "dump_risk":     round(dump_risk, 4),
            "age_s":         int(features.get("token_age_seconds", -1)),
            "liquidity_usd": features.get("liquidity_usd", 0),
        })
    results.sort(key=lambda x: (x["mode"] == "SNIPER", x["pump_prob"]), reverse=True)
    return {"count": len(results), "signals": results[:100]}


@app.get("/trades")
async def get_trades():
    if not os.path.exists(PAPER_TRADES_PATH):
        return {"count": 0, "trades": []}
    with open(PAPER_TRADES_PATH) as f:
        rows = list(csv.DictReader(f))
    return {"count": len(rows), "trades": list(reversed(rows[-100:]))}


@app.get("/pnl")
async def get_pnl():
    if not os.path.exists(PAPER_TRADES_PATH):
        return {"total_pnl": 0, "breakdown": {}}
    with open(PAPER_TRADES_PATH) as f:
        rows = list(csv.DictReader(f))
    by_mode = {}
    for row in rows:
        mode = row.get("mode", "UNKNOWN")
        by_mode[mode] = by_mode.get(mode, 0) + float(row.get("pnl", 0))
    return {
        "total_pnl":   round(sum(by_mode.values()), 6),
        "by_mode":     {k: round(v, 6) for k, v in by_mode.items()},
        "trade_count": len(rows),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
