"""Live application: wires the modules together and runs the scheduling loop."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Callable, Mapping

from . import scheduler as sch
from .alerts import ERROR, INFO, WARN, Alerter
from .broker.base import Broker
from .calendar import EarningsCalendar, TradingCalendar
from .config import Config
from .engine import Engine
from .execution import Executor
from .models import ET
from .risk import RiskGate
from .startup import StartupResult, run_startup_checks
from .store import Store
from .strategy import INTRADAY, OPEN_RANGE, POST_CLOSE, PRE_CLOSE

log = logging.getLogger("trader.app")

RECONCILE_EVERY = timedelta(minutes=5)
HEARTBEAT_EVERY = timedelta(seconds=30)
RECONNECT_EVERY = timedelta(seconds=10)
DISCONNECT_ALERT_AFTER = timedelta(minutes=1)


def load_universe(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return sorted({line.split("#")[0].strip().upper() for line in f} - {""})


class StartupFailed(RuntimeError):
    pass


class TradingApp:
    def __init__(self, cfg: Config, broker: Broker, *,
                 clock: Callable[[], datetime] | None = None,
                 env: Mapping[str, str] | None = None,
                 calendar: TradingCalendar | None = None,
                 earnings: EarningsCalendar | None = None,
                 universe: list[str] | None = None,
                 store: Store | None = None,
                 reviewer: Callable[[datetime], None] | None = None):
        self.cfg = cfg
        self.broker = broker
        self.clock = clock or (lambda: datetime.now(ET))
        self.env = os.environ if env is None else env
        self.calendar = calendar or TradingCalendar()
        self.store = store or Store(cfg.profile.db)
        self.alerter = Alerter(self.store, self.clock, cfg.alert_webhook_url, prefix=f"[{cfg.mode}] ")
        self.executor = Executor(broker, self.store, self.alerter, self.clock, cfg.params.target_r)
        self.engine = Engine(
            cfg, broker, self.store, self.executor, RiskGate(cfg.limits), self.alerter,
            self.calendar, earnings if earnings is not None else EarningsCalendar.from_csv(cfg.earnings_file),
            universe if universe is not None else load_universe(cfg.universe_file))
        self.scheduler = sch.Scheduler(self.calendar, cfg.params.opening_range_minutes)
        self.reviewer = reviewer
        broker.on_fill(self.executor.handle_fill)
        broker.on_order_status(self.executor.handle_order_status)
        broker.on_disconnect(self._on_disconnect)
        self._stop = False
        self._startup_day = None
        self._down_since: datetime | None = None
        self._down_alerted = False
        self._last_reconnect = datetime.min.replace(tzinfo=ET)
        self._last_reconcile = datetime.min.replace(tzinfo=ET)
        self._last_heartbeat = datetime.min.replace(tzinfo=ET)

    # ── startup ──────────────────────────────────────────────────
    def startup(self) -> StartupResult:
        now = self.clock()
        res = run_startup_checks(self.cfg, self.broker, self.store, self.executor, self.alerter,
                                 self.clock, self.env)
        if not res.ok:
            raise StartupFailed("; ".join(res.fatal))
        self.engine.read_only = bool(res.read_only)
        self.engine.data_realtime = "delayed_market_data" not in res.read_only
        self.engine.blocked = False
        self._startup_day = now.date()
        self._last_reconcile = now
        if self.calendar.is_session(now.date()):
            self.engine.start_day(now.date())
        return res

    # ── loop ─────────────────────────────────────────────────────
    def run(self) -> None:
        self.broker.connect()
        self.startup()
        log.info("running: mode=%s account=%s", self.cfg.mode, self.cfg.profile.account)
        while not self._stop:
            self.tick(self.clock())
            self.broker.sleep(1)

    def stop(self) -> None:
        self._stop = True

    def tick(self, now: datetime) -> None:
        if not self.broker.is_connected():
            self._offline(now)
            return
        if self._down_since is not None:
            self._back_online(now)
        run, skipped = self.scheduler.due(now)
        for task in skipped:
            self.alerter.send(WARN, "task_skipped", f"{task.name} scheduled {task.at:%H:%M} was too late to run")
        for task in run:
            self._run_task(task, now)
        if self.scheduler.in_session_window(now):
            if self.executor.needs_reconcile or now - self._last_reconcile >= RECONCILE_EVERY:
                self._guard("reconcile", self._reconcile, now)
            if now - self._last_heartbeat >= HEARTBEAT_EVERY:
                self._last_heartbeat = now
                try:
                    self.broker.server_time()
                except Exception as e:
                    log.warning("heartbeat failed: %s", e)
                    self._on_disconnect()

    def _reconcile(self, now: datetime) -> None:
        self._last_reconcile = now
        self.engine.reconcile()

    def _guard(self, name: str, fn: Callable, *args) -> None:
        """A failing task must never take the loop (and the stop management) down with it."""
        try:
            fn(*args)
        except StartupFailed:
            raise
        except Exception as e:
            log.exception("task %s failed", name)
            self.alerter.send(ERROR, "task_failed", f"{name}: {e}")

    def _run_task(self, task: sch.Task, now: datetime) -> None:
        log.info("task %s", task.name)
        eng, today = self.engine, now.date()
        if task.name == sch.STARTUP:
            if self._startup_day != today:
                self.startup()
        elif task.name == sch.SCAN:
            self._guard(task.name, eng.scan, today)
        elif task.name == sch.EXITS_OPEN:
            self._guard(task.name, eng.exits_open, now)
        elif task.name == sch.OPEN_RANGE:
            self._guard(task.name, eng.run_phase, now, OPEN_RANGE)
        elif task.name == sch.INTRADAY:
            self._guard(task.name, eng.run_phase, now, INTRADAY)
        elif task.name == sch.PRE_CLOSE:
            self._guard(task.name, eng.run_phase, now, PRE_CLOSE)
        elif task.name == sch.POST_CLOSE:
            self._guard("reconcile", self._reconcile, now)
            self._guard(task.name, eng.run_phase, now, POST_CLOSE)
            self._guard("equity", eng.snapshot_equity, today)
        elif task.name == sch.REVIEW and self.reviewer is not None:
            self._guard(task.name, self.reviewer, now)

    # ── connection loss ──────────────────────────────────────────
    def _on_disconnect(self) -> None:
        # No new orders until we are back and fully reconciled. Stops stay live at the broker.
        self.engine.blocked = True
        if self._down_since is None:
            self._down_since = self.clock()
            self._down_alerted = False
            log.warning("disconnected from broker")

    def _offline(self, now: datetime) -> None:
        self._on_disconnect()
        if not self._down_alerted and now - self._down_since >= DISCONNECT_ALERT_AFTER:
            self._down_alerted = True
            self.alerter.send(ERROR, "disconnected", f"no broker connection since {self._down_since:%H:%M:%S}")
        if now - self._last_reconnect >= RECONNECT_EVERY:
            self._last_reconnect = now
            try:
                self.broker.connect()
            except Exception as e:
                log.info("reconnect failed: %s", e)

    def _back_online(self, now: datetime) -> None:
        down_for = now - self._down_since
        self._down_since = None
        self._last_reconcile = now
        res = self.engine.reconcile()
        if res.clean:
            self.engine.blocked = False
        self.alerter.send(INFO if res.clean else ERROR, "reconnected",
                          f"back after {down_for.total_seconds():.0f}s; reconcile clean={res.clean}")
