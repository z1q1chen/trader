from datetime import date, time

import numpy as np
import pytest

from trader.calendar import EarningsCalendar, TradingCalendar
from trader.config import ConfigError, parse_config
from trader.indicators import add_daily_indicators, adr_pct
from trader.watchlist import screen

from .conftest import RAW, make_cfg
from .synth import daily_frame, trend


def test_paper_mode_requires_du_account():
    raw = {"mode": "paper", "profiles": {"paper": {**RAW["profiles"]["paper"], "account": "U1234567"}}}
    with pytest.raises(ConfigError, match="DU"):
        parse_config(raw)


def test_live_mode_rejects_du_account():
    raw = {"mode": "live", "profiles": {"live": {**RAW["profiles"]["live"], "account": "DU1234567"}}}
    with pytest.raises(ConfigError, match="DU"):
        parse_config(raw)


def test_risk_per_trade_is_capped_at_one_percent():
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        make_cfg(limits={"risk_per_trade_pct": 1.5})


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError, match="unknown"):
        make_cfg(strategy={"min_adr": 4})


def test_live_profile_gets_tighter_defaults_and_params_follow_limits():
    live = make_cfg("live")
    assert (live.limits.max_drawdown_pct, live.limits.max_orders_per_day) == (10.0, 10)
    assert live.limits.max_order_usd == 400
    paper = make_cfg(limits={"risk_per_trade_pct": 0.25})
    assert paper.params.risk_per_trade_pct == 0.25
    assert (paper.limits.max_drawdown_pct, paper.limits.max_orders_per_day) == (15.0, 20)


def test_adr_pct_and_indicators():
    df = daily_frame(np.full(60, 100.0), date(2026, 11, 24), range_pct=5.0)
    # high/low = 102.5/97.5
    assert adr_pct(df).iloc[-1] == pytest.approx(100 * (102.5 / 97.5 - 1))
    ind = add_daily_indicators(df).iloc[-1]
    assert ind["ema9"] == pytest.approx(100.0)
    assert ind["dollar_vol"] == pytest.approx(100.0 * 2_000_000)


def test_calendar_half_day_and_holiday():
    cal = TradingCalendar()
    assert cal.session(date(2026, 11, 26)) is None  # Thanksgiving
    open_, close = cal.session(date(2026, 11, 27))
    assert (open_.time(), close.time()) == (time(9, 30), time(13, 0))
    _, close = cal.session(date(2026, 11, 25))
    assert close.time() == time(16, 0)
    assert cal.sessions_until(date(2026, 11, 25), date(2026, 11, 25)) == 0
    assert cal.sessions_until(date(2026, 11, 25), date(2026, 11, 27)) == 1  # skips the holiday
    assert cal.sessions_until(date(2026, 11, 25), date(2026, 11, 30)) == 2


def test_earnings_calendar_sessions_to():
    cal = TradingCalendar()
    earn = EarningsCalendar({"nvda": [date(2026, 11, 30), date(2026, 8, 20)]})
    assert earn.sessions_to("NVDA", date(2026, 11, 25), cal) == 2
    assert earn.sessions_to("AMD", date(2026, 11, 25), cal) is None
    assert not EarningsCalendar()


def test_screen_keeps_only_liquid_high_adr_leaders():
    end = date(2026, 11, 24)
    daily = {
        "LEAD": daily_frame(trend(150, 40, 0.5), end, range_pct=6),
        "SLOW": daily_frame(trend(150, 40, 0.0), end, range_pct=6),
        "DULL": daily_frame(trend(150, 40, 0.6), end, range_pct=2),          # ADR too low
        "THIN": daily_frame(trend(150, 40, 0.7), end, range_pct=6, volume=1000),  # illiquid
        "NEW": daily_frame(trend(50, 40, 2.0), end, range_pct=6),            # too little history
        "QQQ": daily_frame(trend(150, 400, 0.1), end, range_pct=1),
    }
    params = make_cfg(strategy={"momentum_top_pct": 60}).params
    items = screen(daily, date(2026, 11, 25), params, {"LEAD": date(2026, 12, 1)})
    assert [w.symbol for w in items] == ["LEAD"]
    assert items[0].earnings_date == date(2026, 12, 1)
    assert items[0].adr_pct == pytest.approx(100 * (1.03 / 0.97 - 1), rel=1e-3)


def test_screen_ignores_bars_from_asof_onward():
    end = date(2026, 11, 25)
    daily = {"LEAD": daily_frame(trend(150, 40, 0.5), end, range_pct=6),
             "SLOW": daily_frame(trend(150, 40, 0.0), end, range_pct=6)}
    params = make_cfg(strategy={"momentum_top_pct": 60}).params
    a = screen(daily, date(2026, 11, 25), params)
    b = screen({k: v.iloc[:-1] for k, v in daily.items()}, date(2026, 11, 25), params)
    assert a == b and a
