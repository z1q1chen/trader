"""SQLite state store: everything that must survive a restart.

Positions and orders are owned by the broker. This database remembers what the
broker does not know: why a trade was opened, where its stop was planned, whether
it has been scaled out.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .models import T_LIVE, T_PENDING, WORKING, TradePlan, WatchItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    date TEXT NOT NULL, symbol TEXT NOT NULL, theme TEXT NOT NULL DEFAULT '',
    adr_pct REAL, ema9 REAL, ema21 REAL, ema50 REAL, earnings_date TEXT,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL,
    setup TEXT NOT NULL DEFAULT '', reasons TEXT NOT NULL, inputs_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    entry_date TEXT NOT NULL, symbol TEXT NOT NULL, setup TEXT NOT NULL,
    entry_type TEXT NOT NULL, trigger_price REAL NOT NULL, limit_price REAL NOT NULL,
    entry_price REAL, stop_price REAL NOT NULL, target_price REAL NOT NULL,
    qty INTEGER NOT NULL, qty_a INTEGER NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0, open_qty INTEGER NOT NULL DEFAULT 0,
    risk_usd REAL NOT NULL, adr REAL NOT NULL, status TEXT NOT NULL,
    scaled_out INTEGER NOT NULL DEFAULT 0,
    exit_price REAL, exit_reason TEXT NOT NULL DEFAULT '',
    realized_pnl REAL NOT NULL DEFAULT 0, commission REAL NOT NULL DEFAULT 0,
    r_multiple REAL, opened_at TEXT, closed_at TEXT,
    UNIQUE (entry_date, symbol)
);
CREATE TABLE IF NOT EXISTS orders (
    client_ref TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL, broker_order_id TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL, stop_price REAL,
    status TEXT NOT NULL, filled_qty INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fills (
    exec_id TEXT PRIMARY KEY,
    client_ref TEXT NOT NULL, ts TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL, commission REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    date TEXT PRIMARY KEY,
    sod_net_liq REAL NOT NULL, net_liq REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0, peak_net_liq REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, ref TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _update(self, table: str, key_col: str, key: str, fields: dict) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE {key_col} = ?", (*fields.values(), key))
        self.db.commit()

    # ── watchlist ────────────────────────────────────────────────
    def save_watchlist(self, day: date, items: list[WatchItem]) -> None:
        self.db.execute("DELETE FROM watchlist WHERE date = ?", (day.isoformat(),))
        self.db.executemany(
            "INSERT INTO watchlist (date, symbol, theme, adr_pct, ema9, ema21, ema50, earnings_date)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(day.isoformat(), w.symbol, w.theme, w.adr_pct, w.ema9, w.ema21, w.ema50,
              w.earnings_date.isoformat() if w.earnings_date else None) for w in items])
        self.db.commit()

    def get_watchlist(self, day: date) -> list[WatchItem]:
        rows = self._all("SELECT * FROM watchlist WHERE date = ? ORDER BY symbol", (day.isoformat(),))
        return [WatchItem(
            date=day, symbol=r["symbol"], adr_pct=r["adr_pct"], ema9=r["ema9"], ema21=r["ema21"],
            ema50=r["ema50"], theme=r["theme"],
            earnings_date=date.fromisoformat(r["earnings_date"]) if r["earnings_date"] else None,
        ) for r in rows]

    def set_theme(self, day: date, symbol: str, theme: str) -> None:
        """The only write the LLM sidecar is allowed to make."""
        self.db.execute("UPDATE watchlist SET theme = ? WHERE date = ? AND symbol = ?",
                        (theme[:80], day.isoformat(), symbol))
        self.db.commit()

    # ── decisions ────────────────────────────────────────────────
    def add_decision(self, ts: datetime, symbol: str, action: str, setup: str,
                     reasons: tuple[str, ...] | list[str], inputs_hash: str) -> None:
        self.db.execute(
            "INSERT INTO decisions (ts, symbol, action, setup, reasons, inputs_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ts.isoformat(), symbol, action, setup, json.dumps(list(reasons)), inputs_hash))
        self.db.commit()

    def decisions_on(self, day: date) -> list[dict]:
        return self._all("SELECT * FROM decisions WHERE ts LIKE ? ORDER BY id", (f"{day.isoformat()}%",))

    # ── trades ───────────────────────────────────────────────────
    def create_trade(self, plan: TradePlan, day: date) -> bool:
        """Insert a PENDING trade. Returns False when this symbol already traded today."""
        try:
            self.db.execute(
                "INSERT INTO trades (id, entry_date, symbol, setup, entry_type, trigger_price,"
                " limit_price, stop_price, target_price, qty, qty_a, risk_usd, adr, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan.ref, day.isoformat(), plan.symbol, plan.setup, plan.entry_type,
                 plan.trigger_price, plan.limit_price, plan.stop_price, plan.target_price,
                 plan.qty, plan.qty_a, plan.risk_usd, plan.adr, T_PENDING))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def get_trade(self, trade_id: str) -> dict | None:
        return self._one("SELECT * FROM trades WHERE id = ?", (trade_id,))

    def update_trade(self, trade_id: str, **fields) -> None:
        self._update("trades", "id", trade_id, fields)

    def live_trades(self) -> list[dict]:
        marks = ",".join("?" * len(T_LIVE))
        return self._all(f"SELECT * FROM trades WHERE status IN ({marks}) ORDER BY id", T_LIVE)

    def live_trade_for(self, symbol: str) -> dict | None:
        for t in self.live_trades():
            if t["symbol"] == symbol:
                return t
        return None

    def trades_between(self, start: date, end: date) -> list[dict]:
        return self._all("SELECT * FROM trades WHERE entry_date BETWEEN ? AND ? ORDER BY id",
                         (start.isoformat(), end.isoformat()))

    # ── orders ───────────────────────────────────────────────────
    def add_order(self, *, client_ref: str, trade_id: str, role: str, side: str, type: str,
                  qty: int, price: float | None, stop_price: float | None, status: str,
                  created_at: datetime, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO orders (client_ref, trade_id, role, side, type, qty, price, stop_price,"
            " status, created_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (client_ref, trade_id, role, side, type, qty, price, stop_price, status,
             created_at.isoformat(), note))
        self.db.commit()

    def get_order(self, client_ref: str) -> dict | None:
        return self._one("SELECT * FROM orders WHERE client_ref = ?", (client_ref,))

    def update_order(self, client_ref: str, **fields) -> None:
        self._update("orders", "client_ref", client_ref, fields)

    def orders_for_trade(self, trade_id: str) -> list[dict]:
        return self._all("SELECT * FROM orders WHERE trade_id = ? ORDER BY created_at, client_ref",
                         (trade_id,))

    def working_orders(self) -> list[dict]:
        marks = ",".join("?" * len(WORKING))
        return self._all(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY client_ref", WORKING)

    def next_seq(self, trade_id: str, role: str) -> int:
        n = self._one("SELECT COUNT(*) AS n FROM orders WHERE trade_id = ? AND role = ?",
                      (trade_id, role))
        return n["n"] if n else 0

    # ── submissions (daily order-count guard) ────────────────────
    def record_submission(self, day: date, ref: str) -> None:
        self.db.execute("INSERT INTO submissions (date, ref) VALUES (?, ?)", (day.isoformat(), ref))
        self.db.commit()

    def submissions_on(self, day: date) -> int:
        return self._one("SELECT COUNT(*) AS n FROM submissions WHERE date = ?",
                         (day.isoformat(),))["n"]

    # ── fills ────────────────────────────────────────────────────
    def add_fill(self, fill) -> bool:
        """Returns False when this exec_id was already recorded."""
        try:
            self.db.execute(
                "INSERT INTO fills (exec_id, client_ref, ts, symbol, side, qty, price, commission)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fill.exec_id, fill.ref, fill.ts.isoformat(), fill.symbol, fill.side, fill.qty,
                 fill.price, fill.commission))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def fills_for(self, client_ref_prefix: str) -> list[dict]:
        return self._all("SELECT * FROM fills WHERE client_ref LIKE ? ORDER BY ts, exec_id",
                         (client_ref_prefix + "%",))

    # ── equity ───────────────────────────────────────────────────
    def peak_net_liq(self) -> float:
        row = self._one("SELECT MAX(peak_net_liq) AS p FROM equity")
        return float(row["p"]) if row and row["p"] is not None else 0.0

    def start_day(self, day: date, net_liq: float) -> float:
        """Record start-of-day equity once per day. Returns the stored start-of-day value."""
        row = self._one("SELECT sod_net_liq FROM equity WHERE date = ?", (day.isoformat(),))
        if row:
            return float(row["sod_net_liq"])
        peak = max(self.peak_net_liq(), net_liq)
        self.db.execute(
            "INSERT INTO equity (date, sod_net_liq, net_liq, peak_net_liq) VALUES (?, ?, ?, ?)",
            (day.isoformat(), net_liq, net_liq, peak))
        self.db.commit()
        return net_liq

    def snapshot_equity(self, day: date, net_liq: float, realized_pnl: float) -> None:
        self.start_day(day, net_liq)
        peak = max(self.peak_net_liq(), net_liq)
        self.db.execute(
            "UPDATE equity SET net_liq = ?, realized_pnl = ?, peak_net_liq = ? WHERE date = ?",
            (net_liq, realized_pnl, peak, day.isoformat()))
        self.db.commit()

    def equity_curve(self) -> list[dict]:
        return self._all("SELECT * FROM equity ORDER BY date")

    # ── events ───────────────────────────────────────────────────
    def add_event(self, ts: datetime, level: str, kind: str, payload: dict | str) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        self.db.execute("INSERT INTO events (ts, level, kind, payload) VALUES (?, ?, ?, ?)",
                        (ts.isoformat(), level, kind, text))
        self.db.commit()

    def events(self, kind: str | None = None) -> list[dict]:
        if kind:
            return self._all("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))
        return self._all("SELECT * FROM events ORDER BY id")
