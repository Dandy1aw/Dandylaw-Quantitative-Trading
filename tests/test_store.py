from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pandas as pd
import pytest

from quant_signal.datafeed.store import BarStore


def make_bars(ticker: str, n: int = 3) -> pd.DataFrame:
    ts = pd.date_range("2026-01-05", periods=n, freq="B", tz="UTC")
    idx = pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"])
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000_000},
        index=idx,
    )


@pytest.fixture
def store(tmp_path: Path) -> BarStore:
    return BarStore(tmp_path / "bars.duckdb")


def test_roundtrip(store: BarStore) -> None:
    n = store.write_daily_bars(make_bars("SPY"), source="test")
    assert n == 3
    out = store.read_daily_bars(["SPY"])
    assert len(out) == 3
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.names == ["ticker", "ts"]
    assert str(out.index.get_level_values("ts").tz) == "UTC"


def test_upsert_no_duplicates(store: BarStore) -> None:
    store.write_daily_bars(make_bars("SPY"), source="test")
    store.write_daily_bars(make_bars("SPY"), source="test")  # 重复写
    assert store.daily_bar_count("SPY") == 3


def test_read_time_range(store: BarStore) -> None:
    store.write_daily_bars(make_bars("SPY", n=5), source="test")
    start = datetime(2026, 1, 7, tzinfo=timezone.utc)
    out = store.read_daily_bars(["SPY"], start=start)
    assert len(out) == 3


def test_write_daily_bars_normalizes_timestamp_to_midnight(store: BarStore) -> None:
    """Alpaca 日线带具体时刻（如 04:00 UTC），必须归一化为午夜，才能和 yfinance（00:00 UTC）对齐。"""
    ts = pd.Timestamp("2026-07-02 04:00:00", tz="UTC")
    idx = pd.MultiIndex.from_tuples([("SPY", ts)], names=["ticker", "ts"])
    df = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 100.0, "volume": 100}, index=idx
    )
    store.write_daily_bars(df, source="alpaca")
    out = store.read_daily_bars(["SPY"])
    assert len(out) == 1
    assert out.index.get_level_values("ts")[0] == pd.Timestamp("2026-07-02", tz="UTC")


def test_write_daily_bars_dedupes_same_day_across_sources(store: BarStore) -> None:
    """同一交易日，不管来自 alpaca（带时刻）还是 yfinance（午夜），duckdb 里只应有一行。"""
    ts_alpaca = pd.Timestamp("2026-07-02 04:00:00", tz="UTC")
    ts_yf = pd.Timestamp("2026-07-02 00:00:00", tz="UTC")
    idx1 = pd.MultiIndex.from_tuples([("SPY", ts_alpaca)], names=["ticker", "ts"])
    idx2 = pd.MultiIndex.from_tuples([("SPY", ts_yf)], names=["ticker", "ts"])
    df1 = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 100.0, "volume": 100}, index=idx1
    )
    df2 = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 101.0, "volume": 100}, index=idx2
    )
    store.write_daily_bars(df1, source="alpaca")
    store.write_daily_bars(df2, source="yfinance")
    assert store.daily_bar_count("SPY") == 1


def test_shared_duckdb_connection_serializes_concurrent_writes(store: BarStore) -> None:
    class DetectingConnection:
        def __init__(self) -> None:
            self.guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def register(self, name: str, frame: pd.DataFrame) -> None:
            return None

        def execute(self, query: str) -> None:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1

        def unregister(self, name: str) -> None:
            return None

    connection = DetectingConnection()
    store._con = connection  # type: ignore[assignment]
    start = threading.Barrier(3)

    def write(ticker: str) -> None:
        start.wait()
        store.write_daily_bars(make_bars(ticker), source="test")

    threads = [threading.Thread(target=write, args=(ticker,)) for ticker in ("AAA", "BBB")]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert connection.max_active == 1


def test_write_drops_non_finite_close_even_when_volume_exists(store: BarStore) -> None:
    bars = make_bars("7709.HK", n=2)
    bars.iloc[-1, bars.columns.get_loc("close")] = float("nan")

    written = store.write_daily_bars(bars, source="yfinance")

    assert written == 1
    assert store.daily_bar_count("7709.HK") == 1
