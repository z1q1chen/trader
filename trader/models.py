"""Plain data types shared by every module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Action kinds
ENTER = "ENTER"
EXIT = "EXIT"
MODIFY_STOP = "MODIFY_STOP"
NONE = "NONE"

# Exit timing
NOW = "NOW"
NEXT_OPEN = "NEXT_OPEN"

# Setups
SETUP_A = "A_BREAKOUT"
SETUP_B = "B_PULLBACK"

# Entry order types
STOP_LIMIT = "STOP_LIMIT"
LIMIT = "LIMIT"

# Normalised order statuses
PENDING_SUBMIT = "PENDING_SUBMIT"
SUBMITTED = "SUBMITTED"
PARTIAL = "PARTIAL"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
WORKING = (PENDING_SUBMIT, SUBMITTED, PARTIAL, UNKNOWN)
TERMINAL = (FILLED, CANCELLED, REJECTED)

# Trade statuses
T_PENDING = "PENDING"
T_OPEN = "OPEN"
T_EXIT_QUEUED = "EXIT_QUEUED"
T_CLOSED = "CLOSED"
T_CANCELLED = "CANCELLED"
T_REJECTED = "REJECTED"
T_LIVE = (T_PENDING, T_OPEN, T_EXIT_QUEUED)

# Order roles
A_PARENT = "A_PARENT"
A_STOP = "A_STOP"
A_TARGET = "A_TARGET"
B_PARENT = "B_PARENT"
B_STOP = "B_STOP"
EXIT_ORDER = "EXIT"
PROTECT = "PROTECT"
PARENT_ROLES = (A_PARENT, B_PARENT)
STOP_ROLES = (A_STOP, B_STOP, PROTECT)


@dataclass(frozen=True)
class AccountState:
    account_id: str
    net_liq: float
    available_funds: float
    realized_pnl_today: float = 0.0


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_cost: float


@dataclass(frozen=True)
class Order:
    """An order as the broker currently sees it."""

    ref: str
    symbol: str
    side: str  # BUY | SELL
    type: str  # STP | LMT | STP LMT | MKT
    qty: int
    status: str
    stop_price: float | None = None
    limit_price: float | None = None
    filled_qty: int = 0
    broker_order_id: str = ""


@dataclass(frozen=True)
class Fill:
    exec_id: str
    ref: str
    symbol: str
    side: str
    qty: int
    price: float
    commission: float
    ts: datetime


@dataclass(frozen=True)
class TradePlan:
    ref: str  # idempotency key: YYYYMMDD-SYMBOL
    symbol: str
    setup: str
    entry_type: str
    trigger_price: float  # stop trigger (A) or reference price (B)
    limit_price: float
    stop_price: float
    qty: int
    qty_a: int  # part that carries the 3R target; 0 = group B only
    target_price: float
    risk_usd: float
    adr: float  # ADR in dollars

    @property
    def qty_b(self) -> int:
        return self.qty - self.qty_a

    @property
    def notional(self) -> float:
        return self.qty * self.limit_price


@dataclass(frozen=True)
class Action:
    kind: str
    symbol: str
    plan: TradePlan | None = None
    reason: str = ""
    when: str = NOW
    price: float | None = None
    setup: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchItem:
    date: date
    symbol: str
    adr_pct: float
    ema9: float
    ema21: float
    ema50: float
    earnings_date: date | None = None
    theme: str = ""


def order_ref(trade_ref: str, role: str, seq: int = 0) -> str:
    return f"{trade_ref}:{role}" if seq == 0 else f"{trade_ref}:{role}:{seq}"


def split_ref(ref: str) -> tuple[str, str]:
    """'20261125-NVDA:A_STOP' -> ('20261125-NVDA', 'A_STOP')."""
    parts = ref.split(":")
    return parts[0], (parts[1] if len(parts) > 1 else "")
