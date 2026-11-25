from datetime import date, datetime

from trader.killswitch import flatten, halt_active, set_halt
from trader.models import CANCELLED, ET, SUBMITTED, Order
from trader.reconcile import reconcile
from trader.startup import run_startup_checks

from .conftest import Rig, make_cfg, make_plan

TODAY = date(2026, 11, 25)
T = "20261125-NVDA"


def rec(rig):
    return reconcile(rig.broker, rig.store, rig.ex, rig.alerter, rig.clock)


def open_position(rig, qty=30, qty_a=10):
    rig.ex.submit_entry(make_plan(qty=qty, qty_a=qty_a), TODAY)
    if qty_a:
        rig.broker.fill(f"{T}:A_PARENT", qty_a, 100.0)
    rig.broker.fill(f"{T}:B_PARENT", qty - qty_a, 100.0)


def calls(rig, name):
    return [c for c in rig.broker.calls if c[0] == name]


def test_consistent_state_is_clean_and_untouched(rig):
    open_position(rig)
    res = rec(rig)
    assert res.clean and res.repairs == [] and not calls(rig, "place_stop")


def test_position_the_system_did_not_open_is_reported_not_touched(rig):
    rig.broker.pos["TSLA"] = 7
    res = rec(rig)
    assert res.diffs == ["unknown_position:TSLA:7"]
    assert not calls(rig, "place_stop") and not calls(rig, "close_position")


def test_quantity_mismatch_and_unknown_order_are_unresolved(rig):
    open_position(rig)
    rig.broker.pos["NVDA"] = 25
    rig.broker.orders["manual-1"] = Order("manual-1", "AMD", "BUY", "LMT", 5, SUBMITTED, None, 50.0)
    res = rec(rig)
    assert "qty_mismatch:NVDA:broker=25:local=30" in res.diffs
    assert "unknown_order:AMD:manual-1" in res.diffs
    assert not calls(rig, "place_stop")          # never guess while the position is in doubt


def test_missed_fill_is_replayed_from_the_broker(rig):
    rig.ex.submit_entry(make_plan(qty=10, qty_a=0), TODAY)
    rig.broker.fill(f"{T}:B_PARENT", 10, 100.0, notify=False)     # push message lost
    assert rig.store.get_trade(T)["status"] == "PENDING"
    res = rec(rig)
    assert res.clean and rig.store.get_trade(T)["status"] == "OPEN"


