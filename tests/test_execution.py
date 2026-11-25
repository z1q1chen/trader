from datetime import date

import pytest

from trader.broker.base import bracket_legs
from trader.models import CANCELLED, REJECTED

from .conftest import make_plan

TODAY = date(2026, 11, 25)
T = "20261125-NVDA"


def test_bracket_legs_split_into_two_groups():
    legs = bracket_legs(make_plan(qty=30, qty_a=10))
    assert [(l.role, l.side, l.type, l.qty, l.tif, l.transmit) for l in legs] == [
        ("A_PARENT", "BUY", "STP LMT", 10, "DAY", False),
        ("A_TARGET", "SELL", "LMT", 10, "GTC", False),
        ("A_STOP", "SELL", "STP", 10, "GTC", True),
        ("B_PARENT", "BUY", "STP LMT", 20, "DAY", False),
        ("B_STOP", "SELL", "STP", 20, "GTC", True),
    ]
    assert legs[1].oca_group == legs[2].oca_group != "" and legs[4].oca_group == ""
    assert legs[1].parent_ref == legs[2].parent_ref == f"{T}:A_PARENT"


def test_small_plan_uses_group_b_only():
    legs = bracket_legs(make_plan(qty=2, qty_a=0))
    assert [l.role for l in legs] == ["B_PARENT", "B_STOP"]


def test_submit_is_idempotent_per_symbol_per_day(rig):
    assert rig.ex.submit_entry(make_plan(), TODAY) is True
    assert rig.ex.submit_entry(make_plan(), TODAY) is False
    assert rig.ex.submit_entry(make_plan(entry=101.0), TODAY) is False
    assert [c for c in rig.broker.calls if c[0] == "place_bracket"] == [("place_bracket", T)]
    assert rig.store.get_trade(T)["status"] == "PENDING"
    assert len(rig.store.orders_for_trade(T)) == 5
    assert rig.store.submissions_on(TODAY) == 1


def test_entry_fill_opens_trade_and_reprices_target_from_real_fill(rig):
    rig.ex.submit_entry(make_plan(), TODAY)                 # planned 100 / stop 99 / target 103
    rig.broker.fill(f"{T}:A_PARENT", 10, 100.20)
    rig.broker.fill(f"{T}:B_PARENT", 20, 100.20)
    t = rig.store.get_trade(T)
    assert (t["status"], t["filled_qty"], t["open_qty"]) == ("OPEN", 30, 30)
    assert t["entry_price"] == pytest.approx(100.20)
    assert t["risk_usd"] == pytest.approx(30 * 1.20)
    assert t["target_price"] == pytest.approx(103.80)       # 100.20 + 3 * 1.20
    assert ("modify_limit", f"{T}:A_TARGET", 103.80) in rig.broker.calls
    assert rig.store.get_order(f"{T}:A_TARGET")["price"] == 103.80


