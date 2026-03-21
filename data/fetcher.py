"""
BTC/USDT price data fetcher.
Uses ccxt (Binance) for OHLCV data. No API key needed for public market data.
"""

import time
import pandas as pd
import ccxt


def get_exchange(exchange_id: str = "binance") -> ccxt.Exchange:
    exchange_class = getattr(ccxt, exchange_id)
    return exchange_class({"enableRateLimit": True})


def fetch_ohlcv(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    since_days_ago: int = 30,
    limit: int = 500,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    """Fetch recent OHLCV data. Good for quick backtests."""
    exchange = get_exchange(exchange_id)
    since_ms = exchange.milliseconds() - since_days_ago * 86_400_000

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)

    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df


def fetch_ohlcv_full(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    start_date: str = "2024-01-01",
    end_date: str | None = None,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    """Paginate through large date ranges for full history."""
    exchange = get_exchange(exchange_id)

    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    since_ms = exchange.parse8601(f"{start_date}T00:00:00Z")
    end_ms = exchange.milliseconds() if end_date is None else exchange.parse8601(f"{end_date}T00:00:00Z")

    all_candles = []
    print(f"Fetching {symbol} {timeframe} from {start_date} ...")

    while since_ms < end_ms:
        candles = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not candles:
            break
        all_candles.extend(candles)
        since_ms = candles[-1][0] + timeframe_ms
        print(f"  {len(all_candles)} candles, last: {exchange.iso8601(candles[-1][0])}")
        time.sleep(exchange.rateLimit / 1000)

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)
    return df


def fetch_current_price(
    symbol: str = "BTC/USDT",
    exchange_id: str = "binance",
) -> dict:
    """Fetch real-time ticker for live simulation."""
    exchange = get_exchange(exchange_id)
    ticker = exchange.fetch_ticker(symbol)
    return {
        "symbol": ticker["symbol"],
        "last": ticker["last"],
        "bid": ticker["bid"],
        "ask": ticker["ask"],
        "datetime": ticker["datetime"],
    }
