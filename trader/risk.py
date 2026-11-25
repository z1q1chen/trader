"""Risk gate: hard limits every entry plan must pass. The strategy cannot bypass it."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime

from .config import HARD_MAX_RISK_PCT, Limits
from .models import AccountState, TradePlan


@dataclass(frozen=True)
class RiskContext:
    now: datetime
    account: AccountState
    day_start_net_liq: float
    peak_net_liq: float
    open_positions: int          # open positions plus entries still working
    orders_today: int
    last_price: float
    last_bar_end: datetime | None
    data_realtime: bool = True
    sessions_to_earnings: int | None = None
    halted: bool = False         # HALT file present
    trading_enabled: bool = True  # False while disconnected / not reconciled / read-only


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    plan: TradePlan | None
    reasons: tuple[str, ...]
    halt: bool = False  # drawdown breaker: the caller must stop the system


class RiskGate:
    def __init__(self, limits: Limits):
        self._limits = limits

    @property
    def limits(self) -> Limits:
        return self._limits

    def daily_loss_hit(self, ctx: RiskContext) -> bool:
        if ctx.day_start_net_liq <= 0:
            return False
        loss_pct = (ctx.day_start_net_liq - ctx.account.net_liq) / ctx.day_start_net_liq * 100
        return loss_pct >= self._limits.max_daily_loss_pct

    def drawdown_hit(self, ctx: RiskContext) -> bool:
        if ctx.peak_net_liq <= 0:
            return False
        dd_pct = (ctx.peak_net_liq - ctx.account.net_liq) / ctx.peak_net_liq * 100
        return dd_pct >= self._limits.max_drawdown_pct

    def check(self, plan: TradePlan, ctx: RiskContext) -> RiskDecision:
        lim = self._limits

        def reject(reason: str, halt: bool = False) -> RiskDecision:
            return RiskDecision(False, None, (reason,), halt)

        if ctx.halted:
            return reject("halt_file_present")
        if not ctx.trading_enabled:
            return reject("trading_disabled")
        if self.drawdown_hit(ctx):
            return reject("max_drawdown", halt=True)
        if self.daily_loss_hit(ctx):
            return reject("max_daily_loss")
        if not ctx.data_realtime:
            return reject("delayed_market_data")
        if ctx.last_bar_end is None or (
                ctx.now - ctx.last_bar_end).total_seconds() > lim.max_bar_age_sec:
            return reject("stale_market_data")
        if ctx.sessions_to_earnings is not None and (
                ctx.sessions_to_earnings <= lim.earnings_block_sessions):
            return reject("earnings_window")
        if ctx.open_positions >= lim.max_positions:
            return reject("max_positions")
        if ctx.orders_today >= lim.max_orders_per_day:
            return reject("max_orders_per_day")

        width = plan.trigger_price - plan.stop_price
        if width <= 0:
            return reject("stop_not_below_entry")
        if plan.adr <= 0 or width > lim.max_stop_adr_frac * plan.adr + 1e-9:
            return reject("stop_too_wide")
        if ctx.last_price <= 0 or (
                abs(plan.limit_price - ctx.last_price) / ctx.last_price * 100
                > lim.max_limit_deviation_pct):
            return reject("limit_price_deviation")

        reasons: list[str] = []
        qty = plan.qty
        # Risk per trade: never above the configured risk, never above the hard cap.
        risk_pct = min(lim.risk_per_trade_pct, HARD_MAX_RISK_PCT)
        max_by_risk = math.floor(ctx.account.net_liq * risk_pct / 100 / width + 1e-9)
        if qty > max_by_risk:
            qty = max_by_risk
            reasons.append("truncated_to_risk")
        max_by_position = math.floor(
            ctx.account.net_liq * lim.max_position_pct / 100 / plan.limit_price)
        if qty > max_by_position:
            qty = max_by_position
            reasons.append("truncated_to_position_cap")
        if qty < 1:
            return reject("qty_zero_after_limits")
        if qty != plan.qty:
            qty_a = math.floor(qty * plan.qty_a / plan.qty) if plan.qty_a else 0
            if qty_a < 1 or qty - qty_a < 1:
                qty_a = 0
            plan = replace(plan, qty=qty, qty_a=qty_a, risk_usd=round(qty * width, 2))
        if plan.notional > lim.max_order_usd:
            return reject("max_order_usd")
        if plan.notional > ctx.account.available_funds:
            return reject("insufficient_funds")
        return RiskDecision(True, plan, tuple(reasons) or ("ok",))
