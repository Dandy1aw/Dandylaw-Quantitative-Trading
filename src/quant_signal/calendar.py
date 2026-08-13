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


@lru_cache(maxsize=256)
def trading_sessions_ending(target: date, count: int) -> tuple[date, ...]:
    """Return the exact trailing NYSE sessions, including ``target``."""
    if count < 1:
        raise ValueError("trading session count must be positive")
    if not is_trading_day(target):
        raise ValueError(f"{target.isoformat()} is not an NYSE trading session")

    sessions = [target]
    current = target
    while len(sessions) < count:
        current = previous_trading_day(current)
        sessions.append(current)
    return tuple(reversed(sessions))


def session_close_utc(d: date) -> datetime | None:
    return _session_closes_for_year(d.year).get(d)
