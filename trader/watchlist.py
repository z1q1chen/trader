"""Pre-market screen: liquid, high-ADR momentum leaders."""

from __future__ import annotations

from datetime import date

import pandas as pd

from .config import StrategyParams
from .indicators import add_daily_indicators
from .models import WatchItem

MIN_HISTORY = 127  # six months of sessions plus one


def screen(
    daily: dict[str, pd.DataFrame],
    asof: date,
    params: StrategyParams,
    earnings: dict[str, date] | None = None,
) -> list[WatchItem]:
    """Rank the universe on sessions completed before `asof` and filter it.

    A stock qualifies when it is in the top `momentum_top_pct` percent by 1, 3 or
    6 month return, its ADR% is at least `min_adr_pct`, and its 20-day average
    dollar volume is at least `min_dollar_volume`.
    """
    earnings = earnings or {}
    rows = []
    for sym in sorted(daily):
        if sym == params.regime_symbol:
            continue
        df = daily[sym]
        df = df[df.index < pd.Timestamp(asof)]
        if len(df) < MIN_HISTORY:
            continue
        last = add_daily_indicators(df).iloc[-1]
        c = df["close"]
        rows.append({
            "symbol": sym,
            "perf_1m": c.iloc[-1] / c.iloc[-22] - 1,
            "perf_3m": c.iloc[-1] / c.iloc[-64] - 1,
            "perf_6m": c.iloc[-1] / c.iloc[-127] - 1,
            "adr_pct": float(last["adr_pct"]),
            "dollar_vol": float(last["dollar_vol"]),
            "ema9": float(last["ema9"]),
            "ema21": float(last["ema21"]),
            "ema50": float(last["ema50"]),
        })
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    cutoff = 1.0 - params.momentum_top_pct / 100.0
    leader = pd.Series(False, index=frame.index)
    for col in ("perf_1m", "perf_3m", "perf_6m"):
        leader |= frame[col].rank(pct=True) > cutoff - 1e-12
    keep = frame[
        leader
        & (frame["adr_pct"] >= params.min_adr_pct)
        & (frame["dollar_vol"] >= params.min_dollar_volume)
    ]
    return [
        WatchItem(
            date=asof, symbol=r.symbol, adr_pct=round(r.adr_pct, 4),
            ema9=round(r.ema9, 4), ema21=round(r.ema21, 4), ema50=round(r.ema50, 4),
            earnings_date=earnings.get(r.symbol),
        )
        for r in keep.sort_values("symbol").itertuples()
    ]
