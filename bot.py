"""
Trading Bot — paper trading BTC/USDT with SMA crossover + ATR stop-loss.

Usage:
    # Backtest on last 60 days of hourly data
    python bot.py

    # Live paper trading (runs continuously, Ctrl+C to stop)
    python bot.py --live

    # Custom backtest
    python bot.py --timeframe 4h --days 90
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import time

import pandas as pd

from config.settings import BotConfig, StrategyConfig
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
        self._running = True

    # ── State persistence ──────────────────────────────────────

    def save_state(self) -> None:
        state = {
            "broker": self.broker.to_dict(),
            "stop_loss": self.stop_loss.to_dict(),
        }
        with open(self.cfg.state_file, "w") as f:
            json.dump(state, f, indent=2)
        self.logger.debug("State saved to %s", self.cfg.state_file)

    def load_state(self) -> bool:
        if not os.path.exists(self.cfg.state_file):
            return False
        with open(self.cfg.state_file) as f:
            state = json.load(f)
        self.broker = PaperBroker.from_dict(state["broker"], fee_rate=self.cfg.fee_rate)
        self.stop_loss = ATRStopLoss.from_dict(state["stop_loss"])
        self.logger.info("Restored state from %s (cash=$%.2f, trades=%d)",
                         self.cfg.state_file, self.broker.cash, len(self.broker.closed_trades))
        return True

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

    # ── Live paper trading ───────────────────────────────────────

    def run_live(self) -> None:
        """Run continuously, polling for new candles each interval."""
        self.load_state()

        self.logger.info(
            "Starting LIVE paper trading: %s %s | SMA(%d/%d) | poll=%ds",
            self.cfg.strategy.symbol,
            self.cfg.strategy.timeframe,
            self.cfg.strategy.sma_short,
            self.cfg.strategy.sma_long,
            self.cfg.poll_interval_sec,
        )
        self.logger.info("Cash: $%.2f | Open position: %s",
                         self.broker.cash,
                         "yes" if self.broker.has_position() else "no")
        self.logger.info("Press Ctrl+C to stop gracefully.\n")

        last_bar_time = None

        while self._running:
            try:
                # Fetch enough bars to compute indicators (sma_long + buffer)
                bars_needed = self.cfg.strategy.sma_long + 10
                df = fetch_ohlcv(
                    symbol=self.cfg.strategy.symbol,
                    timeframe=self.cfg.strategy.timeframe,
                    since_days_ago=max(bars_needed // 24 + 2, 7),
                    limit=bars_needed,
                )

                if df.empty:
                    self.logger.warning("No data returned, retrying...")
                    time.sleep(self.cfg.poll_interval_sec)
                    continue

                df = self.strategy.compute_indicators(df)
                df = self.strategy.generate_signals(df)

                latest = df.iloc[-1]
                bar_time = df.index[-1]

                # Only act on NEW bars
                if last_bar_time is not None and bar_time <= last_bar_time:
                    self.logger.debug("No new bar yet (latest: %s)", bar_time)
                else:
                    last_bar_time = bar_time
                    price = latest["close"]
                    self.logger.info(
                        "[TICK] %s | price=$%.2f | sma_%d=$%.2f | sma_%d=$%.2f",
                        bar_time,
                        price,
                        self.cfg.strategy.sma_short,
                        latest.get(f"sma_{self.cfg.strategy.sma_short}", 0),
                        self.cfg.strategy.sma_long,
                        latest.get(f"sma_{self.cfg.strategy.sma_long}", 0),
                    )
                    self._process_bar(latest)
                    self.save_state()

            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.error("Error in live loop: %s", e)

            # Sleep in small increments so Ctrl+C is responsive
            for _ in range(self.cfg.poll_interval_sec):
                if not self._running:
                    break
                time.sleep(1)

        self._shutdown()

    def stop(self) -> None:
        """Signal the live loop to stop."""
        self._running = False

    def _shutdown(self) -> None:
        self.save_state()
        summary = self.broker.summary()
        self.logger.info("")
        self.logger.info("=" * 60)
        self.logger.info("LIVE SESSION ENDED")
        self.logger.info("  Cash:         $%.2f", summary["cash"])
        self.logger.info("  Total Trades: %d", summary["total_trades"])
        self.logger.info("  Total PnL:    $%.2f", summary["total_pnl"])
        self.logger.info("  Win Rate:     %.0f%%", summary["win_rate"] * 100)
        self.logger.info("  State saved:  %s", self.cfg.state_file)
        self.logger.info("=" * 60)
        self._print_trade_log()

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
    parser.add_argument("--live", action="store_true", help="Run in live paper trading mode (continuous)")
    parser.add_argument("--timeframe", default="1h", help="Candle timeframe (default: 1h)")
    parser.add_argument("--days", type=int, default=60, help="Days of history for backtest (default: 60)")
    parser.add_argument("--cash", type=float, default=10_000, help="Starting cash (default: 10000)")
    parser.add_argument("--sma-short", type=int, default=10, help="Short SMA period (default: 10)")
    parser.add_argument("--sma-long", type=int, default=50, help="Long SMA period (default: 50)")
    parser.add_argument("--poll", type=int, default=60, help="Poll interval in seconds for live mode (default: 60)")
    args = parser.parse_args()

    cfg = BotConfig(
        mode="live_paper" if args.live else "backtest",
        initial_cash=args.cash,
        poll_interval_sec=args.poll,
        strategy=StrategyConfig(
            timeframe=args.timeframe,
            sma_short=args.sma_short,
            sma_long=args.sma_long,
        ),
    )

    bot = TradingBot(cfg)

    if args.live:
        # Graceful shutdown on Ctrl+C and SIGTERM
        signal.signal(signal.SIGTERM, lambda *_: bot.stop())
        bot.run_live()
    else:
        print(f"Fetching {args.days} days of {args.timeframe} BTC/USDT data...")
        df = fetch_ohlcv(
            symbol=cfg.strategy.symbol,
            timeframe=cfg.strategy.timeframe,
            since_days_ago=args.days,
        )
        print(f"Got {len(df)} candles.\n")
        bot.run_backtest(df)


if __name__ == "__main__":
    main()
