"""LLM sidecar: reads the state store, writes words.

It sits outside the order path. It opens the database read-only, has no access to
the broker, and the only thing it can change is the `theme` label on watchlist
rows (through `Store.set_theme`). If the LLM is unavailable the report is built
from plain statistics and trading is unaffected.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

log = logging.getLogger("trader.llm")

REVIEW_SYSTEM = (
    "You write the end-of-day review for a rule-based momentum swing trading system. "
    "You are given the day's decisions, trades, alerts and equity as JSON. Describe what "
    "the system did and why, using only the data given. Point out anything that looks like "
    "a software fault (missing stops, duplicate orders, reconcile mismatches, rejects). "
    "Do not predict prices and do not recommend trades. Write in Chinese, in Markdown, "
    "under 400 words."
)
THEME_SYSTEM = (
    "You label stocks with a short sector or theme tag (for example: AI semiconductors, "
    "nuclear power, crypto miners). Reply with one line per symbol in the form "
    "SYMBOL: theme. No other text."
)


def open_readonly(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def gather_day(con: sqlite3.Connection, day: date) -> dict:
    d = day.isoformat()

    def rows(sql: str, args: tuple) -> list[dict]:
        return [dict(r) for r in con.execute(sql, args).fetchall()]

    return {
        "date": d,
        "watchlist": rows("SELECT symbol, theme, adr_pct, earnings_date FROM watchlist WHERE date = ?", (d,)),
        "decisions": rows("SELECT ts, symbol, action, setup, reasons FROM decisions WHERE ts LIKE ?", (d + "%",)),
        "trades_opened": rows("SELECT * FROM trades WHERE entry_date = ?", (d,)),
        "trades_closed": rows("SELECT * FROM trades WHERE closed_at LIKE ? AND status = 'CLOSED'", (d + "%",)),
        "open_trades": rows("SELECT id, symbol, setup, entry_price, stop_price, open_qty, scaled_out, status"
                            " FROM trades WHERE status IN ('OPEN', 'EXIT_QUEUED')", ()),
        "alerts": rows("SELECT ts, level, kind, payload FROM events WHERE ts LIKE ? AND level != 'INFO'", (d + "%",)),
        "equity": rows("SELECT * FROM equity WHERE date = ?", (d,)),
    }


def stats_report(data: dict) -> str:
    """Deterministic report used on its own when no LLM is available."""
    actions = Counter(x["action"] for x in data["decisions"])
    lines = [f"# 复盘 {data['date']}", "", "## 数据"]
    eq = data["equity"][0] if data["equity"] else None
    if eq:
        change = eq["net_liq"] - eq["sod_net_liq"]
        lines.append(f"- 净值 {eq['net_liq']:.2f}（当日 {change:+.2f}，历史高点 {eq['peak_net_liq']:.2f}）")
    lines.append(f"- 观察名单 {len(data['watchlist'])} 只：" + ", ".join(w["symbol"] for w in data["watchlist"]))
    lines.append("- 判定：" + (", ".join(f"{k} {v}" for k, v in sorted(actions.items())) or "无"))
    lines.append(f"- 新开交易 {len(data['trades_opened'])} 笔，平仓 {len(data['trades_closed'])} 笔，"
                 f"持仓 {len(data['open_trades'])} 笔")
    for t in data["trades_closed"]:
        r = "n/a" if t["r_multiple"] is None else f"{t['r_multiple']:+.2f}R"
        lines.append(f"  - {t['symbol']} {t['setup']} {t['exit_reason']} {r}")
    lines.append(f"- 报警 {len(data['alerts'])} 条")
    for a in data["alerts"]:
        lines.append(f"  - {a['level']} {a['kind']}")
    return "\n".join(lines) + "\n"


def _ask(model: str, system: str, user: str) -> str | None:
    """One Claude request. Returns None on any failure: the sidecar must never raise."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.RateLimitError as e:
        log.warning("LLM rate limited: %s", e)
        return None
    except anthropic.APIStatusError as e:
        log.warning("LLM API error %s: %s", e.status_code, e)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("LLM unreachable: %s", e)
        return None
    except Exception as e:  # missing credentials and anything else: degrade, never crash
        log.warning("LLM unavailable: %s", e)
        return None
    if response.stop_reason == "refusal":
        log.warning("LLM declined the request")
        return None
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or None


def daily_review(db_path: str, day: date, reports_dir: str, model: str,
                 use_llm: bool = True) -> Path:
    """Write reports/<date>.md and return its path."""
    con = open_readonly(db_path)
    try:
        data = gather_day(con, day)
    finally:
        con.close()
    report = stats_report(data)
    if use_llm:
        narrative = _ask(model, REVIEW_SYSTEM, json.dumps(data, default=str, ensure_ascii=False))
        if narrative:
            report += "\n## 说明\n\n" + narrative + "\n"
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{day.isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    return path


def parse_themes(text: str, allowed: set[str]) -> dict[str, str]:
    """Parse 'SYMBOL: theme' lines, keeping only symbols that are on the watchlist."""
    out = {}
    for line in text.splitlines():
        sym, sep, theme = line.partition(":")
        sym = sym.strip().upper().strip("-* ")
        if sep and sym in allowed and theme.strip():
            out[sym] = theme.strip()[:80]
    return out


def tag_themes(store, day: date, model: str) -> dict[str, str]:
    """Label today's watchlist. `store` is used only for its set_theme method."""
    symbols = [w.symbol for w in store.get_watchlist(day)]
    if not symbols:
        return {}
    text = _ask(model, THEME_SYSTEM, "Symbols: " + ", ".join(symbols))
    themes = parse_themes(text or "", set(symbols))
    for sym, theme in themes.items():
        store.set_theme(day, sym, theme)
    return themes
