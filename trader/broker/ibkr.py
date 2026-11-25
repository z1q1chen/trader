"""IbkrBroker: the Broker interface on top of IB Gateway via ib_async.

Paper and live use this same class; only host, port and account differ.
Not exercised against a real Gateway by the test suite: only order
construction is unit tested.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

from ..models import (
    CANCELLED, ET, FILLED, REJECTED, SUBMITTED, AccountState, Fill, Order, Position, TradePlan,
)
from .base import FillCallback, Leg, StatusCallback, bracket_legs

log = logging.getLogger("trader.ibkr")

_STATUS = {
    "PendingSubmit": SUBMITTED, "ApiPending": SUBMITTED, "PreSubmitted": SUBMITTED,
    "Submitted": SUBMITTED, "ApiUpdate": SUBMITTED, "PendingCancel": SUBMITTED,
    "Filled": FILLED, "Cancelled": CANCELLED, "ApiCancelled": CANCELLED,
    "Inactive": REJECTED, "ValidationError": REJECTED,
}
# Error codes that mean "you are being shown delayed data".
_DELAYED_CODES = {354, 10090, 10167, 10168}
BAR = timedelta(minutes=5)


def build_ib_orders(plan: TradePlan, account: str, next_id: Callable[[], int]):
    """Turn a plan into IB orders. Returns [(leg, order)] in submission order.

    Children reference their parent by id; in each group only the last child has
    transmit=True, so IB activates the whole group atomically.
    """
    from ib_async import Order as IbOrder

    ids: dict[str, int] = {}
    out: list[tuple[Leg, object]] = []
    for leg in bracket_legs(plan):
        o = IbOrder()
        o.orderId = ids[leg.ref] = next_id()
        o.action = leg.side
        o.totalQuantity = leg.qty
        o.orderType = leg.type
        if leg.stop_price is not None:
            o.auxPrice = leg.stop_price
        if leg.limit_price is not None:
            o.lmtPrice = leg.limit_price
        o.tif = leg.tif
        o.orderRef = leg.ref
        o.account = account
        o.outsideRth = False
        o.transmit = leg.transmit
        if leg.parent_ref:
            o.parentId = ids[leg.parent_ref]
        if leg.oca_group:
            o.ocaGroup = leg.oca_group
            o.ocaType = 1  # cancel the rest of the group, with block
        out.append((leg, o))
    return out


class IbkrBroker:
    def __init__(self, host: str, port: int, account: str, client_id: int = 17,
                 probe_symbol: str = "QQQ"):
        from ib_async import IB

        self.host, self.port, self.account, self.client_id = host, port, account, client_id
        self.probe_symbol = probe_symbol
        self.ib = IB()
        self._trades: dict[str, object] = {}     # order ref -> ib Trade
        self._contracts: dict[str, object] = {}
        self._fill_cbs: list[FillCallback] = []
        self._status_cbs: list[StatusCallback] = []
        self._disconnect_cbs: list[Callable[[], None]] = []
        self._delayed = False
        self._hooked = False

    # ── connection ───────────────────────────────────────────────
    def connect(self) -> None:
        if self.ib.isConnected():
            return
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)
        self.ib.reqMarketDataType(1)  # ask for live data; never silently accept delayed
        if not self._hooked:
            self.ib.commissionReportEvent += self._on_commission
            self.ib.orderStatusEvent += self._on_status
            self.ib.errorEvent += self._on_error
            self.ib.disconnectedEvent += self._on_disconnected
            self._hooked = True
        self._index_open_trades()

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()

    def is_connected(self) -> bool:
        return self.ib.isConnected()

    def sleep(self, seconds: float) -> None:
        if self.ib.isConnected():
            self.ib.sleep(seconds)  # also pumps the event loop
        else:
            import time
            time.sleep(seconds)

    def _index_open_trades(self) -> None:
        for trade in self.ib.openTrades():
            if trade.order.orderRef:
                self._trades[trade.order.orderRef] = trade

    def _contract(self, symbol: str):
        if symbol not in self._contracts:
            from ib_async import Stock

            contract = Stock(symbol, "SMART", "USD")
            if not self.ib.qualifyContracts(contract):
                raise ValueError(f"unknown symbol {symbol}")
            self._contracts[symbol] = contract
        return self._contracts[symbol]

    # ── account state ────────────────────────────────────────────
    def account(self) -> AccountState:
        vals = {(v.tag, v.currency): v.value for v in self.ib.accountValues(self.account)}

        def base(tag: str) -> float:
            for (t, _cur), v in vals.items():
                if t == tag:
                    try:
                        return float(v)
                    except ValueError:
                        continue
            return 0.0

        # NetLiquidation and AvailableFunds come in the account's base currency (CAD for
        # an IBKR Canada account). ExchangeRate[USD] is USD -> base, so divide to get USD.
        try:
            usd_rate = float(vals.get(("ExchangeRate", "USD"), 1.0)) or 1.0
        except ValueError:
            usd_rate = 1.0
        return AccountState(
            account_id=self.account,
            net_liq=base("NetLiquidation") / usd_rate,
            available_funds=base("AvailableFunds") / usd_rate,
            realized_pnl_today=base("RealizedPnL") / usd_rate,
        )

    def positions(self) -> list[Position]:
        return [Position(p.contract.symbol, int(p.position), float(p.avgCost))
                for p in self.ib.positions(self.account)
                if p.contract.secType == "STK" and p.position != 0]

    def open_orders(self) -> list[Order]:
        self._index_open_trades()
        out = []
        for t in self.ib.openTrades():
            if t.isDone():
                continue
            o = t.order
            out.append(Order(
                ref=o.orderRef or "", symbol=t.contract.symbol, side=o.action, type=o.orderType,
                qty=int(o.totalQuantity), status=_STATUS.get(t.orderStatus.status, SUBMITTED),
                stop_price=o.auxPrice if o.orderType in ("STP", "STP LMT") else None,
                limit_price=o.lmtPrice if o.orderType in ("LMT", "STP LMT") else None,
                filled_qty=int(t.orderStatus.filled), broker_order_id=str(o.orderId)))
        return out

    def recent_fills(self) -> list[Fill]:
        return [self._to_fill(f) for f in self.ib.fills()
                if f.execution.acctNumber in ("", self.account)]

    @staticmethod
    def _to_fill(f) -> Fill:
        ex = f.execution
        commission = getattr(f.commissionReport, "commission", 0.0) or 0.0
        ts = f.time.astimezone(ET) if f.time else datetime.now(ET)
        return Fill(exec_id=ex.execId, ref=ex.orderRef or "", symbol=f.contract.symbol,
                    side="BUY" if ex.side == "BOT" else "SELL", qty=int(ex.shares),
                    price=float(ex.price), commission=float(commission), ts=ts)

    # ── market data ──────────────────────────────────────────────
    def bars(self, symbol: str, timeframe: str, lookback: int) -> pd.DataFrame:
        from ib_async import util

        contract = self._contract(symbol)
        if timeframe == "1d":
            days = lookback * 7 // 5 + 10
            duration = f"{days} D" if days <= 365 else f"{days // 365 + 1} Y"
            raw = self.ib.reqHistoricalData(contract, "", duration, "1 day", "TRADES",
                                            useRTH=True, formatDate=1)
            df = util.df(raw)
            if df is None or df.empty:
                return pd.DataFrame()
            df.index = pd.DatetimeIndex(pd.to_datetime(df["date"])).normalize()
            return df[["open", "high", "low", "close", "volume"]].tail(lookback)
        if timeframe == "5m":
            raw = self.ib.reqHistoricalData(contract, "", "1 D", "5 mins", "TRADES",
                                            useRTH=True, formatDate=2)
            df = util.df(raw)
            if df is None or df.empty:
                return pd.DataFrame()
            df.index = pd.DatetimeIndex(pd.to_datetime(df["date"], utc=True)).tz_convert(ET)
            now = self.server_time()
            # The last bar from IB is still forming: keep only finished bars of today.
            df = df[(df.index + BAR <= now) & (df.index.date == now.date())]
            return df[["open", "high", "low", "close", "volume"]].tail(lookback)
        raise ValueError(f"unsupported timeframe {timeframe}")

    def subscribe_bars(self, symbol: str, timeframe: str, on_bar: Callable) -> None:
        size = {"5m": "5 mins", "1d": "1 day"}[timeframe]
        bars = self.ib.reqHistoricalData(self._contract(symbol), "", "1 D", size, "TRADES",
                                         useRTH=True, formatDate=2, keepUpToDate=True)
        bars.updateEvent += lambda b, has_new: on_bar(symbol, b[-2]) if has_new and len(b) > 1 else None

    def market_data_realtime(self) -> bool:
        """True when IB serves live (1) or frozen-live (2) quotes for the probe symbol."""
        self._delayed = False
        contract = self._contract(self.probe_symbol)
        ticker = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(3)
        kind = getattr(ticker, "marketDataType", None)
        self.ib.cancelMktData(contract)
        return not self._delayed and kind in (1, 2)

    # ── orders ───────────────────────────────────────────────────
    def place_bracket(self, plan: TradePlan) -> dict[str, str]:
        contract = self._contract(plan.symbol)
        ids = {}
        for leg, order in build_ib_orders(plan, self.account, self.ib.client.getReqId):
            self._trades[leg.ref] = self.ib.placeOrder(contract, order)
            ids[leg.ref] = str(order.orderId)
        return ids

    def place_stop(self, ref: str, symbol: str, qty: int, stop_price: float) -> str:
        from ib_async import Order as IbOrder

        o = IbOrder(action="SELL", totalQuantity=qty, orderType="STP", auxPrice=stop_price,
                    tif="GTC", orderRef=ref, account=self.account)
        self._trades[ref] = self.ib.placeOrder(self._contract(symbol), o)
        return str(o.orderId)

    def close_position(self, symbol: str, qty: int, ref: str) -> str:
        from ib_async import Order as IbOrder

        # A DAY market order sent before 09:30 waits for the open.
        o = IbOrder(action="SELL", totalQuantity=qty, orderType="MKT", tif="DAY",
                    orderRef=ref, account=self.account)
        self._trades[ref] = self.ib.placeOrder(self._contract(symbol), o)
        return str(o.orderId)

    def _trade(self, order_ref: str):
        if order_ref not in self._trades:
            self._index_open_trades()
        return self._trades[order_ref]

    def modify_stop(self, order_ref: str, new_stop: float) -> None:
        trade = self._trade(order_ref)
        trade.order.auxPrice = new_stop
        self.ib.placeOrder(trade.contract, trade.order)

    def modify_limit(self, order_ref: str, new_limit: float) -> None:
        trade = self._trade(order_ref)
        trade.order.lmtPrice = new_limit
        self.ib.placeOrder(trade.contract, trade.order)

    def cancel(self, order_ref: str) -> None:
        trade = self._trades.get(order_ref)
        if trade is None or trade.isDone():
            return
        self.ib.cancelOrder(trade.order)
        for _ in range(30):  # wait up to ~3s so a follow-up sell cannot race the resting stop
            if trade.isDone():
                break
            self.ib.sleep(0.1)

    def cancel_all(self) -> None:
        self.ib.reqGlobalCancel()

    # ── environment ──────────────────────────────────────────────
    def managed_accounts(self) -> list[str]:
        return list(self.ib.managedAccounts())

    def server_time(self) -> datetime:
        return self.ib.reqCurrentTime().astimezone(ET)

    # ── events ───────────────────────────────────────────────────
    def on_fill(self, callback: FillCallback) -> None:
        self._fill_cbs.append(callback)

    def on_order_status(self, callback: StatusCallback) -> None:
        self._status_cbs.append(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        self._disconnect_cbs.append(callback)

    def _on_commission(self, trade, fill, report) -> None:
        # Fired once the commission is known, so the Fill we emit is complete.
        # A fill whose report never arrives is still picked up by reconcile().
        out = self._to_fill(fill)
        for cb in self._fill_cbs:
            cb(out)

    def _on_status(self, trade) -> None:
        ref = trade.order.orderRef
        if not ref:
            return
        status = _STATUS.get(trade.orderStatus.status, SUBMITTED)
        message = trade.log[-1].message if trade.log else ""
        for cb in self._status_cbs:
            cb(ref, status, message)

    def _on_error(self, req_id, code, message, contract) -> None:
        if code in _DELAYED_CODES:
            self._delayed = True
        if code == 201:  # order rejected
            for ref, trade in self._trades.items():
                if trade.order.orderId == req_id:
                    for cb in self._status_cbs:
                        cb(ref, REJECTED, message)

    def _on_disconnected(self) -> None:
        for cb in self._disconnect_cbs:
            cb()
