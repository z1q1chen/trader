from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):

    @abstractmethod
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns to df."""
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add buy_signal / sell_signal boolean columns."""
        ...