def test_local_position_missing_at_broker_is_closed(rig):
    open_position(rig)
    rig.broker.pos.clear()
    rig.broker.fills.clear()
    res = rec(rig)
    t = rig.store.get_trade(T)
    assert res.clean and (t["status"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "reconciled_missing", 0)
    assert f"trade_closed:{T}" in res.repairs and rig.store.events("reconcile_closed")


def test_missing_stop_is_replaced(rig):
    open_position(rig, qty=10, qty_a=0)
    del rig.broker.orders[f"{T}:B_STOP"]                           # vanished at the broker
    res = rec(rig)
    assert res.clean and "stop_placed:NVDA:10" in res.repairs
    assert calls(rig, "place_stop") == [("place_stop", f"{T}:PROTECT", "NVDA", 10, 99.0)]
    assert rig.store.get_order(f"{T}:B_STOP")["status"] == CANCELLED
    assert rec(rig).repairs == []                                  # stable afterwards


def test_partial_fill_then_cancel_shrinks_the_stop_to_the_position(rig):
    """A stop bigger than the position would flip the account short when it fills."""
    rig.ex.submit_entry(make_plan(qty=20, qty_a=0), TODAY)
    rig.broker.fill(f"{T}:B_PARENT", 8, 100.0)
    rig.broker.cancel(f"{T}:B_PARENT")                             # DAY order expires, 12 unfilled
    assert rig.ex.needs_reconcile
    res = rec(rig)
    assert res.clean and ("cancel", f"{T}:B_STOP") in rig.broker.calls
    assert calls(rig, "place_stop") == [("place_stop", f"{T}:PROTECT", "NVDA", 8, 99.0)]
    stops = [o for o in rig.broker.open_orders() if o.type == "STP"]
    assert sum(o.qty for o in stops) == 8 == rig.broker.pos["NVDA"]


def test_child_stops_of_an_unfilled_parent_are_not_counted(rig):
    rig.ex.submit_entry(make_plan(qty=30, qty_a=10), TODAY)
    rig.broker.fill(f"{T}:A_PARENT", 10, 100.0)                    # group B still waiting
    res = rec(rig)
    assert res.clean and res.repairs == [] and not calls(rig, "cancel")


def test_position_being_sold_does_not_get_a_new_stop(rig):
    open_position(rig, qty=10, qty_a=0)
    rig.ex.exit_now("NVDA", "earnings", TODAY)
    res = rec(rig)
    assert res.clean and not calls(rig, "place_stop")


# ── startup checklist ────────────────────────────────────────────

def startup(rig, cfg=None, env=None):
    return run_startup_checks(cfg or make_cfg(), rig.broker, rig.store, rig.ex, rig.alerter,
                              rig.clock, env or {})


def test_startup_passes_and_announces_the_environment(rig):
    res = startup(rig)
    assert res.ok and res.can_trade
    msg = rig.store.events("startup")[0]["payload"]
    assert "mode=paper" in msg and "DU1234567" in msg and "max_order_usd" in msg


def test_startup_is_fatal_when_gateway_account_differs(rig):
    rig.broker.accounts = ["DU9999999"]
    res = startup(rig)
    assert not res.ok and res.fatal[0].startswith("account_mismatch")


def test_paper_config_against_a_live_login_is_fatal(rig):
    rig.broker.accounts = ["U7654321"]
    res = startup(rig)
    assert not res.ok and any(f.startswith("wrong_environment") for f in res.fatal)


def test_live_needs_the_confirmation_variable():
    rig = Rig()
    rig.broker.accounts = ["U7654321"]
    cfg = make_cfg("live")
    assert startup(rig, cfg).fatal == ["live_not_confirmed: set TRADING_LIVE_CONFIRM to the live account id"]
    assert not startup(rig, cfg, {"TRADING_LIVE_CONFIRM": "U0000000"}).ok
    assert startup(rig, cfg, {"TRADING_LIVE_CONFIRM": "U7654321"}).can_trade


def test_delayed_data_clock_skew_and_diffs_mean_read_only(rig):
    rig.broker.realtime = False
    rig.broker.time_skew = 2.5
    rig.broker.pos["TSLA"] = 7
    res = startup(rig)
    assert res.ok and not res.can_trade
    assert res.read_only == ["delayed_market_data", "reconcile:unknown_position:TSLA:7", "clock_skew:2.5s"]
    assert rig.store.events("startup_read_only")


def test_startup_replaces_a_missing_stop(rig):
    open_position(rig, qty=10, qty_a=0)
    del rig.broker.orders[f"{T}:B_STOP"]
    assert startup(rig).can_trade
    assert calls(rig, "place_stop") == [("place_stop", f"{T}:PROTECT", "NVDA", 10, 99.0)]


# ── kill switch ──────────────────────────────────────────────────

def test_halt_file_is_created_once_and_never_removed(tmp_path):
    path = tmp_path / "HALT"
    assert not halt_active(path)
    now = datetime(2026, 11, 25, 10, 0, tzinfo=ET)
    set_halt(path, "max_drawdown", now)
    first = path.read_text()
    set_halt(path, "again", now)
    assert halt_active(path) and path.read_text() == first and "max_drawdown" in first


def test_flatten_cancels_everything_and_sells_every_position(rig):
    open_position(rig, qty=10, qty_a=0)
    rig.broker.pos["TSLA"] = 7                                      # not ours, still sold
    rig.broker.calls.clear()
    out = flatten(rig.broker, rig.ex, rig.alerter, TODAY, rig.broker.now)
    assert sorted(out["closed"]) == ["NVDA", "TSLA"]
    assert rig.broker.calls[0] == ("cancel_all",)
    assert ("close_position", "NVDA", 10, f"{T}:EXIT") in rig.broker.calls
    assert any(c[:3] == ("close_position", "TSLA", 7) for c in rig.broker.calls)
    assert rig.store.events("flatten")
