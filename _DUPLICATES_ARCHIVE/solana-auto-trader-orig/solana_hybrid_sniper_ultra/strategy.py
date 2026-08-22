"""
strategy.py -- Hybrid mode selection and per-mode decision logic.
Mode priority: SNIPER > MG > HWR > NONE
No sub-package imports -- flat layout.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


import time as _time

class TradingMode(str, Enum):
    SNIPER = "SNIPER"
    MG     = "MG"
    HWR    = "HWR"
    NONE   = "NONE"


@dataclass
class ModeDecision:
    mode:            TradingMode
    reason:          str
    enter:           bool = False
    exit:            bool = False
    pyramid_allowed: bool = False
    entry_number:    int  = 1


@dataclass
class PositionState:
    token:          str
    mode:           TradingMode
    entries:        int   = 0
    entry_prices:   list  = field(default_factory=list)
    entry_time:     float = 0.0    # unix timestamp of first entry
    avg_entry_price: float = 0.0   # weighted average entry price


# ── Thresholds ───────────────────────────────────────────────────────────────
# Thresholds apply to `effective_pump_prob` / `effective_dump_risk` which are
# the MAX of the ML probability and a rule-based score computed from raw features.
# This means the bot can trade on strong technical setups even before the ML
# model has been retrained on real labeled data (labeled_outcomes.csv).
# Once the ML is calibrated on real data the ML probability will dominate.

SNIPER_AGE_MAX       = 120
SNIPER_LIQ_MAX       = 150_000
SNIPER_VOL_SPIKE_MIN = 2.0
SNIPER_PUMP_MIN      = 0.35

MG_PUMP_MIN          = 0.48    # raised from 0.42 -- filter weak signal entries
MG_VOL_RATIO_MIN     = 0.85   # raised from 0.6 -- require recent vol > baseline
MG_VOL_SPIKE_MIN     = 1.2    # NEW: last candle must show real buying activity
MG_MOM_MIN           = 0.005  # NEW: require meaningful positive momentum, not just > 0
MG_BUY_PRESSURE_MIN  = 0.52   # raised from 0.40 -- buyers must dominate
MG_RSI_LOW           = 38     # NEW: avoid already-dumped tokens
MG_RSI_HIGH          = 72     # NEW: avoid overbought entries
MG_DUMP_MAX          = 0.55   # tightened from 0.65
MG_MAX_ENTRIES       = 3

HWR_PUMP_MIN         = 0.55
HWR_DUMP_MAX         = 0.45
HWR_RSI_LOW          = 45
HWR_RSI_HIGH         = 75
HWR_VOLATILITY_MAX   = 0.05

# ── Stop-loss & max hold time ─────────────────────────────────────────────────
# Stop-loss: exit if price drops more than X% below average entry
# MG was 8% but trades were hitting -42% -- this means positions weren't being
# scanned frequently enough (circuit breaker or missing from candidate list).
# Tighter stop catches the move earlier on the next scan that does see it.
SNIPER_STOP_LOSS     = 0.05    # 5%   -- sniper entries are tight
MG_STOP_LOSS         = 0.05    # 5%   -- tightened from 8% (was hitting -42% in practice)
HWR_STOP_LOSS        = 0.05    # 5%

# Max hold time in seconds before forcing an exit regardless of signals
SNIPER_MAX_HOLD_S    = 5 * 60      #  5 minutes -- sniper is a scalp
MG_MAX_HOLD_S        = 30 * 60    # 30 minutes -- reduced from 45 (cut losers faster)
HWR_MAX_HOLD_S       = 60 * 60    # 60 minutes -- reduced from 90


# ── Rule-based scoring (ML-independent) ──────────────────────────────────────
# Supplements the XGBoost model until it's retrained on real labeled data.
# Once labeled_outcomes.csv has enough trades, retrain with:
#   python train.py --data labeled_outcomes.csv
# After retraining, ML probs will be higher and naturally dominate.

def rule_based_pump_score(features: Dict[str, float]) -> float:
    """Technical-indicator pump confidence, range 0.0–0.80."""
    score = 0.0

    # Momentum (strongest signal)
    mom1m = features.get("momentum_1m", 0.0)
    if   mom1m > 0.08:  score += 0.22
    elif mom1m > 0.03:  score += 0.14
    elif mom1m > 0.005: score += 0.06

    # Volume spike vs recent average
    vspike = features.get("vol_spike", 0.0)
    if   vspike > 5.0:  score += 0.22
    elif vspike > 2.5:  score += 0.16
    elif vspike > 1.5:  score += 0.08
    elif vspike > 1.0:  score += 0.03

    # RSI – reward healthy range, penalise extremes
    rsi = features.get("rsi", 50.0)
    if   40 <= rsi <= 65: score += 0.14
    elif 30 <= rsi <  40: score += 0.06
    elif 65 <  rsi <= 75: score += 0.06

    # Price vs EMAs (trend alignment)
    if features.get("price_gt_ema5",  0) == 1: score += 0.08
    if features.get("price_gt_ema10", 0) == 1: score += 0.06
    if features.get("ema10_gt_ema20", 0) == 1: score += 0.04

    # Volume ratio (recent vs baseline)
    vol_ratio = features.get("vol_ratio", 1.0)
    if   vol_ratio > 2.0: score += 0.08
    elif vol_ratio > 1.2: score += 0.04

    # Buy pressure
    bp = features.get("buy_pressure", 0.5)
    if   bp > 0.65: score += 0.08
    elif bp > 0.55: score += 0.04

    return min(score, 0.80)


def rule_based_dump_risk(features: Dict[str, float]) -> float:
    """Technical-indicator dump risk, range 0.0–0.80."""
    risk = 0.0

    mom1m = features.get("momentum_1m", 0.0)
    if   mom1m < -0.05: risk += 0.28
    elif mom1m < -0.01: risk += 0.14

    rsi = features.get("rsi", 50.0)
    if   rsi < 20: risk += 0.28
    elif rsi < 30: risk += 0.14

    if features.get("price_gt_ema5",  1) == 0: risk += 0.18
    if features.get("price_gt_ema10", 1) == 0: risk += 0.12

    bp = features.get("buy_pressure", 0.5)
    if bp < 0.30: risk += 0.14

    if features.get("vol_spike", 0) > 2.0 and mom1m < -0.01:
        risk += 0.18

    return min(risk, 0.80)


def effective_probs(
    features:  Dict[str, float],
    pump_prob: float,
    dump_risk: float,
) -> tuple:
    """
    Blend ML probabilities with rule-based scores.
    effective_pump = max(ML, rule) -- rules fill the gap until ML is retrained.
    effective_dump  = averaged when rule says safe but ML is jumpy.
    """
    rule_pump = rule_based_pump_score(features)
    rule_dump = rule_based_dump_risk(features)
    eff_pump  = max(pump_prob, rule_pump)
    if rule_dump < 0.25 and dump_risk > 0.50:
        eff_dump = (dump_risk + rule_dump) / 2.0
    else:
        eff_dump = max(dump_risk, rule_dump)
    return eff_pump, eff_dump


# ── Mode selector ────────────────────────────────────────────────────────────

def select_mode(features: Dict[str, float], pump_prob: float, dump_risk: float) -> TradingMode:
    # SNIPER
    if (
        features.get("token_age_seconds", 9999) < SNIPER_AGE_MAX
        and features.get("liquidity_usd", 9_999_999) < SNIPER_LIQ_MAX
        and features.get("vol_spike", 0) > SNIPER_VOL_SPIKE_MIN
        and pump_prob > SNIPER_PUMP_MIN
    ):
        logger.info("[MODE] SNIPER selected")
        return TradingMode.SNIPER

    # MG -- momentum/growth play; doesn't require micro_pullback (sniper-only)
    if (
        pump_prob > MG_PUMP_MIN
        and features.get("momentum_1m", 0) > MG_MOM_MIN
        and features.get("vol_ratio",   0) > MG_VOL_RATIO_MIN
        and features.get("vol_spike",   0) > MG_VOL_SPIKE_MIN
        and features.get("price_gt_ema5", 0) == 1
        and features.get("buy_pressure", 0) > MG_BUY_PRESSURE_MIN
        and MG_RSI_LOW <= features.get("rsi", 50) <= MG_RSI_HIGH
        and dump_risk < MG_DUMP_MAX
    ):
        logger.info("[MODE] MG selected")
        return TradingMode.MG

    # HWR
    if (
        pump_prob > HWR_PUMP_MIN
        and dump_risk < HWR_DUMP_MAX
        and features.get("ema10_gt_ema20", 0) == 1
        and HWR_RSI_LOW <= features.get("rsi", 0) <= HWR_RSI_HIGH
        and features.get("volatility", 1) < HWR_VOLATILITY_MAX
    ):
        logger.info("[MODE] HWR selected")
        return TradingMode.HWR

    return TradingMode.NONE


# ── Per-mode decisions ───────────────────────────────────────────────────────

def sniper_decision(features, pump_prob, dump_risk, in_position, position=None, current_price=None) -> ModeDecision:
    if not in_position:
        if (
            features.get("token_age_seconds", 9999) < SNIPER_AGE_MAX
            and features.get("vol_spike", 0) > SNIPER_VOL_SPIKE_MIN
            and features.get("first_candles_rising", 0) >= 0.5
            and features.get("active_traders", 0) > 3
            and features.get("liquidity_usd", 0) > 500       # just needs some liquidity
            and features.get("micro_pullback", 0) == 1
            and pump_prob > SNIPER_PUMP_MIN
        ):
            return ModeDecision(mode=TradingMode.SNIPER, reason="All sniper conditions met", enter=True)
        return ModeDecision(mode=TradingMode.SNIPER, reason="Sniper waiting for setup")

    exit_reasons = []

    # Stop-loss check
    if position and position.avg_entry_price > 0 and current_price is not None:
        loss_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
        if loss_pct < -SNIPER_STOP_LOSS:
            exit_reasons.append(f"stop-loss {loss_pct:.1%} < -{SNIPER_STOP_LOSS:.0%}")

    # Max hold time
    if position and position.entry_time > 0:
        held_s = _time.time() - position.entry_time
        if held_s > SNIPER_MAX_HOLD_S:
            exit_reasons.append(f"max hold {held_s/60:.0f}m > {SNIPER_MAX_HOLD_S//60}m")

    if dump_risk > 0.50:
        exit_reasons.append(f"dump_risk={dump_risk:.2f} > 0.50")
    if features.get("price_gt_ema3", 1) == 0:
        exit_reasons.append("price below EMA3")
    if features.get("liquidity_score", 1) < 0.1:
        exit_reasons.append("liquidity collapsed")
    if exit_reasons:
        return ModeDecision(mode=TradingMode.SNIPER, reason=f"SNIPER EXIT: {'; '.join(exit_reasons)}", exit=True)
    return ModeDecision(mode=TradingMode.SNIPER, reason="Sniper holding")


def mg_decision(features, pump_prob, dump_risk, position: Optional[PositionState], current_price=None) -> ModeDecision:
    in_position = position is not None and position.entries > 0

    if not in_position:
        if (
            pump_prob > MG_PUMP_MIN
            and features.get("momentum_1m", 0) > MG_MOM_MIN
            and features.get("vol_ratio",   0) > MG_VOL_RATIO_MIN
            and features.get("vol_spike",   0) > MG_VOL_SPIKE_MIN
            and features.get("price_gt_ema5", 0) == 1
            and features.get("buy_pressure", 0) > MG_BUY_PRESSURE_MIN
            and MG_RSI_LOW <= features.get("rsi", 50) <= MG_RSI_HIGH
            and dump_risk < MG_DUMP_MAX
        ):
            return ModeDecision(mode=TradingMode.MG, reason="MG first entry", enter=True, entry_number=1)
        # Log which condition failed for easier debugging
        reasons = []
        if pump_prob <= MG_PUMP_MIN:                                   reasons.append(f"pump={pump_prob:.2f}<={MG_PUMP_MIN}")
        if features.get("momentum_1m", 0) <= MG_MOM_MIN:              reasons.append(f"mom1m={features.get('momentum_1m',0):+.3f}<={MG_MOM_MIN}")
        if features.get("vol_ratio",   0) <= MG_VOL_RATIO_MIN:        reasons.append(f"vol_ratio={features.get('vol_ratio',0):.2f}<={MG_VOL_RATIO_MIN}")
        if features.get("vol_spike",   0) <= MG_VOL_SPIKE_MIN:        reasons.append(f"vspike={features.get('vol_spike',0):.2f}<={MG_VOL_SPIKE_MIN}")
        if features.get("price_gt_ema5", 0) != 1:                     reasons.append("price<EMA5")
        if features.get("buy_pressure", 0) <= MG_BUY_PRESSURE_MIN:    reasons.append(f"bp={features.get('buy_pressure',0):.2f}<={MG_BUY_PRESSURE_MIN}")
        rsi_val = features.get("rsi", 50)
        if not (MG_RSI_LOW <= rsi_val <= MG_RSI_HIGH):                reasons.append(f"rsi={rsi_val:.0f} out of [{MG_RSI_LOW},{MG_RSI_HIGH}]")
        if dump_risk >= MG_DUMP_MAX:                                   reasons.append(f"dump={dump_risk:.2f}>={MG_DUMP_MAX}")
        return ModeDecision(mode=TradingMode.MG, reason=f"MG waiting: {'; '.join(reasons) or 'conditions not met'}")

    entries = position.entries if position else 0

    exit_reasons = []

    # Stop-loss check
    if position and position.avg_entry_price > 0 and current_price is not None:
        loss_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
        if loss_pct < -MG_STOP_LOSS:
            exit_reasons.append(f"stop-loss {loss_pct:.1%} < -{MG_STOP_LOSS:.0%}")

    # Max hold time
    if position and position.entry_time > 0:
        held_s = _time.time() - position.entry_time
        if held_s > MG_MAX_HOLD_S:
            exit_reasons.append(f"max hold {held_s/60:.0f}m > {MG_MAX_HOLD_S//60}m")

    if dump_risk > MG_DUMP_MAX:
        exit_reasons.append(f"dump_risk={dump_risk:.2f}")
    if features.get("volatility", 0) > 0.08:
        exit_reasons.append("volatility spike")
    if features.get("price_gt_ema5", 1) == 0:
        exit_reasons.append("price below EMA5")
    if exit_reasons:
        return ModeDecision(mode=TradingMode.MG, reason=f"MG EXIT: {'; '.join(exit_reasons)}", exit=True)

    # Pyramid requires real volume confirmation (vol_spike > 1.0) in addition to
    # slope/confidence checks.  Previously vspike=0.0 could still trigger a pyramid
    # because vol_spike was never checked here -- fixed.
    if (
        entries < MG_MAX_ENTRIES
        and features.get("ema10_slope", 0) > 0.005
        and features.get("vol_spike", 0) > 1.0   # FIX: require actual volume before pyramiding
        and pump_prob > MG_PUMP_MIN + 0.05
        and dump_risk < 0.35
    ):
        return ModeDecision(
            mode=TradingMode.MG,
            reason=f"MG pyramid entry #{entries + 1}",
            enter=True,
            pyramid_allowed=True,
            entry_number=entries + 1,
        )
    return ModeDecision(mode=TradingMode.MG, reason="MG holding")


def hwr_decision(features, pump_prob, dump_risk, in_position, position=None, current_price=None) -> ModeDecision:
    if not in_position:
        if (
            pump_prob > HWR_PUMP_MIN
            and dump_risk < HWR_DUMP_MAX
            and features.get("ema10_gt_ema20", 0) == 1
            and HWR_RSI_LOW <= features.get("rsi", 0) <= HWR_RSI_HIGH
            and features.get("volatility", 1) < HWR_VOLATILITY_MAX
            and features.get("liquidity_score", 0) > 0.5
        ):
            return ModeDecision(mode=TradingMode.HWR, reason="HWR conditions met", enter=True)
        return ModeDecision(mode=TradingMode.HWR, reason="HWR waiting for setup")

    exit_reasons = []

    # Stop-loss check
    if position and position.avg_entry_price > 0 and current_price is not None:
        loss_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
        if loss_pct < -HWR_STOP_LOSS:
            exit_reasons.append(f"stop-loss {loss_pct:.1%} < -{HWR_STOP_LOSS:.0%}")

    # Max hold time
    if position and position.entry_time > 0:
        held_s = _time.time() - position.entry_time
        if held_s > HWR_MAX_HOLD_S:
            exit_reasons.append(f"max hold {held_s/60:.0f}m > {HWR_MAX_HOLD_S//60}m")

    if dump_risk > 0.40:
        exit_reasons.append(f"dump_risk={dump_risk:.2f}")
    if features.get("price_gt_ema10", 1) == 0:
        exit_reasons.append("price below EMA10")
    if features.get("liquidity_score", 1) < 0.3:
        exit_reasons.append("liquidity weakened")
    if exit_reasons:
        return ModeDecision(mode=TradingMode.HWR, reason=f"HWR EXIT: {'; '.join(exit_reasons)}", exit=True)
    return ModeDecision(mode=TradingMode.HWR, reason="HWR holding")


# ── Unified dispatcher ───────────────────────────────────────────────────────

def evaluate(
    features:      Dict[str, float],
    pump_prob:     float,
    dump_risk:     float,
    position:      Optional[PositionState] = None,
    current_price: Optional[float] = None,
) -> ModeDecision:
    in_position = position is not None and position.entries > 0

    # Blend ML probabilities with rule-based scores so the bot can trade
    # on strong technical setups even before the ML is retrained on real data.
    pump_prob, dump_risk = effective_probs(features, pump_prob, dump_risk)

    mode = select_mode(features, pump_prob, dump_risk)

    if mode == TradingMode.SNIPER:
        return sniper_decision(features, pump_prob, dump_risk, in_position, position, current_price)
    elif mode == TradingMode.MG:
        return mg_decision(features, pump_prob, dump_risk, position, current_price)
    elif mode == TradingMode.HWR:
        return hwr_decision(features, pump_prob, dump_risk, in_position, position, current_price)

    # Even in NONE mode, check stop-loss and time exit for existing positions
    if in_position and position:
        exit_reasons = []
        stop_loss = {TradingMode.SNIPER: SNIPER_STOP_LOSS,
                     TradingMode.MG:     MG_STOP_LOSS,
                     TradingMode.HWR:    HWR_STOP_LOSS}.get(position.mode, MG_STOP_LOSS)
        max_hold  = {TradingMode.SNIPER: SNIPER_MAX_HOLD_S,
                     TradingMode.MG:     MG_MAX_HOLD_S,
                     TradingMode.HWR:    HWR_MAX_HOLD_S}.get(position.mode, MG_MAX_HOLD_S)
        if position.avg_entry_price > 0 and current_price is not None:
            loss_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
            if loss_pct < -stop_loss:
                exit_reasons.append(f"stop-loss {loss_pct:.1%}")
        if position.entry_time > 0:
            held_s = _time.time() - position.entry_time
            if held_s > max_hold:
                exit_reasons.append(f"max hold {held_s/60:.0f}m")
        if exit_reasons:
            return ModeDecision(
                mode=TradingMode.NONE,
                reason=f"FORCED EXIT ({position.mode.value}): {'; '.join(exit_reasons)}",
                exit=True,
            )

    return ModeDecision(mode=TradingMode.NONE, reason="No qualifying mode")
