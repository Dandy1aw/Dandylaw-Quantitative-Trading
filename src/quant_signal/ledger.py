from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from quant_signal.strategies.base import Signal, dedup_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- UTC ISO8601，信号触发时刻（可能是历史 bar 的时间）
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    reason TEXT,
    suggested_weight REAL,
    pushed INTEGER DEFAULT 0,
    pushed_at TEXT,               -- UTC ISO8601，实际执行/推送的墙钟时间，去重以此为准
    dedup_key TEXT,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key, pushed_at);
CREATE TABLE IF NOT EXISTS holdings (
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    PRIMARY KEY (strategy_id, ticker)
);
"""


class SignalLedger:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 调度器在后台线程池跑 job，连接需跨线程可用；用 Lock 串行化访问保证安全。
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._con.executescript(_SCHEMA)

    def insert(self, s: Signal, pushed: bool, now: datetime | None = None) -> int:
        pushed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO signals (ts, ticker, direction, price, strategy_id, reason,"
                " suggested_weight, pushed, pushed_at, dedup_key, extra_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.ts.astimezone(timezone.utc).isoformat(),
                    s.ticker,
                    s.direction.value,
                    s.price,
                    s.strategy_id,
                    s.reason,
                    s.suggested_weight,
                    int(pushed),
                    pushed_at.isoformat(),
                    dedup_key(s),
                    json.dumps(s.extra, ensure_ascii=False) if s.extra else None,
                ),
            )
            self._con.commit()
            return int(cur.lastrowid or 0)

    def last_push_by_key(self, since: datetime) -> dict[str, datetime]:
        """按 pushed_at（推送执行的墙钟时间）而非信号自身 ts 判断去重窗口。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT dedup_key, max(pushed_at) AS pushed_at FROM signals"
                " WHERE pushed = 1 AND pushed_at >= ? GROUP BY dedup_key",
                (since.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
        return {r["dedup_key"]: datetime.fromisoformat(r["pushed_at"]) for r in rows}

    def latest_signal_price(
        self, strategy_id: str, ticker: str, since: datetime
    ) -> float | None:
        """取该标的最近一次信号的价格（不论是否实际推送），用作盘中偏离监控的参考价。"""
        with self._lock:
            row = self._con.execute(
                "SELECT price FROM signals WHERE strategy_id = ? AND ticker = ?"
                " AND pushed_at >= ? ORDER BY pushed_at DESC LIMIT 1",
                (strategy_id, ticker, since.astimezone(timezone.utc).isoformat()),
            ).fetchone()
        return float(row["price"]) if row else None

    def latest_price_for(
        self, strategy_id: str, ticker: str, direction: str
    ) -> float | None:
        """取该标的最近一次指定方向信号的价格(不限时间窗)，用于卖出时算持有期收益。"""
        with self._lock:
            row = self._con.execute(
                "SELECT price FROM signals WHERE strategy_id = ? AND ticker = ?"
                " AND direction = ? ORDER BY pushed_at DESC LIMIT 1",
                (strategy_id, ticker, direction),
            ).fetchone()
        return float(row["price"]) if row else None

    def pushed_count_since(
        self, since: datetime, strategy_ids: set[str] | None = None
    ) -> int:
        if strategy_ids == set():
            return 0
        query = "SELECT count(*) AS n FROM signals WHERE pushed = 1 AND pushed_at >= ?"
        params: list[object] = [since.astimezone(timezone.utc).isoformat()]
        if strategy_ids is not None:
            placeholders = ",".join("?" for _ in strategy_ids)
            query += f" AND strategy_id IN ({placeholders})"
            params.extend(sorted(strategy_ids))
        with self._lock:
            row = self._con.execute(query, params).fetchone()
        return int(row["n"])

    def pushed_signals(self, since: datetime) -> list[dict[str, object]]:
        """近窗口内全部已推送信号(按推送时间升序)，供绩效虚拟盘重放。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM signals WHERE pushed = 1 AND pushed_at >= ?"
                " ORDER BY pushed_at",
                (since.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def signals_on(self, day: date) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM signals WHERE substr(ts, 1, 10) = ? ORDER BY ts",
                (day.isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def backup_to(self, dest: Path) -> None:
        """在线备份(sqlite backup API, 与写入互斥安全)。台账是唯一不可再生数据。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._con.backup(target)
            finally:
                target.close()

    def get_holdings(self, strategy_id: str) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT ticker FROM holdings WHERE strategy_id = ?", (strategy_id,)
            ).fetchall()
        return [r["ticker"] for r in rows]

    def set_holdings(self, strategy_id: str, tickers: list[str]) -> None:
        with self._lock:
            self._con.execute("DELETE FROM holdings WHERE strategy_id = ?", (strategy_id,))
            self._con.executemany(
                "INSERT INTO holdings (strategy_id, ticker) VALUES (?, ?)",
                [(strategy_id, t) for t in tickers],
            )
            self._con.commit()
