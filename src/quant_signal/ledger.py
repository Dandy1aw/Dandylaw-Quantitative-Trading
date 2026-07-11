from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from quant_signal.account import AccountState
from quant_signal.execution import (
    TERMINAL_STATES,
    ExecutionPlan,
    PlanState,
    plan_from_dict,
    plan_to_dict,
)
from quant_signal.notifier.base import Card, card_from_dict, card_to_dict
from quant_signal.strategies.base import Signal, dedup_key

if TYPE_CHECKING:
    from quant_signal.options_flow import OptionFlowSnapshot
    from quant_signal.portfolio_import import ValidatedPortfolioImport

_SCHEMA_VERSION = 6

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
CREATE TABLE IF NOT EXISTS scan_candidates (
    scan_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    price REAL NOT NULL,
    extra_json TEXT,
    as_of TEXT NOT NULL,
    PRIMARY KEY (scan_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_scan_candidates_latest
    ON scan_candidates(scan_date DESC, rank ASC);
CREATE TABLE IF NOT EXISTS scan_candidate_runs (
    scan_date TEXT PRIMARY KEY,
    as_of TEXT NOT NULL
);
INSERT OR IGNORE INTO scan_candidate_runs (scan_date, as_of)
    SELECT scan_date, max(as_of) FROM scan_candidates GROUP BY scan_date;
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_targets (
    strategy_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    target_weight REAL NOT NULL,
    as_of TEXT NOT NULL,
    PRIMARY KEY (strategy_id, ticker)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    equity TEXT NOT NULL,          -- Decimal 以 TEXT 保存, 避免浮点损失
    cash TEXT NOT NULL,
    buying_power TEXT NOT NULL,
    currency TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_time
    ON account_snapshots(retrieved_at DESC);
CREATE TABLE IF NOT EXISTS broker_positions (
    symbol TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    side TEXT NOT NULL,
    avg_entry_price TEXT NOT NULL,
    market_value TEXT NOT NULL,
    retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broker_orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    qty TEXT,
    limit_price TEXT,
    submitted_at TEXT,
    filled_qty TEXT NOT NULL,
    filled_avg_price TEXT,
    bucket TEXT NOT NULL,          -- open / recent
    retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, plan_version)
);
CREATE INDEX IF NOT EXISTS idx_execution_plans_date_state
    ON execution_plans(plan_date, state);
CREATE TABLE IF NOT EXISTS plan_events (
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, plan_version, event_type)
);
CREATE TABLE IF NOT EXISTS portfolio_imports (
    import_id TEXT PRIMARY KEY,
    image_sha256 TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    model TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    account_valid INTEGER NOT NULL,
    positions_complete INTEGER NOT NULL,
    account_active INTEGER NOT NULL DEFAULT 0,
    positions_active INTEGER NOT NULL DEFAULT 0,
    reported_position_count INTEGER NOT NULL,
    visible_position_count INTEGER NOT NULL,
    validation_errors_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_portfolio_imports_active
    ON portfolio_imports(account_active, positions_active, uploaded_at DESC);
CREATE TABLE IF NOT EXISTS observed_account_snapshots (
    import_id TEXT PRIMARY KEY,
    equity TEXT NOT NULL,
    market_value TEXT NOT NULL,
    cash TEXT NOT NULL,
    buying_power TEXT NOT NULL,
    frozen_cash TEXT NOT NULL,
    processing_cash TEXT NOT NULL,
    currency TEXT NOT NULL,
    capital_limit TEXT NOT NULL,
    max_financing_ratio TEXT NOT NULL,
    FOREIGN KEY(import_id) REFERENCES portfolio_imports(import_id)
);
CREATE TABLE IF NOT EXISTS observed_positions (
    import_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT,
    avg_entry_price TEXT,
    current_price TEXT,
    market_value TEXT,
    estimated_market_value TEXT,
    pnl TEXT,
    pnl_pct TEXT,
    weight_pct TEXT,
    precision TEXT NOT NULL,
    PRIMARY KEY(import_id, symbol),
    FOREIGN KEY(import_id) REFERENCES portfolio_imports(import_id)
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_key TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    last_error TEXT,
    sent_at TEXT,
    UNIQUE(plan_id, plan_version, event_type)
);
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox(status, next_retry_at);
CREATE TABLE IF NOT EXISTS feishu_processed_messages (
    message_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feishu_pending_imports (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS option_flow_scans (
    slot TEXT PRIMARY KEY,
    session_date TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    venue_coverage REAL NOT NULL,
    scan_type TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_option_flow_scans_session
    ON option_flow_scans(session_date, captured_at DESC);
CREATE TABLE IF NOT EXISTS option_flow_rows (
    slot TEXT NOT NULL,
    side TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    rank INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(slot, side, contract_symbol),
    FOREIGN KEY(slot) REFERENCES option_flow_scans(slot)
);
CREATE INDEX IF NOT EXISTS idx_option_flow_rows_rank
    ON option_flow_rows(slot, side, rank);
CREATE TABLE IF NOT EXISTS option_flow_outbox (
    event_key TEXT PRIMARY KEY,
    slot TEXT NOT NULL,
    session_date TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_error TEXT,
    sent_at TEXT,
    UNIQUE(slot, alert_type),
    FOREIGN KEY(slot) REFERENCES option_flow_scans(slot)
);
CREATE INDEX IF NOT EXISTS idx_option_flow_outbox_due
    ON option_flow_outbox(status, next_retry_at, expires_at);
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
            self._con.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                " WHERE CAST(schema_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
                (str(_SCHEMA_VERSION),),
            )
            self._con.commit()

    def schema_version(self) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
        return int(row["value"]) if row else 0

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

    def replace_scan_candidates(
        self,
        scan_date: date,
        candidates: Sequence[Mapping[str, object]],
        *,
        as_of: date | datetime,
    ) -> None:
        as_of_text = as_of.isoformat()
        values: list[tuple[object, ...]] = []
        for candidate in candidates:
            raw_extra = candidate.get("extra_json", candidate.get("extra"))
            if isinstance(raw_extra, str):
                json.loads(raw_extra)
                extra_json = raw_extra
            elif raw_extra is None:
                extra_json = None
            else:
                extra_json = json.dumps(raw_extra, ensure_ascii=False, sort_keys=True)
            values.append(
                (
                    scan_date.isoformat(),
                    str(candidate["ticker"]),
                    int(str(candidate["rank"])),
                    float(str(candidate["score"])),
                    float(str(candidate["price"])),
                    extra_json,
                    as_of_text,
                )
            )
        with self._lock:
            try:
                self._con.execute(
                    "INSERT OR REPLACE INTO scan_candidate_runs (scan_date, as_of) "
                    "VALUES (?, ?)",
                    (scan_date.isoformat(), as_of_text),
                )
                self._con.execute(
                    "DELETE FROM scan_candidates WHERE scan_date = ?",
                    (scan_date.isoformat(),),
                )
                self._con.executemany(
                    "INSERT INTO scan_candidates "
                    "(scan_date, ticker, rank, score, price, extra_json, as_of) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def latest_scan_candidates(
        self, scan_date: date | None = None
    ) -> list[dict[str, object]]:
        with self._lock:
            if scan_date is None:
                row = self._con.execute(
                    "SELECT max(scan_date) AS scan_date FROM scan_candidate_runs"
                ).fetchone()
                selected = str(row["scan_date"]) if row and row["scan_date"] else None
            else:
                selected = scan_date.isoformat()
            if selected is None:
                return []
            rows = self._con.execute(
                "SELECT scan_date, ticker, rank, score, price, extra_json, as_of "
                "FROM scan_candidates WHERE scan_date = ? ORDER BY rank, ticker",
                (selected,),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            raw_extra = item.get("extra_json")
            item["extra"] = json.loads(str(raw_extra)) if raw_extra else {}
            output.append(item)
        return output

    # ------------------------------------------------------------ execution ledger

    def set_strategy_targets(
        self, strategy_id: str, targets: Mapping[str, float], *, as_of: datetime
    ) -> None:
        """策略目标组合(不代表成交), 与 legacy holdings 表分开存放。"""
        as_of_text = as_of.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute(
                    "DELETE FROM strategy_targets WHERE strategy_id = ?", (strategy_id,)
                )
                self._con.executemany(
                    "INSERT INTO strategy_targets"
                    " (strategy_id, ticker, target_weight, as_of) VALUES (?, ?, ?, ?)",
                    [
                        (strategy_id, ticker, float(weight), as_of_text)
                        for ticker, weight in targets.items()
                    ],
                )
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def get_strategy_targets(self, strategy_id: str) -> dict[str, float]:
        with self._lock:
            rows = self._con.execute(
                "SELECT ticker, target_weight FROM strategy_targets"
                " WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchall()
        return {r["ticker"]: float(r["target_weight"]) for r in rows}

    def replace_account_state(self, state: AccountState) -> None:
        """整体替换券商实际状态; 账户快照保留历史, 持仓/订单只保留最新一次同步。"""
        retrieved_at = state.snapshot.retrieved_at.isoformat()
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO account_snapshots"
                    " (account_id, equity, cash, buying_power, currency, retrieved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        state.snapshot.account_id,
                        str(state.snapshot.equity),
                        str(state.snapshot.cash),
                        str(state.snapshot.buying_power),
                        state.snapshot.currency,
                        retrieved_at,
                    ),
                )
                self._con.execute("DELETE FROM broker_positions")
                self._con.executemany(
                    "INSERT INTO broker_positions"
                    " (symbol, qty, side, avg_entry_price, market_value, retrieved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            position.symbol,
                            str(position.qty),
                            position.side,
                            str(position.avg_entry_price),
                            str(position.market_value),
                            retrieved_at,
                        )
                        for position in state.positions
                    ],
                )
                self._con.execute("DELETE FROM broker_orders")
                order_rows = [
                    (order, "open") for order in state.open_orders
                ] + [(order, "recent") for order in state.recent_orders]
                self._con.executemany(
                    "INSERT OR REPLACE INTO broker_orders"
                    " (order_id, symbol, side, status, qty, limit_price, submitted_at,"
                    " filled_qty, filled_avg_price, bucket, retrieved_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            order.order_id,
                            order.symbol,
                            order.side,
                            order.status,
                            str(order.qty) if order.qty is not None else None,
                            str(order.limit_price)
                            if order.limit_price is not None
                            else None,
                            order.submitted_at.isoformat()
                            if order.submitted_at is not None
                            else None,
                            str(order.filled_qty),
                            str(order.filled_avg_price)
                            if order.filled_avg_price is not None
                            else None,
                            bucket,
                            retrieved_at,
                        )
                        for order, bucket in order_rows
                    ],
                )
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def latest_account_snapshot(self) -> dict[str, object] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM account_snapshots ORDER BY retrieved_at DESC, id DESC"
                " LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def broker_positions(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM broker_positions ORDER BY symbol"
            ).fetchall()
        return [dict(r) for r in rows]

    def broker_orders(self, bucket: str | None = None) -> list[dict[str, object]]:
        query = "SELECT * FROM broker_orders"
        params: tuple[object, ...] = ()
        if bucket is not None:
            query += " WHERE bucket = ?"
            params = (bucket,)
        with self._lock:
            rows = self._con.execute(query + " ORDER BY order_id", params).fetchall()
        return [dict(r) for r in rows]

    def save_pending_import(
        self, record: "ValidatedPortfolioImport", now: datetime
    ) -> None:
        payload_json = record.model_dump_json()
        stored_at = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._con.execute(
                "INSERT INTO feishu_pending_imports (id, payload_json, stored_at)"
                " VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " payload_json = excluded.payload_json,"
                " stored_at = excluded.stored_at",
                (payload_json, stored_at),
            )
            self._con.commit()

    def pop_pending_import(
        self,
    ) -> tuple["ValidatedPortfolioImport", datetime] | None:
        from quant_signal.portfolio_import import ValidatedPortfolioImport

        with self._lock:
            try:
                row = self._con.execute(
                    "DELETE FROM feishu_pending_imports WHERE id = 1"
                    " RETURNING payload_json, stored_at"
                ).fetchone()
                if row is None:
                    self._con.commit()
                    return None
                record = ValidatedPortfolioImport.model_validate_json(
                    str(row["payload_json"])
                )
                stored_at = datetime.fromisoformat(str(row["stored_at"]))
                self._con.commit()
                return record, stored_at
            except Exception:
                self._con.rollback()
                raise

    def save_portfolio_import(self, record: "ValidatedPortfolioImport") -> bool:
        """Persist one idempotent screenshot result and atomically activate valid layers."""
        account = record.extraction.account
        raw_json = record.extraction.model_dump_json()
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO portfolio_imports"
                    " (import_id, image_sha256, source, model, uploaded_at, observed_at,"
                    " status, account_valid, positions_complete, account_active,"
                    " positions_active, reported_position_count, visible_position_count,"
                    " validation_errors_json, raw_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)",
                    (
                        record.import_id,
                        record.image_sha256,
                        record.source,
                        record.model,
                        record.uploaded_at.astimezone(timezone.utc).isoformat(),
                        record.observed_at.astimezone(timezone.utc).isoformat(),
                        record.status.value,
                        int(record.account_valid),
                        int(record.positions_complete),
                        account.reported_position_count,
                        len(record.positions),
                        json.dumps(record.validation_errors, ensure_ascii=False),
                        raw_json,
                    ),
                )
                if record.account_valid:
                    self._con.execute(
                        "INSERT INTO observed_account_snapshots"
                        " (import_id, equity, market_value, cash, buying_power, frozen_cash,"
                        " processing_cash, currency, capital_limit, max_financing_ratio)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.import_id,
                            str(account.equity),
                            str(account.market_value),
                            str(account.cash),
                            str(account.buying_power),
                            str(account.frozen_cash),
                            str(account.processing_cash),
                            account.currency,
                            str(record.capital_limit),
                            str(record.max_financing_ratio),
                        ),
                    )
                    self._con.executemany(
                        "INSERT INTO observed_positions"
                        " (import_id, symbol, qty, avg_entry_price, current_price,"
                        " market_value, estimated_market_value, pnl, pnl_pct, weight_pct,"
                        " precision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                record.import_id,
                                row.symbol,
                                str(row.qty) if row.qty is not None else None,
                                str(row.avg_entry_price)
                                if row.avg_entry_price is not None
                                else None,
                                str(row.current_price)
                                if row.current_price is not None
                                else None,
                                str(row.market_value)
                                if row.market_value is not None
                                else None,
                                str(row.estimated_market_value)
                                if row.estimated_market_value is not None
                                else None,
                                str(row.pnl) if row.pnl is not None else None,
                                str(row.pnl_pct) if row.pnl_pct is not None else None,
                                str(row.weight_pct) if row.weight_pct is not None else None,
                                row.precision,
                            )
                            for row in record.positions
                        ],
                    )
                    self._con.execute("UPDATE portfolio_imports SET account_active = 0")
                    self._con.execute(
                        "UPDATE portfolio_imports SET account_active = 1 WHERE import_id = ?",
                        (record.import_id,),
                    )
                    if record.positions_complete:
                        self._con.execute("UPDATE portfolio_imports SET positions_active = 0")
                        self._con.execute(
                            "UPDATE portfolio_imports SET positions_active = 1"
                            " WHERE import_id = ?",
                            (record.import_id,),
                        )
                self._con.commit()
                return True
            except sqlite3.IntegrityError:
                self._con.rollback()
                return False
            except Exception:
                self._con.rollback()
                raise

    def latest_observed_account(self) -> dict[str, object] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT a.*, i.import_id, i.uploaded_at, i.observed_at, i.status,"
                " i.positions_complete, i.reported_position_count,"
                " i.visible_position_count FROM portfolio_imports AS i"
                " JOIN observed_account_snapshots AS a ON a.import_id = i.import_id"
                " WHERE i.account_active = 1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def active_observed_positions(
        self, *, exact_only: bool = False
    ) -> list[dict[str, object]]:
        flag = "positions_active" if exact_only else "account_active"
        with self._lock:
            rows = self._con.execute(
                "SELECT p.*, i.observed_at FROM observed_positions AS p"
                " JOIN portfolio_imports AS i ON i.import_id = p.import_id"
                f" WHERE i.{flag} = 1 ORDER BY p.symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_execution_plan(self, plan: ExecutionPlan) -> None:
        payload = json.dumps(plan_to_dict(plan), ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO execution_plans"
                " (plan_id, plan_version, plan_date, ticker, state, payload_json,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    plan.plan_id,
                    plan.plan_version,
                    plan.plan_date.isoformat(),
                    plan.ticker,
                    plan.state.value,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._con.commit()

    def active_execution_plans(self, plan_date: date | None = None) -> list[ExecutionPlan]:
        """每个 plan_id 只取最高版本, 且排除终态。"""
        terminal = tuple(state.value for state in TERMINAL_STATES)
        placeholders = ",".join("?" for _ in terminal)
        query = (
            "SELECT payload_json FROM execution_plans AS ep"
            " WHERE plan_version = (SELECT max(plan_version) FROM execution_plans"
            "   WHERE plan_id = ep.plan_id)"
            f" AND state NOT IN ({placeholders})"
        )
        params: list[object] = list(terminal)
        if plan_date is not None:
            query += " AND plan_date = ?"
            params.append(plan_date.isoformat())
        query += " ORDER BY plan_date, ticker"
        with self._lock:
            rows = self._con.execute(query, params).fetchall()
        return [plan_from_dict(json.loads(r["payload_json"])) for r in rows]

    def invalidate_active_plans(self, reason: str, *, now: datetime) -> int:
        plans = self.active_execution_plans()
        for plan in plans:
            self.upsert_execution_plan(
                dataclasses.replace(
                    plan,
                    state=PlanState.INVALIDATED,
                    block_reason=reason,
                    suggested_qty=None,
                    suggested_notional=None,
                    account_at=now,
                )
            )
        with self._lock:
            self._con.execute(
                "UPDATE notification_outbox SET status = 'CANCELLED', last_error = ?"
                " WHERE status = 'PENDING'",
                (reason,),
            )
            self._con.commit()
        return len(plans)

    def record_plan_event(
        self, plan_id: str, plan_version: int, event_type: str, *, now: datetime
    ) -> bool:
        """同一 plan_id + plan_version + event_type 只允许成功记录一次(通知幂等)。"""
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO plan_events (plan_id, plan_version, event_type,"
                    " created_at) VALUES (?, ?, ?, ?)",
                    (
                        plan_id,
                        plan_version,
                        event_type,
                        now.astimezone(timezone.utc).isoformat(),
                    ),
                )
                self._con.commit()
                return True
            except sqlite3.IntegrityError:
                self._con.rollback()
                return False

    def queue_plan_event(
        self,
        plan_id: str,
        plan_version: int,
        event_type: str,
        card: Card,
        *,
        now: datetime,
    ) -> bool:
        event_key = f"{plan_id}:{plan_version}:{event_type}"
        timestamp = now.astimezone(timezone.utc).isoformat()
        payload = json.dumps(card_to_dict(card), ensure_ascii=False, sort_keys=True)
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO notification_outbox"
                    " (event_key, plan_id, plan_version, event_type, status, payload_json,"
                    " created_at, attempts, next_retry_at)"
                    " VALUES (?, ?, ?, ?, 'PENDING', ?, ?, 0, ?)",
                    (
                        event_key,
                        plan_id,
                        plan_version,
                        event_type,
                        payload,
                        timestamp,
                        timestamp,
                    ),
                )
                self._con.commit()
                return True
            except sqlite3.IntegrityError:
                self._con.rollback()
                return False

    def due_plan_events(self, now: datetime) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM notification_outbox"
                " WHERE status = 'PENDING' AND next_retry_at <= ?"
                " ORDER BY created_at, event_key",
                (now.astimezone(timezone.utc).isoformat(),),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["card"] = card_from_dict(json.loads(str(item.pop("payload_json"))))
            output.append(item)
        return output

    def mark_plan_event_failed(
        self,
        event_key: str,
        error: str,
        *,
        now: datetime,
        retry_at: datetime,
    ) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE notification_outbox SET attempts = attempts + 1,"
                " last_error = ?, next_retry_at = ?"
                " WHERE event_key = ? AND status = 'PENDING'",
                (
                    error[:1000],
                    retry_at.astimezone(timezone.utc).isoformat(),
                    event_key,
                ),
            )
            self._con.commit()

    def mark_plan_event_sent(self, event_key: str, *, now: datetime) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE notification_outbox SET status = 'SENT', sent_at = ?,"
                " last_error = NULL WHERE event_key = ? AND status = 'PENDING'",
                (now.astimezone(timezone.utc).isoformat(), event_key),
            )
            self._con.commit()

    def event_was_delivered(
        self, plan_id: str, plan_version: int, event_type: str
    ) -> bool:
        with self._lock:
            outbox_row = self._con.execute(
                "SELECT status FROM notification_outbox WHERE plan_id = ?"
                " AND plan_version = ? AND event_type = ?",
                (plan_id, plan_version, event_type),
            ).fetchone()
            if outbox_row is not None:
                return str(outbox_row["status"]) == "SENT"
            row = self._con.execute(
                "SELECT 1 FROM plan_events WHERE plan_id = ? AND plan_version = ?"
                " AND event_type = ?",
                (plan_id, plan_version, event_type),
            ).fetchone()
        return row is not None

    def save_option_flow_scan(
        self,
        snapshot: "OptionFlowSnapshot",
        scan_type: str,
        card: Card | None,
        *,
        now: datetime,
        expires_at: datetime | None = None,
    ) -> bool:
        """Atomically persist one idempotent scan and its optional notification."""
        from quant_signal.options_flow import snapshot_to_dict

        if card is not None and expires_at is None:
            raise ValueError("expires_at is required when queueing an option alert")
        serialized = snapshot_to_dict(snapshot)
        raw_rows = serialized.get("rows")
        if not isinstance(raw_rows, list):
            raise ValueError("option snapshot rows are invalid")
        created = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO option_flow_scans"
                    " (slot, session_date, captured_at, provider, venue_coverage,"
                    " scan_type, row_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.slot,
                        snapshot.session_date.isoformat(),
                        snapshot.captured_at.astimezone(timezone.utc).isoformat(),
                        snapshot.provider,
                        snapshot.venue_coverage,
                        scan_type,
                        len(snapshot.rows),
                        created,
                    ),
                )
                for raw in raw_rows:
                    if not isinstance(raw, Mapping):
                        raise ValueError("option snapshot row is invalid")
                    self._con.execute(
                        "INSERT INTO option_flow_rows"
                        " (slot, side, contract_symbol, rank, volume, payload_json)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            snapshot.slot,
                            str(raw["side"]),
                            str(raw["contract_symbol"]),
                            int(raw["rank"]),
                            int(raw["volume"]),
                            json.dumps(raw, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                if card is not None:
                    assert expires_at is not None
                    event_key = f"option-flow:{snapshot.slot}:{scan_type}"
                    payload = json.dumps(
                        card_to_dict(card), ensure_ascii=False, sort_keys=True
                    )
                    self._con.execute(
                        "INSERT INTO option_flow_outbox"
                        " (event_key, slot, session_date, alert_type, status, payload_json,"
                        " created_at, attempts, next_retry_at, expires_at)"
                        " VALUES (?, ?, ?, ?, 'PENDING', ?, ?, 0, ?, ?)",
                        (
                            event_key,
                            snapshot.slot,
                            snapshot.session_date.isoformat(),
                            scan_type,
                            payload,
                            created,
                            created,
                            expires_at.astimezone(timezone.utc).isoformat(),
                        ),
                    )
                self._con.commit()
                return True
            except sqlite3.IntegrityError:
                self._con.rollback()
                return False
            except Exception:
                self._con.rollback()
                raise

    def latest_option_flow_snapshot(
        self, session: date | None = None
    ) -> "OptionFlowSnapshot | None":
        from quant_signal.options_flow import snapshot_from_dict

        query = "SELECT * FROM option_flow_scans"
        params: tuple[object, ...] = ()
        if session is not None:
            query += " WHERE session_date = ?"
            params = (session.isoformat(),)
        query += " ORDER BY captured_at DESC, slot DESC LIMIT 1"
        with self._lock:
            header = self._con.execute(query, params).fetchone()
            if header is None:
                return None
            rows = self._con.execute(
                "SELECT payload_json FROM option_flow_rows WHERE slot = ?"
                " ORDER BY CASE side WHEN 'call' THEN 0 ELSE 1 END, rank, contract_symbol",
                (header["slot"],),
            ).fetchall()
        payload = {
            "slot": str(header["slot"]),
            "captured_at": str(header["captured_at"]),
            "provider": str(header["provider"]),
            "venue_coverage": float(header["venue_coverage"]),
            "rows": [json.loads(str(row["payload_json"])) for row in rows],
        }
        return snapshot_from_dict(payload)

    def option_flow_alert_count(self, session: date) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT count(*) AS n FROM option_flow_outbox"
                " WHERE session_date = ? AND status IN ('PENDING', 'SENT')",
                (session.isoformat(),),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def try_mark_feishu_message(self, message_id: str, *, now: datetime) -> bool:
        """事件 at-least-once 投递的幂等闸：首次 True，重复 False。"""
        with self._lock:
            cursor = self._con.execute(
                "INSERT OR IGNORE INTO feishu_processed_messages"
                " (message_id, processed_at) VALUES (?, ?)",
                (message_id, now.astimezone(timezone.utc).isoformat()),
            )
            self._con.commit()
        return cursor.rowcount > 0

    def last_option_flow_alert_at(self, session: date) -> datetime | None:
        # 冷却只约束变化卡之间的间隔；基线/收盘卡不占用冷却窗口
        with self._lock:
            row = self._con.execute(
                "SELECT max(created_at) AS created_at FROM option_flow_outbox"
                " WHERE session_date = ? AND alert_type = 'change'"
                " AND status IN ('PENDING', 'SENT')",
                (session.isoformat(),),
            ).fetchone()
        value = row["created_at"] if row is not None else None
        return datetime.fromisoformat(str(value)) if value is not None else None

    def due_option_flow_alerts(self, now: datetime) -> list[dict[str, object]]:
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._con.execute(
                "UPDATE option_flow_outbox SET status = 'EXPIRED',"
                " last_error = 'STALE_OPTION_FLOW_ALERT'"
                " WHERE status = 'PENDING' AND expires_at < ?",
                (timestamp,),
            )
            rows = self._con.execute(
                "SELECT * FROM option_flow_outbox"
                " WHERE status = 'PENDING' AND next_retry_at <= ? AND expires_at >= ?"
                " ORDER BY created_at, event_key",
                (timestamp, timestamp),
            ).fetchall()
            self._con.commit()
        output: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["card"] = card_from_dict(json.loads(str(item.pop("payload_json"))))
            output.append(item)
        return output

    def mark_option_flow_alert_failed(
        self,
        event_key: str,
        error: str,
        *,
        now: datetime,
        retry_at: datetime,
    ) -> None:
        del now
        with self._lock:
            self._con.execute(
                "UPDATE option_flow_outbox SET attempts = attempts + 1,"
                " last_error = ?, next_retry_at = ?"
                " WHERE event_key = ? AND status = 'PENDING'",
                (
                    error[:1000],
                    retry_at.astimezone(timezone.utc).isoformat(),
                    event_key,
                ),
            )
            self._con.commit()

    def mark_option_flow_alert_sent(self, event_key: str, *, now: datetime) -> None:
        with self._lock:
            self._con.execute(
                "UPDATE option_flow_outbox SET status = 'SENT', sent_at = ?,"
                " last_error = NULL WHERE event_key = ? AND status = 'PENDING'",
                (now.astimezone(timezone.utc).isoformat(), event_key),
            )
            self._con.commit()

    def option_flow_alert_status(self, slot: str, alert_type: str) -> str | None:
        with self._lock:
            row = self._con.execute(
                "SELECT status FROM option_flow_outbox WHERE slot = ? AND alert_type = ?",
                (slot, alert_type),
            ).fetchone()
        return str(row["status"]) if row is not None else None
