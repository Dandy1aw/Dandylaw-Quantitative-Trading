from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone

import structlog

from quant_signal.config import Settings, load_settings
from quant_signal.datafeed.base import DataSource, get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
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


def ingest_daily_split(
    store: BarStore, settings: Settings, tickers: list[str], days: int = 730
) -> int:
    """按 settings.international_tickers 把标的分流：国际标的固定走 yfinance，其余走配置的数据源。"""
    intl = [t for t in tickers if t in settings.international_tickers]
    primary = [t for t in tickers if t not in settings.international_tickers]
    n = 0
    if primary:
        n += ingest_daily(store, get_source(settings), primary, days=days)
    if intl:
        n += ingest_daily(store, YFinanceSource(), intl, days=days)
    return n


def find_missing_sessions(
    store: BarStore, ticker: str, sessions: list[date]
) -> list[date]:
    return store.missing_daily_sessions([ticker], sessions)[ticker]


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

    ingest_daily_split(store, settings, tickers, days=args.days)
    for t in tickers:
        log.info("bar_count", ticker=t, bars=store.daily_bar_count(t))


if __name__ == "__main__":
    main()
