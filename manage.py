"""CLI management entrypoint for Nexus US (test, chart, seed-mock, clean-db, status, run)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import unittest
from pathlib import Path
from typing import List, Optional

from us_core._bootstrap import PROJECT_ROOT, ensure_nexus_quant_core_on_path

ensure_nexus_quant_core_on_path()

from broker.paper_broker import UsPaperBroker
from chart_hist.config import DB_PATH as CHART_DB_PATH
from chart_hist.config import DEFAULT_HOST, DEFAULT_PORT, ORDERED_TIMEFRAMES, PRESET_SYMBOLS
from chart_hist.database import get_available_symbols, save_candles
from chart_hist.fetchers import get_fetcher
from chart_hist.server import start_server
from main import run_us_main
from us_core.calendar import DEFAULT_US_CALENDAR
from us_core.clock import DEFAULT_CLOCK
from us_core.config import load_us_settings
from us_core.time_utils import format_et_iso


def cmd_test(args: argparse.Namespace) -> int:
    tests_dir = PROJECT_ROOT / "tests"
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(tests_dir), pattern=args.pattern, top_level_dir=str(PROJECT_ROOT))
    runner = unittest.TextTestRunner(verbosity=args.verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def cmd_chart(args: argparse.Namespace) -> int:
    start_server(
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
    return 0


async def _seed_mock_async(symbols: List[str], days: int) -> int:
    fetcher = get_fetcher("mock")
    total_saved = 0
    for sym in symbols:
        for tf in ORDERED_TIMEFRAMES:
            bars = await fetcher.fetch(sym, tf, days=days)
            saved = save_candles(bars)
            total_saved += saved
            print(f"[seed-mock] {sym:<6} {tf:<4} -> saved {saved:>5} bars")
    print(f"[seed-mock] Complete! Total rows upserted: {total_saved}")
    return 0


def cmd_seed_mock(args: argparse.Namespace) -> int:
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = [item["symbol"] for item in PRESET_SYMBOLS]
    return asyncio.run(_seed_mock_async(syms, args.days))


def _remove_sqlite_bundle(db_file: Path) -> bool:
    removed = False
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_file) + suffix)
        if candidate.exists():
            candidate.unlink()
            removed = True
    return removed


def cmd_clean_db(args: argparse.Namespace) -> int:
    settings = load_us_settings()
    targets = []
    if args.target in ("all", "chart"):
        targets.append(("Chart DB", CHART_DB_PATH))
    if args.target in ("all", "broker"):
        targets.append(("Paper Broker DB", settings.paper_db_path))

    for label, path in targets:
        if _remove_sqlite_bundle(path):
            print(f"[clean-db] Removed {label}: {path}")
        else:
            print(f"[clean-db] {label} already clean: {path}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    settings = load_us_settings()
    now_ts = DEFAULT_CLOCK.now_epoch()
    dt_et = DEFAULT_US_CALENDAR.to_et_datetime(now_ts)
    eq_sess = DEFAULT_US_CALENDAR.get_session_type(now_ts, symbol="NVDA")
    fut_sess = DEFAULT_US_CALENDAR.get_session_type(now_ts, symbol=settings.macro_symbol)
    eq_td = DEFAULT_US_CALENDAR.get_trading_day(now_ts, symbol="NVDA").isoformat()
    fut_td = DEFAULT_US_CALENDAR.get_trading_day(now_ts, symbol=settings.macro_symbol).isoformat()
    is_early = DEFAULT_US_CALENDAR.is_early_close_day(dt_et.date())
    is_hol = DEFAULT_US_CALENDAR.is_nyse_holiday(dt_et.date())

    broker = UsPaperBroker(db_path=settings.paper_db_path, initial_capital=settings.initial_capital)
    summary = broker.get_account_summary()
    positions = broker.get_all_positions()
    chart_syms = get_available_symbols()

    print("=" * 62)
    print("NEXUS US — SYSTEM STATUS")
    print("=" * 62)
    print(f"New York Time      : {format_et_iso(now_ts)}")
    print(f"NYSE Equity Session: {eq_sess} (trading_date={eq_td}, holiday={is_hol}, early_close_13ET={is_early})")
    print(f"CME Globex ({settings.macro_symbol}): {fut_sess} (trading_date={fut_td})")
    print("-" * 62)
    print(
        f"Paper Account (USD): Equity=${summary['equity']:,.2f} | Cash=${summary['cash']:,.2f} | "
        f"Realized=${summary['realized_pnl']:,.2f} | Fees=${summary['total_fees']:,.4f}"
    )
    print(f"Circuit Breaker    : {'TRIPPED' if summary['circuit_breaker_tripped'] else 'OK'} | Open Positions: {len(positions)}")
    print(f"Chart DB Symbols   : {len(chart_syms)} stored in {CHART_DB_PATH}")
    print("=" * 62)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_us_main(provider=args.provider, iterations=args.iterations))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage.py", description="Nexus US Quantitative System CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_test = sub.add_parser("test", help="Run Nexus US unittest suite")
    p_test.add_argument("-p", "--pattern", default="test_*.py", help="Test file glob pattern")
    p_test.add_argument("-v", "--verbosity", type=int, default=2, help="Unittest verbosity")
    p_test.set_defaults(func=cmd_test)

    p_chart = sub.add_parser("chart", help="Start US Historical Chart Workstation on port 8081")
    p_chart.add_argument("--host", default=DEFAULT_HOST)
    p_chart.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_chart.add_argument("--open-browser", action="store_true")
    p_chart.set_defaults(func=cmd_chart)

    p_seed = sub.add_parser("seed-mock", help="Seed US chart database with deterministic mock bars")
    p_seed.add_argument("--symbols", default="", help="Comma-separated US tickers (default: all presets)")
    p_seed.add_argument("--days", type=int, default=75, help="Lookback trading days")
    p_seed.set_defaults(func=cmd_seed_mock)

    p_clean = sub.add_parser("clean-db", help="Clean local SQLite databases")
    p_clean.add_argument("--target", choices=("all", "chart", "broker"), default="all")
    p_clean.set_defaults(func=cmd_clean_db)

    p_status = sub.add_parser("status", help="Show US session calendar, USD broker, and chart DB status")
    p_status.set_defaults(func=cmd_status)

    p_run = sub.add_parser("run", help="Run async US live/sandbox trading engine")
    p_run.add_argument("--provider", default=None, help="Feed provider override (sandbox|yfinance|ibkr|alpaca|schwab)")
    p_run.add_argument("--iterations", type=int, default=None, help="Optional max loop iterations")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
