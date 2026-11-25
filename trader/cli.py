"""Command line: python -m trader <command>."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .config import Config, load_config
from .models import ET


def _setup_logging(cfg: Config) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
    file = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=5_000_000, backupCount=5,
                                                encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)
    logging.getLogger("ib_async").setLevel(logging.WARNING)


def _ibkr(cfg: Config):
    from .broker.ibkr import IbkrBroker

    p = cfg.profile
    return IbkrBroker(p.host, p.port, p.account, p.client_id, probe_symbol=cfg.params.regime_symbol)


def _app(cfg: Config, broker):
    from .app import TradingApp
    from .llm.sidecar import daily_review, tag_themes

    def reviewer(now: datetime) -> None:
        daily_review(cfg.profile.db, now.date(), cfg.reports_dir, cfg.llm_model)

    app = TradingApp(cfg, broker, reviewer=reviewer)
    scan = app.engine.scan

    def scan_and_tag(today: date) -> int:
        n = scan(today)
        try:
            tag_themes(app.store, today, cfg.llm_model)  # labels only; failure is harmless
        except Exception as e:
            logging.getLogger("trader.llm").warning("theme tagging failed: %s", e)
        return n

    app.engine.scan = scan_and_tag
    return app


def cmd_run(cfg: Config, args) -> int:
    from .app import StartupFailed

    _setup_logging(cfg)
    app = _app(cfg, _ibkr(cfg))
    try:
        app.run()
    except StartupFailed as e:
        print(f"startup check failed: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped. Open positions keep their stop orders at the broker.")
    return 0


def cmd_check(cfg: Config, args) -> int:
    """Run the startup checklist once. May place a missing protective stop; nothing else."""
    from .app import StartupFailed

    _setup_logging(cfg)
    broker = _ibkr(cfg)
    broker.connect()
    app = _app(cfg, broker)
    try:
        res = app.startup()
    except StartupFailed as e:
        print(f"FATAL: {e}")
        return 2
    finally:
        broker.disconnect()
    print(f"mode={cfg.mode} account={cfg.profile.account}")
    print("read-only: " + ("; ".join(res.read_only) if res.read_only else "no"))
    return 1 if res.read_only else 0


def cmd_backtest(cfg: Config, args) -> int:
    from .backtest import run_backtest
    from .broker.sim import HistoricalData
    from .calendar import EarningsCalendar

    data = HistoricalData.from_dir(args.data)
    kwargs = dict(cash=args.cash, slippage_pct=args.slippage_pct,
                  earnings=EarningsCalendar.from_csv(cfg.earnings_file),
                  intraday_checks=args.intraday_checks)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    res = run_backtest(cfg, data, start, end, db_path=args.db, **kwargs)
    print(json.dumps(res.stats(), indent=2))
    if args.replay_check:
        again = run_backtest(cfg, data, start, end, **kwargs)
        same = again.trades == res.trades
        print(f"replay check: {'identical' if same else 'DIFFERENT'}")
        return 0 if same else 1
    return 0


def cmd_status(cfg: Config, args) -> int:
    from .killswitch import halt_active
    from .llm.sidecar import open_readonly

    if not os.path.exists(cfg.profile.db):
        print(f"no database at {cfg.profile.db} yet")
        return 0
    con = open_readonly(cfg.profile.db)
    print(f"mode={cfg.mode} account={cfg.profile.account} HALT={'yes' if halt_active(cfg.halt_file) else 'no'}")
    eq = con.execute("SELECT * FROM equity ORDER BY date DESC LIMIT 1").fetchone()
    if eq:
        print(f"equity {eq['date']}: {eq['net_liq']:.2f} (peak {eq['peak_net_liq']:.2f})")
    print("open trades:")
    for t in con.execute("SELECT * FROM trades WHERE status IN ('PENDING','OPEN','EXIT_QUEUED') ORDER BY id"):
        entry = "-" if t["entry_price"] is None else f"{t['entry_price']:.2f}"
        print(f"  {t['id']} {t['setup']} {t['status']} qty={t['open_qty']}/{t['qty']} "
              f"entry={entry} stop={t['stop_price']:.2f} scaled_out={t['scaled_out']}")
    print("last alerts:")
    for e in con.execute("SELECT * FROM events WHERE level != 'INFO' ORDER BY id DESC LIMIT 10"):
        print(f"  {e['ts'][:19]} {e['level']} {e['kind']} {json.loads(e['payload']).get('message', '')}")
    con.close()
    return 0


def cmd_flatten(cfg: Config, args) -> int:
    from .app import TradingApp
    from .killswitch import flatten

    if args.confirm != cfg.profile.account:
        print(f"refusing: pass --confirm {cfg.profile.account} to cancel all orders and sell everything")
        return 2
    _setup_logging(cfg)
    broker = _ibkr(cfg)
    broker.connect()
    app = TradingApp(cfg, broker)
    now = datetime.now(ET)
    result = flatten(broker, app.executor, app.alerter, now.date(), now)
    broker.sleep(5)  # let the fills arrive
    broker.disconnect()
    print(json.dumps(result))
    return 0


def cmd_review(cfg: Config, args) -> int:
    from .llm.sidecar import daily_review

    day = date.fromisoformat(args.date) if args.date else datetime.now(ET).date()
    path = daily_review(cfg.profile.db, day, cfg.reports_dir, cfg.llm_model, use_llm=not args.no_llm)
    print(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trader", description="Momentum swing trading engine for IBKR")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="start the trading loop (paper or live, per config)")
    sub.add_parser("check", help="run the startup checklist and exit")
    sub.add_parser("status", help="show positions, equity and recent alerts from the local database")
    bt = sub.add_parser("backtest", help="replay historical data through the same engine")
    bt.add_argument("--data", required=True, help="folder with daily/SYM.csv and 5min/SYM.csv")
    bt.add_argument("--start", required=True)
    bt.add_argument("--end", required=True)
    bt.add_argument("--cash", type=float, default=10_000.0)
    bt.add_argument("--slippage-pct", type=float, default=0.05)
    bt.add_argument("--db", default=":memory:", help="keep the run's state in this SQLite file")
    bt.add_argument("--intraday-checks", action="store_true", help="also log the 5-minute pullback checks")
    bt.add_argument("--replay-check", action="store_true", help="run twice and compare the trades")
    fl = sub.add_parser("flatten", help="cancel every order and sell every position")
    fl.add_argument("--confirm", default="", help="the account id, to confirm")
    rv = sub.add_parser("review", help="write the daily review report")
    rv.add_argument("--date", default="")
    rv.add_argument("--no-llm", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    handler = {"run": cmd_run, "check": cmd_check, "status": cmd_status, "backtest": cmd_backtest,
               "flatten": cmd_flatten, "review": cmd_review}[args.command]
    return handler(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
