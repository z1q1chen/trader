"""Scheduler, live loop, IBKR order construction and the LLM sidecar."""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from trader import scheduler as sch
from trader.app import StartupFailed, TradingApp, load_universe
from trader.broker.ibkr import build_ib_orders
from trader.calendar import EarningsCalendar, TradingCalendar
from trader.llm import sidecar
from trader.models import ET, WatchItem
from trader.store import Store

from .conftest import FakeBroker, make_cfg, make_plan

CAL = TradingCalendar()


def at(hh, mm, ss=0, day=25):
    return datetime(2026, 11, day, hh, mm, ss, tzinfo=ET)


# ── scheduler ────────────────────────────────────────────────────

def test_regular_day_schedule_matches_the_design():
    tasks = sch.Scheduler(CAL).tasks_for(date(2026, 11, 25))
    first = {}
    for t in tasks:
        first.setdefault(t.name, t.at.strftime("%H:%M"))
    assert first == {"startup": "08:30", "scan": "08:45", "exits_open": "09:25",
                     "open_range": "09:35", "intraday": "09:40", "pre_close": "15:50",
                     "post_close": "16:05", "review": "16:15"}
    intraday = [t.at for t in tasks if t.name == "intraday"]
    assert intraday[-1].strftime("%H:%M") == "15:45" and len(intraday) == 74


def test_half_day_moves_the_closing_tasks_and_holidays_have_none():
    s = sch.Scheduler(CAL)
    times = {t.name: t.at.strftime("%H:%M") for t in s.tasks_for(date(2026, 11, 27))}
    assert (times["pre_close"], times["post_close"], times["review"]) == ("12:50", "13:05", "13:15")
    assert times["intraday"] == "12:45"                    # the last intraday check
    assert s.tasks_for(date(2026, 11, 26)) == [] and s.tasks_for(date(2026, 11, 28)) == []


def test_tasks_fire_once_and_late_ones_are_skipped():
    s = sch.Scheduler(CAL)
    assert s.due(at(8, 0)) == ([], [])
    run, skipped = s.due(at(8, 30, 1))
    assert [t.name for t in run] == ["startup"] and skipped == []
    assert s.due(at(8, 30, 2)) == ([], [])
    # the process was stuck until 09:50: the opening-range entry must not be placed late
    run, skipped = s.due(at(9, 50))
    assert [t.name for t in run] == ["scan", "exits_open", "intraday"]
    assert [t.name for t in skipped] == ["open_range", "intraday", "intraday"]
    assert s.in_session_window(at(12, 0)) and not s.in_session_window(at(7, 0))
    assert not s.in_session_window(at(12, 0, day=26))


# ── live loop ────────────────────────────────────────────────────

class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_app(cfg=None, env=None):
    broker = FakeBroker()
    clock = Clock(at(8, 0))
    broker.now = clock.now
    app = TradingApp(cfg or make_cfg(halt_file="__no_halt__"), broker, clock=clock, env=env or {},
                     calendar=CAL, earnings=EarningsCalendar(), universe=["NVDA"], store=Store(":memory:"))
    return app, broker, clock


def advance(app, broker, clock, now):
    clock.now = broker.now = now
    app.tick(now)


def test_startup_failure_stops_the_app():
    app, broker, _ = make_app()
    broker.accounts = ["DU0000001"]
    with pytest.raises(StartupFailed, match="account_mismatch"):
        app.startup()


def test_read_only_startup_blocks_entries_but_keeps_running():
    app, broker, _ = make_app()
    broker.realtime = False
    app.startup()
    assert app.engine.read_only and app.engine.data_realtime is False


def test_disconnect_blocks_orders_until_reconnected_and_reconciled():
    app, broker, clock = make_app()
    app.startup()
    assert not app.engine.blocked
    broker.connected = False
    broker.connect = lambda: None                       # gateway still down
    advance(app, broker, clock, at(10, 0))
    assert app.engine.blocked and not app.store.events("disconnected")
    advance(app, broker, clock, at(10, 1, 5))           # down for more than a minute
    assert len(app.store.events("disconnected")) == 1
    advance(app, broker, clock, at(10, 2))
    assert len(app.store.events("disconnected")) == 1   # alerted once
    broker.connected = True
    advance(app, broker, clock, at(10, 3))
    assert not app.engine.blocked and app.store.events("reconnected")


def test_reconnect_with_a_mismatch_stays_blocked():
    app, broker, clock = make_app()
    app.startup()
    broker.connected = False
    advance(app, broker, clock, at(10, 0))
    broker.connected = True
    broker.pos["TSLA"] = 5                              # appeared while we were away
    advance(app, broker, clock, at(10, 0, 30))
    assert app.engine.blocked and app.engine.read_only


def test_a_failing_task_alerts_and_the_loop_survives():
    app, broker, clock = make_app()
    app.startup()

    def boom(today):
        raise RuntimeError("data feed exploded")

    app.engine.scan = boom
    advance(app, broker, clock, at(8, 45, 1))
    assert "data feed exploded" in app.store.events("task_failed")[0]["payload"]
    advance(app, broker, clock, at(8, 46))              # still ticking


