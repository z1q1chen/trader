import pandas as pd


def add_sma(df: pd.DataFrame, short: int = 10, long: int = 50) -> pd.DataFrame:
    """Add short and long SMA columns based on close price."""
    df = df.copy()
    df[f"sma_{short}"] = df["close"].rolling(window=short).mean()
    df[f"sma_{long}"] = df["close"].rolling(window=long).mean()
    return df
