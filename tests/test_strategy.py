from dataclasses import replace
from datetime import date, datetime

import pandas as pd
import pytest

from trader.indicators import add_daily_indicators
from trader.models import (
    ENTER, ET, EXIT, LIMIT, NEXT_OPEN, NONE, NOW, SETUP_A, SETUP_B, STOP_LIMIT,
    AccountState, Position, WatchItem,
)
from trader.strategy import (
    INTRADAY, OPEN_RANGE, POST_CLOSE, PRE_CLOSE, StrategyInput, decide, inputs_hash,
)

from .conftest import make_cfg
from .synth import bars_5m, daily_frame, trend

TODAY, YDAY = date(2026, 11, 25), date(2026, 11, 24)
ACCOUNT = AccountState("DU1234567", 10_000.0, 10_000.0)
PARAMS = make_cfg().params
WATCH = (WatchItem(TODAY, "NVDA", 5.0, 0, 0, 0),)


def at(hh, mm):
    return datetime(2026, 11, 25, hh, mm, tzinfo=ET)


def qqq(daily_pct=0.1):
    return daily_frame(trend(80, 400, daily_pct), YDAY, range_pct=1)


def tight_daily(inside=True):
    """Flat-ish uptrend near 99.5 with 5% daily ranges; optionally ends on an inside day."""
    df = daily_frame(trend(80, 92, 0.1), YDAY, range_pct=5)
    if inside:
        df.iloc[-1, df.columns.get_loc("high")] = df["high"].iloc[-2] - 0.5
        df.iloc[-1, df.columns.get_loc("low")] = df["low"].iloc[-2] + 0.5
    return df


def scene_a(or_bar=(100.0, 100.8, 99.9, 100.5), inside=True, **kw):
    base = dict(now=at(9, 35), phase=OPEN_RANGE, watchlist=WATCH,
                daily={"NVDA": tight_daily(inside), "QQQ": qqq()},
                intraday={"NVDA": bars_5m(TODAY, [or_bar])})
    base.update(kw)
    return StrategyInput(**base)


def only(actions, symbol="NVDA"):
    found = [a for a in actions if a.symbol == symbol]
    assert len(found) == 1, actions
    return found[0]


# ── setup A ──────────────────────────────────────────────────────

def test_setup_a_builds_a_stop_limit_plan_over_the_opening_range():
    a = only(decide(scene_a(), ACCOUNT, [], PARAMS))
    assert a.kind == ENTER and a.setup == SETUP_A
    p = a.plan
    assert p.ref == "20261125-NVDA" and p.entry_type == STOP_LIMIT
    assert p.trigger_price == 100.81          # opening-range high + one tick
    assert p.stop_price == 99.90              # opening-range low
    assert p.limit_price == round(100.81 * 1.003, 2)
    assert p.target_price == pytest.approx(100.81 + 3 * 0.91)
    # risk sizing gives 54 shares, the 30% position cap cuts it to 29
    assert p.qty == 29 and p.qty_a == 9 and p.qty_b == 20


def test_size_is_risk_budget_over_per_share_risk():
    a = only(decide(scene_a(or_bar=(100.0, 101.5, 99.5, 101.0)), ACCOUNT, [], PARAMS))
    width = 101.51 - 99.5
    assert a.plan.qty == int(10_000 * 0.005 / width) == 24
    assert a.plan.risk_usd == pytest.approx(24 * width, abs=0.01)


def test_stop_wider_than_half_adr_is_skipped():
    a = only(decide(scene_a(or_bar=(100.0, 102.0, 99.0, 101.0)), ACCOUNT, [], PARAMS))
    assert a.kind == NONE and any(r.startswith("stop_too_wide") for r in a.reasons)


def test_no_tightening_means_no_entry():
    a = only(decide(scene_a(inside=False), ACCOUNT, [], PARAMS))
    assert a.kind == NONE
    assert "inside_day=False" in a.reasons and "narrow_day=False" in a.reasons


