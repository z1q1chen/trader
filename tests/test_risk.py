from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.models import ET, AccountState
from trader.risk import RiskContext, RiskGate

from .conftest import make_cfg, make_plan

NOW = datetime(2026, 11, 25, 9, 35, 5, tzinfo=ET)
GATE = RiskGate(make_cfg().limits)


def ctx(**kw):
    base = dict(now=NOW, account=AccountState("DU1234567", 10_000.0, 10_000.0),
                day_start_net_liq=10_000.0, peak_net_liq=10_000.0, open_positions=0,
                orders_today=0, last_price=99.8, last_bar_end=NOW - timedelta(seconds=5))
    base.update(kw)
    return RiskContext(**base)


def plan(**kw):
    # entry 100, stop 99: 15 shares = $15 risk (0.15%), $1.5k notional
    return make_plan(**{"qty": 15, "qty_a": 5, **kw})


def test_a_clean_plan_passes_unchanged():
    d = GATE.check(plan(), ctx())
    assert d.approved and d.plan == plan() and d.reasons == ("ok",)


@pytest.mark.parametrize("overrides, reason", [
    (dict(halted=True), "halt_file_present"),
    (dict(trading_enabled=False), "trading_disabled"),
    (dict(account=AccountState("DU1234567", 9_800.0, 9_800.0)), "max_daily_loss"),        # -2%
    (dict(data_realtime=False), "delayed_market_data"),
    (dict(last_bar_end=NOW - timedelta(seconds=121)), "stale_market_data"),
    (dict(last_bar_end=None), "stale_market_data"),
    (dict(sessions_to_earnings=2), "earnings_window"),
    (dict(open_positions=5), "max_positions"),
    (dict(orders_today=20), "max_orders_per_day"),
    (dict(last_price=97.0), "limit_price_deviation"),                                     # limit 3.4% away
])
def test_hard_limits_reject(overrides, reason):
    d = GATE.check(plan(), ctx(**overrides))
    assert not d.approved and d.plan is None and d.reasons == (reason,) and not d.halt


def test_drawdown_breaker_rejects_and_asks_for_a_halt():
    d = GATE.check(plan(), ctx(peak_net_liq=11_800.0))   # 15.25% below the peak
    assert not d.approved and d.reasons == ("max_drawdown",) and d.halt


def test_earnings_three_sessions_away_is_allowed():
    assert GATE.check(plan(), ctx(sessions_to_earnings=3)).approved


def test_stop_wider_than_half_adr_is_rejected():
    d = GATE.check(plan(stop=97.0, adr=5.0), ctx())      # 3.0 wide, limit 2.5
    assert d.reasons == ("stop_too_wide",)


def test_order_over_the_dollar_cap_is_rejected_not_shrunk():
    d = GATE.check(plan(qty=25, qty_a=8), ctx())          # ~$2.5k > $2k
    assert not d.approved and d.reasons == ("max_order_usd",)


def test_oversized_risk_is_truncated():
    gate = RiskGate(replace(make_cfg().limits, max_order_usd=1e9, max_position_pct=100.0))
    d = gate.check(plan(qty=90, qty_a=30), ctx())         # $90 risk vs $50 budget
    assert d.approved and d.plan.qty == 50 and d.plan.qty_a == 16
    assert d.plan.risk_usd == pytest.approx(50.0) and "truncated_to_risk" in d.reasons


def test_position_cap_truncates_quantity():
    gate = RiskGate(replace(make_cfg().limits, max_order_usd=1e9))
    d = gate.check(plan(qty=45, qty_a=15), ctx())         # 30% of 10k at 100.30 = 29 shares
    assert d.approved and d.plan.qty == 29 and "truncated_to_position_cap" in d.reasons


def test_live_limits_are_tighter():
    live = RiskGate(make_cfg("live").limits)
    assert live.check(plan(qty=5, qty_a=0), ctx()).reasons == ("max_order_usd",)   # $500 > $400
    assert live.check(plan(qty=3, qty_a=0), ctx()).approved
    assert live.check(plan(qty=3, qty_a=0), ctx(open_positions=3)).reasons == ("max_positions",)
    loss = AccountState("U7654321", 9_850.0, 9_850.0)                              # -1.5%
    assert live.check(plan(qty=3, qty_a=0), ctx(account=loss)).reasons == ("max_daily_loss",)
