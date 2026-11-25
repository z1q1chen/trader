from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest

from trader.backtest import run_backtest
from trader.broker.sim import HistoricalData, SimBroker
from trader.calendar import EarningsCalendar, TradingCalendar
from trader.models import ET
from trader.store import Store

from .conftest import make_cfg, make_plan
from .synth import bars_5m, market

DAY, NEXT = date(2026, 11, 24), date(2026, 11, 25)
T = "20261125-NVDA"


def sim_with(rows, next_rows=None, **kw):
    frames = [bars_5m(DAY, rows)] + ([bars_5m(NEXT, next_rows)] if next_rows else [])
    sim = SimBroker(HistoricalData({}, {"NVDA": pd.concat(frames)}), cash=10_000.0,
                    slippage_pct=0.0, commission_per_share=0.0, min_commission=0.0, **kw)
    sim.fills_seen, sim.statuses = [], []
    sim.on_fill(sim.fills_seen.append)
    sim.on_order_status(lambda ref, status, msg: sim.statuses.append((ref, status)))
    sim.begin_session(datetime.combine(DAY, time(9, 30), tzinfo=ET),
                      datetime.combine(DAY, time(16, 0), tzinfo=ET))
    return sim


def run(sim, day, n):
    start = datetime.combine(day, time(9, 30), tzinfo=ET)
    for i in range(n):
        sim.step(start + timedelta(minutes=5 * i))


def fills(sim):
    return [(f.ref.split(":")[1], f.qty, f.price) for f in sim.fills_seen]


def test_breakout_fills_at_the_trigger_and_children_arm():
    sim = sim_with([(99.5, 99.9, 99.4, 99.8), (99.8, 100.4, 99.7, 100.3), (100.3, 100.6, 100.2, 100.5)])
    sim.place_bracket(make_plan(qty=30, qty_a=10))          # trigger 100, limit 100.3
    run(sim, DAY, 1)
    assert fills(sim) == []                                  # high 99.9 never reached the stop
    run(sim, DAY, 3)
    assert fills(sim) == [("A_PARENT", 10, 100.0), ("B_PARENT", 20, 100.0)]
    assert sim.positions()[0].qty == 30 and sim.account().available_funds == 7_000.0


def test_gap_over_the_limit_does_not_fill():
    sim = sim_with([(101.0, 101.5, 100.8, 101.2), (101.2, 101.4, 100.2, 100.4)])
    sim.place_bracket(make_plan(qty=10, qty_a=0))            # limit 100.3
    run(sim, DAY, 1)
    assert fills(sim) == []                                  # opened above the limit, never came back
    run(sim, DAY, 2)
    assert fills(sim) == [("B_PARENT", 10, 100.3)]           # came back to the limit on a later bar


def test_stop_fills_before_target_when_one_bar_hits_both():
    sim = sim_with([(99.9, 100.2, 99.8, 100.1), (100.1, 103.5, 98.5, 101.0)])
    sim.place_bracket(make_plan(qty=30, qty_a=10))           # stop 99, target 103
    run(sim, DAY, 2)
    assert fills(sim)[2:] == [("A_STOP", 10, 99.0), ("B_STOP", 20, 99.0)]
    assert (f"{T}:A_TARGET", "CANCELLED") in sim.statuses and sim.positions() == []


def test_target_fill_cancels_its_sibling_stop_only():
    sim = sim_with([(99.9, 100.2, 99.8, 100.1), (100.1, 103.5, 100.0, 103.2)])
    sim.place_bracket(make_plan(qty=30, qty_a=10))
    run(sim, DAY, 2)
    assert fills(sim)[2:] == [("A_TARGET", 10, 103.0)]
    assert (f"{T}:A_STOP", "CANCELLED") in sim.statuses
    assert [o.ref for o in sim.open_orders()] == [f"{T}:B_STOP"] and sim.positions()[0].qty == 20


def test_entry_bar_stop_only_fills_if_the_bar_closes_below_it():
    sim = sim_with([(99.5, 100.4, 98.5, 100.2)])             # dipped first, then broke out
    sim.place_bracket(make_plan(qty=10, qty_a=0))
    run(sim, DAY, 1)
    assert fills(sim) == [("B_PARENT", 10, 100.0)]
    sim = sim_with([(99.5, 100.4, 98.5, 98.8)])              # broke out and failed in the same bar
    sim.place_bracket(make_plan(qty=10, qty_a=0))
    run(sim, DAY, 1)
    assert fills(sim) == [("B_PARENT", 10, 100.0), ("B_STOP", 10, 99.0)]


def test_gap_through_the_stop_fills_at_the_open_for_more_than_1r():
    sim = sim_with([(99.9, 100.2, 99.8, 100.1)], next_rows=[(96.0, 96.5, 95.5, 96.2)])
    sim.place_bracket(make_plan(qty=10, qty_a=0))
    run(sim, DAY, 1)
    sim.end_of_day()
    run(sim, NEXT, 1)
    assert fills(sim)[-1] == ("B_STOP", 10, 96.0)            # planned exit 99: a 4R loss


def test_untriggered_day_order_expires_with_its_children():
    sim = sim_with([(99.0, 99.5, 98.9, 99.2)])
    sim.place_bracket(make_plan(qty=30, qty_a=10))
    run(sim, DAY, 1)
    sim.end_of_day()
    assert fills(sim) == [] and sim.open_orders() == []
    assert {s for _, s in sim.statuses} == {"CANCELLED"} and len(sim.statuses) == 5


