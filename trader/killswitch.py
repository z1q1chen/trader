"""Kill switch: the HALT file and the manual flatten command."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .alerts import ERROR, Alerter
from .broker.base import Broker
from .execution import Executor


def halt_active(path: str | Path) -> bool:
    return Path(path).exists()


def set_halt(path: str | Path, reason: str, now: datetime) -> None:
    """Create the HALT file. Only a human removes it; the program never does."""
    p = Path(path)
    if not p.exists():
        p.write_text(f"{now.isoformat()} {reason}\n", encoding="utf-8")


def flatten(broker: Broker, executor: Executor, alerter: Alerter, today: date,
            now: datetime) -> dict:
    """Cancel every open order and sell every position. Run by a human only."""
    broker.cancel_all()
    broker.sleep(1)
    closed = []
    for pos in broker.positions():
        if pos.qty <= 0:
            continue
        if not executor.exit_now(pos.symbol, "flatten", today):
            # Position this system did not open: close it directly.
            broker.close_position(pos.symbol, pos.qty, f"FLATTEN-{pos.symbol}-{now:%H%M%S}")
        closed.append(pos.symbol)
    alerter.send(ERROR, "flatten", f"flatten executed: {closed or 'no positions'}")
    return {"closed": closed}
