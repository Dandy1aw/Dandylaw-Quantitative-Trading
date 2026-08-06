from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import threading
import time
from typing import Literal, Self

import duckdb
import numpy as np
import pandas as pd

BarState = Literal["final", "live"]


class BarCoverageError(RuntimeError):
    """Required cached sessions are incomplete."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    ticker VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT,
    source VARCHAR NOT NULL,
    bar_state VARCHAR NOT NULL DEFAULT 'final',
    adjustment VARCHAR NOT NULL DEFAULT 'split',
    fetched_at TIMESTAMPTZ,
    data_version VARCHAR NOT NULL DEFAULT 'legacy',
    PRIMARY KEY (ticker, ts)
);
"""
_COVERAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS bar_coverage_sessions (
    table_name VARCHAR NOT NULL,
    ticker VARCHAR NOT NULL,
    session_date DATE NOT NULL,
    source VARCHAR NOT NULL,
    bar_state VARCHAR NOT NULL,
    adjustment VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ,
    data_version VARCHAR NOT NULL,
    PRIMARY KEY (table_name, ticker, session_date)
);
"""
_METADATA_COLUMNS = (
    ("bar_state", "VARCHAR DEFAULT 'final'"),
    ("adjustment", "VARCHAR DEFAULT 'split'"),
    ("fetched_at", "TIMESTAMPTZ"),
    ("data_version", "VARCHAR DEFAULT 'legacy'"),
)
_COLS = ["open", "high", "low", "close", "volume"]
_STORED_COLS = [
    "ticker",
    "ts",
    *_COLS,
    "source",
    "bar_state",
    "adjustment",
    "fetched_at",
    "data_version",
]
_CONNECT_RETRY_ATTEMPTS = 6
_CONNECT_RETRY_SECONDS = 1.0


def _is_transient_duckdb_lock(error: duckdb.IOException) -> bool:
    message = str(error).lower()
    return (
        "cannot open file" in message
        and ("already open" in message or "another program" in message)
    )


def _connect_with_retry(
    db_path: Path, *, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    last_error: duckdb.IOException | None = None
    for attempt in range(_CONNECT_RETRY_ATTEMPTS):
        try:
            if read_only:
                return duckdb.connect(str(db_path), read_only=True)
            return duckdb.connect(str(db_path))
        except duckdb.IOException as error:
            if not _is_transient_duckdb_lock(error):
                raise
            last_error = error
            if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                time.sleep(_CONNECT_RETRY_SECONDS)
    assert last_error is not None
    raise last_error


class BarStore:
    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        if not read_only:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._read_only = read_only
        self._lock = threading.RLock()
        self._con = _connect_with_retry(db_path, read_only=read_only)
        if read_only:
            return
        with self._lock:
            for table in ("bars_1d", "bars_5min"):
                self._con.execute(_SCHEMA.format(table=table))
                for name, definition in _METADATA_COLUMNS:
                    self._con.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                        f"{name} {definition}"
                    )
                self._con.execute(
                    f"UPDATE {table} SET bar_state = 'final' WHERE bar_state IS NULL"
                )
                self._con.execute(
                    f"UPDATE {table} SET adjustment = 'split' WHERE adjustment IS NULL"
                )
                self._con.execute(
                    f"UPDATE {table} SET data_version = 'legacy' "
                    "WHERE data_version IS NULL"
                )
            self._con.execute(_COVERAGE_SCHEMA)
            self._con.execute(
                "INSERT OR IGNORE INTO bar_coverage_sessions "
                "SELECT 'bars_1d', ticker, CAST(ts AS DATE), source, bar_state, "
                "adjustment, fetched_at, data_version FROM bars_1d"
            )

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def export_database(self, destination: Path) -> dict[str, int]:
        """Export a consistent snapshot without copying the locked DuckDB file."""
        if destination.exists():
            raise FileExistsError(f"export destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        escaped = destination.resolve().as_posix().replace("'", "''")
        with self._lock:
            self._con.execute(
                f"EXPORT DATABASE '{escaped}' (FORMAT PARQUET)"
            )
        probe = duckdb.connect(":memory:")
        try:
            probe.execute(f"IMPORT DATABASE '{escaped}'")
            counts: dict[str, int] = {}
            for table in ("bars_1d", "bars_5min", "bar_coverage_sessions"):
                row = probe.execute(f"SELECT count(*) FROM {table}").fetchone()
                assert row is not None
                counts[table] = int(row[0])
            return counts
        finally:
            probe.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _assert_writable(self) -> None:
        if self._read_only:
            raise RuntimeError("bar store is read-only")

    def _write(
        self,
        table: str,
        df: pd.DataFrame,
        source: str,
        *,
        bar_state: BarState,
        adjustment: str,
        fetched_at: datetime | None,
        data_version: str | None,
    ) -> int:
        self._assert_writable()
        if df.empty:
            return 0
        if bar_state not in ("final", "live"):
            raise ValueError("bar_state must be final or live")
        if not adjustment.strip():
            raise ValueError("adjustment must not be empty")
        flat = df.reset_index()[["ticker", "ts", *_COLS]].copy()
        flat = flat[np.isfinite(pd.to_numeric(flat["close"], errors="coerce"))].copy()
        if flat.empty:
            return 0
        observed_at = fetched_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        observed_at = observed_at.astimezone(timezone.utc)
        version = data_version or (
            f"{source}:{bar_state}:{adjustment}:{observed_at.isoformat()}"
        )
        flat["source"] = source
        flat["bar_state"] = bar_state
        flat["adjustment"] = adjustment
        flat["fetched_at"] = observed_at
        flat["data_version"] = version
        with self._lock:
            self._con.register("_incoming", flat)
            try:
                self._con.execute("BEGIN TRANSACTION")
                source_query = f"SELECT {', '.join(_STORED_COLS)} FROM _incoming"
                if table == "bars_1d":
                    source_query += (
                        " incoming WHERE incoming.bar_state = 'final' OR NOT EXISTS ("
                        "SELECT 1 FROM bars_1d stored "
                        "WHERE stored.ticker = incoming.ticker "
                        "AND stored.ts = incoming.ts "
                        "AND stored.bar_state = 'final')"
                    )
                self._con.execute(
                    f"INSERT OR REPLACE INTO {table} "
                    f"({', '.join(_STORED_COLS)}) "
                    f"{source_query}"
                )
                if table == "bars_1d":
                    self._con.execute(
                        "INSERT OR REPLACE INTO bar_coverage_sessions "
                        "SELECT 'bars_1d', stored.ticker, CAST(stored.ts AS DATE), "
                        "stored.source, stored.bar_state, stored.adjustment, "
                        "stored.fetched_at, stored.data_version FROM bars_1d stored "
                        "WHERE EXISTS (SELECT 1 FROM _incoming incoming "
                        "WHERE incoming.ticker = stored.ticker "
                        "AND incoming.ts = stored.ts)"
                    )
                self._con.execute("COMMIT")
            except Exception:
                self._con.execute("ROLLBACK")
                raise
            finally:
                self._con.unregister("_incoming")
        return len(flat)

    def _read(
        self,
        table: str,
        tickers: list[str],
        start: datetime | None,
        end: datetime | None,
        bar_state: BarState | None,
    ) -> pd.DataFrame:
        q = f"SELECT ticker, ts, {', '.join(_COLS)} FROM {table} WHERE ticker IN ?"
        params: list[object] = [tickers]
        if bar_state is not None:
            q += " AND bar_state = ?"
            params.append(bar_state)
        if start is not None:
            q += " AND ts >= ?"
            params.append(start)
        if end is not None:
            q += " AND ts <= ?"
            params.append(end)
        q += " ORDER BY ticker, ts"
        with self._lock:
            flat = self._con.execute(q, params).df()
        flat["ts"] = pd.to_datetime(flat["ts"], utc=True)
        return flat.set_index(["ticker", "ts"])

    def write_daily_bars(
        self,
        df: pd.DataFrame,
        source: str,
        *,
        bar_state: BarState = "final",
        adjustment: str = "split",
        fetched_at: datetime | None = None,
        data_version: str | None = None,
    ) -> int:
        if not df.empty:
            # 不同数据源日线的时刻分量不一致（Alpaca 带具体时刻，yfinance 为午夜），
            # 归一化为午夜，确保同一交易日跨数据源能正确对齐/去重。
            tickers = df.index.get_level_values("ticker")
            ts = pd.DatetimeIndex(df.index.get_level_values("ts")).normalize()
            df = df.copy()
            df.index = pd.MultiIndex.from_arrays([tickers, ts], names=["ticker", "ts"])
        return self._write(
            "bars_1d",
            df,
            source,
            bar_state=bar_state,
            adjustment=adjustment,
            fetched_at=fetched_at,
            data_version=data_version,
        )

    def read_daily_bars(
        self,
        tickers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        bar_state: BarState | None = "final",
    ) -> pd.DataFrame:
        return self._read("bars_1d", tickers, start, end, bar_state)

    def write_intraday_bars(
        self,
        df: pd.DataFrame,
        source: str,
        *,
        fetched_at: datetime | None = None,
        data_version: str | None = None,
    ) -> int:
        return self._write(
            "bars_5min",
            df,
            source,
            bar_state="live",
            adjustment="raw",
            fetched_at=fetched_at,
            data_version=data_version,
        )

    def read_intraday_bars(
        self,
        tickers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        return self._read("bars_5min", tickers, start, end, None)

    def read_daily_bar_metadata(self, tickers: list[str]) -> pd.DataFrame:
        query = (
            "SELECT ticker, ts, source, bar_state, adjustment, fetched_at, "
            "data_version FROM bars_1d WHERE ticker IN ? ORDER BY ticker, ts"
        )
        with self._lock:
            frame = self._con.execute(query, [tickers]).df()
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
        frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
        return frame

    def read_daily_coverage(
        self,
        tickers: list[str],
        *,
        bar_state: BarState | None = "final",
        adjustment: str | None = None,
    ) -> pd.DataFrame:
        query = "SELECT * FROM bar_coverage_sessions WHERE table_name = 'bars_1d' AND ticker IN ?"
        params: list[object] = [tickers]
        if bar_state is not None:
            query += " AND bar_state = ?"
            params.append(bar_state)
        if adjustment is not None:
            query += " AND adjustment = ?"
            params.append(adjustment)
        query += " ORDER BY ticker, session_date"
        with self._lock:
            return self._con.execute(query, params).df()

    def missing_daily_sessions(
        self,
        tickers: list[str],
        sessions: list[date],
        *,
        bar_state: BarState = "final",
        adjustment: str | None = None,
    ) -> dict[str, list[date]]:
        coverage = self.read_daily_coverage(
            tickers, bar_state=bar_state, adjustment=adjustment
        )
        available: dict[str, set[date]] = {ticker: set() for ticker in tickers}
        for row in coverage.itertuples(index=False):
            session_date = date.fromisoformat(str(row.session_date)[:10])
            available.setdefault(str(row.ticker), set()).add(session_date)
        return {
            ticker: [session for session in sessions if session not in available[ticker]]
            for ticker in sorted(available)
        }

    def require_daily_coverage(
        self,
        tickers: list[str],
        sessions: list[date],
        *,
        adjustment: str | None = None,
    ) -> None:
        missing = self.missing_daily_sessions(
            tickers, sessions, adjustment=adjustment
        )
        incomplete = {ticker: dates for ticker, dates in missing.items() if dates}
        if incomplete:
            counts = ", ".join(
                f"{ticker}:{len(dates)}" for ticker, dates in incomplete.items()
            )
            raise BarCoverageError(f"daily bar coverage is incomplete: {counts}")

    def daily_bar_count(self, ticker: str) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT count(*) FROM bars_1d WHERE ticker = ? AND bar_state = 'final'",
                [ticker],
            ).fetchone()
        return int(row[0]) if row else 0
