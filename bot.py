"""
Trading Bot — paper trading BTC/USDT with SMA crossover + ATR stop-loss.

Usage:
    # Backtest on last 60 days of hourly data
    python bot.py

    # Custom: 90 days of 4h candles
    python bot.py --timeframe 4h --days 90
"""

import argparse
import logging
import logging.handlers
import os
import sys

import pandas as pd

from config.settings import BotConfig
from data.fetcher import fetch_ohlcv
from strategy.sma_crossover import SMACrossoverStrategy
from risk.stop_loss import ATRStopLoss
from execution.paper_broker import PaperBroker


def setup_logging(cfg: BotConfig) -> logging.Logger:
    log = logging.getLogger("trader")
    log.setLevel(cfg.log_level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    os.makedirs(os.path.dirname(cfg.log_file), exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        cfg.log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    return log


class TradingBot:

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.logger = setup_logging(cfg)
        self.strategy = SMACrossoverStrategy(
            short=cfg.strategy.sma_short,
            long=cfg.strategy.sma_long,
            atr_period=cfg.strategy.atr_period,
        )
        self.broker = PaperBroker(
            initial_cash=cfg.initial_cash,
            fee_rate=cfg.fee_rate,
        )
        self.stop_loss = ATRStopLoss(multiplier=cfg.strategy.atr_multiplier)

    def run_backtest(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run strategy over historical data bar by bar."""
        self.logger.info(
            "Starting backtest: %s %s | SMA(%d/%d) | ATR(%d) x%.1f | cash=$%.0f",
            self.cfg.strategy.symbol,
            self.cfg.strategy.timeframe,
            self.cfg.strategy.sma_short,
            self.cfg.strategy.sma_long,
            self.cfg.strategy.atr_period,
            self.cfg.strategy.atr_multiplier,
            self.cfg.initial_cash,
        )
        self.logger.info("Data: %d bars from %s to %s", len(df), df.index[0], df.index[-1])

        df = self.strategy.compute_indicators(df)
        df = self.strategy.generate_signals(df)

        for _, row in df.iterrows():
            self._process_bar(row)

        # Force-close any open position at end of data
        if self.broker.has_position():
            last_price = df.iloc[-1]["close"]
            self.logger.info("[END] Force-closing open position @ $%.2f", last_price)
            self.broker.sell(self.cfg.strategy.symbol, last_price, reason="end_of_data")
            self.stop_loss.reset()

        summary = self.broker.summary()
        self.logger.info("=" * 60)
        self.logger.info("BACKTEST RESULTS")
        self.logger.info("  Cash:         $%.2f", summary["cash"])
        self.logger.info("  Total Trades: %d", summary["total_trades"])
        self.logger.info("  Total PnL:    $%.2f", summary["total_pnl"])
        self.logger.info("  Win Rate:     %.0f%%", summary["win_rate"] * 100)
        self.logger.info("=" * 60)

        self._print_trade_log()
        return df

    def _process_bar(self, row: pd.Series) -> None:
        price = row["close"]

        # 1. Check stop-loss FIRST (risk management priority)
        if self.broker.has_position():
            self.stop_loss.update(price)
            if self.stop_loss.is_stopped(price):
                self.logger.warning(
                    "[STOP] Triggered @ $%.2f | stop=$%.2f",
                    price, self.stop_loss.stop_price,
                )
                self.broker.sell(self.cfg.strategy.symbol, price, reason="stop_loss")
                self.stop_loss.reset()
                return

        # 2. Sell signal (death cross)
        if self.broker.has_position() and row.get("sell_signal", False):
            self.broker.sell(self.cfg.strategy.symbol, price, reason="death_cross")
            self.stop_loss.reset()

        # 3. Buy signal (golden cross)
        elif not self.broker.has_position() and row.get("buy_signal", False):
            qty = (self.broker.cash * self.cfg.position_pct) / price
            self.broker.buy(self.cfg.strategy.symbol, price, qty)
            atr_val = row.get("atr", 0)
            self.stop_loss.on_entry(price, atr_val)
            self.logger.info(
                "[SIGNAL] Golden cross | ATR=%.2f | stop=$%.2f",
                atr_val, self.stop_loss.stop_price,
            )

    def _print_trade_log(self) -> None:
        trades = self.broker.closed_trades
        if not trades:
            self.logger.info("No trades executed.")
            return

        self.logger.info("")
        self.logger.info("TRADE LOG:")
        self.logger.info(
            "%-8s  %-10s  %-10s  %-10s  %-8s",
            "ID", "Entry", "Exit", "PnL", "Reason",
        )
        for t in trades:
            self.logger.info(
                "%-8s  $%-9.2f  $%-9.2f  $%-9.2f  %s",
                t.trade_id,
                t.entry_price,
                t.exit_price or 0,
                t.pnl or 0,
                t.exit_reason,
            )


def main():
    parser = argparse.ArgumentParser(description="BTC/USDT Paper Trading Bot")
    parser.add_argument("--timeframe", default="1h", help="Candle timeframe (default: 1h)")
    parser.add_argument("--days", type=int, default=60, help="Days of history (default: 60)")
    parser.add_argument("--cash", type=float, default=10_000, help="Starting cash (default: 10000)")
    parser.add_argument("--sma-short", type=int, default=10, help="Short SMA period (default: 10)")
    parser.add_argument("--sma-long", type=int, default=50, help="Long SMA period (default: 50)")
    args = parser.parse_args()

    cfg = BotConfig(
        initial_cash=args.cash,
        strategy=__import__("config.settings", fromlist=["StrategyConfig"]).StrategyConfig(
            timeframe=args.timeframe,
            sma_short=args.sma_short,
            sma_long=args.sma_long,
        ),
    )

    print(f"Fetching {args.days} days of {args.timeframe} BTC/USDT data...")
    df = fetch_ohlcv(
        symbol=cfg.strategy.symbol,
        timeframe=cfg.strategy.timeframe,
        since_days_ago=args.days,
    )
    print(f"Got {len(df)} candles.\n")

    bot = TradingBot(cfg)
    bot.run_backtest(df)


if __name__ == "__main__":
    main()
