import pandas as pd

from quant_signal.dataqa import divergences, latest_common_closes


def _frame(data: dict[str, list[tuple[str, float]]]) -> pd.DataFrame:
    rows = []
    for ticker, points in data.items():
        for day, close in points:
            rows.append((ticker, pd.Timestamp(day, tz="UTC"), close))
    idx = pd.MultiIndex.from_tuples([(t, ts) for t, ts, _ in rows], names=["ticker", "ts"])
    return pd.DataFrame({"close": [c for _, _, c in rows]}, index=idx)


def test_latest_common_closes_uses_shared_latest_day() -> None:
    a = _frame({"MU": [("2026-07-06", 100.0), ("2026-07-07", 101.0)]})
    b = _frame({"MU": [("2026-07-06", 100.4)], "XX": [("2026-07-07", 1.0)]})
    pairs = latest_common_closes(a, b)
    assert pairs == {"MU": (100.0, 100.4)}   # 共同最新日=07-06; XX 只在单源, 跳过


def test_latest_common_closes_matches_by_calendar_day() -> None:
    """Alpaca 日bar时间戳带盘前时刻(04:00Z), yfinance 是零点——必须按日历日对齐。"""
    a = _frame({"MU": [("2026-07-06 04:00", 100.0)]})
    b = _frame({"MU": [("2026-07-06 00:00", 100.8)]})
    assert latest_common_closes(a, b) == {"MU": (100.0, 100.8)}


def test_divergences_threshold_and_order() -> None:
    pairs = {
        "OK": (100.0, 100.2),     # 0.2% 不报
        "BAD": (100.0, 102.0),    # ~2.0%
        "WORSE": (100.0, 105.0),  # ~4.8%
    }
    hits = divergences(pairs, threshold=0.005)
    assert [h[0] for h in hits] == ["WORSE", "BAD"]


def test_divergences_empty_inputs() -> None:
    assert divergences({}) == []
    assert latest_common_closes(_frame({}), _frame({})) == {}
