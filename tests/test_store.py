from datetime import date, datetime, timezone
from pathlib import Path
import threading
import time

import pandas as pd
import pytest
import duckdb

from quant_signal.datafeed.store import BarCoverageError, BarStore


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


def test_startup_retries_transient_duckdb_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_connect = duckdb.connect

    def flaky_connect(path: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls < 3:
            raise duckdb.IOException(
                f'IO Error: Cannot open file "{path}": file is already open'
            )
        return real_connect(path)

    monkeypatch.setattr("quant_signal.datafeed.store.duckdb.connect", flaky_connect)
    monkeypatch.setattr("quant_signal.datafeed.store.time.sleep", lambda _: None)

    store = BarStore(tmp_path / "locked-once.duckdb")

    assert calls == 3
    assert store.daily_bar_count("SPY") == 0


def test_startup_does_not_retry_non_lock_duckdb_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def broken_connect(path: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise duckdb.IOException("IO Error: database file is corrupt")

    monkeypatch.setattr("quant_signal.datafeed.store.duckdb.connect", broken_connect)
    monkeypatch.setattr("quant_signal.datafeed.store.time.sleep", lambda _: None)

    with pytest.raises(duckdb.IOException, match="corrupt"):
        BarStore(tmp_path / "corrupt.duckdb")

    assert calls == 1


def test_daily_metadata_is_persisted_and_default_read_is_final_only(
    store: BarStore,
) -> None:
    fetched_at = datetime(2026, 1, 10, 12, 30, tzinfo=timezone.utc)
    live = make_bars("SPY", n=1)
    final = make_bars("SPY", n=2).iloc[1:]

    store.write_daily_bars(
        live,
        source="alpaca",
        bar_state="live",
        adjustment="raw",
        fetched_at=fetched_at,
        data_version="alpaca-live-1",
    )
    store.write_daily_bars(
        final,
        source="alpaca",
        fetched_at=fetched_at,
        data_version="alpaca-final-1",
    )

    assert len(store.read_daily_bars(["SPY"])) == 1
    assert len(store.read_daily_bars(["SPY"], bar_state=None)) == 2
    metadata = store.read_daily_bar_metadata(["SPY"])
    assert set(metadata["bar_state"]) == {"final", "live"}
    assert set(metadata["adjustment"]) == {"raw", "split"}
    assert set(metadata["data_version"]) == {"alpaca-live-1", "alpaca-final-1"}
    assert metadata["fetched_at"].notna().all()


def test_live_daily_bar_cannot_overwrite_existing_final_bar(store: BarStore) -> None:
    final = make_bars("SPY", n=1)
    live = final.copy()
    live.loc[:, "close"] = 999.0
    store.write_daily_bars(final, source="alpaca", data_version="final-1")

    store.write_daily_bars(
        live,
        source="alpaca",
        bar_state="live",
        adjustment="raw",
        data_version="live-late",
    )

    bars = store.read_daily_bars(["SPY"])
    metadata = store.read_daily_bar_metadata(["SPY"])
    assert float(bars.iloc[0]["close"]) == 100.5
    assert metadata.loc[0, "bar_state"] == "final"
    assert metadata.loc[0, "data_version"] == "final-1"


def test_legacy_database_is_migrated_with_explicit_metadata(tmp_path: Path) -> None:
    path = tmp_path / "legacy.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE bars_1d (ticker VARCHAR NOT NULL, ts TIMESTAMPTZ NOT NULL, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, "
        "source VARCHAR NOT NULL, PRIMARY KEY(ticker, ts))"
    )
    connection.execute(
        "INSERT INTO bars_1d VALUES "
        "('SPY', '2026-01-05T00:00:00Z', 1, 1, 1, 1, 100, 'legacy-source')"
    )
    connection.close()

    store = BarStore(path)
    metadata = store.read_daily_bar_metadata(["SPY"])

    assert metadata.loc[0, "bar_state"] == "final"
    assert metadata.loc[0, "adjustment"] == "split"
    assert metadata.loc[0, "data_version"] == "legacy"


def test_coverage_ledger_reports_missing_sessions_and_fails_closed(
    store: BarStore,
) -> None:
    store.write_daily_bars(make_bars("SPY", n=2), source="test")
    sessions = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]

    missing = store.missing_daily_sessions(["SPY", "QQQ"], sessions)

    assert missing == {
        "QQQ": sessions,
        "SPY": [date(2026, 1, 7)],
    }
    with pytest.raises(BarCoverageError, match="QQQ.*3.*SPY.*1"):
        store.require_daily_coverage(["SPY", "QQQ"], sessions)


def test_coverage_metadata_tracks_replacement_source(store: BarStore) -> None:
    bars = make_bars("SPY", n=1)
    store.write_daily_bars(bars, source="alpaca", data_version="a1")
    store.write_daily_bars(bars, source="yfinance", data_version="y1")

    coverage = store.read_daily_coverage(["SPY"])

    assert len(coverage) == 1
    assert coverage.loc[0, "source"] == "yfinance"
    assert coverage.loc[0, "data_version"] == "y1"


def test_read_only_store_reads_but_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "research.duckdb"
    writer = BarStore(path)
    writer.write_daily_bars(make_bars("SPY"), source="test")
    writer.close()

    with BarStore(path, read_only=True) as research:
        assert len(research.read_daily_bars(["SPY"])) == 3
        with pytest.raises(RuntimeError, match="read-only"):
            research.write_daily_bars(make_bars("QQQ"), source="test")


def test_open_store_exports_consistent_database_and_restore_probe(
    tmp_path: Path,
) -> None:
    source = BarStore(tmp_path / "source.duckdb")
    source.write_daily_bars(make_bars("SPY"), source="test", data_version="v1")
    export_dir = tmp_path / "bars_export"

    probe = source.export_database(export_dir)

    assert (export_dir / "schema.sql").exists()
    assert (export_dir / "load.sql").exists()
    assert probe["bars_1d"] == 3
    assert probe["bar_coverage_sessions"] == 3
    assert source.daily_bar_count("SPY") == 3
    source.close()
