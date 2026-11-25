"""Strategy engine: a pure function from market data and account state to actions.

`decide()` never reads the clock, the network or the environment. Time and the
phase of the day arrive in `StrategyInput`, so any decision can be replayed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping

import pandas as pd

from .config import StrategyParams
from .indicators import add_daily_indicators
from .models import (
    ENTER, EXIT, LIMIT, NEXT_OPEN, NONE, NOW, SETUP_A, SETUP_B, STOP_LIMIT,
    AccountState, Action, Position, TradePlan, WatchItem,
)

OPEN_RANGE = "OPEN_RANGE"    # 09:35, opening range just completed
INTRADAY = "INTRADAY"        # every 5-minute bar
PRE_CLOSE = "PRE_CLOSE"      # about 10 minutes before the close
POST_CLOSE = "POST_CLOSE"    # after the daily bar is final

SESSION_OPEN = time(9, 30)
MIN_DAILY_BARS = 60
BAR_MINUTES = 5


@dataclass(frozen=True)
class StrategyInput:
    now: datetime
    phase: str
    daily: Mapping[str, pd.DataFrame]
    intraday: Mapping[str, pd.DataFrame]
    watchlist: tuple[WatchItem, ...] = ()
    sessions_to_earnings: Mapping[str, int] = field(default_factory=dict)


def trade_ref(today: date, symbol: str) -> str:
    return f"{today:%Y%m%d}-{symbol}"


def _history(df: pd.DataFrame | None, today: date) -> pd.DataFrame | None:
    """Completed sessions before today, with indicators. Drops a partial bar for today."""
    if df is None or df.empty:
        return None
    hist = df[df.index < pd.Timestamp(today)]
    if len(hist) < MIN_DAILY_BARS:
        return None
    return add_daily_indicators(hist)


def _today_bars(df: pd.DataFrame | None, now: datetime) -> pd.DataFrame | None:
    """Today's 5-minute bars that have finished by `now`."""
    if df is None or df.empty:
        return None
    out = df[(df.index.date == now.date())
             & (df.index + pd.Timedelta(minutes=BAR_MINUTES) <= now)]
    return None if out.empty else out


def _size(entry: float, limit: float, stop: float, account: AccountState,
          params: StrategyParams) -> tuple[int, int]:
    """Shares = floor(net liq * risk% / per-share risk), capped by position size and cash."""
    per_share = entry - stop
    if per_share <= 0 or limit <= 0:
        return 0, 0
    qty = math.floor(account.net_liq * params.risk_per_trade_pct / 100.0 / per_share)
    qty = min(qty,
              math.floor(account.net_liq * params.max_position_pct / 100.0 / limit),
              math.floor(account.available_funds / limit))
    qty = max(qty, 0)
    qty_a = math.floor(qty * params.partial_fraction)
    if qty_a < 1 or qty - qty_a < 1:
        qty_a = 0
    return qty, qty_a


def _plan(symbol: str, setup: str, entry_type: str, entry: float, stop: float,
          adr_usd: float, today: date, account: AccountState,
          params: StrategyParams, reasons: list[str]) -> Action:
    entry, stop = round(entry, 2), round(stop, 2)
    width = entry - stop
    if width <= 0:
        return Action(NONE, symbol, setup=setup, reasons=(*reasons, "stop_not_below_entry"))
    if width > params.max_stop_adr_frac * adr_usd:
        return Action(NONE, symbol, setup=setup, reasons=(
            *reasons, f"stop_too_wide={width / adr_usd:.2f}adr"))
    limit = round(entry * (1 + params.max_entry_slippage_pct / 100.0), 2)
    qty, qty_a = _size(entry, limit, stop, account, params)
    if qty < 1:
        return Action(NONE, symbol, setup=setup, reasons=(*reasons, "qty_zero"))
    plan = TradePlan(
        ref=trade_ref(today, symbol), symbol=symbol, setup=setup, entry_type=entry_type,
        trigger_price=entry, limit_price=limit, stop_price=stop, qty=qty, qty_a=qty_a,
        target_price=round(entry + params.target_r * width, 2),
        risk_usd=round(qty * width, 2), adr=round(adr_usd, 4),
    )
    return Action(ENTER, symbol, plan=plan, setup=setup,
                  reasons=(*reasons, f"stop_width={width / adr_usd:.2f}adr"))


