"""Reconciliation: the broker is the source of truth for positions and orders.

Used on a timer, after a reconnect, and for restart recovery. Whatever can be
repaired safely is repaired; anything else is returned as an unresolved diff,
which puts the system in read-only mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .alerts import WARN, Alerter
from .broker.base import Broker
from .execution import Executor
from .models import (
    A_PARENT, A_STOP, A_TARGET, B_PARENT, B_STOP, CANCELLED, EXIT_ORDER, PROTECT, SUBMITTED,
    T_CLOSED, TERMINAL, WORKING, order_ref, split_ref,
)
from .store import Store


@dataclass
class ReconcileResult:
    diffs: list[str] = field(default_factory=list)      # unresolved, need a human
    repairs: list[str] = field(default_factory=list)    # fixed automatically

    @property
    def clean(self) -> bool:
        return not self.diffs


def _coverage(store: Store, o) -> tuple[int, bool]:
    """How much of a resting sell order protects shares we actually hold.

    A bracket child only counts for what its parent has filled. It is "settled"
    once the parent is final (or it has no parent), and only then may it be replaced.
    """
    remaining = o.qty - o.filled_qty
    trade_id, role = split_ref(o.ref)
    parent_role = {A_STOP: A_PARENT, A_TARGET: A_PARENT, B_STOP: B_PARENT}.get(role)
    parent = store.get_order(order_ref(trade_id, parent_role)) if parent_role else None
    if parent is None or parent["status"] in TERMINAL:
        return remaining, True
    return min(remaining, parent["filled_qty"]), False


def reconcile(broker: Broker, store: Store, executor: Executor, alerter: Alerter,
              clock: Callable[[], datetime]) -> ReconcileResult:
    res = ReconcileResult()
    executor.needs_reconcile = False

    # 1. Replay executions we may have missed (duplicates are ignored by exec_id).
    for fill in broker.recent_fills():
        executor.handle_fill(fill)

    # 2. Order status: local working orders the broker no longer has are finished.
    at_broker = {o.ref: o for o in broker.open_orders()}
    for o in store.working_orders():
        if o["client_ref"] not in at_broker:
            executor.handle_order_status(o["client_ref"], CANCELLED, "not at broker (reconcile)")
            res.repairs.append(f"order_closed:{o['client_ref']}")
    for ref, bo in at_broker.items():
        local = store.get_order(ref) if ref else None
        if local is None:
            res.diffs.append(f"unknown_order:{bo.symbol}:{ref or bo.broker_order_id}")
        elif local["status"] in TERMINAL:
            store.update_order(ref, status=SUBMITTED)
            res.repairs.append(f"order_reopened:{ref}")

    # 3. Positions.
    held = {p.symbol: p.qty for p in broker.positions() if p.qty != 0}
    trades = {t["symbol"]: t for t in store.live_trades()}
    for sym, qty in sorted(held.items()):
        t = trades.get(sym)
        if qty < 0:
            res.diffs.append(f"short_position:{sym}:{qty}")
        elif t is None or t["open_qty"] == 0:
            res.diffs.append(f"unknown_position:{sym}:{qty}")  # alert only, never auto-handled
        elif t["open_qty"] != qty:
            res.diffs.append(f"qty_mismatch:{sym}:broker={qty}:local={t['open_qty']}")
    for sym, t in sorted(trades.items()):
        if t["open_qty"] > 0 and sym not in held:
            # The broker is flat and its fills (step 1) did not explain it: treat as closed.
            store.update_trade(t["id"], status=T_CLOSED, open_qty=0,
                               exit_reason="reconciled_missing", closed_at=clock().isoformat())
            executor._cancel_working(t["id"])
            res.repairs.append(f"trade_closed:{t['id']}")
            alerter.send(WARN, "reconcile_closed", f"{t['id']} is flat at the broker; marked closed")

    # 4. Every position must have stop orders at the broker for exactly its size.
    blocked = {d.split(":")[1] for d in res.diffs if d.split(":")[0] in (
        "unknown_position", "qty_mismatch", "short_position")}
    open_orders = broker.open_orders()
    for sym, qty in sorted(held.items()):
        if sym in blocked or qty <= 0:
            continue
        t = store.live_trade_for(sym)
        if t is None:
            continue
        local_orders = store.orders_for_trade(t["id"])
        if any(o["role"] == EXIT_ORDER and o["status"] in WORKING for o in local_orders):
            continue  # being sold; its stops were pulled on purpose
        sells = [o for o in open_orders if o.symbol == sym and o.side == "SELL"]
        stop_qty, settled = 0, []
        for o in sells:
            counted, is_settled = _coverage(store, o)
            if is_settled:
                settled.append(o)
            if o.type == "STP":
                stop_qty += counted
        if stop_qty == qty:
            continue
        if stop_qty > qty:
            # Over-covered: a stop fill would flip the account short. Replace the
            # orders whose entry is finished with one stop for the real position.
            for o in settled:
                broker.cancel(o.ref)
                if o.type == "STP":
                    stop_qty -= _coverage(store, o)[0]
        need = qty - stop_qty
        if need <= 0:
            continue
        ref = order_ref(t["id"], PROTECT, store.next_seq(t["id"], PROTECT))
        store.add_order(client_ref=ref, trade_id=t["id"], role=PROTECT, side="SELL", type="STP",
                        qty=need, price=None, stop_price=t["stop_price"], status=SUBMITTED,
                        created_at=clock(), note="placed by reconcile")
        broker_id = broker.place_stop(ref, sym, need, t["stop_price"])
        store.update_order(ref, broker_order_id=str(broker_id))
        res.repairs.append(f"stop_placed:{sym}:{need}")
        alerter.send(WARN, "stop_replaced",
                     f"{sym}: stops covered {stop_qty} of {qty}; placed stop for {need} @ {t['stop_price']:.2f}")
    executor.needs_reconcile = False
    return res
