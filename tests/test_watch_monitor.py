from datetime import datetime, timezone

from quant_signal.strategies.base import Direction
from quant_signal.watch_monitor import check_deviations

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_positive_deviation_triggers_buy() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 103.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1
    s = signals[0]
    assert s.ticker == "NVDA" and s.direction is Direction.BUY
    assert s.price == 103.0
    assert s.strategy_id == "price_deviation"
    assert s.ts == NOW


def test_negative_deviation_triggers_sell() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 97.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1 and signals[0].direction is Direction.SELL


def test_within_threshold_no_signal() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 101.0}, now=NOW, threshold=0.02)
    assert signals == []


def test_just_below_threshold_not_triggered() -> None:
    signals = check_deviations({"NVDA": 100.0}, {"NVDA": 101.9}, now=NOW, threshold=0.02)
    assert signals == []


def test_missing_live_price_skipped() -> None:
    signals = check_deviations({"NVDA": 100.0, "AMD": 50.0}, {"NVDA": 110.0}, now=NOW, threshold=0.02)
    assert len(signals) == 1 and signals[0].ticker == "NVDA"


def test_multiple_tickers_independent() -> None:
    ref = {"NVDA": 100.0, "AMD": 50.0, "MU": 900.0}
    live = {"NVDA": 103.0, "AMD": 50.1, "MU": 850.0}
    signals = check_deviations(ref, live, now=NOW, threshold=0.02)
    tickers = {s.ticker for s in signals}
    assert tickers == {"NVDA", "MU"}
