from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import structlog

from quant_signal.config import load_settings
from quant_signal.datafeed.base import DataSource, get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.logging_setup import setup_logging

log = structlog.get_logger()


def ingest_daily(
    store: BarStore, source: DataSource, tickers: list[str], days: int = 730
) -> int:
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=days)
    df = source.fetch_daily_bars(tickers, start, end)
    n = store.write_daily_bars(
        df, source=type(source).__name__.replace("Source", "").lower()
    )
    log.info("ingest_daily.done", tickers=len(tickers), rows=n)
    return n


def find_missing_sessions(
    store: BarStore, ticker: str, sessions: list[date]
) -> list[date]:
    bars = store.read_daily_bars([ticker])
    have = {ts.date() for ts in bars.index.get_level_values("ts")}
    return [s for s in sessions if s not in have]


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="拉取历史日线入 duckdb")
    parser.add_argument("--days", type=int, default=730)
    parser.add_argument("--check", action="store_true", help="仅检查缺失交易日")
    args = parser.parse_args()

    settings = load_settings()
    store = BarStore(settings.db_path / "bars.duckdb")
    tickers = sorted(set(settings.universe) | set(settings.watchlist))

    if args.check:
        import pandas_market_calendars as mcal

        nyse = mcal.get_calendar("NYSE")
        end = datetime.now(timezone.utc).date()
        sched = nyse.schedule(start_date=end - timedelta(days=args.days), end_date=end)
        sessions = [d.date() for d in sched.index]
        for t in tickers:
            missing = find_missing_sessions(store, t, sessions)
            log.info(
                "check", ticker=t, bars=store.daily_bar_count(t), missing=len(missing)
            )
        return

    ingest_daily(store, get_source(settings), tickers, days=args.days)
    for t in tickers:
        log.info("bar_count", ticker=t, bars=store.daily_bar_count(t))


if __name__ == "__main__":
    main()
