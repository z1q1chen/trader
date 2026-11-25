from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from trader.alerts import Alerter
from trader.broker.base import bracket_legs
from trader.config import parse_config
from trader.execution import Executor
from trader.models import (
    CANCELLED, ET, SUBMITTED, AccountState, Fill, Order, Position, TradePlan,
)
from trader.store import Store

RAW = {
    "mode": "paper",
    "profiles": {
        "paper": {"host": "127.0.0.1", "port": 4002, "account": "DU1234567", "db": "state_paper.db",
                  "limits": {"risk_per_trade_pct": 0.5, "max_daily_loss_pct": 2.0,
                             "max_positions": 5, "max_order_usd": 2000}},
        "live": {"host": "127.0.0.1", "port": 4001, "account": "U7654321", "db": "state_live.db",
                 "limits": {"risk_per_trade_pct": 0.5, "max_daily_loss_pct": 1.5,
                            "max_positions": 3, "max_order_usd": 400}},
    },
}


def make_cfg(mode="paper", strategy=None, limits=None, **top):
    raw = {"mode": mode, "profiles": {k: {**v, "limits": dict(v["limits"])}
                                      for k, v in RAW["profiles"].items()}}
    if limits:
        raw["profiles"][mode]["limits"].update(limits)
    if strategy:
        raw["strategy"] = strategy
    raw.update(top)
    return parse_config(raw)


class FakeBroker:
    """Records order calls; positions, orders and fills are set by the test."""

    def __init__(self, account_id="DU1234567", net_liq=10_000.0):
        self.account_id = account_id
        self.net_liq = net_liq
        self.now = datetime(2026, 11, 25, 9, 35, tzinfo=ET)
        self.pos: dict[str, int] = {}
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.calls: list[tuple] = []
        self.accounts = [account_id]
        self.realtime = True
        self.connected = True
        self.time_skew = 0.0
        self.fail_place = False
        self._fill_cb = self._status_cb = None
        self._n = 0

    def account(self):
        return AccountState(self.account_id, self.net_liq, self.net_liq)

    def positions(self):
        return [Position(s, q, 0.0) for s, q in self.pos.items() if q]

    def open_orders(self):
        return [o for o in self.orders.values() if o.status == SUBMITTED]

    def recent_fills(self):
        return list(self.fills)

    def bars(self, symbol, timeframe, lookback):
        return pd.DataFrame()

    def subscribe_bars(self, *a):
        pass

    def place_bracket(self, plan: TradePlan):
        self.calls.append(("place_bracket", plan.ref))
        if self.fail_place:
            raise RuntimeError("gateway down")
        for leg in bracket_legs(plan):
            self.orders[leg.ref] = Order(leg.ref, plan.symbol, leg.side, leg.type, leg.qty,
                                         SUBMITTED, leg.stop_price, leg.limit_price)
        return {leg.ref: f"B{i}" for i, leg in enumerate(bracket_legs(plan))}

    def place_stop(self, ref, symbol, qty, stop_price):
        self.calls.append(("place_stop", ref, symbol, qty, stop_price))
        self.orders[ref] = Order(ref, symbol, "SELL", "STP", qty, SUBMITTED, stop_price)
        return "S1"

    def modify_stop(self, ref, new_stop):
        self.calls.append(("modify_stop", ref, new_stop))

    def modify_limit(self, ref, new_limit):
        self.calls.append(("modify_limit", ref, new_limit))

    def close_position(self, symbol, qty, ref):
        self.calls.append(("close_position", symbol, qty, ref))
        self.orders[ref] = Order(ref, symbol, "SELL", "MKT", qty, SUBMITTED)
        return "X1"

    def cancel(self, ref):
        self.calls.append(("cancel", ref))
        o = self.orders.get(ref)
        if o and o.status == SUBMITTED:
            self.orders[ref] = Order(o.ref, o.symbol, o.side, o.type, o.qty, CANCELLED,
                                     o.stop_price, o.limit_price, o.filled_qty)
            if self._status_cb:
                self._status_cb(ref, CANCELLED, "")

    def cancel_all(self):
        self.calls.append(("cancel_all",))
        for ref in list(self.orders):
            self.cancel(ref)

    def on_fill(self, cb):
        self._fill_cb = cb

    def on_order_status(self, cb):
        self._status_cb = cb

    def on_disconnect(self, cb):
        pass

    def connect(self):
        self.calls.append(("connect",))
        self.connected = True

    def managed_accounts(self):
        return list(self.accounts)

    def server_time(self):
        return self.now + pd.Timedelta(seconds=self.time_skew)

    def market_data_realtime(self):
        return self.realtime

    def is_connected(self):
        return self.connected

    def sleep(self, s):
        pass

    # test helpers
    def fill(self, ref, qty, price, commission=1.0, notify=True):
        """Simulate an execution: updates position and order, then notifies."""
        o = self.orders[ref]
        self._n += 1
        sign = 1 if o.side == "BUY" else -1
        self.pos[o.symbol] = self.pos.get(o.symbol, 0) + sign * qty
        filled = o.filled_qty + qty
        self.orders[ref] = Order(o.ref, o.symbol, o.side, o.type, o.qty,
                                 "FILLED" if filled >= o.qty else SUBMITTED,
                                 o.stop_price, o.limit_price, filled)
        f = Fill(f"E{self._n}", ref, o.symbol, o.side, qty, price, commission, self.now)
        self.fills.append(f)
        if notify and self._fill_cb:
            self._fill_cb(f)
        return f


class Rig:
    def __init__(self):
        self.broker = FakeBroker()
        self.store = Store(":memory:")
        self.clock = lambda: self.broker.now
        self.alerter = Alerter(self.store, self.clock)
        self.ex = Executor(self.broker, self.store, self.alerter, self.clock)
        self.broker.on_fill(self.ex.handle_fill)
        self.broker.on_order_status(self.ex.handle_order_status)


@pytest.fixture
def rig():
    """A FakeBroker wired to a real Store and Executor."""
    return Rig()


def make_plan(symbol="NVDA", qty=30, qty_a=10, entry=100.0, stop=99.0, ref=None, adr=5.0):
    return TradePlan(ref=ref or f"20261125-{symbol}", symbol=symbol, setup="A_BREAKOUT",
                     entry_type="STOP_LIMIT", trigger_price=entry, limit_price=round(entry * 1.003, 2),
                     stop_price=stop, qty=qty, qty_a=qty_a,
                     target_price=round(entry + 3 * (entry - stop), 2),
                     risk_usd=qty * (entry - stop), adr=adr)