def test_narrow_day_also_counts_as_tightening():
    df = tight_daily(inside=False)
    c = df["close"].iloc[-1]
    df.iloc[-1, df.columns.get_loc("high")] = c * 1.03   # above the prior high: not inside
    df.iloc[-1, df.columns.get_loc("low")] = c * 1.012
    a = only(decide(scene_a(daily={"NVDA": df, "QQQ": qqq()}), ACCOUNT, [], PARAMS))
    assert a.kind == ENTER and "inside_day=False" in a.reasons and "narrow_day=True" in a.reasons


def test_spread_out_emas_block_the_breakout():
    df = daily_frame(trend(80, 40, 1.2), YDAY, range_pct=5)
    df.iloc[-1, df.columns.get_loc("high")] = df["high"].iloc[-2] - 0.2
    df.iloc[-1, df.columns.get_loc("low")] = df["low"].iloc[-2] + 0.2
    c = float(df["close"].iloc[-1])
    bars = scene_a(or_bar=(c, c + 0.4, c - 0.1, c + 0.2), daily={"NVDA": df, "QQQ": qqq()})
    a = only(decide(bars, ACCOUNT, [], PARAMS))
    assert a.kind == NONE and "ema_converged=False" in a.reasons


def test_regime_filter_blocks_all_entries():
    actions = decide(scene_a(daily={"NVDA": tight_daily(), "QQQ": qqq(-0.3)}), ACCOUNT, [], PARAMS)
    assert [a.kind for a in actions] == [NONE]
    assert actions[0].symbol == "QQQ" and actions[0].reasons[0].startswith("regime_off")


def test_held_symbols_and_earnings_window_are_skipped():
    assert decide(scene_a(), ACCOUNT, [Position("NVDA", 10, 100.0)], PARAMS) == []
    a = only(decide(scene_a(sessions_to_earnings={"NVDA": 2}), ACCOUNT, [], PARAMS))
    assert a.kind == NONE and a.reasons == ("earnings_window=2",)
    assert only(decide(scene_a(sessions_to_earnings={"NVDA": 3}), ACCOUNT, [], PARAMS)).kind == ENTER


def test_a_partial_daily_bar_for_today_is_ignored():
    clean = scene_a()
    df = tight_daily()
    df.loc[pd.Timestamp(TODAY)] = [100.0, 130.0, 60.0, 61.0, 1.0]  # garbage still forming
    dirty = scene_a(daily={"NVDA": df, "QQQ": qqq()})
    assert decide(dirty, ACCOUNT, [], PARAMS) == decide(clean, ACCOUNT, [], PARAMS)


def test_unfinished_opening_bar_is_not_used():
    a = only(decide(scene_a(now=at(9, 34)), ACCOUNT, [], PARAMS))
    assert a.kind == NONE and a.reasons == ("no_data",)


def test_decide_is_deterministic_and_hash_tracks_inputs():
    bars = scene_a()
    assert decide(bars, ACCOUNT, [], PARAMS) == decide(scene_a(), ACCOUNT, [], PARAMS)
    h = inputs_hash(bars, ACCOUNT, [], PARAMS)
    assert h == inputs_hash(scene_a(), ACCOUNT, [], PARAMS)
    assert h != inputs_hash(scene_a(or_bar=(100.0, 100.9, 99.9, 100.5)), ACCOUNT, [], PARAMS)
    assert h != inputs_hash(bars, replace(ACCOUNT, net_liq=9_000.0), [], PARAMS)


# ── setup B ──────────────────────────────────────────────────────

def uptrend():
    return daily_frame(trend(80, 70, 0.4), YDAY, range_pct=5)


