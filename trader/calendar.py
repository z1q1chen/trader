"""Trading calendar (NYSE sessions, half days) and earnings dates."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .models import ET


class TradingCalendar:
    def __init__(self, name: str = "XNYS"):
        import exchange_calendars as xcals

        self._cal = xcals.get_calendar(name)

    def is_session(self, d: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(d)))

    def session(self, d: date) -> tuple[datetime, datetime] | None:
        """(open, close) in ET for a trading day, or None when the market is shut."""
        if not self.is_session(d):
            return None
        ts = pd.Timestamp(d)
        o = self._cal.session_open(ts).tz_convert(ET).to_pydatetime()
        c = self._cal.session_close(ts).tz_convert(ET).to_pydatetime()
        return o, c

    def sessions(self, start: date, end: date) -> list[date]:
        return [s.date() for s in self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]

    def sessions_until(self, today: date, target: date) -> int:
        """Trading sessions from today to target: 0 = today, 1 = next session."""
        if target <= today:
            return 0
        return max(len(self.sessions(today + timedelta(days=1), target)), 1)


class EarningsCalendar:
    """Earnings dates from a CSV with `symbol,date` rows (ISO dates)."""

    def __init__(self, dates: dict[str, list[date]] | None = None):
        self._dates = {k.upper(): sorted(v) for k, v in (dates or {}).items()}

    @classmethod
    def from_csv(cls, path: str | Path) -> "EarningsCalendar":
        p = Path(path)
        if not p.exists():
            return cls()
        out: dict[str, list[date]] = {}
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out.setdefault(row["symbol"].strip().upper(), []).append(
                    date.fromisoformat(row["date"].strip()))
        return cls(out)

    def __bool__(self) -> bool:
        return bool(self._dates)

    def next_date(self, symbol: str, today: date) -> date | None:
        for d in self._dates.get(symbol.upper(), []):
            if d >= today:
                return d
        return None

    def sessions_to(self, symbol: str, today: date, cal: TradingCalendar) -> int | None:
        """Sessions until the next earnings date; None when no date is known."""
        nxt = self.next_date(symbol, today)
        return None if nxt is None else cal.sessions_until(today, nxt)