def _setup_a(symbol: str, hist: pd.DataFrame, intra: pd.DataFrame, today: date,
             account: AccountState, params: StrategyParams) -> Action:
    """Tightening then breakout over the opening-range high."""
    last = hist.iloc[-1]
    adr_usd = last["adr_pct"] / 100.0 * last["close"]
    n = params.tight_lookback_days
    recent, prior = hist.iloc[-n:], hist.iloc[-n - 1:-1]
    inside = bool(((recent["high"].values <= prior["high"].values)
                   & (recent["low"].values >= prior["low"].values)).any())
    rng_pct = (recent["high"] - recent["low"]) / recent["close"] * 100.0
    narrow = bool((rng_pct <= params.narrow_range_adr_frac * last["adr_pct"]).any())
    emas = [last["ema9"], last["ema21"], last["ema50"]]
    converged = bool(max(emas) - min(emas) <= params.ema_converge_adr_mult * adr_usd)
    reasons = [f"inside_day={inside}", f"narrow_day={narrow}", f"ema_converged={converged}"]

    n_bars = params.opening_range_minutes // BAR_MINUTES
    open_dt = intra.index[0]
    if open_dt.time() != SESSION_OPEN or len(intra) < n_bars:
        return Action(NONE, symbol, setup=SETUP_A, reasons=(*reasons, "opening_range_incomplete"))
    if not ((inside or narrow) and converged):
        return Action(NONE, symbol, setup=SETUP_A, reasons=tuple(reasons))
    opening = intra[intra.index < open_dt + timedelta(minutes=params.opening_range_minutes)]
    entry = float(opening["high"].max()) + params.tick
    stop = float(opening["low"].min())
    return _plan(symbol, SETUP_A, STOP_LIMIT, entry, stop, adr_usd, today, account, params, reasons)


def _pullback_state(hist: pd.DataFrame, intra: pd.DataFrame, params: StrategyParams):
    last = hist.iloc[-1]
    adr_usd = last["adr_pct"] / 100.0 * last["close"]
    ema9 = float(last["ema9"])
    low, high = float(intra["low"].min()), float(intra["high"].max())
    price = float(intra["close"].iloc[-1])
    touched = low <= ema9
    not_too_deep = low >= ema9 - adr_usd
    reclaimed = price > ema9
    strong = high > low and (price - low) / (high - low) >= params.close_in_range_min
    stacked = (not params.require_stacked_emas) or bool(last["ema9"] > last["ema21"] > last["ema50"])
    flags = dict(touched_ema9=touched, not_too_deep=not_too_deep, reclaimed=reclaimed,
                 strong_close=strong, emas_stacked=stacked)
    return flags, price, low, adr_usd


def _setup_b(symbol: str, hist: pd.DataFrame, intra: pd.DataFrame, today: date,
             account: AccountState, params: StrategyParams) -> Action:
    """Pullback to the prior day's EMA9 that is reclaimed into the close."""
    flags, price, low, adr_usd = _pullback_state(hist, intra, params)
    reasons = [f"{k}={v}" for k, v in flags.items()]
    if not all(flags.values()):
        return Action(NONE, symbol, setup=SETUP_B, reasons=tuple(reasons))
    return _plan(symbol, SETUP_B, LIMIT, price, low, adr_usd, today, account, params, reasons)


def _regime(bars: StrategyInput, params: StrategyParams, today: date) -> tuple[bool, str]:
    hist = _history(bars.daily.get(params.regime_symbol), today)
    if hist is None:
        return False, "regime_no_data"
    last = hist.iloc[-1]
    if last["close"] < last["ema50"]:
        return False, f"regime_off:{params.regime_symbol}_below_ema50"
    return True, "regime_on"


