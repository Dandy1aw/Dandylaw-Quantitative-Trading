from datetime import date
from pathlib import Path

import pandas as pd

from quant_signal.datafeed.store import BarStore
from quant_signal.ingest import find_missing_sessions, ingest_daily


class FakeSource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        ts = pd.date_range("2026-01-05", periods=4, freq="B", tz="UTC")
        idx = pd.MultiIndex.from_product([tickers, ts], names=["ticker", "ts"])
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100},
            index=idx,
        )

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_ingest_daily_writes_all(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    n = ingest_daily(store, FakeSource(), ["SPY", "QQQ"], days=30)
    assert n == 8
    assert store.daily_bar_count("SPY") == 4


def test_find_missing_sessions(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    ingest_daily(store, FakeSource(), ["SPY"], days=30)
    sessions = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 9)]  # 1/9 无数据
    missing = find_missing_sessions(store, "SPY", sessions)
    assert missing == [date(2026, 1, 9)]
