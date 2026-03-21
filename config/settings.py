from dataclasses import dataclass, field
from typing import Literal


@dataclass
class StrategyConfig:
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    sma_short: int = 10
    sma_long: int = 50
    atr_period: int = 14
    atr_multiplier: float = 2.0


@dataclass
class BotConfig:
    mode: Literal["backtest", "live_paper"] = "backtest"
    initial_cash: float = 10_000.0
    position_pct: float = 0.95  # fraction of cash to deploy per trade
    fee_rate: float = 0.001  # 0.1% taker fee (Binance default)
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