def _trailing_exits(bars: StrategyInput, positions: list[Position]) -> list[Action]:
    today, out = bars.now.date(), []
    for pos in sorted(positions, key=lambda p: p.symbol):
        if pos.qty <= 0:
            continue
        df = bars.daily.get(pos.symbol)
        if df is None or df.empty or df.index[-1].date() != today:
            out.append(Action(NONE, pos.symbol, reasons=("daily_bar_missing",)))
            continue
        last = add_daily_indicators(df).iloc[-1]
        if last["close"] < last["ema9"]:
            out.append(Action(EXIT, pos.symbol, reason="close_below_ema9", when=NEXT_OPEN,
                              reasons=(f"close={last['close']:.2f}", f"ema9={last['ema9']:.2f}")))
    return out


def decide(bars: StrategyInput, account: AccountState, positions: list[Position],
           params: StrategyParams) -> list[Action]:
    today = bars.now.date()
    if bars.phase == POST_CLOSE:
        return _trailing_exits(bars, positions)

    actions: list[Action] = []
    held = {p.symbol for p in positions if p.qty > 0}
    if bars.phase == PRE_CLOSE:
        for sym in sorted(held):
            n = bars.sessions_to_earnings.get(sym)
            if n is not None and n <= 1:
                actions.append(Action(EXIT, sym, reason="earnings", when=NOW,
                                      reasons=(f"sessions_to_earnings={n}",)))

    ok, why = _regime(bars, params, today)
    if not ok:
        actions.append(Action(NONE, params.regime_symbol, reasons=(why,)))
        return actions

    for item in sorted(bars.watchlist, key=lambda w: w.symbol):
        sym = item.symbol
        if sym in held:
            continue
        hist = _history(bars.daily.get(sym), today)
        intra = _today_bars(bars.intraday.get(sym), bars.now)
        if hist is None or intra is None:
            if bars.phase != INTRADAY:
                actions.append(Action(NONE, sym, reasons=("no_data",)))
            continue
        n = bars.sessions_to_earnings.get(sym)
        if n is not None and n <= params.earnings_block_sessions:
            if bars.phase != INTRADAY:
                actions.append(Action(NONE, sym, reasons=(f"earnings_window={n}",)))
            continue
        if bars.phase == OPEN_RANGE:
            actions.append(_setup_a(sym, hist, intra, today, account, params))
        elif bars.phase == PRE_CLOSE:
            actions.append(_setup_b(sym, hist, intra, today, account, params))
        elif bars.phase == INTRADAY:
            flags, *_ = _pullback_state(hist, intra, params)
            if flags["touched_ema9"] and flags["not_too_deep"]:
                actions.append(Action(NONE, sym, setup=SETUP_B, reasons=(
                    "pullback_precondition", f"reclaimed={flags['reclaimed']}")))
    return actions


def inputs_hash(bars: StrategyInput, account: AccountState, positions: list[Position],
                params: StrategyParams) -> str:
    """Fingerprint of everything decide() reads. Same hash must give the same actions."""
    h = hashlib.sha256()

    def put(text: str) -> None:
        h.update(text.encode())
        h.update(b"\x00")

    put(bars.now.isoformat())
    put(bars.phase)
    for name, frames in (("daily", bars.daily), ("intraday", bars.intraday)):
        for sym in sorted(frames):
            put(f"{name}:{sym}")
            h.update(pd.util.hash_pandas_object(frames[sym], index=True).values.tobytes())
    put(repr(sorted(bars.watchlist, key=lambda w: w.symbol)))
    put(repr(sorted(bars.sessions_to_earnings.items())))
    put(repr(account))
    put(repr(sorted(positions, key=lambda p: p.symbol)))
    put(repr(params))
    return h.hexdigest()[:16]
