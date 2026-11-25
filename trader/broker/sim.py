"""SimBroker: matches orders against historical 5-minute bars for backtests.

Fill model (deliberately pessimistic):
- buy stop-limit triggers when the bar trades at the stop; it fills at the trigger
  (or the open, on a gap) plus slippage, and not at all above its limit
- sell stops fill at the stop minus slippage, or at the open when the bar gaps through
- when a stop and a target could both fill in one bar, the stop goes first
- on the bar that opened the position the stop only fills if the bar closes below
  it, and the target cannot fill
- market orders fill at the next bar's open
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from ..models import (
    CANCELLED, ET, FILLED, SUBMITTED, AccountState, Fill, Order, Position, TradePlan,
)
from .base import FillCallback, StatusCallback, bracket_legs

BAR = timedelta(minutes=5)


class HistoricalData:
    """Daily frames indexed by session date; 5-minute frames indexed by ET bar start."""

    def __init__(self, daily: dict[str, pd.DataFrame], intraday: dict[str, pd.DataFrame]):
        self.daily = daily
        self._intraday = intraday
        self._by_day: dict[str, dict[date, pd.DataFrame]] = {}

    @classmethod
    def from_dir(cls, root: str | Path) -> "HistoricalData":
        """Reads <root>/daily/SYM.csv and <root>/5min/SYM.csv (ts,open,high,low,close,volume)."""
        root = Path(root)
        daily, intraday = {}, {}
        for p in sorted((root / "daily").glob("*.csv")):
            df = pd.read_csv(p, index_col=0, parse_dates=True)
            df.index = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
            daily[p.stem.upper()] = df.sort_index()
        for p in sorted((root / "5min").glob("*.csv")):
            df = pd.read_csv(p, index_col=0)
            if _has_offset(df.index):
                idx = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            else:
                idx = pd.to_datetime(df.index).tz_localize(ET)
            df.index = pd.DatetimeIndex(idx)
            intraday[p.stem.upper()] = df.sort_index()
        return cls(daily, intraday)

    def symbols(self) -> list[str]:
        return sorted(self.daily)

    def day_bars(self, symbol: str, day: date) -> pd.DataFrame | None:
        if symbol not in self._by_day:
            df = self._intraday.get(symbol)
            self._by_day[symbol] = {} if df is None else {
                d: g for d, g in df.groupby(df.index.date)}
        return self._by_day[symbol].get(day)


def _has_offset(index) -> bool:
    """True when the timestamps carry a UTC offset; naive ones are taken as ET."""
    return bool(len(index)) and bool(re.search(r"(Z|[+-]\d\d:?\d\d)$", str(index[0])))


@dataclass
class _SimOrder:
    ref: str
    symbol: str
    side: str
    type: str
    qty: int
    stop_price: float | None
    limit_price: float | None
    tif: str
    parent_ref: str
    oca_group: str
    status: str = SUBMITTED
    active: bool = True
    triggered: bool = False
    filled_qty: int = 0
    entry_bar: datetime | None = None  # children: the bar their parent filled on


class SimBroker:
    def __init__(self, data: HistoricalData, cash: float = 10_000.0,
                 account_id: str = "DU0000000", slippage_pct: float = 0.05,
                 commission_per_share: float = 0.005, min_commission: float = 1.0):
        self.data = data
        self.cash = cash
        self.account_id = account_id
        self.slippage = slippage_pct / 100.0
        self.commission_per_share = commission_per_share
        self.min_commission = min_commission
        self.now: datetime | None = None
        self.session_close: datetime | None = None
        self._orders: dict[str, _SimOrder] = {}
        self._pos: dict[str, list] = {}  # symbol -> [qty, avg_cost]
        self._last: dict[str, float] = {}
        self._fills: list[Fill] = []
        self._realized_today = 0.0
        self._n = 0
        self._fill_cbs: list[FillCallback] = []
        self._status_cbs: list[StatusCallback] = []

    # ── clock (driven by the backtester) ─────────────────────────
    def begin_session(self, open_: datetime, close: datetime) -> None:
        self.session_close = close
        self.now = open_
        self._fills = []
        self._realized_today = 0.0

    def set_time(self, now: datetime) -> None:
        self.now = now

    def end_of_day(self) -> None:
        for o in list(self._orders.values()):
            if o.tif == "DAY" and o.status == SUBMITTED:
                self.cancel(o.ref)

    # ── account state ────────────────────────────────────────────
    def account(self) -> AccountState:
        value = sum(q * self._price(s, c) for s, (q, c) in self._pos.items())
        return AccountState(self.account_id, round(self.cash + value, 2), round(self.cash, 2),
                            round(self._realized_today, 2))

    def _price(self, symbol: str, fallback: float) -> float:
        return self._last.get(symbol, fallback)

    def positions(self) -> list[Position]:
        return [Position(s, q, c) for s, (q, c) in sorted(self._pos.items()) if q != 0]

    def open_orders(self) -> list[Order]:
        return [Order(o.ref, o.symbol, o.side, o.type, o.qty, o.status, o.stop_price,
                      o.limit_price, o.filled_qty, o.ref)
                for o in self._orders.values() if o.status == SUBMITTED]

    def recent_fills(self) -> list[Fill]:
        return list(self._fills)

    # ── market data ──────────────────────────────────────────────
    def bars(self, symbol: str, timeframe: str, lookback: int) -> pd.DataFrame:
        today = self.now.date()
        if timeframe == "1d":
            df = self.data.daily.get(symbol)
            if df is None:
                return pd.DataFrame()
            done = self.session_close is not None and self.now >= self.session_close
            cut = pd.Timestamp(today)
            df = df[df.index <= cut] if done else df[df.index < cut]
            return df.tail(lookback)
        df = self.data.day_bars(symbol, today)
        if df is None:
            return pd.DataFrame()
        return df[df.index + BAR <= self.now].tail(lookback)

    def subscribe_bars(self, symbol: str, timeframe: str, on_bar: Callable) -> None:
        pass  # backtests are stepped bar by bar

    # ── orders ───────────────────────────────────────────────────
    def place_bracket(self, plan: TradePlan) -> dict[str, str]:
        for leg in bracket_legs(plan):
            self._orders[leg.ref] = _SimOrder(
                leg.ref, plan.symbol, leg.side, leg.type, leg.qty, leg.stop_price,
                leg.limit_price, leg.tif, leg.parent_ref, leg.oca_group,
                active=not leg.parent_ref)
        return {leg.ref: leg.ref for leg in bracket_legs(plan)}

    def place_stop(self, ref: str, symbol: str, qty: int, stop_price: float) -> str:
        self._orders[ref] = _SimOrder(ref, symbol, "SELL", "STP", qty, stop_price, None, "GTC", "", "")
        return ref

    def modify_stop(self, order_ref: str, new_stop: float) -> None:
        self._orders[order_ref].stop_price = new_stop

    def modify_limit(self, order_ref: str, new_limit: float) -> None:
        self._orders[order_ref].limit_price = new_limit

    def close_position(self, symbol: str, qty: int, ref: str) -> str:
        self._orders[ref] = _SimOrder(ref, symbol, "SELL", "MKT", qty, None, None, "DAY", "", "")
        return ref

    def cancel(self, order_ref: str) -> None:
        o = self._orders.get(order_ref)
        if o is None or o.status != SUBMITTED:
            return
        o.status = CANCELLED
        self._emit_status(o.ref, CANCELLED, "")
        if o.filled_qty == 0:
            for child in list(self._orders.values()):
                if child.parent_ref == o.ref and not child.active:
                    self.cancel(child.ref)

    def cancel_all(self) -> None:
        for o in list(self._orders.values()):
            self.cancel(o.ref)

    # ── events ───────────────────────────────────────────────────
    def on_fill(self, callback: FillCallback) -> None:
        self._fill_cbs.append(callback)

    def on_order_status(self, callback: StatusCallback) -> None:
        self._status_cbs.append(callback)

    def on_disconnect(self, callback: Callable[[], None]) -> None:
        pass

    def _emit_status(self, ref: str, status: str, message: str) -> None:
        for cb in self._status_cbs:
            cb(ref, status, message)

    # ── environment ──────────────────────────────────────────────
    def connect(self) -> None:
        pass

    def managed_accounts(self) -> list[str]:
        return [self.account_id]

    def server_time(self) -> datetime:
        return self.now

    def market_data_realtime(self) -> bool:
        return True

    def is_connected(self) -> bool:
        return True

    def sleep(self, seconds: float) -> None:
        pass

    # ── matching ─────────────────────────────────────────────────
    def step(self, bar_start: datetime) -> None:
        """Match every working order against the bar that starts at `bar_start`."""
        symbols = sorted({o.symbol for o in self._orders.values() if o.status == SUBMITTED}
                         | set(self._pos))
        rank = {"MKT": 0, "STP LMT": 1, "STP": 2, "LMT": 3}
        for sym in symbols:
            day = self.data.day_bars(sym, bar_start.date())
            if day is None or bar_start not in day.index:
                continue
            bar = day.loc[bar_start]
            o_, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
            # Buys first so a breakout and its same-bar stop are seen in the right order;
            # among sells: market, then stops, then targets.
            todo = sorted((o for o in self._orders.values() if o.symbol == sym),
                          key=lambda o: (0 if o.side == "BUY" else 1, rank[o.type], o.ref))
            for order in todo:
                if order.status != SUBMITTED or not order.active:
                    continue
                price = self._match(order, bar_start, o_, h, l, c)
                if price is not None:
                    self._fill(order, round(price, 4), bar_start)
            self._last[sym] = c

    def _match(self, o: _SimOrder, bar_start: datetime, open_: float, high: float,
               low: float, close: float) -> float | None:
        s = self.slippage
        if o.side == "BUY":
            if o.type == "STP LMT" and not o.triggered:
                if high < o.stop_price:
                    return None
                o.triggered = True
                want = max(o.stop_price, open_) * (1 + s)
                if want <= o.limit_price:
                    return want
                return o.limit_price if low <= o.limit_price else None
            # plain limit, or a stop-limit that triggered on an earlier bar
            if open_ <= o.limit_price:
                return min(open_ * (1 + s), o.limit_price)
            return o.limit_price if low <= o.limit_price else None
        if o.type == "MKT":
            return open_ * (1 - s)
        if o.type == "STP":
            if o.entry_bar == bar_start:
                return o.stop_price * (1 - s) if close <= o.stop_price else None
            if open_ <= o.stop_price:
                return open_ * (1 - s)
            return o.stop_price * (1 - s) if low <= o.stop_price else None
        if o.type == "LMT":
            if o.entry_bar == bar_start:
                return None
            if open_ >= o.limit_price:
                return open_
            return o.limit_price if high >= o.limit_price else None
        return None

    def _fill(self, o: _SimOrder, price: float, bar_start: datetime) -> None:
        qty = o.qty
        if o.side == "SELL":
            qty = min(qty, self._pos.get(o.symbol, [0, 0.0])[0])
            if qty <= 0:
                self.cancel(o.ref)
                return
        commission = max(self.min_commission, self.commission_per_share * qty)
        pos = self._pos.setdefault(o.symbol, [0, 0.0])
        if o.side == "BUY":
            pos[1] = (pos[0] * pos[1] + qty * price) / (pos[0] + qty)
            pos[0] += qty
            self.cash -= qty * price + commission
        else:
            self._realized_today += (price - pos[1]) * qty - commission
            pos[0] -= qty
            self.cash += qty * price - commission
            if pos[0] == 0:
                del self._pos[o.symbol]
        o.filled_qty, o.status = qty, FILLED
        self._last[o.symbol] = price
        self._n += 1
        fill = Fill(f"SIM{self._n:08d}", o.ref, o.symbol, o.side, qty, price, round(commission, 4),
                    bar_start + BAR)
        self._fills.append(fill)
        # Children go live when their parent fills; one-cancels-other siblings are pulled.
        for other in list(self._orders.values()):
            if other.parent_ref == o.ref and not other.active:
                other.active, other.entry_bar = True, bar_start
            elif (o.oca_group and other.oca_group == o.oca_group and other.ref != o.ref
                  and other.status == SUBMITTED):
                self.cancel(other.ref)
        for cb in self._fill_cbs:
            cb(fill)
