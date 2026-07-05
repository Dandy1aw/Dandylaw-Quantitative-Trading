from datetime import date

from quant_signal.calendar import is_trading_day, previous_trading_day


def test_weekday_is_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 6)) is True        # 周一


def test_weekend_not_trading_day() -> None:
    assert is_trading_day(date(2026, 7, 4)) is False       # 周六


def test_independence_day_observed_closed() -> None:
    assert is_trading_day(date(2026, 7, 3)) is False       # 独立日补休，NYSE 休市


def test_previous_trading_day_skips_holiday_weekend() -> None:
    assert previous_trading_day(date(2026, 7, 6)) == date(2026, 7, 2)