def test_market_sell_fills_at_next_open_with_slippage_and_commission():
    frames = bars_5m(DAY, [(99.9, 100.2, 99.8, 100.1), (100.5, 100.9, 100.4, 100.8)])
    sim = SimBroker(HistoricalData({}, {"NVDA": frames}), cash=10_000.0, slippage_pct=0.1)
    out = []
    sim.on_fill(out.append)
    sim.begin_session(datetime.combine(DAY, time(9, 30), tzinfo=ET),
                      datetime.combine(DAY, time(16, 0), tzinfo=ET))
    sim.place_bracket(make_plan(qty=10, qty_a=0))
    run(sim, DAY, 1)
    sim.cancel(f"{T}:B_STOP")
    sim.close_position("NVDA", 10, f"{T}:EXIT")
    sim.step(datetime.combine(DAY, time(9, 35), tzinfo=ET))
    assert out[0].price == pytest.approx(100.0 * 1.001) and out[0].commission == 1.0
    assert out[1].price == pytest.approx(100.5 * 0.999)
    assert sim.account().net_liq == pytest.approx(10_000 + 10 * (100.3995 - 100.1) - 2.0)


def test_bars_never_show_the_future():
    daily = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
                         index=pd.DatetimeIndex([pd.Timestamp(DAY) - pd.Timedelta(days=1), pd.Timestamp(DAY)]))
    rows = [(1.0, 1.0, 1.0, 1.0)] * 78
    sim = SimBroker(HistoricalData({"NVDA": daily}, {"NVDA": bars_5m(DAY, rows)}))
    open_ = datetime.combine(DAY, time(9, 30), tzinfo=ET)
    close = datetime.combine(DAY, time(16, 0), tzinfo=ET)
    sim.begin_session(open_, close)
    sim.set_time(open_ + timedelta(minutes=7))
    assert len(sim.bars("NVDA", "5m", 100)) == 1             # the 09:35 bar is still forming
    assert len(sim.bars("NVDA", "1d", 10)) == 1              # today's daily bar is not final
    sim.set_time(close + timedelta(minutes=5))
    assert len(sim.bars("NVDA", "1d", 10)) == 2 and len(sim.bars("NVDA", "5m", 100)) == 78


# ── full backtest ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def synthetic():
    cal = TradingCalendar()
    daily, intra = market(cal.sessions(date(2025, 1, 2), date(2025, 12, 31)))
    return cal, HistoricalData(daily, intra)


def backtest(synthetic, **kw):
    cal, data = synthetic
    cfg = make_cfg(strategy={"ema_converge_adr_mult": 3.0}, limits={"max_order_usd": 5000})
    return run_backtest(cfg, data, date(2025, 9, 1), date(2025, 12, 31), calendar=cal, **kw)


def test_backtest_runs_the_whole_pipeline(synthetic):
    store = Store(":memory:")
    res = backtest(synthetic, store=store)
    stats = res.stats()
    assert stats["trades"] >= 10 and stats["commission"] > 0
    assert {t["symbol"] for t in res.trades} == {"LEAD"}
    assert any(t["scaled_out"] for t in res.trades)                    # some 3R targets were hit
    assert {t["exit_reason"] for t in res.trades if t["status"] == "CLOSED"} >= {"stop"}
    assert len(res.equity) == len(synthetic[0].sessions(date(2025, 9, 1), date(2025, 12, 31)))
    closed = [t for t in res.trades if t["status"] == "CLOSED"]
    assert all(t["open_qty"] == 0 and t["r_multiple"] is not None for t in closed)
    # no trade ever left a position without a stop, no unresolved mismatch
    assert not store.events("stop_replaced") and not [e for e in store.events() if e["kind"].startswith("reconcile")]
    # equity is consistent with the trades (open positions are marked to the last price)
    open_now = [t for t in res.trades if t["status"] in ("OPEN", "EXIT_QUEUED")]
    if not open_now:
        pnl = sum(t["realized_pnl"] - t["commission"] for t in res.trades)
        assert res.end_equity == pytest.approx(10_000 + pnl, abs=0.05)
    decisions = store.decisions_on(date(2025, 9, 2))
    assert decisions and all(len(d["inputs_hash"]) == 16 for d in decisions)


def test_replaying_the_same_data_gives_identical_trades(synthetic):
    a, b = backtest(synthetic), backtest(synthetic)
    assert a.trades == b.trades and a.equity == b.equity and a.trades


def test_earnings_window_blocks_entries_in_backtest(synthetic):
    base = backtest(synthetic)
    first = date.fromisoformat(base.trades[0]["entry_date"])
    res = backtest(synthetic, earnings=EarningsCalendar({"LEAD": [first + timedelta(days=1)]}))
    assert first.isoformat() not in {t["entry_date"] for t in res.trades}


def test_order_cap_rejects_instead_of_shrinking(synthetic):
    cal, data = synthetic
    store = Store(":memory:")
    cfg = make_cfg(strategy={"ema_converge_adr_mult": 3.0}, limits={"max_order_usd": 100})
    res = run_backtest(cfg, data, date(2025, 9, 1), date(2025, 10, 31), calendar=cal, store=store)
    assert res.trades == []
    rejected = store.db.execute("SELECT reasons FROM decisions WHERE action = 'REJECT'").fetchall()
    assert rejected and all("max_order_usd" in r["reasons"] for r in rejected)
