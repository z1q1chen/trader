"""Engine: the business logic behind each scheduled task.

The same engine runs live (IbkrBroker) and in backtests (SimBroker). It never
reads the wall clock: every method takes `now`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from .alerts import ERROR, WARN, Alerter
from .broker.base import Broker
from .calendar import EarningsCalendar, TradingCalendar
from .config import Config
from .execution import Executor
from .killswitch import halt_active, set_halt
from .models import ENTER, EXIT, NEXT_OPEN, NONE, Action
from .reconcile import ReconcileResult, reconcile
from .risk import RiskContext, RiskGate
from .store import Store
from .strategy import (
    BAR_MINUTES, INTRADAY, POST_CLOSE, StrategyInput, decide, inputs_hash,
)
from .watchlist import screen

log = logging.getLogger("trader.engine")

DAILY_LOOKBACK = 260


class Engine:
    def __init__(self, cfg: Config, broker: Broker, store: Store, executor: Executor,
                 gate: RiskGate, alerter: Alerter, calendar: TradingCalendar,
                 earnings: EarningsCalendar, universe: list[str]):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.executor = executor
        self.gate = gate
        self.alerter = alerter
        self.calendar = calendar
        self.earnings = earnings
        self.universe = sorted(set(universe))
        self.read_only = False       # set by startup checks or an unresolved reconcile diff
        self.blocked = False         # set while disconnected, cleared after a clean reconcile
        self.data_realtime = True
        self.halt_file = cfg.halt_file
        self._daily: dict[str, pd.DataFrame] = {}
        self._daily_date: date | None = None
        self._last_reasons: dict[str, tuple] = {}
        self._alerted: set[str] = set()
        self._warned_no_earnings = False

    # ── day start / scan ─────────────────────────────────────────
    def start_day(self, today: date) -> None:
        self.store.start_day(today, self.broker.account().net_liq)
        self._last_reasons.clear()
        self._alerted.clear()

    def scan(self, today: date) -> int:
        """Pre-market: pull daily bars, compute indicators, build today's watchlist."""
        symbols = self.universe + [self.cfg.params.regime_symbol]
        daily = {}
        for sym in symbols:
            df = self.broker.bars(sym, "1d", DAILY_LOOKBACK)
            if df is not None and not df.empty:
                daily[sym] = df
        self._daily, self._daily_date = daily, today
        dates = {s: d for s in daily if (d := self.earnings.next_date(s, today))}
        items = screen(daily, today, self.cfg.params, dates)
        self.store.save_watchlist(today, items)
        if not self.earnings:
            if not self._warned_no_earnings:
                self._warned_no_earnings = True
                self.alerter.send(WARN, "no_earnings_data",
                                  "no earnings calendar loaded: the earnings window is not enforced")
        log.info("watchlist %s: %s", today, [w.symbol for w in items])
        return len(items)

    def _daily_for(self, symbols: list[str], today: date, refresh: bool) -> dict[str, pd.DataFrame]:
        if self._daily_date != today:
            self._daily, self._daily_date = {}, today
        out = {}
        for sym in symbols:
            if refresh or sym not in self._daily:
                df = self.broker.bars(sym, "1d", DAILY_LOOKBACK)
                if df is None or df.empty:
                    continue
                self._daily[sym] = df
            out[sym] = self._daily[sym]
        return out

    # ── decision pipeline ────────────────────────────────────────
    def run_phase(self, now: datetime, phase: str) -> list[Action]:
        today = now.date()
        params = self.cfg.params
        account = self.broker.account()
        positions = self.broker.positions()
        watch = tuple(self.store.get_watchlist(today))
        held = [p.symbol for p in positions if p.qty > 0]
        symbols = sorted({w.symbol for w in watch} | set(held) | {params.regime_symbol})
        daily = self._daily_for(symbols, today, refresh=(phase == POST_CLOSE))
        intraday = {}
        if phase != POST_CLOSE:
            for w in watch:
                df = self.broker.bars(w.symbol, "5m", 100)
                if df is not None and not df.empty:
                    intraday[w.symbol] = df
        to_earnings = {}
        for sym in symbols:
            n = self.earnings.sessions_to(sym, today, self.calendar)
            if n is not None:
                to_earnings[sym] = n
        bars = StrategyInput(now=now, phase=phase, daily=daily, intraday=intraday,
                             watchlist=watch, sessions_to_earnings=to_earnings)
        digest = inputs_hash(bars, account, positions, params)
        actions = decide(bars, account, positions, params)
        for action in actions:
            self._dispatch(action, now, phase, bars, digest)
        return actions

    def _dispatch(self, action: Action, now: datetime, phase: str, bars: StrategyInput,
                  digest: str) -> None:
        today = now.date()
        if action.kind == NONE:
            # Intraday checks repeat every bar: only record a symbol when its reasons change.
            key = (phase, action.symbol)
            if phase != INTRADAY or self._last_reasons.get(key) != action.reasons:
                self._last_reasons[key] = action.reasons
                self.store.add_decision(now, action.symbol, NONE, action.setup, action.reasons, digest)
            return
        if action.kind == EXIT:
            self.store.add_decision(now, action.symbol, EXIT, action.reason, action.reasons, digest)
            if action.when == NEXT_OPEN:
                self.executor.queue_exit(action.symbol, action.reason)
            else:
                self.executor.exit_now(action.symbol, action.reason, today)
            return
        if action.kind == ENTER:
            self._enter(action, now, bars, digest)

    def _enter(self, action: Action, now: datetime, bars: StrategyInput, digest: str) -> None:
        today, plan = now.date(), action.plan
        intra = bars.intraday.get(plan.symbol)
        last_price, last_bar_end = 0.0, None
        if intra is not None and not intra.empty:
            last_price = float(intra["close"].iloc[-1])
            last_bar_end = (intra.index[-1] + timedelta(minutes=BAR_MINUTES)).to_pydatetime()
        account = self.broker.account()  # fresh equity and cash right before sizing is checked
        ctx = RiskContext(
            now=now, account=account,
            day_start_net_liq=self.store.start_day(today, account.net_liq),
            peak_net_liq=max(self.store.peak_net_liq(), account.net_liq),
            open_positions=len(self.store.live_trades()),
            orders_today=self.store.submissions_on(today),
            last_price=last_price, last_bar_end=last_bar_end,
            data_realtime=self.data_realtime,
            sessions_to_earnings=bars.sessions_to_earnings.get(plan.symbol),
            halted=halt_active(self.halt_file),
            trading_enabled=(not self.read_only and not self.blocked
                             and self.broker.is_connected()),
        )
        verdict = self.gate.check(plan, ctx)
        self.store.add_decision(now, plan.symbol, ENTER if verdict.approved else "REJECT",
                                plan.setup, (*action.reasons, *verdict.reasons), digest)
        if verdict.halt:
            set_halt(self.halt_file, "max_drawdown", now)
            self._alert_once("max_drawdown", ERROR,
                             "drawdown limit hit: HALT file created, a human must remove it")
        elif "max_daily_loss" in verdict.reasons:
            self._alert_once("max_daily_loss", ERROR, "daily loss limit hit: no new positions today")
        elif "max_orders_per_day" in verdict.reasons:
            self._alert_once("max_orders_per_day", ERROR, "daily order count limit hit")
        if verdict.approved:
            self.executor.submit_entry(verdict.plan, today)

    def _alert_once(self, key: str, level: str, message: str) -> None:
        if key not in self._alerted:
            self._alerted.add(key)
            self.alerter.send(level, key, message)

    # ── other tasks ──────────────────────────────────────────────
    def exits_open(self, now: datetime) -> int:
        """Submit the sell orders decided after yesterday's close."""
        return self.executor.submit_queued_exits(now.date())

    def reconcile(self) -> ReconcileResult:
        res = reconcile(self.broker, self.store, self.executor, self.alerter, self.executor.clock)
        if res.diffs:
            self.read_only = True
            self._alert_once("reconcile:" + "|".join(res.diffs), ERROR,
                             f"reconcile mismatch, read-only until restart: {res.diffs}")
        return res

    def snapshot_equity(self, today: date) -> None:
        acct = self.broker.account()
        self.store.snapshot_equity(today, acct.net_liq, acct.realized_pnl_today)
