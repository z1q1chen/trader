"""Backtest: drives the real Engine, risk gate, executor and store with a SimBroker."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta

from .alerts import Alerter
from .broker.sim import BAR, HistoricalData, SimBroker
from .calendar import EarningsCalendar, TradingCalendar
from .config import Config
from .engine import Engine
from .execution import Executor
from .models import T_CLOSED
from .risk import RiskGate
from .store import Store
from .strategy import INTRADAY, OPEN_RANGE, POST_CLOSE, PRE_CLOSE


@dataclass
class BacktestResult:
    start_equity: float
    end_equity: float
    trades: list[dict] = field(default_factory=list)
    equity: list[dict] = field(default_factory=list)

    def stats(self) -> dict:
        closed = [t for t in self.trades if t["status"] == T_CLOSED and t["r_multiple"] is not None]
        rs = [t["r_multiple"] for t in closed]
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        peak, max_dd = self.start_equity, 0.0
        for row in self.equity:
            peak = max(peak, row["net_liq"])
            max_dd = max(max_dd, (peak - row["net_liq"]) / peak * 100 if peak else 0.0)
        streak = worst = 0
        for r in rs:
            streak = streak + 1 if r <= 0 else 0
            worst = max(worst, streak)
        n = len(rs)
        return {
            "trades": n,
            "win_rate": round(len(wins) / n, 4) if n else 0.0,
            "avg_win_r": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss_r": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "expectancy_r": round(sum(rs) / n, 4) if n else 0.0,
            "net_pnl": round(sum(t["realized_pnl"] - t["commission"] for t in closed), 2),
            "commission": round(sum(t["commission"] for t in self.trades), 2),
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.end_equity, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "max_losing_streak": worst,
        }


def run_backtest(cfg: Config, data: HistoricalData, start: date, end: date, *,
                 cash: float = 10_000.0, db_path: str = ":memory:",
                 earnings: EarningsCalendar | None = None,
                 calendar: TradingCalendar | None = None,
                 slippage_pct: float = 0.05, commission_per_share: float = 0.005,
                 min_commission: float = 1.0, intraday_checks: bool = False,
                 store: Store | None = None) -> BacktestResult:
    """Replay every session from start to end. Deterministic: same inputs, same trades."""
    calendar = calendar or TradingCalendar()
    earnings = earnings or EarningsCalendar()
    store = store or Store(db_path)
    sim = SimBroker(data, cash=cash, account_id=cfg.profile.account, slippage_pct=slippage_pct,
                    commission_per_share=commission_per_share, min_commission=min_commission)
    clock = lambda: sim.now  # noqa: E731
    alerter = Alerter(store, clock)
    executor = Executor(sim, store, alerter, clock, target_r=cfg.params.target_r)
    sim.on_fill(executor.handle_fill)
    sim.on_order_status(executor.handle_order_status)
    universe = [s for s in data.symbols() if s != cfg.params.regime_symbol]
    engine = Engine(cfg, sim, store, executor, RiskGate(cfg.limits), alerter, calendar,
                    earnings, universe)
    or_delta = timedelta(minutes=cfg.params.opening_range_minutes)
    # The drawdown breaker must stop the simulated run without touching the real HALT file.
    halt_dir = tempfile.TemporaryDirectory()
    engine.halt_file = os.path.join(halt_dir.name, "HALT")

    for day in calendar.sessions(start, end):
        open_, close = calendar.session(day)
        sim.begin_session(open_, close)
        sim.set_time(open_ - timedelta(minutes=45))
        engine.start_day(day)
        engine.scan(day)
        sim.set_time(open_ - timedelta(minutes=5))
        engine.exits_open(sim.now)

        t = open_
        while t < close:
            sim.step(t)
            t += BAR
            sim.set_time(t)
            if executor.needs_reconcile:
                engine.reconcile()
            if t == open_ + or_delta:
                engine.run_phase(t, OPEN_RANGE)
            elif t == close - timedelta(minutes=10):
                engine.run_phase(t, PRE_CLOSE)
            elif intraday_checks and open_ + or_delta < t <= close - timedelta(minutes=15):
                engine.run_phase(t, INTRADAY)

        sim.end_of_day()
        sim.set_time(close + timedelta(minutes=5))
        engine.reconcile()
        engine.run_phase(sim.now, POST_CLOSE)
        engine.snapshot_equity(day)

    halt_dir.cleanup()
    return BacktestResult(
        start_equity=cash, end_equity=sim.account().net_liq,
        trades=store.trades_between(start, end), equity=store.equity_curve())
