"""Startup checklist. Any failure means exit (fatal) or read-only mode: no new orders."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable, Mapping

from .alerts import ERROR, INFO, Alerter
from .broker.base import Broker
from .config import Config, ConfigError, validate_account
from .execution import Executor
from .reconcile import reconcile
from .store import Store

MAX_CLOCK_SKEW_SEC = 2.0


@dataclass
class StartupResult:
    fatal: list[str] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.fatal

    @property
    def can_trade(self) -> bool:
        return not self.fatal and not self.read_only


def run_startup_checks(cfg: Config, broker: Broker, store: Store, executor: Executor,
                       alerter: Alerter, clock: Callable[[], datetime],
                       env: Mapping[str, str]) -> StartupResult:
    res = StartupResult()
    account = cfg.profile.account

    # 2-3. The logged-in account must be exactly the configured one. The DU prefix
    # decides paper vs live; the port number is never trusted for that.
    logged_in = broker.managed_accounts()
    if logged_in != [account]:
        res.fatal.append(f"account_mismatch: gateway={logged_in} config={account}")
    for acct in logged_in:
        try:
            validate_account(cfg.mode, acct)
        except ConfigError as e:
            res.fatal.append(f"wrong_environment: {e}")
    # 4. Live trading needs an explicit confirmation in the environment.
    if cfg.mode == "live" and env.get("TRADING_LIVE_CONFIRM", "") != account:
        res.fatal.append("live_not_confirmed: set TRADING_LIVE_CONFIRM to the live account id")
    if res.fatal:
        alerter.send(ERROR, "startup_fatal", "; ".join(res.fatal))
        return res

    # 5. Real-time data. Delayed quotes mean no new positions.
    if not broker.market_data_realtime():
        res.read_only.append("delayed_market_data")
    # 6-7. Reconcile with the broker and make sure every position has its stop.
    rec = reconcile(broker, store, executor, alerter, clock)
    res.read_only.extend(f"reconcile:{d}" for d in rec.diffs)
    # 8. Clock skew against the broker's server.
    skew = abs((clock() - broker.server_time()).total_seconds())
    if skew >= MAX_CLOCK_SKEW_SEC:
        res.read_only.append(f"clock_skew:{skew:.1f}s")

    # 9. Announce where we are running.
    if res.read_only:
        alerter.send(ERROR, "startup_read_only", "; ".join(res.read_only))
    alerter.send(INFO, "startup",
                 f"mode={cfg.mode} account={account} read_only={bool(res.read_only)}",
                 {"limits": asdict(cfg.limits), "repairs": rec.repairs})
    return res
