"""Broker interface. Strategy, risk and execution only ever talk to this."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

import pandas as pd

from ..models import (
    A_PARENT, A_STOP, A_TARGET, B_PARENT, B_STOP, STOP_LIMIT,
    AccountState, Fill, Order, Position, TradePlan, order_ref,
)

FillCallback = Callable[[Fill], None]
# (order ref, normalised status, message)
StatusCallback = Callable[[str, str, str], None]


class Broker(Protocol):
    # account state
    def account(self) -> AccountState: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[Order]: ...
    def recent_fills(self) -> list[Fill]: ...
    # market data: timeframe is "1d" or "5m"; only completed bars are returned
    def bars(self, symbol: str, timeframe: str, lookback: int) -> pd.DataFrame: ...
    def subscribe_bars(self, symbol: str, timeframe: str, on_bar: Callable) -> None: ...
    # orders
    def place_bracket(self, plan: TradePlan) -> dict[str, str]: ...
    def place_stop(self, ref: str, symbol: str, qty: int, stop_price: float) -> str: ...
    def modify_stop(self, order_ref: str, new_stop: float) -> None: ...
    def modify_limit(self, order_ref: str, new_limit: float) -> None: ...
    def close_position(self, symbol: str, qty: int, ref: str) -> str: ...
    def cancel(self, order_ref: str) -> None: ...
    def cancel_all(self) -> None: ...
    # events
    def on_fill(self, callback: FillCallback) -> None: ...
    def on_order_status(self, callback: StatusCallback) -> None: ...
    def on_disconnect(self, callback: Callable[[], None]) -> None: ...
    # environment
    def connect(self) -> None: ...
    def managed_accounts(self) -> list[str]: ...
    def server_time(self) -> datetime: ...
    def market_data_realtime(self) -> bool: ...
    def is_connected(self) -> bool: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class Leg:
    ref: str
    role: str
    side: str
    type: str  # STP LMT | LMT | STP
    qty: int
    stop_price: float | None
    limit_price: float | None
    tif: str  # DAY | GTC
    parent_ref: str
    oca_group: str
    transmit: bool


def bracket_legs(plan: TradePlan) -> list[Leg]:
    """Split a plan into its orders.

    Group A carries a stop and the 3R target (one cancels the other). Group B
    carries only a stop and is trailed by the program. Within each group only the
    last child transmits, so the whole group goes live together.
    """
    legs: list[Leg] = []
    groups = (("A", plan.qty_a, A_PARENT, A_STOP), ("B", plan.qty_b, B_PARENT, B_STOP))
    for group, qty, parent_role, stop_role in groups:
        if qty <= 0:
            continue
        parent = order_ref(plan.ref, parent_role)
        if plan.entry_type == STOP_LIMIT:
            legs.append(Leg(parent, parent_role, "BUY", "STP LMT", qty, plan.trigger_price,
                            plan.limit_price, "DAY", "", "", False))
        else:
            legs.append(Leg(parent, parent_role, "BUY", "LMT", qty, None,
                            plan.limit_price, "DAY", "", "", False))
        oca = f"{plan.ref}:OCA" if group == "A" else ""
        if group == "A":
            legs.append(Leg(order_ref(plan.ref, A_TARGET), A_TARGET, "SELL", "LMT", qty, None,
                            plan.target_price, "GTC", parent, oca, False))
        legs.append(Leg(order_ref(plan.ref, stop_role), stop_role, "SELL", "STP", qty,
                        plan.stop_price, None, "GTC", parent, oca, True))
    return legs
