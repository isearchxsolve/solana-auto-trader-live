# trading_system.py
# PRODUCTION — Portfolio & Execution Engine
# ADDED: stop-loss, take-profit, equity tracking, structured logging

import time
from dataclasses import dataclass, field
from typing import Optional

import config
from logger_setup import get_logger

log = get_logger("trading_system")


@dataclass
class Position:
    token:       str
    qty:         float
    avg_price:   float
    open_time:   float = field(default_factory=time.time)

    def unrealised_pnl(self, current_price: float) -> float:
        return (current_price - self.avg_price) * self.qty

    def pct_change(self, current_price: float) -> float:
        return (current_price - self.avg_price) / (self.avg_price + 1e-12)


class TradingSystem:
    """
    Paper (or live) order management with stop-loss and take-profit.
    In REAL mode, swap calls are delegated to `executor` (not supplied here).
    """

    def __init__(self, mode: str = "PAPER", executor=None):
        self.mode     = mode
        self.executor = executor

        self.cash:      float = config.PAPER_STARTING_CASH
        self.equity:    float = config.PAPER_STARTING_CASH

        self.positions:     dict[str, Position] = {}
        self.trade_history: list[dict]          = []

        log.info(
            "TradingSystem started | mode=%s | cash=$%.2f",
            self.mode, self.cash,
        )

    # ------------------------------------------------------------------
    # PORTFOLIO HELPERS
    # ------------------------------------------------------------------
    def position_count(self) -> int:
        return len(self.positions)

    def has_position(self, token: str) -> bool:
        return token in self.positions

    def position_value(self, token: str, price: float) -> float:
        if token not in self.positions:
            return 0.0
        return self.positions[token].qty * price

    # ------------------------------------------------------------------
    # BUY
    # ------------------------------------------------------------------
    def buy(self, token: str, price: float, size_fraction: float) -> bool:
        if self.position_count() >= config.MAX_POSITIONS:
            log.warning("Max positions (%d) reached — skipping BUY %s",
                        config.MAX_POSITIONS, token)
            return False

        usd_amount = self.cash * size_fraction
        if usd_amount < 1.0:
            log.warning("Insufficient cash ($%.2f) for BUY %s", self.cash, token)
            return False

        qty = usd_amount / price
        self.cash -= usd_amount

        if token in self.positions:
            pos = self.positions[token]
            total_value = pos.qty * pos.avg_price + usd_amount
            total_qty   = pos.qty + qty
            pos.qty       = total_qty
            pos.avg_price = total_value / total_qty
        else:
            self.positions[token] = Position(token=token, qty=qty, avg_price=price)

        self._record_trade("BUY", token, price, qty, usd_amount)
        log.info(
            "BUY  %-15s | price=$%-12.6f | qty=%-14.6f | cost=$%.2f | cash_left=$%.2f",
            token, price, qty, usd_amount, self.cash,
        )
        return True

    # ------------------------------------------------------------------
    # SELL
    # ------------------------------------------------------------------
    def sell(self, token: str, price: float, reason: str = "SIGNAL") -> bool:
        if token not in self.positions:
            log.debug("SELL %s requested but no position", token)
            return False

        pos          = self.positions.pop(token)
        usd_received = pos.qty * price
        pnl          = pos.unrealised_pnl(price)
        self.cash   += usd_received

        self._record_trade("SELL", token, price, pos.qty, usd_received, pnl=pnl, reason=reason)
        log.info(
            "SELL %-15s | price=$%-12.6f | qty=%-14.6f | recv=$%.2f | pnl=$%+.2f | reason=%s",
            token, price, pos.qty, usd_received, pnl, reason,
        )
        return True

    # ------------------------------------------------------------------
    # STOP-LOSS / TAKE-PROFIT GUARDIAN
    # Called on every tick for each open position.
    # ------------------------------------------------------------------
    def check_risk_exits(self, live_prices: dict[str, float]):
        for token in list(self.positions.keys()):
            price = live_prices.get(token)
            if price is None:
                continue
            pos = self.positions[token]
            pct = pos.pct_change(price)

            if pct <= -config.STOP_LOSS_PCT:
                log.warning(
                    "STOP-LOSS triggered for %s (%.2f%% loss)", token, pct * 100
                )
                self.sell(token, price, reason="STOP_LOSS")

            elif pct >= config.TAKE_PROFIT_PCT:
                log.info(
                    "TAKE-PROFIT triggered for %s (+%.2f%%)", token, pct * 100
                )
                self.sell(token, price, reason="TAKE_PROFIT")

    # ------------------------------------------------------------------
    # EXECUTE SIGNAL (called by controller)
    # ------------------------------------------------------------------
    def process(
        self,
        token:         str,
        price:         float,
        signal:        str,
        size_fraction: float,
    ):
        if signal == "BUY":
            self.buy(token, price, size_fraction)
        elif signal == "BUY_SMALL":
            self.buy(token, price, size_fraction * 0.5)
        elif signal == "SELL":
            self.sell(token, price, reason="SIGNAL")
        # HOLD — no action

    # ------------------------------------------------------------------
    # EQUITY SNAPSHOT
    # ------------------------------------------------------------------
    def update_equity(self, live_prices: dict[str, float]) -> float:
        pos_value = sum(
            pos.qty * live_prices[tok]
            for tok, pos in self.positions.items()
            if tok in live_prices
        )
        self.equity = self.cash + pos_value

        open_pnl = self.equity - config.PAPER_STARTING_CASH
        log.info(
            "EQUITY $%.2f | cash=$%.2f | positions=%d | open_pnl=$%+.2f",
            self.equity, self.cash, len(self.positions), open_pnl,
        )
        return self.equity

    # ------------------------------------------------------------------
    # TRADE RECORD
    # ------------------------------------------------------------------
    def _record_trade(
        self,
        ttype: str,
        token: str,
        price: float,
        qty:   float,
        usd:   float,
        pnl:   float = 0.0,
        reason: str  = "",
    ):
        self.trade_history.append(
            {
                "type":      ttype,
                "token":     token,
                "price":     price,
                "qty":       qty,
                "usd":       usd,
                "pnl":       pnl,
                "reason":    reason,
                "timestamp": time.time(),
            }
        )

    # ------------------------------------------------------------------
    # REAL MODE SWAP (stub — implement with your DEX SDK)
    # ------------------------------------------------------------------
    def execute_real_swap(self, token: str, amount_usd: float):
        if self.mode != "REAL":
            return
        if self.executor is None:
            log.error("No executor configured for REAL mode")
            return
        tx = self.executor.swap_usdc_to_token(token, amount_usd)
        log.info("REAL SWAP tx: %s", tx)
