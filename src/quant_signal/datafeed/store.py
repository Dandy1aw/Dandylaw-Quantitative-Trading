from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    ticker VARCHAR NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume BIGINT,
    source VARCHAR NOT NULL,
    PRIMARY KEY (ticker, ts)
);
"""
_COLS = ["open", "high", "low", "close", "volume"]


class BarStore:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db_path))
        for table in ("bars_1d", "bars_5min"):
            self._con.execute(_SCHEMA.format(table=table))

    def _write(self, table: str, df: pd.DataFrame, source: str) -> int:
        if df.empty:
            return 0
        flat = df.reset_index()[["ticker", "ts", *_COLS]].copy()
        flat["source"] = source
        self._con.register("_incoming", flat)
        self._con.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM _incoming")
        self._con.unregister("_incoming")
        return len(flat)

    def _read(
        self,
        table: str,
        tickers: list[str],
        start: datetime | None,
        end: datetime | None,
    ) -> pd.DataFrame:
        q = f"SELECT ticker, ts, {', '.join(_COLS)} FROM {table} WHERE ticker IN ?"
        params: list[object] = [tickers]
        if start is not None:
            q += " AND ts >= ?"
            params.append(start)
        if end is not None:
            q += " AND ts <= ?"
            params.append(end)
        q += " ORDER BY ticker, ts"
        flat = self._con.execute(q, params).df()
        flat["ts"] = pd.to_datetime(flat["ts"], utc=True)
        return flat.set_index(["ticker", "ts"])

    def write_daily_bars(self, df: pd.DataFrame, source: str) -> int:
        return self._write("bars_1d", df, source)

    def read_daily_bars(
        self,
        tickers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        return self._read("bars_1d", tickers, start, end)

    def write_intraday_bars(self, df: pd.DataFrame, source: str) -> int:
        return self._write("bars_5min", df, source)

    def read_intraday_bars(
        self,
        tickers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        return self._read("bars_5min", tickers, start, end)

    def daily_bar_count(self, ticker: str) -> int:
        row = self._con.execute(
            "SELECT count(*) FROM bars_1d WHERE ticker = ?", [ticker]
        ).fetchone()
        return int(row[0]) if row else 0
