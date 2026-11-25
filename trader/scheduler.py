"""Daily schedule, built from the session's open and close so half days shift correctly.

Regular day (ET): 08:30 startup, 08:45 scan, 09:25 queued exits, 09:35 opening
range, every 5 minutes to 15:45 intraday checks, 15:50 pullback entries,
16:05 post-close, 16:15 review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .calendar import TradingCalendar

STARTUP, SCAN, EXITS_OPEN = "startup", "scan", "exits_open"
OPEN_RANGE, INTRADAY, PRE_CLOSE = "open_range", "intraday", "pre_close"
POST_CLOSE, REVIEW = "post_close", "review"


@dataclass(frozen=True)
class Task:
    name: str
    at: datetime
    grace: timedelta  # a task that is later than this is skipped, not run late


def build_schedule(open_: datetime, close: datetime, or_minutes: int = 5) -> list[Task]:
    m = lambda n: timedelta(minutes=n)  # noqa: E731
    tasks = [
        Task(STARTUP, open_ - m(60), close - open_ + m(60)),
        Task(SCAN, open_ - m(45), close - open_ + m(30)),
        Task(EXITS_OPEN, open_ - m(5), close - open_),
        Task(OPEN_RANGE, open_ + m(or_minutes), m(3)),
    ]
    t = open_ + m(or_minutes + 5)
    while t <= close - m(15):
        tasks.append(Task(INTRADAY, t, m(4)))
        t += m(5)
    tasks += [
        Task(PRE_CLOSE, close - m(10), m(5)),
        Task(POST_CLOSE, close + m(5), timedelta(hours=6)),
        Task(REVIEW, close + m(15), timedelta(hours=6)),
    ]
    return tasks


class Scheduler:
    def __init__(self, calendar: TradingCalendar, or_minutes: int = 5):
        self._calendar = calendar
        self._or_minutes = or_minutes
        self._day: date | None = None
        self._tasks: list[Task] = []
        self._done: set[tuple[str, datetime]] = set()

    def tasks_for(self, day: date) -> list[Task]:
        session = self._calendar.session(day)
        return [] if session is None else build_schedule(*session, self._or_minutes)

    def due(self, now: datetime) -> tuple[list[Task], list[Task]]:
        """Tasks to run now and tasks skipped because they are too late. Each fires once."""
        if now.date() != self._day:
            self._day, self._tasks, self._done = now.date(), self.tasks_for(now.date()), set()
        run, skipped = [], []
        for task in self._tasks:
            key = (task.name, task.at)
            if key in self._done or task.at > now:
                continue
            self._done.add(key)
            (run if now <= task.at + task.grace else skipped).append(task)
        return run, skipped

    def in_session_window(self, now: datetime) -> bool:
        """True from the startup task until six hours after the close."""
        if now.date() != self._day:
            self._day, self._tasks, self._done = now.date(), self.tasks_for(now.date()), set()
        if not self._tasks:
            return False
        return self._tasks[0].at <= now <= self._tasks[-1].at + self._tasks[-1].grace
