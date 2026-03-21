import pandas as pd

from strategy.base import BaseStrategy
from indicators.moving_averages import add_sma
from indicators.volatility import compute_atr


class SMACrossoverStrategy(BaseStrategy):
    """
    Simple Moving Average crossover strategy.
    Buy on golden cross (short SMA > long SMA), sell on death cross.
    """

    def __init__(self, short: int = 10, long: int = 50, atr_period: int = 14):
        self.short = short
        self.long = long
        self.atr_period = atr_period

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = add_sma(df, self.short, self.long)
        df["atr"] = compute_atr(df, self.atr_period)
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        sc = f"sma_{self.short}"
        lc = f"sma_{self.long}"

        # Use shift(1) to prevent look-ahead bias: we only act on
        # the CONFIRMED previous-bar crossover
        prev_below = df[sc].shift(1) < df[lc].shift(1)
        curr_above = df[sc] >= df[lc]

        df["buy_signal"] = prev_below & curr_above
        df["sell_signal"] = ~prev_below & ~curr_above

        # Zero out signals where indicators aren't populated yet
        insufficient = df[sc].isna() | df[lc].isna() | df["atr"].isna()
        df.loc[insufficient, ["buy_signal", "sell_signal"]] = False

        return df
