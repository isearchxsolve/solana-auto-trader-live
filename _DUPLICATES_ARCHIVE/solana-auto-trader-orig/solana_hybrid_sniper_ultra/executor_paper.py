"""
executor_paper.py -- Paper trading simulator.
Simulates buy/sell with slippage, fees, and liquidity curves.
Records all trades to paper_trades.csv. Flat layout.
"""

import os
import csv
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PAPER_TRADES_PATH = "paper_trades.csv"
FEE_RATE          = 0.003
DEFAULT_SLIPPAGE  = 0.003


@dataclass
class PaperPosition:
    token:        str
    mode:         str
    entries:      int   = 0
    avg_price:    float = 0.0
    total_sol:    float = 0.0
    total_tokens: float = 0.0
    entry_prices: list  = field(default_factory=list)
    pnl:          float = 0.0
    open:         bool  = True


@dataclass
class TradeRecord:
    timestamp:  float
    token:      str
    mode:       str
    side:       str
    price:      float
    amount_sol: float
    slippage:   float
    fee:        float
    net_sol:    float
    pnl:        float = 0.0
    note:       str   = ""


class PaperExecutor:
    def __init__(self, initial_balance_sol: float = 1.0, trade_amount_sol: float = 0.05):
        self.balance      = initial_balance_sol
        self.trade_amount = trade_amount_sol
        self.positions:   Dict[str, PaperPosition] = {}
        self.trade_log:   List[TradeRecord]        = []
        self.total_pnl    = 0.0
        self._ensure_csv()

    def buy(
        self,
        token:      str,
        mode:       str,
        price:      float,
        amount_sol: Optional[float] = None,
        slippage:   float = DEFAULT_SLIPPAGE,
        note:       str   = "",
    ) -> bool:
        amt = amount_sol or self.trade_amount
        if self.balance < amt:
            logger.warning(f"[Paper] Insufficient balance ({self.balance:.4f} SOL)")
            return False

        effective_price = price * (1 + slippage)
        fee             = amt * FEE_RATE
        net_sol         = amt - fee
        tokens_received = net_sol / (effective_price + 1e-12)
        self.balance   -= amt

        if token not in self.positions:
            self.positions[token] = PaperPosition(token=token, mode=mode)

        pos = self.positions[token]
        pos.open          = True   # re-open if this is a re-entry on a previously closed position
        pos.entries      += 1
        pos.total_sol    += net_sol
        pos.total_tokens += tokens_received
        pos.avg_price     = pos.total_sol / (pos.total_tokens + 1e-12)
        pos.entry_prices.append(effective_price)

        self._log_trade(TradeRecord(
            timestamp=time.time(), token=token, mode=mode, side="BUY",
            price=effective_price, amount_sol=amt, slippage=slippage,
            fee=fee, net_sol=net_sol, note=note,
        ))
        logger.info(
            f"[Paper] BUY  {token[:8]}.. | mode={mode} | price={effective_price:.8f}"
            f" | tokens={tokens_received:.2f} | bal={self.balance:.4f} SOL"
        )
        return True

    def sell(
        self,
        token:    str,
        price:    float,
        slippage: float = DEFAULT_SLIPPAGE,
        note:     str   = "",
    ) -> float:
        pos = self.positions.get(token)
        if not pos or not pos.open:
            logger.warning(f"[Paper] No open position for {token}")
            return 0.0

        effective_price = price * (1 - slippage)
        gross_sol       = pos.total_tokens * effective_price
        fee             = gross_sol * FEE_RATE
        net_sol         = gross_sol - fee
        pnl             = net_sol - pos.total_sol

        self.balance   += net_sol
        self.total_pnl += pnl
        pos.pnl         = pnl
        pos.open        = False

        self._log_trade(TradeRecord(
            timestamp=time.time(), token=token, mode=pos.mode, side="SELL",
            price=effective_price, amount_sol=gross_sol, slippage=slippage,
            fee=fee, net_sol=net_sol, pnl=pnl, note=note,
        ))
        logger.info(
            f"[Paper] SELL {token[:8]}.. | mode={pos.mode} | PnL={pnl:+.4f} SOL"
            f" | bal={self.balance:.4f} SOL"
        )
        return pnl

    def sell_partial(
        self,
        token:    str,
        fraction: float,          # 0.0 – 1.0  (e.g. 0.5 = sell half)
        price:    float,
        slippage: float = DEFAULT_SLIPPAGE,
        note:     str   = "",
    ) -> float:
        """Sell a fraction of the position without closing it fully.
        Returns the realized PnL for this partial sell."""
        pos = self.positions.get(token)
        if not pos or not pos.open:
            logger.warning(f"[Paper] No open position for {token}")
            return 0.0

        fraction        = max(0.0, min(1.0, fraction))
        tokens_to_sell  = pos.total_tokens * fraction
        cost_basis_part = pos.total_sol    * fraction

        effective_price = price * (1 - slippage)
        gross_sol       = tokens_to_sell * effective_price
        fee             = gross_sol * FEE_RATE
        net_sol         = gross_sol - fee
        pnl             = net_sol - cost_basis_part

        self.balance      += net_sol
        self.total_pnl    += pnl
        pos.total_tokens  -= tokens_to_sell
        pos.total_sol     -= cost_basis_part
        pos.pnl           += pnl

        self._log_trade(TradeRecord(
            timestamp=time.time(), token=token, mode=pos.mode,
            side=f"SELL_PARTIAL_{fraction:.0%}",
            price=effective_price, amount_sol=gross_sol, slippage=slippage,
            fee=fee, net_sol=net_sol, pnl=pnl, note=note,
        ))
        logger.info(
            f"[Paper] SELL {fraction:.0%} {token[:8]}.. | PnL={pnl:+.4f} SOL"
            f" | remaining_tokens={pos.total_tokens:.2f} | bal={self.balance:.4f} SOL"
        )
        return pnl

    def get_position(self, token: str) -> Optional[PaperPosition]:
        pos = self.positions.get(token)
        return pos if (pos and pos.open) else None

    def open_positions(self) -> List[PaperPosition]:
        return [p for p in self.positions.values() if p.open]

    def summary(self) -> Dict:
        return {
            "balance_sol":    self.balance,
            "total_pnl":      self.total_pnl,
            "open_positions": len(self.open_positions()),
            "total_trades":   len(self.trade_log),
        }

    def _ensure_csv(self):
        if not os.path.exists(PAPER_TRADES_PATH):
            with open(PAPER_TRADES_PATH, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(TradeRecord.__dataclass_fields__.keys()))
                writer.writeheader()

    def _log_trade(self, record: TradeRecord):
        self.trade_log.append(record)
        with open(PAPER_TRADES_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(TradeRecord.__dataclass_fields__.keys()))
            writer.writerow(asdict(record))
