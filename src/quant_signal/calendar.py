from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

import pandas_market_calendars as mcal


@lru_cache(maxsize=8)
def _sessions_for_year(year: int) -> frozenset[date]:
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    return frozenset(d.date() for d in sched.index)


@lru_cache(maxsize=8)
def _session_closes_for_year(year: int) -> dict[date, datetime]:
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
    return {
        session.date(): close.to_pydatetime()
        for session, close in sched["market_close"].items()
    }


def is_trading_day(d: date) -> bool:
    return d in _sessions_for_year(d.year)


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def session_close_utc(d: date) -> datetime | None:
    return _session_closes_for_year(d.year).get(d)
