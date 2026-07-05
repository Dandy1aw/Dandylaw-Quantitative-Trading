from datetime import datetime, timedelta, timezone

from quant_signal.notifier.dedup import apply_dedup
from quant_signal.strategies.base import Direction, Signal, dedup_key

NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)


def sig(ticker: str = "NVDA") -> Signal:
    return Signal(
        ticker=ticker,
        direction=Direction.BUY,
        price=100.0,
        reason="r",
        strategy_id="breakout_20d",
        ts=NOW,
    )


def test_suppresses_within_window() -> None:
    s = sig()
    last = {dedup_key(s): NOW - timedelta(hours=2)}   # 2h 前推过，窗口 4h
    r = apply_dedup([s], NOW, last, pushed_in_last_hour=0)
    assert r.to_push == [] and r.suppressed == [s]


def test_allows_after_window() -> None:
    s = sig()
    last = {dedup_key(s): NOW - timedelta(hours=5)}   # 已过 4h 窗口
    r = apply_dedup([s], NOW, last, pushed_in_last_hour=0)
    assert r.to_push == [s]


def test_hourly_limit_overflow() -> None:
    signals = [sig(f"T{i:02d}") for i in range(12)]
    r = apply_dedup(signals, NOW, {}, pushed_in_last_hour=5, hourly_limit=10)
    assert len(r.to_push) == 5 and len(r.overflow) == 7   # 配额只剩 5
