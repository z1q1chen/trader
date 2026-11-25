"""Synthetic market data for tests. Everything is seeded and deterministic."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from trader.models import ET

COLS = ["open", "high", "low", "close", "volume"]


def daily_frame(closes, end: date, range_pct: float = 5.0, volume: float = 2_000_000) -> pd.DataFrame:
    """Daily bars ending on business day `end`; every bar's range is `range_pct` of its close."""
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    half = range_pct / 200.0
    df = pd.DataFrame({
        "open": np.r_[closes[0], closes[:-1]],
        "high": closes * (1 + half), "low": closes * (1 - half),
        "close": closes, "volume": volume,
    }, index=idx)
    return df[COLS]


def trend(n: int, start: float, daily_pct: float) -> np.ndarray:
    return start * (1 + daily_pct / 100.0) ** np.arange(n)


def bars_5m(day: date, rows: list[tuple[float, float, float, float]], volume: float = 20_000) -> pd.DataFrame:
    """5-minute bars from 09:30 ET. rows are (open, high, low, close)."""
    start = datetime.combine(day, time(9, 30), tzinfo=ET)
    idx = pd.DatetimeIndex([start + timedelta(minutes=5 * i) for i in range(len(rows))])
    df = pd.DataFrame(rows, columns=COLS[:4], index=idx)
    df["volume"] = volume
    return df


def path_bars(day: date, open_: float, close: float, wiggle_pct: float, rng, n: int = 78,
              first_bar_pct: float | None = None, volume: float = 15_000) -> pd.DataFrame:
    """A noisy straight-line path from open to close over n bars."""
    base = np.linspace(open_, close, n + 1)
    noise = rng.normal(0, wiggle_pct / 100.0 * open_, n + 1)
    noise[0] = noise[-1] = 0
    px = base + noise
    rows = []
    for i in range(n):
        o, c = px[i], px[i + 1]
        pad = abs(rng.normal(0, wiggle_pct / 400.0 * open_))
        rows.append((o, max(o, c) + pad, min(o, c) - pad, c))
    if first_bar_pct is not None:  # a tight, controlled opening range
        half = first_bar_pct / 200.0 * open_
        first_close = open_ + half / 2
        rows[0] = (open_, open_ + half, open_ - half, first_close)
        _, h1, l1, c1 = rows[1]
        rows[1] = (first_close, max(h1, first_close), min(l1, first_close), c1)
    return bars_5m(day, rows, volume)


def aggregate_daily(intraday: pd.DataFrame) -> pd.DataFrame:
    g = intraday.groupby(intraday.index.date)
    df = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    })
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index))
    return df[COLS]


def market(sessions: list[date], seed: int = 7):
    """A small market: LEAD repeats tighten-then-breakout, three laggards drift, QQQ rises.

    Returns (daily, intraday) dicts keyed by symbol.
    """
    rng = np.random.default_rng(seed)
    frames: dict[str, list[pd.DataFrame]] = {s: [] for s in ("LEAD", "LAG1", "LAG2", "LAG3", "QQQ")}
    px = {"LEAD": 40.0, "LAG1": 60.0, "LAG2": 25.0, "LAG3": 90.0, "QQQ": 400.0}
    # (close change %, intraday wiggle %, opening-range width %)
    cycle = [(2.0, 1.6, None), (-2.5, 1.6, None), (-1.0, 1.4, None), (0.0, 0.25, None), (4.0, 0.35, 0.4)]
    for i, day in enumerate(sessions):
        chg, wig, first = cycle[i % len(cycle)]
        o = px["LEAD"]
        c = o * (1 + chg / 100.0)
        frames["LEAD"].append(path_bars(day, o, c, wig, rng, first_bar_pct=first, volume=20_000))
        px["LEAD"] = c
        for sym, drift, wig in (("LAG1", 0.0, 0.8), ("LAG2", -0.05, 0.9), ("LAG3", 0.02, 0.7),
                                ("QQQ", 0.08, 0.3)):
            o = px[sym]
            c = o * (1 + drift / 100.0 + rng.normal(0, 0.002))
            frames[sym].append(path_bars(day, o, c, wig, rng, volume=12_000))
            px[sym] = c
    intra = {s: pd.concat(parts) for s, parts in frames.items()}
    daily = {s: aggregate_daily(df) for s, df in intra.items()}
    return daily, intra
