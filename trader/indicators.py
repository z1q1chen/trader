"""Daily indicators. Inputs are OHLCV frames indexed by session date."""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def adr_pct(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Average daily range in percent: 100 * (mean(high / low) - 1)."""
    return 100.0 * ((df["high"] / df["low"]).rolling(n).mean() - 1.0)


def add_daily_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema9"] = ema(out["close"], 9)
    out["ema21"] = ema(out["close"], 21)
    out["ema50"] = ema(out["close"], 50)
    out["adr_pct"] = adr_pct(out)
    out["dollar_vol"] = (out["close"] * out["volume"]).rolling(20).mean()
    return out
