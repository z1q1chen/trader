"""Configuration: one `mode` switch selects the paper or live profile."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import yaml

HARD_MAX_RISK_PCT = 1.0  # design: 0.5% per trade, never above 1%


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_positions: int = 5
    max_order_usd: float = 2000.0
    max_position_pct: float = 30.0
    max_drawdown_pct: float = 15.0
    max_orders_per_day: int = 20
    max_limit_deviation_pct: float = 3.0
    max_bar_age_sec: int = 120
    max_stop_adr_frac: float = 0.5
    earnings_block_sessions: int = 2


LIVE_DEFAULTS = dict(
    max_daily_loss_pct=1.5, max_positions=3, max_order_usd=400.0,
    max_drawdown_pct=10.0, max_orders_per_day=10,
)


@dataclass(frozen=True)
class StrategyParams:
    regime_symbol: str = "QQQ"
    min_adr_pct: float = 4.0
    momentum_top_pct: float = 3.0
    min_dollar_volume: float = 20_000_000.0
    opening_range_minutes: int = 5
    # Setup A
    tight_lookback_days: int = 3
    narrow_range_adr_frac: float = 0.5
    ema_converge_adr_mult: float = 1.0
    # Setup B
    close_in_range_min: float = 0.7
    require_stacked_emas: bool = True
    # Orders
    tick: float = 0.01
    max_entry_slippage_pct: float = 0.3
    target_r: float = 3.0
    partial_fraction: float = 1 / 3
    # Copied from the active profile's limits so decide() stays a pure function.
    max_stop_adr_frac: float = 0.5
    risk_per_trade_pct: float = 0.5
    max_position_pct: float = 30.0
    earnings_block_sessions: int = 2


@dataclass(frozen=True)
class Profile:
    host: str
    port: int
    account: str
    db: str
    client_id: int = 17
    limits: Limits = field(default_factory=Limits)


@dataclass(frozen=True)
class Config:
    mode: str
    profile: Profile
    params: StrategyParams
    universe_file: str = "universe.txt"
    earnings_file: str = "earnings.csv"
    halt_file: str = "HALT"
    reports_dir: str = "reports"
    log_file: str = "logs/trader.log"
    alert_webhook_url: str = ""
    llm_model: str = "claude-opus-5-5"

    @property
    def limits(self) -> Limits:
        return self.profile.limits


def _pick(cls, raw: dict, where: str) -> dict:
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown keys in {where}: {sorted(unknown)}")
    return raw


def validate_account(mode: str, account: str) -> None:
    """Paper accounts start with DU; live accounts must not."""
    if not account:
        raise ConfigError("account is empty")
    is_demo = account.upper().startswith("DU")
    if mode == "paper" and not is_demo:
        raise ConfigError(f"paper mode requires a DU account, got {account}")
    if mode == "live" and is_demo:
        raise ConfigError(f"live mode must not use a DU account, got {account}")


def parse_config(raw: dict) -> Config:
    mode = raw.get("mode")
    if mode not in ("paper", "live"):
        raise ConfigError(f"mode must be 'paper' or 'live', got {mode!r}")
    profiles = raw.get("profiles") or {}
    if mode not in profiles:
        raise ConfigError(f"no profile for mode {mode!r}")
    p = dict(profiles[mode])
    lim_raw = _pick(Limits, dict(p.pop("limits", {}) or {}), f"profiles.{mode}.limits")
    base = LIVE_DEFAULTS if mode == "live" else {}
    limits = Limits(**{**base, **lim_raw})
    if not 0 < limits.risk_per_trade_pct <= HARD_MAX_RISK_PCT:
        raise ConfigError(
            f"risk_per_trade_pct must be in (0, {HARD_MAX_RISK_PCT}], got {limits.risk_per_trade_pct}")
    p = _pick(Profile, p, f"profiles.{mode}")
    for key in ("host", "port", "account", "db"):
        if key not in p:
            raise ConfigError(f"profiles.{mode}.{key} is required")
    profile = Profile(limits=limits, **p)
    validate_account(mode, profile.account)

    s_raw = _pick(StrategyParams, dict(raw.get("strategy", {}) or {}), "strategy")
    for owned in ("max_stop_adr_frac", "risk_per_trade_pct", "max_position_pct",
                  "earnings_block_sessions"):
        if owned in s_raw:
            raise ConfigError(f"strategy.{owned} is set by the profile limits, not here")
    params = replace(
        StrategyParams(**s_raw),
        max_stop_adr_frac=limits.max_stop_adr_frac,
        risk_per_trade_pct=limits.risk_per_trade_pct,
        max_position_pct=limits.max_position_pct,
        earnings_block_sessions=limits.earnings_block_sessions,
    )

    top = {k: v for k, v in raw.items() if k not in ("mode", "profiles", "strategy")}
    _pick(Config, top, "config")
    return Config(mode=mode, profile=profile, params=params, **top)


def load_config(path: str | Path = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f) or {})
