from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import quant_signal.ingest as ingest_mod
from conftest import make_test_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.ingest import find_missing_sessions, ingest_daily, ingest_daily_split


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


def test_find_missing_sessions_uses_final_coverage_only(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    bars = FakeSource().fetch_daily_bars(["SPY"], date(2026, 1, 1), date(2026, 2, 1))
    store.write_daily_bars(bars.iloc[:1], source="alpaca", bar_state="live")

    assert find_missing_sessions(store, "SPY", [date(2026, 1, 5)]) == [
        date(2026, 1, 5)
    ]


class RecordingSource:
    def __init__(self, tag: str, calls: list[tuple[str, list[str]]]) -> None:
        self.tag = tag
        self.calls = calls

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        self.calls.append((self.tag, sorted(tickers)))
        ts = pd.date_range("2026-01-05", periods=2, freq="B", tz="UTC")
        idx = pd.MultiIndex.from_product([tickers, ts], names=["ticker", "ts"])
        return pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 100}, index=idx
        )

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


def test_ingest_daily_split_routes_international_to_yfinance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = BarStore(tmp_path / "b.duckdb")
    settings = make_test_settings(
        universe=["SPY", "7709.HK"], watchlist=[],
        international_tickers={"7709.HK": "HKD"},
    )
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(ingest_mod, "get_source", lambda s: RecordingSource("primary", calls))
    monkeypatch.setattr(ingest_mod, "YFinanceSource", lambda: RecordingSource("intl", calls))

    n = ingest_daily_split(store, settings, ["SPY", "7709.HK"], days=10)
    assert n == 4
    assert ("primary", ["SPY"]) in calls
    assert ("intl", ["7709.HK"]) in calls