def test_stop_out_closes_the_trade_at_about_minus_one_r(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    rig.broker.fill(f"{T}:A_PARENT", 10, 100.0, commission=0)
    rig.broker.fill(f"{T}:B_PARENT", 20, 100.0, commission=0)
    rig.broker.fill(f"{T}:A_STOP", 10, 99.0, commission=0)
    assert rig.store.get_trade(T)["status"] == "OPEN"
    rig.broker.fill(f"{T}:B_STOP", 20, 99.0, commission=0)
    t = rig.store.get_trade(T)
    assert (t["status"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "stop", 0)
    assert t["r_multiple"] == pytest.approx(-1.0) and t["scaled_out"] == 0
    assert ("cancel", f"{T}:A_TARGET") in rig.broker.calls   # nothing left resting


def test_target_fill_marks_scale_out_and_group_b_keeps_running(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    rig.broker.fill(f"{T}:A_PARENT", 10, 100.0, commission=0)
    rig.broker.fill(f"{T}:B_PARENT", 20, 100.0, commission=0)
    rig.broker.fill(f"{T}:A_TARGET", 10, 103.0, commission=0)
    t = rig.store.get_trade(T)
    assert (t["status"], t["scaled_out"], t["open_qty"]) == ("OPEN", 1, 20)
    assert t["realized_pnl"] == pytest.approx(30.0)


def test_commission_is_included_in_r_multiple(rig):
    rig.ex.submit_entry(make_plan(qty=10, qty_a=0), TODAY)
    rig.broker.fill(f"{T}:B_PARENT", 10, 100.0, commission=1.0)
    rig.broker.fill(f"{T}:B_STOP", 10, 99.0, commission=1.0)
    assert rig.store.get_trade(T)["r_multiple"] == pytest.approx(-1.2)   # (-10 - 2) / 10


def test_duplicate_execution_report_is_counted_once(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    fill = rig.broker.fill(f"{T}:A_PARENT", 10, 100.0)
    rig.ex.handle_fill(fill)
    rig.ex.handle_fill(fill)
    t = rig.store.get_trade(T)
    assert (t["filled_qty"], t["open_qty"], t["commission"]) == (10, 10, 1.0)


def test_rejected_order_alerts_and_is_never_retried(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    rig.ex.handle_order_status(f"{T}:A_PARENT", REJECTED, "insufficient buying power")
    rig.ex.handle_order_status(f"{T}:B_PARENT", REJECTED, "insufficient buying power")
    assert rig.store.get_trade(T)["status"] == "REJECTED"
    assert len(rig.store.events("order_rejected")) == 2
    assert len([c for c in rig.broker.calls if c[0] == "place_bracket"]) == 1


def test_untriggered_day_order_cancels_the_trade(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    rig.ex.handle_order_status(f"{T}:A_PARENT", CANCELLED)
    assert rig.store.get_trade(T)["status"] == "PENDING"
    rig.ex.handle_order_status(f"{T}:B_PARENT", CANCELLED)
    t = rig.store.get_trade(T)
    assert (t["status"], t["exit_reason"]) == ("CANCELLED", "not_triggered")


def test_partial_fill_then_cancel_requests_a_stop_resync(rig):
    rig.ex.submit_entry(make_plan(qty=20, qty_a=0), TODAY)
    rig.broker.fill(f"{T}:B_PARENT", 8, 100.0)
    assert rig.store.get_order(f"{T}:B_PARENT")["status"] == "PARTIAL"
    assert not rig.ex.needs_reconcile
    rig.ex.handle_order_status(f"{T}:B_PARENT", CANCELLED)
    assert rig.ex.needs_reconcile and rig.store.get_trade(T)["status"] == "OPEN"


def test_failed_submission_is_flagged_not_retried(rig):
    rig.broker.fail_place = True
    assert rig.ex.submit_entry(make_plan(), TODAY) is False
    assert rig.store.get_order(f"{T}:B_PARENT")["status"] == "UNKNOWN"
    assert rig.ex.needs_reconcile and rig.store.events("submit_failed")
    assert rig.ex.submit_entry(make_plan(), TODAY) is False       # still one attempt only
    assert len([c for c in rig.broker.calls if c[0] == "place_bracket"]) == 1


def test_queued_exit_cancels_stops_then_sells_at_the_open(rig):
    rig.ex.submit_entry(make_plan(), TODAY)
    rig.broker.fill(f"{T}:A_PARENT", 10, 100.0, commission=0)
    rig.broker.fill(f"{T}:B_PARENT", 20, 100.0, commission=0)
    assert rig.ex.queue_exit("NVDA", "close_below_ema9") is True
    assert rig.store.get_trade(T)["status"] == "EXIT_QUEUED"
    rig.broker.calls.clear()
    assert rig.ex.submit_queued_exits(date(2026, 11, 27)) == 1
    names = [c[0] for c in rig.broker.calls]
    assert names.index("close_position") > max(i for i, n in enumerate(names) if n == "cancel")
    assert ("close_position", "NVDA", 30, f"{T}:EXIT") in rig.broker.calls
    assert rig.ex.submit_queued_exits(date(2026, 11, 27)) == 0    # not sent twice
    rig.broker.fill(f"{T}:EXIT", 30, 104.0, commission=0)
    t = rig.store.get_trade(T)
    assert (t["status"], t["exit_reason"]) == ("CLOSED", "close_below_ema9")
    assert t["r_multiple"] == pytest.approx(4.0)


def test_exit_now_and_modify_stop(rig):
    rig.ex.submit_entry(make_plan(qty=10, qty_a=0), TODAY)
    rig.broker.fill(f"{T}:B_PARENT", 10, 100.0)
    rig.ex.modify_stop("NVDA", 99.5)
    assert ("modify_stop", f"{T}:B_STOP", 99.5) in rig.broker.calls
    assert rig.store.get_trade(T)["stop_price"] == 99.5
    assert rig.ex.exit_now("NVDA", "earnings", TODAY) is True
    assert ("close_position", "NVDA", 10, f"{T}:EXIT") in rig.broker.calls
    assert rig.ex.exit_now("AMD", "earnings", TODAY) is False


def test_fill_for_an_unknown_order_alerts(rig):
    from trader.models import Fill
    rig.ex.handle_fill(Fill("E99", "", "TSLA", "BUY", 5, 200.0, 1.0, rig.broker.now))
    assert rig.store.events("fill_unknown_ref") and rig.ex.needs_reconcile
