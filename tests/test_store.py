from datetime import datetime, timezone
from pathlib import Path

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
