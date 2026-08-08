from __future__ import annotations

import dataclasses
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
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
from quant_signal.position_discipline import DisciplineState
from quant_signal.strategies.base import Signal, dedup_key

if TYPE_CHECKING:
    from quant_signal.company_profiles import CompanyProfile
    from quant_signal.extreme_movers import ExtremeMoverEvent
    from quant_signal.options_flow import HoldingOptionFlowSnapshot, OptionFlowSnapshot
    from quant_signal.options_intel import OptionIntel
    from quant_signal.portfolio_import import ValidatedPortfolioImport

_SCHEMA_VERSION = 17

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
CREATE TABLE IF NOT EXISTS broker_fills (
    source TEXT NOT NULL,
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    filled_qty TEXT NOT NULL,
    filled_avg_price TEXT NOT NULL,
    filled_at TEXT,
    retrieved_at TEXT NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (source, order_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_fills_time
    ON broker_fills(source, filled_at, retrieved_at);
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
CREATE TABLE IF NOT EXISTS job_runtime (
    job_id TEXT PRIMARY KEY,
    running_since TEXT,
    last_success TEXT,
    last_failure TEXT,
    last_duration REAL,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS holding_option_flow_scans (
    slot TEXT PRIMARY KEY,
    session_date TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_holding_option_flow_scans_session
    ON holding_option_flow_scans(session_date, captured_at DESC);
CREATE TABLE IF NOT EXISTS holding_option_flow_rows (
    slot TEXT NOT NULL,
    underlying TEXT NOT NULL,
    call_volume INTEGER NOT NULL,
    put_volume INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(slot, underlying),
    FOREIGN KEY(slot) REFERENCES holding_option_flow_scans(slot)
);
CREATE TABLE IF NOT EXISTS company_profiles (
    ticker TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_company_profiles_fetched
    ON company_profiles(fetched_at DESC);
CREATE TABLE IF NOT EXISTS option_intel_daily (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    spot TEXT NOT NULL,
    atm_iv REAL,
    realized_vol_20d REAL,
    expected_move_pct REAL,
    expected_move_expiry TEXT,
    pc_volume_ratio REAL,
    pc_oi_ratio REAL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(session_date, symbol)
);
CREATE TABLE IF NOT EXISTS market_regime_snapshots (
    report_kind TEXT NOT NULL,
    as_of TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(report_kind, as_of)
);
CREATE INDEX IF NOT EXISTS idx_market_regime_latest
    ON market_regime_snapshots(report_kind, captured_at DESC);
CREATE TABLE IF NOT EXISTS candidate_lane_snapshots (
    report_kind TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rank INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    lane TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(report_kind, as_of, rank)
);
CREATE TABLE IF NOT EXISTS candidate_forward_evaluations (
    report_kind TEXT NOT NULL,
    as_of TEXT NOT NULL,
    rank INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    horizon_sessions INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    return_pct REAL NOT NULL,
    benchmark_ticker TEXT NOT NULL DEFAULT 'QQQ',
    benchmark_entry_price REAL,
    benchmark_exit_price REAL,
    benchmark_return_pct REAL,
    excess_return_pct REAL,
    max_favorable_excursion_pct REAL,
    max_adverse_excursion_pct REAL,
    net_return_pct REAL,
    transaction_cost_bps REAL NOT NULL DEFAULT 0,
    rule_version TEXT,
    model_version TEXT,
    data_version TEXT,
    input_semantic_hash TEXT,
    config_hash TEXT,
    universe_hash TEXT,
    point_in_time_status TEXT,
    cost_model_json TEXT,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY(report_kind, as_of, rank, horizon_sessions)
);
CREATE TABLE IF NOT EXISTS position_discipline_states (
    ticker TEXT PRIMARY KEY,
    basis_version TEXT NOT NULL,
    notified_stage INTEGER NOT NULL,
    peak_price TEXT NOT NULL,
    basis_quantity TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_briefing_runs (
    run_id TEXT PRIMARY KEY,
    report_kind TEXT NOT NULL,
    as_of TEXT NOT NULL,
    data_version TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(report_kind, as_of)
);
CREATE TABLE IF NOT EXISTS extreme_mover_runs (
    session_date TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    universe_count INTEGER NOT NULL,
    covered_count INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    screened_count INTEGER NOT NULL DEFAULT 0,
    confirmed_count INTEGER NOT NULL DEFAULT 0,
    feed TEXT NOT NULL DEFAULT 'unknown',
    error TEXT,
    universe_hash TEXT NOT NULL DEFAULT '',
    config_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS extreme_mover_events (
    session_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    daily_return TEXT NOT NULL,
    close_price TEXT NOT NULL,
    avg_dollar_volume_20d TEXT,
    sector TEXT,
    industry TEXT,
    quote_type TEXT,
    eligibility TEXT NOT NULL,
    source TEXT NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_extreme_mover_events_window
    ON extreme_mover_events(session_date DESC, eligibility, direction);
CREATE TABLE IF NOT EXISTS manual_price_monitors (
    ticker TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclasses.dataclass(frozen=True)
class USBriefingRun:
    run_id: str
    report_kind: str
    as_of: date
    data_version: str
    status: str
    created: bool


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)  # type: ignore[arg-type]
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


def _payload_json(payload: object) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=_json_default
    )


class SignalLedger:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 调度器在后台线程池跑 job，连接需跨线程可用；用 Lock 串行化访问保证安全。
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("PRAGMA foreign_keys = ON")
            self._con.execute("PRAGMA busy_timeout = 5000")
            self._con.execute("PRAGMA journal_mode = WAL")
            self._con.executescript(_SCHEMA)
            self._migrate_schema()
            self._con.execute(
                "DELETE FROM us_briefing_runs WHERE rowid NOT IN ("
                " SELECT rowid FROM ("
                "  SELECT rowid, row_number() OVER ("
                "   PARTITION BY report_kind, as_of ORDER BY"
                "    CASE status WHEN 'DELIVERED' THEN 0 WHEN 'SHADOWED' THEN 1"
                "     WHEN 'STARTED' THEN 2 ELSE 3 END, created_at DESC"
                "  ) AS slot_rank FROM us_briefing_runs"
                " ) WHERE slot_rank = 1"
                ")"
            )
            self._con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_us_briefing_run_slot"
                " ON us_briefing_runs(report_kind, as_of)"
            )
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

    def replace_extreme_mover_run(
        self,
        run: object,
        events: Sequence[object],
    ) -> None:
        """Atomically replace one completed session and its immutable events."""
        from quant_signal.extreme_movers import ExtremeMoverEvent, ExtremeMoverRun

        if not isinstance(run, ExtremeMoverRun):
            raise TypeError("run must be an ExtremeMoverRun")
        typed_events: list[ExtremeMoverEvent] = []
        for event in events:
            if not isinstance(event, ExtremeMoverEvent):
                raise TypeError("events must contain ExtremeMoverEvent values")
            if event.session != run.session:
                raise ValueError("event session must match run session")
            typed_events.append(event)
        with self._lock:
            try:
                self._con.execute("BEGIN")
                self._con.execute(
                    "INSERT INTO extreme_mover_runs"
                    " (session_date, status, universe_count, covered_count, completed_at,"
                    " screened_count, confirmed_count, feed, error, universe_hash,"
                    " config_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(session_date) DO UPDATE SET"
                    " status=excluded.status, universe_count=excluded.universe_count,"
                    " covered_count=excluded.covered_count, completed_at=excluded.completed_at,"
                    " screened_count=excluded.screened_count,"
                    " confirmed_count=excluded.confirmed_count, feed=excluded.feed,"
                    " error=excluded.error, universe_hash=excluded.universe_hash,"
                    " config_hash=excluded.config_hash",
                    (
                        run.session.isoformat(),
                        run.status,
                        run.universe_count,
                        run.covered_count,
                        run.completed_at.astimezone(timezone.utc).isoformat(),
                        run.screened_count,
                        run.confirmed_count,
                        run.feed,
                        run.error,
                        run.universe_hash,
                        run.config_hash,
                    ),
                )
                self._con.execute(
                    "DELETE FROM extreme_mover_events WHERE session_date = ?",
                    (run.session.isoformat(),),
                )
                self._con.executemany(
                    "INSERT INTO extreme_mover_events"
                    " (session_date, ticker, direction, daily_return, close_price,"
                    " avg_dollar_volume_20d, sector, industry, quote_type, eligibility,"
                    " source, backfilled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            event.session.isoformat(), event.ticker,
                            event.direction.value, str(event.daily_return),
                            str(event.close),
                            str(event.avg_dollar_volume_20d)
                            if event.avg_dollar_volume_20d is not None else None,
                            event.sector, event.industry, event.quote_type,
                            event.eligibility.value, event.source,
                            int(event.backfilled),
                        )
                        for event in typed_events
                    ],
                )
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def record_extreme_mover_run(self, run: object) -> None:
        """Persist a failed/incomplete attempt without downgrading a complete session."""
        from quant_signal.extreme_movers import ExtremeMoverRun

        if not isinstance(run, ExtremeMoverRun):
            raise TypeError("run must be an ExtremeMoverRun")
        with self._lock:
            self._con.execute(
                "INSERT INTO extreme_mover_runs"
                " (session_date, status, universe_count, covered_count, completed_at,"
                " screened_count, confirmed_count, feed, error, universe_hash,"
                " config_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(session_date) DO UPDATE SET"
                " status=excluded.status, universe_count=excluded.universe_count,"
                " covered_count=excluded.covered_count, completed_at=excluded.completed_at,"
                " screened_count=excluded.screened_count,"
                " confirmed_count=excluded.confirmed_count, feed=excluded.feed,"
                " error=excluded.error, universe_hash=excluded.universe_hash,"
                " config_hash=excluded.config_hash"
                " WHERE extreme_mover_runs.status != 'COMPLETE'",
                (
                    run.session.isoformat(), run.status, run.universe_count,
                    run.covered_count,
                    run.completed_at.astimezone(timezone.utc).isoformat(),
                    run.screened_count, run.confirmed_count, run.feed, run.error,
                    run.universe_hash, run.config_hash,
                ),
            )
            self._con.commit()

    def extreme_mover_run(self, session: date) -> dict[str, object] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM extreme_mover_runs WHERE session_date = ?",
                (session.isoformat(),),
            ).fetchone()
        return dict(row) if row else None

    def latest_complete_extreme_mover_session(self) -> date | None:
        with self._lock:
            row = self._con.execute(
                "SELECT session_date FROM extreme_mover_runs"
                " WHERE status = 'COMPLETE' ORDER BY session_date DESC LIMIT 1"
            ).fetchone()
        return date.fromisoformat(str(row["session_date"])) if row else None

    def extreme_mover_events(
        self,
        through: date,
        *,
        window_sessions: int = 1,
    ) -> list["ExtremeMoverEvent"]:
        from quant_signal.extreme_movers import (
            Eligibility,
            ExtremeMoverEvent,
            MoverDirection,
        )

        if window_sessions < 1:
            raise ValueError("window_sessions must be positive")
        with self._lock:
            rows = self._con.execute(
                "SELECT e.* FROM extreme_mover_events e"
                " WHERE e.session_date IN ("
                "  SELECT session_date FROM extreme_mover_runs"
                "  WHERE status = 'COMPLETE' AND session_date <= ?"
                "  ORDER BY session_date DESC LIMIT ?"
                " ) ORDER BY e.session_date, e.ticker",
                (through.isoformat(), window_sessions),
            ).fetchall()
        return [
            ExtremeMoverEvent(
                session=date.fromisoformat(str(row["session_date"])),
                ticker=str(row["ticker"]),
                direction=MoverDirection(str(row["direction"])),
                daily_return=Decimal(str(row["daily_return"])),
                close=Decimal(str(row["close_price"])),
                avg_dollar_volume_20d=(
                    Decimal(str(row["avg_dollar_volume_20d"]))
                    if row["avg_dollar_volume_20d"] is not None else None
                ),
                sector=str(row["sector"]) if row["sector"] is not None else None,
                industry=str(row["industry"]) if row["industry"] is not None else None,
                quote_type=(
                    str(row["quote_type"]) if row["quote_type"] is not None else None
                ),
                eligibility=Eligibility(str(row["eligibility"])),
                source=str(row["source"]),
                backfilled=bool(row["backfilled"]),
            )
            for row in rows
        ]

    def enable_manual_monitor(self, ticker: str, *, now: datetime) -> bool:
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker is required")
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            existing = self._con.execute(
                "SELECT enabled FROM manual_price_monitors WHERE ticker = ?", (symbol,)
            ).fetchone()
            changed = existing is None or not bool(existing["enabled"])
            self._con.execute(
                "INSERT INTO manual_price_monitors"
                " (ticker, enabled, created_at, updated_at) VALUES (?, 1, ?, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET enabled=1, updated_at=excluded.updated_at",
                (symbol, timestamp, timestamp),
            )
            self._con.commit()
        return changed

    def disable_manual_monitor(self, ticker: str, *, now: datetime) -> bool:
        symbol = ticker.strip().upper()
        with self._lock:
            cursor = self._con.execute(
                "UPDATE manual_price_monitors SET enabled=0, updated_at=?"
                " WHERE ticker=? AND enabled=1",
                (now.astimezone(timezone.utc).isoformat(), symbol),
            )
            self._con.commit()
        return cursor.rowcount > 0

    def active_manual_monitors(self) -> list[str]:
        with self._lock:
            rows = self._con.execute(
                "SELECT ticker FROM manual_price_monitors"
                " WHERE enabled=1 ORDER BY ticker"
            ).fetchall()
        return [str(row["ticker"]) for row in rows]

    def save_company_profiles(
        self,
        profiles: Sequence[object],
        *,
        fetched_at: datetime,
    ) -> None:
        from quant_signal.company_profiles import CompanyProfile

        timestamp = fetched_at.astimezone(timezone.utc).isoformat()
        rows: list[tuple[str, str, str, str, str]] = []
        for value in profiles:
            if not isinstance(value, CompanyProfile):
                raise TypeError("profiles must contain CompanyProfile values")
            rows.append(
                (
                    value.ticker,
                    value.as_of.isoformat(),
                    value.data_status,
                    timestamp,
                    _payload_json(value),
                )
            )
        with self._lock:
            self._con.executemany(
                "INSERT INTO company_profiles"
                " (ticker, as_of, status, fetched_at, payload_json)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET"
                " as_of=excluded.as_of, status=excluded.status,"
                " fetched_at=excluded.fetched_at, payload_json=excluded.payload_json",
                rows,
            )
            self._con.commit()

    def _migrate_schema(self) -> None:
        """Apply additive migrations before publishing the new schema version."""

        columns = {
            str(row[1])
            for row in self._con.execute(
                "PRAGMA table_info(candidate_forward_evaluations)"
            ).fetchall()
        }
        additions = {
            "benchmark_ticker": "TEXT NOT NULL DEFAULT 'QQQ'",
            "benchmark_entry_price": "REAL",
            "benchmark_exit_price": "REAL",
            "benchmark_return_pct": "REAL",
            "excess_return_pct": "REAL",
            "max_favorable_excursion_pct": "REAL",
            "max_adverse_excursion_pct": "REAL",
            "net_return_pct": "REAL",
            "transaction_cost_bps": "REAL NOT NULL DEFAULT 0",
            "rule_version": "TEXT",
            "model_version": "TEXT",
            "data_version": "TEXT",
            "input_semantic_hash": "TEXT",
            "config_hash": "TEXT",
            "universe_hash": "TEXT",
            "point_in_time_status": "TEXT",
            "cost_model_json": "TEXT",
        }
        for column, declaration in additions.items():
            if column not in columns:
                self._con.execute(
                    f"ALTER TABLE candidate_forward_evaluations "
                    f"ADD COLUMN {column} {declaration}"
                )
        mover_columns = {
            str(row[1])
            for row in self._con.execute(
                "PRAGMA table_info(extreme_mover_runs)"
            ).fetchall()
        }
        mover_additions = {
            "screened_count": "INTEGER NOT NULL DEFAULT 0",
            "confirmed_count": "INTEGER NOT NULL DEFAULT 0",
            "feed": "TEXT NOT NULL DEFAULT 'unknown'",
            "error": "TEXT",
            "universe_hash": "TEXT NOT NULL DEFAULT ''",
            "config_hash": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in mover_additions.items():
            if column not in mover_columns:
                self._con.execute(
                    f"ALTER TABLE extreme_mover_runs ADD COLUMN {column} {declaration}"
                )

    def cached_company_profiles(
        self,
        tickers: Sequence[str],
        *,
        now: datetime,
        success_max_age: timedelta,
        failure_max_age: timedelta,
    ) -> dict[str, "CompanyProfile"]:
        from quant_signal.company_profiles import CompanyProfile

        wanted = tuple(sorted({ticker.strip().upper() for ticker in tickers if ticker.strip()}))
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._con.execute(
                f"SELECT * FROM company_profiles WHERE ticker IN ({placeholders})",
                wanted,
            ).fetchall()
        output: dict[str, CompanyProfile] = {}
        now_utc = now.astimezone(timezone.utc)
        for row in rows:
            fetched = datetime.fromisoformat(str(row["fetched_at"]))
            max_age = (
                success_max_age if str(row["status"]) == "ok" else failure_max_age
            )
            if now_utc - fetched.astimezone(timezone.utc) > max_age:
                continue
            payload = json.loads(str(row["payload_json"]))
            output[str(row["ticker"])] = CompanyProfile(
                ticker=str(payload["ticker"]),
                as_of=date.fromisoformat(str(payload["as_of"])),
                market_cap_usd=(
                    int(payload["market_cap_usd"])
                    if payload.get("market_cap_usd") is not None else None
                ),
                gics_sector=(str(payload["gics_sector"]) if payload.get("gics_sector") else None),
                industry=str(payload["industry"]) if payload.get("industry") else None,
                company_name=(str(payload["company_name"]) if payload.get("company_name") else None),
                business_summary=(str(payload["business_summary"]) if payload.get("business_summary") else None),
                total_revenue=(int(payload["total_revenue"]) if payload.get("total_revenue") is not None else None),
                revenue_growth=(float(payload["revenue_growth"]) if payload.get("revenue_growth") is not None else None),
                earnings_growth=(float(payload["earnings_growth"]) if payload.get("earnings_growth") is not None else None),
                profit_margin=(float(payload["profit_margin"]) if payload.get("profit_margin") is not None else None),
                return_on_equity=(float(payload["return_on_equity"]) if payload.get("return_on_equity") is not None else None),
                free_cash_flow=(int(payload["free_cash_flow"]) if payload.get("free_cash_flow") is not None else None),
                quote_type=str(payload["quote_type"]) if payload.get("quote_type") else None,
                source=str(payload["source"]),
                data_status=str(payload["data_status"]),  # type: ignore[arg-type]
            )
        return output

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

    def record_job_start(self, job_id: str, *, now: datetime) -> None:
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._con.execute(
                "INSERT INTO job_runtime (job_id, running_since, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " running_since = excluded.running_since,"
                " updated_at = excluded.updated_at",
                (job_id, timestamp, timestamp),
            )
            self._con.commit()

    def record_job_success(
        self, job_id: str, *, now: datetime, duration: float
    ) -> None:
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._con.execute(
                "INSERT INTO job_runtime"
                " (job_id, running_since, last_success, last_duration,"
                "  last_error, consecutive_failures, updated_at)"
                " VALUES (?, NULL, ?, ?, NULL, 0, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " running_since = NULL, last_success = excluded.last_success,"
                " last_duration = excluded.last_duration, last_error = NULL,"
                " consecutive_failures = 0, updated_at = excluded.updated_at",
                (job_id, timestamp, duration, timestamp),
            )
            self._con.commit()

    def record_job_failure(
        self,
        job_id: str,
        *,
        now: datetime,
        duration: float,
        error: str,
    ) -> None:
        timestamp = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            self._con.execute(
                "INSERT INTO job_runtime"
                " (job_id, running_since, last_failure, last_duration,"
                "  last_error, consecutive_failures, updated_at)"
                " VALUES (?, NULL, ?, ?, ?, 1, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " running_since = NULL, last_failure = excluded.last_failure,"
                " last_duration = excluded.last_duration,"
                " last_error = excluded.last_error,"
                " consecutive_failures = job_runtime.consecutive_failures + 1,"
                " updated_at = excluded.updated_at",
                (job_id, timestamp, duration, error, timestamp),
            )
            self._con.commit()

    def job_runtime_snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM job_runtime ORDER BY job_id"
            ).fetchall()
        return [dict(row) for row in rows]

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

    def all_held_tickers(self) -> list[str]:
        """全部策略的虚拟持仓并集（期权情报层的覆盖来源之一）。"""
        with self._lock:
            rows = self._con.execute(
                "SELECT DISTINCT ticker FROM holdings ORDER BY ticker"
            ).fetchall()
        return [r["ticker"] for r in rows]

    def save_option_intel_daily(self, intel: OptionIntel, *, now: datetime) -> None:
        """同 (session, symbol) 覆盖写入——任务重跑以最后一次为准。"""
        with self._lock:
            self._con.execute(
                "INSERT OR REPLACE INTO option_intel_daily"
                " (session_date, symbol, spot, atm_iv, realized_vol_20d,"
                " expected_move_pct, expected_move_expiry, pc_volume_ratio,"
                " pc_oi_ratio, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intel.session.isoformat(),
                    intel.symbol,
                    str(intel.spot),
                    intel.atm_iv,
                    intel.realized_vol_20d,
                    intel.expected_move_pct,
                    intel.expected_move_expiry.isoformat()
                    if intel.expected_move_expiry is not None
                    else None,
                    intel.pc_volume_ratio,
                    intel.pc_oi_ratio,
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            self._con.commit()

    def option_intel_history(self, symbol: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM option_intel_daily WHERE symbol = ?"
                " ORDER BY session_date",
                (symbol,),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune_option_intel(self, before: datetime) -> int:
        cutoff = before.astimezone(timezone.utc).isoformat()
        with self._lock:
            cursor = self._con.execute(
                "DELETE FROM option_intel_daily WHERE captured_at < ?", (cutoff,)
            )
            self._con.commit()
        return cursor.rowcount

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
                fill_rows = [
                    order
                    for order in state.recent_orders
                    if order.filled_qty > 0 and order.filled_avg_price is not None
                ]
                self._con.executemany(
                    "INSERT INTO broker_fills"
                    " (source, order_id, symbol, side, filled_qty, filled_avg_price,"
                    "  filled_at, retrieved_at, currency)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(source, order_id) DO UPDATE SET"
                    " symbol = excluded.symbol, side = excluded.side,"
                    " filled_qty = excluded.filled_qty,"
                    " filled_avg_price = excluded.filled_avg_price,"
                    " filled_at = COALESCE(excluded.filled_at, broker_fills.filled_at),"
                    " retrieved_at = excluded.retrieved_at,"
                    " currency = excluded.currency",
                    [
                        (
                            state.snapshot.source,
                            order.order_id,
                            order.symbol,
                            order.side,
                            str(order.filled_qty),
                            str(order.filled_avg_price),
                            order.filled_at.isoformat()
                            if order.filled_at is not None
                            else None,
                            retrieved_at,
                            state.snapshot.currency,
                        )
                        for order in fill_rows
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

    def broker_fills(self, source: str | None = None) -> list[dict[str, object]]:
        """Immutable broker-reported aggregate fills retained across snapshots."""
        query = "SELECT * FROM broker_fills"
        params: tuple[object, ...] = ()
        if source is not None:
            query += " WHERE source = ?"
            params = (source,)
        with self._lock:
            rows = self._con.execute(
                query
                + " ORDER BY COALESCE(filled_at, retrieved_at), source, order_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def save_pending_import(
        self, record: "ValidatedPortfolioImport", now: datetime
    ) -> bool:
        """Store the newest PARTIAL import and report whether one was replaced."""
        payload_json = record.model_dump_json()
        stored_at = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
                replaced = self._con.execute(
                    "SELECT 1 FROM feishu_pending_imports WHERE id = 1"
                ).fetchone() is not None
                self._con.execute(
                    "INSERT INTO feishu_pending_imports (id, payload_json, stored_at)"
                    " VALUES (1, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET"
                    " payload_json = excluded.payload_json,"
                    " stored_at = excluded.stored_at",
                    (payload_json, stored_at),
                )
                self._con.commit()
                return replaced
            except Exception:
                self._con.rollback()
                raise

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
                    assert account.equity is not None
                    assert account.market_value is not None
                    assert account.cash is not None
                    assert account.buying_power is not None
                    assert account.currency is not None
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
                            str(account.frozen_cash or Decimal("0")),
                            str(account.processing_cash or Decimal("0")),
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
        positions = [dict(row) for row in rows]
        # Brokerage screenshots can keep closed symbols in the holdings table with
        # quantity 0.  Retain those rows in the import audit trail, but do not expose
        # them as active holdings to sizing, risk checks, or bot replies.
        return [
            position
            for position in positions
            if position["qty"] is None
            or Decimal(str(position["qty"])) != Decimal("0")
        ]

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

    def save_holding_option_flow_snapshot(
        self,
        snapshot: "HoldingOptionFlowSnapshot",
        *,
        now: datetime,
    ) -> bool:
        """Persist one idempotent observed-holdings option-volume snapshot."""
        created = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute(
                    "INSERT INTO holding_option_flow_scans"
                    " (slot, session_date, captured_at, provider, row_count, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        snapshot.slot,
                        snapshot.session_date.isoformat(),
                        snapshot.captured_at.astimezone(timezone.utc).isoformat(),
                        snapshot.provider,
                        len(snapshot.rows),
                        created,
                    ),
                )
                self._con.executemany(
                    "INSERT INTO holding_option_flow_rows"
                    " (slot, underlying, call_volume, put_volume, payload_json)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            snapshot.slot,
                            row.underlying,
                            row.call_volume,
                            row.put_volume,
                            json.dumps(
                                {
                                    "underlying": row.underlying,
                                    "call_volume": row.call_volume,
                                    "put_volume": row.put_volume,
                                    "call_delta": row.call_delta,
                                    "put_delta": row.put_delta,
                                    "data_status": row.data_status,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                        for row in snapshot.rows
                    ],
                )
                self._con.commit()
                return True
            except sqlite3.IntegrityError:
                self._con.rollback()
                return False
            except Exception:
                self._con.rollback()
                raise

    def latest_holding_option_flow_snapshot(
        self, session: date | None = None
    ) -> "HoldingOptionFlowSnapshot | None":
        from quant_signal.options_flow import HoldingOptionFlow, HoldingOptionFlowSnapshot

        query = "SELECT * FROM holding_option_flow_scans"
        params: tuple[object, ...] = ()
        if session is not None:
            query += " WHERE session_date = ?"
            params = (session.isoformat(),)
        query += " ORDER BY captured_at DESC, slot DESC LIMIT 1"
        with self._lock:
            header = self._con.execute(query, params).fetchone()
            if header is None:
                return None
            raw_rows = self._con.execute(
                "SELECT payload_json FROM holding_option_flow_rows"
                " WHERE slot = ? ORDER BY underlying",
                (header["slot"],),
            ).fetchall()
        rows = []
        for record in raw_rows:
            payload = json.loads(str(record["payload_json"]))
            rows.append(
                HoldingOptionFlow(
                    underlying=str(payload["underlying"]),
                    call_volume=int(payload["call_volume"]),
                    put_volume=int(payload["put_volume"]),
                    call_delta=(
                        int(payload["call_delta"])
                        if payload.get("call_delta") is not None
                        else None
                    ),
                    put_delta=(
                        int(payload["put_delta"])
                        if payload.get("put_delta") is not None
                        else None
                    ),
                    data_status=str(payload["data_status"]),  # type: ignore[arg-type]
                )
            )
        return HoldingOptionFlowSnapshot(
            slot=str(header["slot"]),
            captured_at=datetime.fromisoformat(str(header["captured_at"])),
            provider=str(header["provider"]),
            rows=tuple(rows),
        )

    def prune_holding_option_flow(self, before: datetime) -> int:
        cutoff = before.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
                self._con.execute(
                    "DELETE FROM holding_option_flow_rows WHERE slot IN ("
                    " SELECT slot FROM holding_option_flow_scans WHERE captured_at < ?)",
                    (cutoff,),
                )
                cursor = self._con.execute(
                    "DELETE FROM holding_option_flow_scans WHERE captured_at < ?",
                    (cutoff,),
                )
                self._con.commit()
                return cursor.rowcount
            except Exception:
                self._con.rollback()
                raise

    def prune_option_flow(self, before: datetime) -> int:
        cutoff = before.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute("BEGIN IMMEDIATE")
                self._con.execute(
                    "DELETE FROM option_flow_outbox"
                    " WHERE status <> 'PENDING' AND slot IN ("
                    " SELECT slot FROM option_flow_scans WHERE captured_at < ?"
                    ")",
                    (cutoff,),
                )
                self._con.execute(
                    "DELETE FROM option_flow_rows WHERE slot IN ("
                    " SELECT scans.slot FROM option_flow_scans AS scans"
                    " WHERE scans.captured_at < ? AND NOT EXISTS ("
                    " SELECT 1 FROM option_flow_outbox AS outbox"
                    " WHERE outbox.slot = scans.slot AND outbox.status = 'PENDING'"
                    ")"
                    ")",
                    (cutoff,),
                )
                cursor = self._con.execute(
                    "DELETE FROM option_flow_scans"
                    " WHERE captured_at < ? AND NOT EXISTS ("
                    " SELECT 1 FROM option_flow_outbox"
                    " WHERE option_flow_outbox.slot = option_flow_scans.slot"
                    " AND option_flow_outbox.status = 'PENDING'"
                    ")",
                    (cutoff,),
                )
                self._con.execute(
                    "DELETE FROM feishu_processed_messages WHERE processed_at < ?",
                    (cutoff,),
                )
                self._con.commit()
                return cursor.rowcount
            except Exception:
                self._con.rollback()
                raise

    def option_flow_alert_count(
        self,
        session: date,
        *,
        alert_types: Sequence[str] | None = None,
    ) -> int:
        type_clause = ""
        params: list[object] = [session.isoformat()]
        if alert_types:
            placeholders = ",".join("?" for _ in alert_types)
            type_clause = f" AND alert_type IN ({placeholders})"
            params.extend(alert_types)
        with self._lock:
            row = self._con.execute(
                "SELECT count(*) AS n FROM option_flow_outbox"
                " WHERE session_date = ? AND status IN ('PENDING', 'SENT')"
                + type_clause,
                params,
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

    def last_option_flow_alert_at(
        self,
        session: date,
        *,
        alert_types: Sequence[str] = ("change",),
    ) -> datetime | None:
        # 冷却只约束变化卡之间的间隔；基线/收盘卡不占用冷却窗口
        if not alert_types:
            return None
        placeholders = ",".join("?" for _ in alert_types)
        with self._lock:
            row = self._con.execute(
                "SELECT max(created_at) AS created_at FROM option_flow_outbox"
                f" WHERE session_date = ? AND alert_type IN ({placeholders})"
                " AND status IN ('PENDING', 'SENT')",
                (session.isoformat(), *alert_types),
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

    def save_market_regime_snapshot(
        self,
        report_kind: str,
        payload: Mapping[str, object],
        *,
        now: datetime,
    ) -> None:
        as_of = str(payload.get("as_of", ""))
        if not as_of:
            raise ValueError("market regime payload requires as_of")
        with self._lock:
            self._con.execute(
                "INSERT INTO market_regime_snapshots"
                " (report_kind, as_of, payload_json, captured_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(report_kind, as_of) DO UPDATE SET"
                " payload_json = excluded.payload_json, captured_at = excluded.captured_at",
                (
                    report_kind,
                    as_of,
                    _payload_json(payload),
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            self._con.commit()

    def latest_market_regime_snapshot(
        self, report_kind: str
    ) -> dict[str, object] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT payload_json FROM market_regime_snapshots"
                " WHERE report_kind = ? ORDER BY captured_at DESC LIMIT 1",
                (report_kind,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return dict(payload) if isinstance(payload, dict) else None

    def replace_candidate_lane_snapshot(
        self,
        report_kind: str,
        as_of: date,
        candidates: Sequence[Mapping[str, object]],
        *,
        now: datetime,
    ) -> None:
        captured_at = now.astimezone(timezone.utc).isoformat()
        with self._lock:
            try:
                self._con.execute("BEGIN")
                self._con.execute(
                    "DELETE FROM candidate_lane_snapshots"
                    " WHERE report_kind = ? AND as_of = ?",
                    (report_kind, as_of.isoformat()),
                )
                self._con.executemany(
                    "INSERT INTO candidate_lane_snapshots"
                    " (report_kind, as_of, rank, ticker, lane, payload_json, captured_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            report_kind,
                            as_of.isoformat(),
                            rank,
                            str(candidate.get("ticker", "")),
                            str(candidate.get("lane", "")),
                            _payload_json(candidate),
                            captured_at,
                        )
                        for rank, candidate in enumerate(candidates, start=1)
                    ],
                )
                self._con.commit()
            except Exception:
                self._con.rollback()
                raise

    def candidate_lane_snapshot(
        self, report_kind: str, as_of: date
    ) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT payload_json FROM candidate_lane_snapshots"
                " WHERE report_kind = ? AND as_of = ? ORDER BY rank",
                (report_kind, as_of.isoformat()),
            ).fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if isinstance(payload, dict):
                output.append(dict(payload))
        return output

    def pending_candidate_forward_evaluations(
        self, horizons: Sequence[int] = (5, 10, 20, 21, 63)
    ) -> list[dict[str, object]]:
        wanted = tuple(sorted({int(value) for value in horizons if int(value) > 0}))
        if not wanted:
            return []
        with self._lock:
            snapshots = self._con.execute(
                "SELECT c.report_kind, c.as_of, c.rank, c.ticker,"
                " c.payload_json, u.payload_json AS run_payload_json"
                " FROM candidate_lane_snapshots AS c"
                " LEFT JOIN us_briefing_runs AS u"
                " ON u.report_kind = c.report_kind AND u.as_of = c.as_of"
                " ORDER BY c.as_of, c.report_kind, c.rank"
            ).fetchall()
            completed = {
                (
                    str(row["report_kind"]),
                    str(row["as_of"]),
                    int(row["rank"]),
                    int(row["horizon_sessions"]),
                )
                for row in self._con.execute(
                    "SELECT report_kind, as_of, rank, horizon_sessions"
                    " FROM candidate_forward_evaluations"
                ).fetchall()
            }
        output: list[dict[str, object]] = []
        for row in snapshots:
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                continue
            run_payload: dict[str, object] = {}
            if row["run_payload_json"] is not None:
                decoded = json.loads(str(row["run_payload_json"]))
                if isinstance(decoded, dict):
                    run_payload = dict(decoded)
            for horizon in wanted:
                key = (
                    str(row["report_kind"]),
                    str(row["as_of"]),
                    int(row["rank"]),
                    horizon,
                )
                if key in completed:
                    continue
                output.append(
                    {
                        "report_kind": key[0],
                        "as_of": date.fromisoformat(key[1]),
                        "rank": key[2],
                        "ticker": str(row["ticker"]),
                        "horizon_sessions": horizon,
                        "payload": dict(payload),
                        "evidence": {
                            key: run_payload.get(key)
                            for key in (
                                "rule_version",
                                "model_version",
                                "data_version",
                                "input_semantic_hash",
                                "config_hash",
                                "universe_hash",
                                "point_in_time",
                                "cost_model",
                            )
                        },
                    }
                )
        return output

    def save_candidate_forward_evaluation(
        self,
        *,
        report_kind: str,
        as_of: date,
        rank: int,
        ticker: str,
        horizon_sessions: int,
        entry_price: float,
        exit_price: float,
        return_pct: float,
        benchmark_ticker: str = "QQQ",
        benchmark_entry_price: float | None = None,
        benchmark_exit_price: float | None = None,
        benchmark_return_pct: float | None = None,
        excess_return_pct: float | None = None,
        max_favorable_excursion_pct: float | None = None,
        max_adverse_excursion_pct: float | None = None,
        net_return_pct: float | None = None,
        transaction_cost_bps: float = 0.0,
        rule_version: str | None = None,
        model_version: str | None = None,
        data_version: str | None = None,
        input_semantic_hash: str | None = None,
        config_hash: str | None = None,
        universe_hash: str | None = None,
        point_in_time_status: str | None = None,
        cost_model: Mapping[str, object] | None = None,
        now: datetime,
    ) -> bool:
        with self._lock:
            cursor = self._con.execute(
                "INSERT OR IGNORE INTO candidate_forward_evaluations"
                " (report_kind, as_of, rank, ticker, horizon_sessions,"
                " entry_price, exit_price, return_pct, benchmark_ticker,"
                " benchmark_entry_price, benchmark_exit_price, benchmark_return_pct,"
                " excess_return_pct, max_favorable_excursion_pct,"
                " max_adverse_excursion_pct, net_return_pct, transaction_cost_bps,"
                " rule_version, model_version, data_version, input_semantic_hash,"
                " config_hash, universe_hash, point_in_time_status, cost_model_json,"
                " evaluated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_kind,
                    as_of.isoformat(),
                    rank,
                    ticker,
                    horizon_sessions,
                    entry_price,
                    exit_price,
                    return_pct,
                    benchmark_ticker,
                    benchmark_entry_price,
                    benchmark_exit_price,
                    benchmark_return_pct,
                    excess_return_pct,
                    max_favorable_excursion_pct,
                    max_adverse_excursion_pct,
                    net_return_pct,
                    transaction_cost_bps,
                    rule_version,
                    model_version,
                    data_version,
                    input_semantic_hash,
                    config_hash,
                    universe_hash,
                    point_in_time_status,
                    _payload_json(cost_model) if cost_model is not None else None,
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            self._con.commit()
        return cursor.rowcount > 0

    def candidate_forward_evaluations(self) -> list[dict[str, object]]:
        with self._lock:
            rows = self._con.execute(
                "SELECT * FROM candidate_forward_evaluations"
                " ORDER BY as_of, report_kind, rank, horizon_sessions"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_position_discipline_state(
        self, state: DisciplineState, *, now: datetime
    ) -> None:
        with self._lock:
            self._con.execute(
                "INSERT INTO position_discipline_states"
                " (ticker, basis_version, notified_stage, peak_price, basis_quantity, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET"
                " basis_version = excluded.basis_version,"
                " notified_stage = excluded.notified_stage,"
                " peak_price = excluded.peak_price,"
                " basis_quantity = excluded.basis_quantity,"
                " updated_at = excluded.updated_at",
                (
                    state.ticker,
                    state.basis_version,
                    state.notified_stage,
                    str(state.peak_price),
                    str(state.basis_quantity) if state.basis_quantity is not None else None,
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            self._con.commit()

    def position_discipline_state(self, ticker: str) -> DisciplineState | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM position_discipline_states WHERE ticker = ?", (ticker,)
            ).fetchone()
        if row is None:
            return None
        value = row["basis_quantity"]
        return DisciplineState(
            ticker=str(row["ticker"]),
            basis_version=str(row["basis_version"]),
            notified_stage=int(row["notified_stage"]),
            peak_price=Decimal(str(row["peak_price"])),
            basis_quantity=Decimal(str(value)) if value is not None else None,
        )

    def begin_us_briefing_run(
        self,
        report_kind: str,
        as_of: date,
        data_version: str,
        *,
        now: datetime,
    ) -> USBriefingRun:
        identity = f"{report_kind}|{as_of.isoformat()}"
        run_id = sha256(identity.encode("utf-8")).hexdigest()[:20]
        with self._lock:
            cursor = self._con.execute(
                "INSERT OR IGNORE INTO us_briefing_runs"
                " (run_id, report_kind, as_of, data_version, status, created_at)"
                " VALUES (?, ?, ?, ?, 'STARTED', ?)",
                (
                    run_id,
                    report_kind,
                    as_of.isoformat(),
                    data_version,
                    now.astimezone(timezone.utc).isoformat(),
                ),
            )
            created = cursor.rowcount > 0
            row = self._con.execute(
                "SELECT * FROM us_briefing_runs"
                " WHERE report_kind = ? AND as_of = ?",
                (report_kind, as_of.isoformat()),
            ).fetchone()
            self._con.commit()
        assert row is not None
        return USBriefingRun(
            run_id=run_id,
            report_kind=str(row["report_kind"]),
            as_of=date.fromisoformat(str(row["as_of"])),
            data_version=str(row["data_version"]),
            status=str(row["status"]),
            created=created,
        )

    def complete_us_briefing_run(
        self,
        run_id: str,
        *,
        payload: Mapping[str, object],
        delivered: bool,
        shadowed: bool = False,
        now: datetime,
    ) -> None:
        status = "DELIVERED" if delivered else "SHADOWED" if shadowed else "FAILED"
        with self._lock:
            self._con.execute(
                "UPDATE us_briefing_runs SET status = ?, payload_json = ?, completed_at = ?"
                " WHERE run_id = ?",
                (
                    status,
                    _payload_json(payload),
                    now.astimezone(timezone.utc).isoformat(),
                    run_id,
                ),
            )
            self._con.commit()

    def us_briefing_run(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._con.execute(
                "SELECT * FROM us_briefing_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        item: dict[str, object] = dict(row)
        raw = item.pop("payload_json", None)
        item["payload"] = json.loads(str(raw)) if raw is not None else None
        return item

    def count_us_briefing_runs(self) -> int:
        with self._lock:
            row = self._con.execute(
                "SELECT count(*) AS n FROM us_briefing_runs"
            ).fetchone()
        return int(row["n"]) if row is not None else 0
