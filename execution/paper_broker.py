"""
Paper broker — simulates order execution without real money.
Tracks positions, P&L, and trade history.
"""

import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    trade_id: str
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    entry_price: float
    exit_price: float | None = None
    pnl: float | None = None
    exit_reason: str = ""


class PaperBroker:

    def __init__(self, initial_cash: float = 10_000.0, fee_rate: float = 0.001):
        self.cash = initial_cash
        self.fee_rate = fee_rate
        self.position_qty = 0.0
        self.open_trade: Trade | None = None
        self.closed_trades: list[Trade] = []

    def buy(self, symbol: str, price: float, qty: float) -> Trade | None:
        cost = price * qty
        fee = cost * self.fee_rate
        if cost + fee > self.cash:
            logger.warning("Insufficient cash: need %.2f, have %.2f", cost + fee, self.cash)
            return None

        self.cash -= cost + fee
        self.position_qty = qty
        self.open_trade = Trade(
            trade_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side="buy",
            qty=qty,
            entry_price=price,
        )
        logger.info(
            "[BUY]  %s | qty=%.6f @ $%.2f | fee=$%.2f | cash=$%.2f",
            symbol, qty, price, fee, self.cash,
        )
        return self.open_trade

    def sell(self, symbol: str, price: float, reason: str = "signal") -> Trade | None:
        if self.open_trade is None or self.position_qty <= 0:
            logger.warning("Sell called with no open position")
            return None

        proceeds = price * self.position_qty
        fee = proceeds * self.fee_rate
        self.cash += proceeds - fee

        pnl = (price - self.open_trade.entry_price) * self.position_qty - fee
        self.open_trade.exit_price = price
        self.open_trade.pnl = pnl
        self.open_trade.exit_reason = reason

        closed = self.open_trade
        self.closed_trades.append(closed)

        logger.info(
            "[SELL] %s | qty=%.6f @ $%.2f | PnL=$%.2f (%s) | cash=$%.2f",
            symbol, self.position_qty, price, pnl, reason, self.cash,
        )

        self.position_qty = 0.0
        self.open_trade = None
        return closed

    def has_position(self) -> bool:
        return self.position_qty > 0

    def summary(self) -> dict:
        total_pnl = sum(t.pnl for t in self.closed_trades if t.pnl is not None)
        wins = sum(1 for t in self.closed_trades if (t.pnl or 0) > 0)
        total = len(self.closed_trades)
        return {
            "cash": round(self.cash, 2),
            "total_trades": total,
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(wins / total, 2) if total > 0 else 0.0,
        }