def scene_b(low_off=-0.3, last_off=1.5, phase=PRE_CLOSE, now=None):
    df = uptrend()
    last = add_daily_indicators(df).iloc[-1]
    ema9, c = float(last["ema9"]), float(last["close"])
    rows = [(c, c + 0.2, ema9 + low_off, ema9 + 0.2),
            (ema9 + 0.2, max(ema9 + last_off, ema9 + 0.3) + 0.1, ema9 + 0.1, ema9 + last_off)]
    bars = StrategyInput(now=now or at(15, 50), phase=phase, watchlist=WATCH,
                         daily={"NVDA": df, "QQQ": qqq()}, intraday={"NVDA": bars_5m(TODAY, rows)})
    return bars, ema9, float(last["adr_pct"]) / 100 * c


def test_setup_b_enters_on_a_reclaimed_pullback():
    bars, ema9, _ = scene_b()
    a = only(decide(bars, ACCOUNT, [], PARAMS))
    assert a.kind == ENTER and a.setup == SETUP_B and a.plan.entry_type == LIMIT
    assert a.plan.trigger_price == round(ema9 + 1.5, 2)
    assert a.plan.stop_price == round(ema9 - 0.3, 2)   # the day's low


@pytest.mark.parametrize("kwargs, failed", [
    (dict(low_off=0.05), "touched_ema9=False"),
    (dict(last_off=-0.1), "reclaimed=False"),
    (dict(last_off=0.25), "strong_close=False"),
])
def test_setup_b_needs_every_condition(kwargs, failed):
    bars, _, _ = scene_b(**kwargs)
    a = only(decide(bars, ACCOUNT, [], PARAMS))
    assert a.kind == NONE and failed in a.reasons


def test_setup_b_rejects_a_pullback_deeper_than_one_adr():
    _, _, adr_usd = scene_b()
    bars, _, _ = scene_b(low_off=-(adr_usd + 0.5))
    a = only(decide(bars, ACCOUNT, [], PARAMS))
    assert a.kind == NONE and "not_too_deep=False" in a.reasons


def test_intraday_phase_only_reports_the_pullback_precondition():
    bars, _, _ = scene_b(phase=INTRADAY, now=at(11, 0))
    a = only(decide(bars, ACCOUNT, [], PARAMS))
    assert a.kind == NONE and a.reasons[0] == "pullback_precondition"
    bars, _, _ = scene_b(low_off=0.05, phase=INTRADAY, now=at(11, 0))
    assert decide(bars, ACCOUNT, [], PARAMS) == []


# ── exits ────────────────────────────────────────────────────────

def post_close(today_close_factor):
    df = uptrend()
    c = float(df["close"].iloc[-1]) * today_close_factor
    df.loc[pd.Timestamp(TODAY)] = [c, c * 1.01, c * 0.99, c, 2_000_000]
    return StrategyInput(now=at(16, 5), phase=POST_CLOSE, daily={"NVDA": df, "QQQ": qqq()}, intraday={})


def test_close_below_ema9_queues_an_exit_for_the_next_open():
    pos = [Position("NVDA", 10, 90.0)]
    a = only(decide(post_close(0.93), ACCOUNT, pos, PARAMS))
    assert (a.kind, a.reason, a.when) == (EXIT, "close_below_ema9", NEXT_OPEN)
    assert decide(post_close(1.01), ACCOUNT, pos, PARAMS) == []


def test_post_close_without_todays_bar_does_nothing():
    bars = StrategyInput(now=at(16, 5), phase=POST_CLOSE, daily={"NVDA": uptrend()}, intraday={})
    a = only(decide(bars, ACCOUNT, [Position("NVDA", 10, 90.0)], PARAMS))
    assert a.kind == NONE and a.reasons == ("daily_bar_missing",)


def test_position_is_sold_before_the_close_on_the_day_before_earnings():
    bars, _, _ = scene_b()
    bars = replace(bars, sessions_to_earnings={"NVDA": 1})
    actions = decide(bars, ACCOUNT, [Position("NVDA", 10, 90.0)], PARAMS)
    assert [(a.kind, a.reason, a.when) for a in actions] == [(EXIT, "earnings", NOW)]
    bars = replace(bars, sessions_to_earnings={"NVDA": 2})
    assert decide(bars, ACCOUNT, [Position("NVDA", 10, 90.0)], PARAMS) == []