def test_reconcile_runs_on_a_timer_and_on_demand():
    app, broker, clock = make_app()
    app.startup()
    seen = []
    app.engine.reconcile = lambda: seen.append(clock.now)
    app._last_reconcile = at(8, 30)
    advance(app, broker, clock, at(8, 31))
    assert seen == []
    app.executor.needs_reconcile = True
    advance(app, broker, clock, at(8, 32))
    assert len(seen) == 1
    app.executor.needs_reconcile = False
    advance(app, broker, clock, at(8, 36))
    assert len(seen) == 1                               # 4 minutes since the last one
    advance(app, broker, clock, at(8, 37, 1))
    assert len(seen) == 2                               # 5 minutes: timer fires


def test_halt_file_stops_new_entries(tmp_path):
    from trader.risk import RiskContext
    from trader.models import AccountState
    halt = tmp_path / "HALT"
    app, broker, clock = make_app(make_cfg(halt_file=str(halt)))
    app.startup()
    halt.write_text("stop")
    from trader.killswitch import halt_active
    ctx = RiskContext(now=clock.now, account=AccountState("DU1234567", 10_000, 10_000),
                      day_start_net_liq=10_000, peak_net_liq=10_000, open_positions=0, orders_today=0,
                      last_price=99.8, last_bar_end=clock.now, halted=halt_active(app.engine.halt_file))
    assert app.engine.gate.check(make_plan(qty=15, qty_a=5), ctx).reasons == ("halt_file_present",)


def test_load_universe(tmp_path):
    p = tmp_path / "universe.txt"
    p.write_text("nvda\n# comment\nAMD  # chips\n\nNVDA\n")
    assert load_universe(str(p)) == ["AMD", "NVDA"]
    assert load_universe(str(tmp_path / "missing.txt")) == []


# ── IBKR order construction ──────────────────────────────────────

def test_ib_bracket_orders_transmit_as_one_group():
    ids = iter(range(100, 200))
    built = build_ib_orders(make_plan(qty=30, qty_a=10), "DU1234567", lambda: next(ids))
    by_role = {leg.role: o for leg, o in built}
    assert [o.transmit for _, o in built] == [False, False, True, False, True]
    a, b = by_role["A_PARENT"], by_role["B_PARENT"]
    assert (a.orderType, a.action, a.auxPrice, a.lmtPrice, a.tif) == ("STP LMT", "BUY", 100.0, 100.3, "DAY")
    assert (a.totalQuantity, b.totalQuantity) == (10, 20)
    assert by_role["A_TARGET"].parentId == by_role["A_STOP"].parentId == a.orderId
    assert by_role["B_STOP"].parentId == b.orderId != a.orderId
    assert by_role["A_TARGET"].ocaGroup == by_role["A_STOP"].ocaGroup != ""
    stop = by_role["B_STOP"]
    assert (stop.orderType, stop.action, stop.auxPrice, stop.tif, stop.totalQuantity) == ("STP", "SELL", 99.0, "GTC", 20)
    assert (by_role["A_TARGET"].orderType, by_role["A_TARGET"].lmtPrice) == ("LMT", 103.0)
    assert all(o.account == "DU1234567" and o.orderRef == leg.ref and not o.outsideRth for leg, o in built)


# ── LLM sidecar ──────────────────────────────────────────────────

def test_sidecar_reads_through_a_read_only_connection(tmp_path):
    db = tmp_path / "state.db"
    Store(db).close()
    con = sidecar.open_readonly(str(db))
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO events (ts, level, kind, payload) VALUES ('t', 'INFO', 'k', '{}')")
    con.close()


def test_review_without_an_llm_is_a_plain_statistics_report(tmp_path, rig):
    db = tmp_path / "state.db"
    store = Store(db)
    day = date(2026, 11, 25)
    store.save_watchlist(day, [WatchItem(day, "NVDA", 5.0, 1, 1, 1)])
    store.add_decision(at(9, 35), "NVDA", "ENTER", "A_BREAKOUT", ("ok",), "abc")
    store.start_day(day, 10_000.0)
    store.snapshot_equity(day, 10_050.0, 50.0)
    store.add_event(at(10, 0), "ERROR", "order_rejected", {"message": "x"})
    store.close()
    path = sidecar.daily_review(str(db), day, str(tmp_path / "reports"), "claude-opus-5-5", use_llm=False)
    text = path.read_text(encoding="utf-8")
    assert path.name == "2026-11-25.md" and "NVDA" in text and "ENTER 1" in text
    assert "+50.00" in text and "order_rejected" in text


def test_review_survives_an_unavailable_llm(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    Store(db).close()
    monkeypatch.setattr(sidecar, "_ask", lambda *a: None)
    path = sidecar.daily_review(str(db), date(2026, 11, 25), str(tmp_path / "r"), "claude-opus-5-5")
    assert path.exists() and "说明" not in path.read_text(encoding="utf-8")


def test_theme_tags_only_touch_watchlist_symbols(monkeypatch):
    store = Store(":memory:")
    day = date(2026, 11, 25)
    store.save_watchlist(day, [WatchItem(day, "NVDA", 5.0, 1, 1, 1), WatchItem(day, "OKLO", 8.0, 1, 1, 1)])
    monkeypatch.setattr(sidecar, "_ask", lambda *a: "NVDA: AI semiconductors\n- oklo: nuclear power\nTSLA: EVs\nbuy NVDA now")
    assert sidecar.tag_themes(store, day, "m") == {"NVDA": "AI semiconductors", "OKLO": "nuclear power"}
    assert {w.symbol: w.theme for w in store.get_watchlist(day)} == {"NVDA": "AI semiconductors", "OKLO": "nuclear power"}
    assert store.live_trades() == []                    # labels are all it can change
