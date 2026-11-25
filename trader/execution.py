"""Execution layer: turns approved plans into broker orders and tracks them.

Only this module calls the broker's order methods.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

from .alerts import ERROR, INFO, WARN, Alerter
from .broker.base import Broker, bracket_legs
from .models import (
    A_TARGET, CANCELLED, EXIT_ORDER, FILLED, PARENT_ROLES, PARTIAL, PENDING_SUBMIT,
    REJECTED, STOP_ROLES, SUBMITTED, T_CANCELLED, T_CLOSED, T_EXIT_QUEUED, T_OPEN,
    T_PENDING, T_REJECTED, TERMINAL, UNKNOWN, WORKING, Fill, TradePlan, order_ref, split_ref,
)
from .store import Store

log = logging.getLogger("trader.execution")


class Executor:
    def __init__(self, broker: Broker, store: Store, alerter: Alerter,
                 clock: Callable[[], datetime], target_r: float = 3.0):
        self.broker = broker
        self.store = store
        self.alerter = alerter
        self.clock = clock
        self.target_r = target_r
        # Set when stops may no longer match positions; the engine reconciles when it sees it.
        self.needs_reconcile = False

    # ── entries ──────────────────────────────────────────────────
    def submit_entry(self, plan: TradePlan, today: date) -> bool:
        """Persist the plan, then place its bracket orders. Idempotent per symbol per day."""
        if not self.store.create_trade(plan, today):
            log.info("skip %s: already submitted today", plan.ref)
            return False
        now = self.clock()
        legs = bracket_legs(plan)
        for leg in legs:
            self.store.add_order(
                client_ref=leg.ref, trade_id=plan.ref, role=leg.role, side=leg.side,
                type=leg.type, qty=leg.qty, price=leg.limit_price, stop_price=leg.stop_price,
                status=PENDING_SUBMIT, created_at=now)
        self.store.record_submission(today, plan.ref)
        try:
            ids = self.broker.place_bracket(plan)
        except Exception as e:
            # We do not know what reached the broker. Do not retry; reconcile will sort it out.
            for leg in legs:
                self.store.update_order(leg.ref, status=UNKNOWN, note=str(e)[:200])
            self.needs_reconcile = True
            self.alerter.send(ERROR, "submit_failed", f"{plan.ref}: {e}")
            return False
        for ref, broker_id in ids.items():
            order = self.store.get_order(ref)
            fields = {"broker_order_id": str(broker_id)}
            if order and order["status"] == PENDING_SUBMIT:
                fields["status"] = SUBMITTED
            self.store.update_order(ref, **fields)
        log.info("submitted %s %s qty=%d (A=%d) trigger=%.2f stop=%.2f target=%.2f",
                 plan.ref, plan.setup, plan.qty, plan.qty_a, plan.trigger_price,
                 plan.stop_price, plan.target_price)
        return True

    # ── broker events ────────────────────────────────────────────
    def handle_fill(self, fill: Fill) -> None:
        if not self.store.add_fill(fill):
            return  # duplicate execution report
        trade_id, role = split_ref(fill.ref)
        trade = self.store.get_trade(trade_id)
        order = self.store.get_order(fill.ref)
        if trade is None or order is None:
            self.needs_reconcile = True
            self.alerter.send(WARN, "fill_unknown_ref",
                              f"{fill.symbol} {fill.side} {fill.qty}@{fill.price} ref={fill.ref!r}")
            return
        filled = order["filled_qty"] + fill.qty
        self.store.update_order(fill.ref, filled_qty=filled,
                                status=FILLED if filled >= order["qty"] else PARTIAL)
        commission = trade["commission"] + fill.commission
        if fill.side == "BUY":
            self._on_entry_fill(trade, fill, commission)
        else:
            self._on_exit_fill(trade, order, role, fill, commission)

    def _on_entry_fill(self, trade: dict, fill: Fill, commission: float) -> None:
        tid = trade["id"]
        new_filled = trade["filled_qty"] + fill.qty
        entry = ((trade["entry_price"] or 0.0) * trade["filled_qty"] + fill.price * fill.qty) / new_filled
        width = max(entry - trade["stop_price"], 0.01)
        target = round(entry + self.target_r * width, 2)
        fields = dict(filled_qty=new_filled, open_qty=trade["open_qty"] + fill.qty,
                      entry_price=entry, risk_usd=round(width * new_filled, 4),
                      target_price=target, commission=commission)
        if trade["status"] == T_PENDING:
            fields["status"] = T_OPEN
        if not trade["opened_at"]:
            fields["opened_at"] = fill.ts.isoformat()
        self.store.update_trade(tid, **fields)
        # R is defined from the real fill, so the 3R target moves with it.
        tgt_ref = order_ref(tid, A_TARGET)
        tgt = self.store.get_order(tgt_ref)
        if tgt and tgt["status"] in WORKING and abs((tgt["price"] or 0) - target) >= 0.01:
            try:
                self.broker.modify_limit(tgt_ref, target)
                self.store.update_order(tgt_ref, price=target)
            except Exception as e:
                self.alerter.send(WARN, "modify_target_failed", f"{tid}: {e}")
        self.alerter.send(INFO, "entry_fill",
                          f"{fill.symbol} bought {fill.qty}@{fill.price:.2f} stop={trade['stop_price']:.2f}")

    def _on_exit_fill(self, trade: dict, order: dict, role: str, fill: Fill,
                      commission: float) -> None:
        tid = trade["id"]
        entry = trade["entry_price"] or fill.price
        open_qty = trade["open_qty"] - fill.qty
        sold_before = trade["filled_qty"] - trade["open_qty"]
        sold = sold_before + fill.qty
        realized = trade["realized_pnl"] + (fill.price - entry) * fill.qty
        exit_price = ((trade["exit_price"] or 0.0) * sold_before + fill.price * fill.qty) / sold
        fields = dict(open_qty=max(open_qty, 0), realized_pnl=realized,
                      exit_price=exit_price, commission=commission)
        if role == A_TARGET:
            fields["scaled_out"] = 1
        if open_qty < 0:
            self.needs_reconcile = True
            self.alerter.send(ERROR, "oversold", f"{tid}: sold {-open_qty} more than held")
        if open_qty <= 0:
            if role in STOP_ROLES:
                reason = "stop"
            elif role == A_TARGET:
                reason = "target"
            else:
                reason = order["note"] or trade["exit_reason"] or "exit"
            fields.update(status=T_CLOSED, exit_reason=reason, closed_at=fill.ts.isoformat())
            risk = trade["risk_usd"]
            fields["r_multiple"] = round((realized - commission) / risk, 4) if risk > 0 else None
        self.store.update_trade(tid, **fields)
        self.alerter.send(INFO, "exit_fill",
                          f"{fill.symbol} sold {fill.qty}@{fill.price:.2f} ({role})")
        if open_qty <= 0:
            self._cancel_working(tid)

    def handle_order_status(self, ref: str, status: str, message: str = "") -> None:
        order = self.store.get_order(ref)
        if order is None or order["status"] == status:
            return
        if order["status"] in TERMINAL:
            return  # never move an order back out of a final state
        if status == SUBMITTED and order["status"] == PARTIAL:
            return
        if status == FILLED:
            return  # fills are the source of truth for FILLED
        self.store.update_order(ref, status=status, note=message[:200] if message else order["note"])
        trade_id, role = split_ref(ref)
        if status == REJECTED:
            # Never retried automatically.
            self.alerter.send(ERROR, "order_rejected", f"{ref}: {message}")
        if status in (CANCELLED, REJECTED):
            if role in PARENT_ROLES:
                self._settle_entry(trade_id)
            elif role in STOP_ROLES:
                trade = self.store.get_trade(trade_id)
                if trade and trade["open_qty"] > 0:
                    self.needs_reconcile = True  # a live position may have lost its stop

    def _settle_entry(self, trade_id: str) -> None:
        """Once every parent order is final, close out a trade that never filled."""
        trade = self.store.get_trade(trade_id)
        if trade is None:
            return
        parents = [o for o in self.store.orders_for_trade(trade_id) if o["role"] in PARENT_ROLES]
        if any(o["status"] not in TERMINAL for o in parents):
            return
        if trade["filled_qty"] == 0:
            rejected = any(o["status"] == REJECTED for o in parents)
            self.store.update_trade(
                trade_id, status=T_REJECTED if rejected else T_CANCELLED,
                exit_reason="rejected" if rejected else "not_triggered",
                closed_at=self.clock().isoformat())
            self._cancel_working(trade_id)
        elif any(o["filled_qty"] < o["qty"] for o in parents):
            # Partially filled, rest cancelled: stops must be resized to the real position.
            self.needs_reconcile = True

    def _cancel_working(self, trade_id: str) -> None:
        for o in self.store.orders_for_trade(trade_id):
            if o["status"] in WORKING:
                try:
                    self.broker.cancel(o["client_ref"])
                except Exception as e:
                    self.alerter.send(WARN, "cancel_failed", f"{o['client_ref']}: {e}")

    # ── exits ────────────────────────────────────────────────────
    def queue_exit(self, symbol: str, reason: str) -> bool:
        """Mark a position to be sold at the next open."""
        trade = self.store.live_trade_for(symbol)
        if trade is None or trade["open_qty"] <= 0 or trade["status"] == T_EXIT_QUEUED:
            return False
        self.store.update_trade(trade["id"], status=T_EXIT_QUEUED, exit_reason=reason)
        return True

    def submit_queued_exits(self, today: date) -> int:
        n = 0
        for trade in self.store.live_trades():
            if trade["status"] == T_EXIT_QUEUED and trade["open_qty"] > 0:
                n += self._exit(trade, trade["exit_reason"], today)
        return n

    def exit_now(self, symbol: str, reason: str, today: date) -> bool:
        trade = self.store.live_trade_for(symbol)
        if trade is None or trade["open_qty"] <= 0:
            return False
        self.store.update_trade(trade["id"], exit_reason=reason)
        return bool(self._exit(trade, reason, today))

    def _exit(self, trade: dict, reason: str, today: date) -> int:
        tid = trade["id"]
        orders = self.store.orders_for_trade(tid)
        if any(o["role"] == EXIT_ORDER and o["status"] in WORKING for o in orders):
            return 0
        # Pull the resting sell orders first so the position cannot be sold twice.
        self._cancel_working(tid)
        ref = order_ref(tid, EXIT_ORDER, self.store.next_seq(tid, EXIT_ORDER))
        self.store.add_order(client_ref=ref, trade_id=tid, role=EXIT_ORDER, side="SELL",
                             type="MKT", qty=trade["open_qty"], price=None, stop_price=None,
                             status=PENDING_SUBMIT, created_at=self.clock(), note=reason)
        self.store.record_submission(today, ref)
        try:
            broker_id = self.broker.close_position(trade["symbol"], trade["open_qty"], ref)
        except Exception as e:
            self.store.update_order(ref, status=UNKNOWN, note=f"{reason}: {e}"[:200])
            self.needs_reconcile = True
            self.alerter.send(ERROR, "exit_failed", f"{tid}: {e}")
            return 0
        order = self.store.get_order(ref)
        fields = {"broker_order_id": str(broker_id)}
        if order and order["status"] == PENDING_SUBMIT:
            fields["status"] = SUBMITTED
        self.store.update_order(ref, **fields)
        return 1

    def modify_stop(self, symbol: str, new_stop: float) -> None:
        trade = self.store.live_trade_for(symbol)
        if trade is None:
            return
        for o in self.store.orders_for_trade(trade["id"]):
            if o["role"] in STOP_ROLES and o["status"] in WORKING:
                self.broker.modify_stop(o["client_ref"], new_stop)
                self.store.update_order(o["client_ref"], stop_price=new_stop)
        self.store.update_trade(trade["id"], stop_price=new_stop)
