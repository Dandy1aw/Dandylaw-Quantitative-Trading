from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from quant_signal.strategies.base import Signal, dedup_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- UTC ISO8601
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    reason TEXT,
    suggested_weight REAL,
    pushed INTEGER DEFAULT 0,
    dedup_key TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key, ts);
CREATE TABLE IF NOT EXISTS holdings (
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    PRIMARY KEY (strategy_id, ticker)
);
"""


class SignalLedger:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(db_path))
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)

    def insert(self, s: Signal, pushed: bool) -> int:
        cur = self._con.execute(
            "INSERT INTO signals (ts, ticker, direction, price, strategy_id, reason,"
            " suggested_weight, pushed, dedup_key, extra_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.ts.astimezone(timezone.utc).isoformat(),
                s.ticker,
                s.direction.value,
                s.price,
                s.strategy_id,
                s.reason,
                s.suggested_weight,
                int(pushed),
                dedup_key(s),
                json.dumps(s.extra, ensure_ascii=False) if s.extra else None,
            ),
        )
        self._con.commit()
        return int(cur.lastrowid or 0)

    def last_push_by_key(self, since: datetime) -> dict[str, datetime]:
        rows = self._con.execute(
            "SELECT dedup_key, max(ts) AS ts FROM signals"
            " WHERE pushed = 1 AND ts >= ? GROUP BY dedup_key",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        return {r["dedup_key"]: datetime.fromisoformat(r["ts"]) for r in rows}

    def pushed_count_since(self, since: datetime) -> int:
        row = self._con.execute(
            "SELECT count(*) AS n FROM signals WHERE pushed = 1 AND ts >= ?",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchone()
        return int(row["n"])

    def signals_on(self, day: date) -> list[dict[str, object]]:
        rows = self._con.execute(
            "SELECT * FROM signals WHERE substr(ts, 1, 10) = ? ORDER BY ts",
            (day.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_holdings(self, strategy_id: str) -> list[str]:
        rows = self._con.execute(
            "SELECT ticker FROM holdings WHERE strategy_id = ?", (strategy_id,)
        ).fetchall()
        return [r["ticker"] for r in rows]

    def set_holdings(self, strategy_id: str, tickers: list[str]) -> None:
        self._con.execute("DELETE FROM holdings WHERE strategy_id = ?", (strategy_id,))
        self._con.executemany(
            "INSERT INTO holdings (strategy_id, ticker) VALUES (?, ?)",
            [(strategy_id, t) for t in tickers],
        )
        self._con.commit()
