from datetime import UTC, date, datetime

from quant_signal.calendar import (
    is_trading_day,
    previous_trading_day,
    session_close_utc,
    trading_sessions_ending,
)


def test_weekday_is_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 6)) is True        # 周一


def test_weekend_not_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 4)) is False       # 周六


def test_independence_day_observed_closed() -> None:
    assert is_trading_day(date(2026, 7, 3)) is False       # 独立日补休，NYSE 休市


def test_previous_trading_day_skips_holiday_weekend() -> None:
    assert previous_trading_day(date(2026, 7, 6)) == date(2026, 7, 2)


def test_trading_sessions_ending_returns_exact_nyse_window_through_target() -> None:
    assert trading_sessions_ending(date(2026, 9, 8), 5) == (
        date(2026, 9, 1),
        date(2026, 9, 2),
        date(2026, 9, 3),
        date(2026, 9, 4),
        date(2026, 9, 8),
    )


def test_session_close_utc_for_regular_trading_day() -> None:
    assert session_close_utc(date(2026, 7, 6)) == datetime(
        2026, 7, 6, 20, 0, tzinfo=UTC
    )


def test_session_close_utc_for_half_day() -> None:
    assert session_close_utc(date(2026, 11, 27)) == datetime(
        2026, 11, 27, 18, 0, tzinfo=UTC
    )


def test_session_close_utc_for_weekend_is_none() -> None:
    assert session_close_utc(date(2026, 7, 4)) is None
