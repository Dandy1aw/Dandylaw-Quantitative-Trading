from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import math
import sqlite3

import pytest

from quant_signal.account import (
    AccountSnapshot,
    AccountState,
    BrokerOrder,
    BrokerPosition,
)
from quant_signal.execution import ExecutionPlan, PlanState
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card, CardKind, CardSection
from quant_signal.options_flow import (
    OptionContractVolume,
    OptionFlowSnapshot,
    scan_slot,
)
from quant_signal.strategies.base import Direction, Signal, dedup_key

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def sig(
    ticker: str = "SPY",
    ts: datetime = NOW,
    strategy_id: str = "momentum_rotation",
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=Direction.BUY,
        price=100.0,
        reason="r",
        strategy_id=strategy_id,
        ts=ts,
        suggested_weight=0.33,
    )


@pytest.fixture
def ledger(tmp_path: Path) -> SignalLedger:
    return SignalLedger(tmp_path / "signals.db")


def option_snapshot(at: datetime = NOW) -> OptionFlowSnapshot:
    row = OptionContractVolume(
        contract_symbol="AAPL260717C00300000",
        underlying="AAPL",
        side="call",
        expiration=date(2026, 7, 17),
        strike=Decimal("300"),
        volume=10_000,
        rank=1,
        venues=("cone", "opt"),
        captured_at=at,
    )
    return OptionFlowSnapshot(
        slot=scan_slot(at),
        captured_at=at,
        provider="cboe-four-venues",
        venue_coverage=1.0,
        rows=(row,),
    )


def option_card() -> Card:
    return Card(CardKind.REPORT, "美股期权热度 · Cboe四市场", "CALL Top10")


def test_insert_and_query_day(ledger: SignalLedger) -> None:
    ledger.insert(sig(), pushed=True)
    ledger.insert(sig("QQQ"), pushed=False)
    rows = ledger.signals_on(date(2026, 7, 6))
    assert len(rows) == 2
    assert {r["ticker"] for r in rows} == {"SPY", "QQQ"}
    assert [r["pushed"] for r in sorted(rows, key=lambda r: str(r["ticker"]))] == [0, 1]


def test_ledger_usable_from_another_thread(ledger: SignalLedger) -> None:
    """调度器在后台线程池跑 job，ledger 的 sqlite 连接必须能跨线程使用
    (check_same_thread=False)，否则 rotation/premarket/watch_deviation 全崩。"""
    import threading

    errors: list[Exception] = []

    def worker() -> None:
        try:
            ledger.insert(sig("QQQ"), pushed=True, now=NOW)
            ledger.set_holdings("momentum_rotation", ["QQQ"])
            assert ledger.get_holdings("momentum_rotation") == ["QQQ"]
            assert ledger.pushed_count_since(NOW - timedelta(hours=1)) >= 1
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert not errors, f"跨线程使用 ledger 失败: {errors}"


def test_last_push_by_key_only_pushed(ledger: SignalLedger) -> None:
    s = sig()
    ledger.insert(s, pushed=False, now=NOW)
    assert ledger.last_push_by_key(NOW - timedelta(hours=4)) == {}
    ledger.insert(s, pushed=True, now=NOW)
    got = ledger.last_push_by_key(NOW - timedelta(hours=4))
    assert got == {dedup_key(s): NOW}


def test_pushed_count_since(ledger: SignalLedger) -> None:
    ledger.insert(sig("A"), pushed=True, now=NOW)
    ledger.insert(sig("B"), pushed=True, now=NOW)
    ledger.insert(sig("C"), pushed=False, now=NOW)
    assert ledger.pushed_count_since(NOW - timedelta(hours=1)) == 2


def test_pushed_count_since_can_filter_strategy_channel(ledger: SignalLedger) -> None:
    ledger.insert(sig("A", strategy_id="momentum_rotation"), pushed=True, now=NOW)
    ledger.insert(sig("B", strategy_id="breakout_20d"), pushed=True, now=NOW)
    ledger.insert(sig("C", strategy_id="price_deviation"), pushed=True, now=NOW)

    assert ledger.pushed_count_since(
        NOW - timedelta(hours=1), {"momentum_rotation", "rsi_reversion"}
    ) == 1
    assert ledger.pushed_count_since(
        NOW - timedelta(hours=1), {"breakout_20d"}
    ) == 1


def test_latest_signal_price_returns_most_recent(ledger: SignalLedger) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW - timedelta(hours=2))
    ledger.insert(
        Signal(
            ticker="SPY", direction=Direction.BUY, price=105.0, reason="r2",
            strategy_id="momentum_rotation", ts=NOW,
        ),
        pushed=False,
        now=NOW,
    )
    price = ledger.latest_signal_price("momentum_rotation", "SPY", since=NOW - timedelta(hours=24))
    assert price == 105.0   # 取最近一次插入的信号价，即使未实际推送


def test_latest_signal_price_none_when_no_signal_today(ledger: SignalLedger) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW - timedelta(hours=30))
    price = ledger.latest_signal_price("momentum_rotation", "SPY", since=NOW - timedelta(hours=24))
    assert price is None


def test_holdings_roundtrip(ledger: SignalLedger) -> None:
    assert ledger.get_holdings("momentum_rotation") == []
    ledger.set_holdings("momentum_rotation", ["SPY", "QQQ"])
    ledger.set_holdings("momentum_rotation", ["QQQ", "GLD"])   # 覆盖式更新
    assert sorted(ledger.get_holdings("momentum_rotation")) == ["GLD", "QQQ"]


def test_pushed_signals_window_and_order(ledger: SignalLedger) -> None:
    ledger.insert(sig("OLD"), pushed=True, now=NOW - timedelta(days=120))   # 窗口外
    ledger.insert(sig("B"), pushed=True, now=NOW - timedelta(days=2))
    ledger.insert(sig("A"), pushed=True, now=NOW - timedelta(days=1))
    ledger.insert(sig("N"), pushed=False, now=NOW)                          # 未推送不计
    rows = ledger.pushed_signals(NOW - timedelta(days=90))
    assert [r["ticker"] for r in rows] == ["B", "A"]


def test_latest_price_for_direction(ledger: SignalLedger) -> None:
    """P3 卖出持有期盈亏：取该标的最近一次 BUY 信号价, 不限时间窗口。"""
    ledger.insert(sig("MU"), pushed=True, now=NOW - timedelta(days=40))     # BUY @100
    ledger.insert(
        Signal(ticker="MU", direction=Direction.SELL, price=120.0, reason="r",
               strategy_id="momentum_rotation", ts=NOW),
        pushed=True, now=NOW,
    )
    assert ledger.latest_price_for("momentum_rotation", "MU", "buy") == 100.0
    assert ledger.latest_price_for("momentum_rotation", "XX", "buy") is None


def test_signal_rejects_non_finite_price() -> None:
    with pytest.raises(ValueError, match="finite"):
        Signal(
            ticker="BAD",
            direction=Direction.BUY,
            price=math.nan,
            reason="bad feed",
            strategy_id="momentum_rotation",
            ts=NOW,
        )


def test_scan_candidates_replace_and_round_trip(ledger: SignalLedger) -> None:
    scan_date = date(2026, 7, 9)
    ledger.replace_scan_candidates(
        scan_date,
        [
            {
                "ticker": "MSFT",
                "rank": 2,
                "score": 0.31,
                "price": 510.0,
                "extra": {"memberships": ["nasdaq100", "sp500"]},
            },
            {
                "ticker": "AAPL",
                "rank": 1,
                "score": 0.42,
                "price": 220.0,
                "extra": {"entry_low": 215.0, "entry_high": 220.0},
            },
        ],
        as_of=date(2026, 7, 8),
    )

    rows = ledger.latest_scan_candidates(scan_date)

    assert [row["ticker"] for row in rows] == ["AAPL", "MSFT"]
    assert rows[0]["scan_date"] == "2026-07-09"
    assert rows[0]["as_of"] == "2026-07-08"
    assert rows[0]["extra"] == {"entry_low": 215.0, "entry_high": 220.0}
    assert json.loads(str(rows[1]["extra_json"])) == {
        "memberships": ["nasdaq100", "sp500"]
    }


def test_scan_candidates_same_day_replace_removes_old_rows(
    ledger: SignalLedger,
) -> None:
    scan_date = date(2026, 7, 9)
    ledger.replace_scan_candidates(
        scan_date,
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=scan_date,
    )
    ledger.replace_scan_candidates(
        scan_date,
        [{"ticker": "NEW", "rank": 1, "score": 0.2, "price": 20.0}],
        as_of=scan_date,
    )

    assert [row["ticker"] for row in ledger.latest_scan_candidates(scan_date)] == [
        "NEW"
    ]


def test_latest_scan_candidates_uses_most_recent_scan_date(
    ledger: SignalLedger,
) -> None:
    ledger.replace_scan_candidates(
        date(2026, 7, 8),
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=date(2026, 7, 7),
    )
    ledger.replace_scan_candidates(
        date(2026, 7, 9),
        [{"ticker": "NEW", "rank": 1, "score": 0.2, "price": 20.0}],
        as_of=date(2026, 7, 8),
    )

    assert [row["ticker"] for row in ledger.latest_scan_candidates()] == ["NEW"]
    assert [
        row["ticker"]
        for row in ledger.latest_scan_candidates(date(2026, 7, 8))
    ] == ["OLD"]


def test_empty_new_scan_does_not_fall_back_to_previous_candidates(
    ledger: SignalLedger,
) -> None:
    old_date = date(2026, 7, 8)
    new_date = date(2026, 7, 9)
    ledger.replace_scan_candidates(
        old_date,
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=date(2026, 7, 7),
    )
    ledger.replace_scan_candidates(new_date, [], as_of=date(2026, 7, 8))

    assert ledger.latest_scan_candidates() == []
    assert [row["ticker"] for row in ledger.latest_scan_candidates(old_date)] == [
        "OLD"
    ]


def test_scan_candidate_schema_is_additive_to_existing_signals(
    ledger: SignalLedger,
) -> None:
    ledger.insert(sig("SPY"), pushed=True, now=NOW)
    ledger.replace_scan_candidates(
        NOW.date(),
        [{"ticker": "AAPL", "rank": 1, "score": 0.5, "price": 220.0}],
        as_of=NOW.date(),
    )

    assert [row["ticker"] for row in ledger.signals_on(NOW.date())] == ["SPY"]


def test_scan_candidate_replace_rolls_back_on_insert_failure(
    ledger: SignalLedger,
) -> None:
    scan_date = NOW.date()
    ledger.replace_scan_candidates(
        scan_date,
        [{"ticker": "OLD", "rank": 1, "score": 0.1, "price": 10.0}],
        as_of=scan_date,
    )

    with pytest.raises(sqlite3.IntegrityError):
        ledger.replace_scan_candidates(
            scan_date,
            [
                {"ticker": "DUP", "rank": 1, "score": 0.2, "price": 20.0},
                {"ticker": "DUP", "rank": 2, "score": 0.3, "price": 21.0},
            ],
            as_of=scan_date,
        )

    assert [row["ticker"] for row in ledger.latest_scan_candidates(scan_date)] == [
        "OLD"
    ]


# ---------------------------------------------------------------- execution ledger


def make_execution_plan(
    ticker: str = "AAPL",
    state: PlanState = PlanState.CANDIDATE,
    plan_id: str = "plan-1",
    plan_version: int = 1,
) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        plan_version=plan_version,
        plan_date=date(2026, 7, 10),
        ticker=ticker,
        currency="USD",
        source_strategies=("index_scan",),
        memberships=("sp500",),
        score=0.8,
        entry_low=100.0,
        entry_high=102.0,
        limit_price=102.0,
        stop_loss=95.0,
        take_profit=115.0,
        target_weight=0.1,
        gap_qty=98,
        risk_qty=71,
        cash_qty=294,
        cap_qty=117,
        suggested_qty=71,
        suggested_notional=7242.0,
        valid_from=NOW,
        expires_at=NOW + timedelta(hours=6),
        quote_at=NOW,
        account_at=NOW,
        state=state,
        block_reason=None,
        rule_version="exec-v1",
    )


def account_state(retrieved_at: datetime = NOW) -> AccountState:
    snapshot = AccountSnapshot(
        account_id="paper-1",
        equity=Decimal("100000.25"),
        cash=Decimal("40000.10"),
        buying_power=Decimal("80000.20"),
        currency="USD",
        retrieved_at=retrieved_at,
    )
    position = BrokerPosition(
        symbol="AAPL",
        qty=Decimal("50"),
        side="long",
        avg_entry_price=Decimal("100.5"),
        market_value=Decimal("5100"),
    )
    order = BrokerOrder(
        order_id="order-1",
        symbol="MSFT",
        side="buy",
        status="new",
        qty=Decimal("5"),
        limit_price=Decimal("430.25"),
        submitted_at=retrieved_at,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )
    return AccountState(
        snapshot=snapshot,
        positions=(position,),
        open_orders=(order,),
        recent_orders=(),
    )


def test_existing_database_upgrades_without_losing_signals(tmp_path: Path) -> None:
    db_path = tmp_path / "signals.db"
    con = sqlite3.connect(str(db_path))
    con.execute(
        "CREATE TABLE signals (id INTEGER PRIMARY KEY, ts TEXT NOT NULL,"
        " ticker TEXT NOT NULL, direction TEXT NOT NULL, price REAL NOT NULL,"
        " strategy_id TEXT NOT NULL, reason TEXT, suggested_weight REAL,"
        " pushed INTEGER DEFAULT 0, pushed_at TEXT, dedup_key TEXT, extra_json TEXT)"
    )
    con.execute("CREATE TABLE holdings (strategy_id TEXT NOT NULL, ticker TEXT NOT NULL, PRIMARY KEY (strategy_id, ticker))")
    con.execute(
        "INSERT INTO signals (ts, ticker, direction, price, strategy_id, pushed, pushed_at)"
        " VALUES (?, 'SPY', 'BUY', 100.0, 'momentum_rotation', 1, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    con.execute("INSERT INTO holdings (strategy_id, ticker) VALUES ('m', 'SPY')")
    con.commit()
    con.close()

    ledger = SignalLedger(db_path)
    assert [row["ticker"] for row in ledger.signals_on(NOW.date())] == ["SPY"]
    assert ledger.get_holdings("m") == ["SPY"]
    assert ledger.schema_version() >= 2
    ledger.upsert_execution_plan(make_execution_plan())
    assert len(ledger.active_execution_plans()) == 1


def test_strategy_targets_are_separate_from_legacy_holdings(
    ledger: SignalLedger,
) -> None:
    ledger.set_holdings("momentum_rotation", ["SPY"])
    ledger.set_strategy_targets(
        "momentum_rotation", {"QQQ": 0.5, "IWM": 0.25}, as_of=NOW
    )

    assert ledger.get_holdings("momentum_rotation") == ["SPY"]
    assert ledger.get_strategy_targets("momentum_rotation") == {
        "QQQ": 0.5,
        "IWM": 0.25,
    }

    ledger.set_strategy_targets("momentum_rotation", {"QQQ": 1.0}, as_of=NOW)
    assert ledger.get_strategy_targets("momentum_rotation") == {"QQQ": 1.0}
    assert ledger.get_holdings("momentum_rotation") == ["SPY"]


def test_replace_account_state_keeps_only_latest_positions_and_orders(
    ledger: SignalLedger,
) -> None:
    ledger.replace_account_state(account_state())
    later = account_state(retrieved_at=NOW + timedelta(minutes=5))
    ledger.replace_account_state(later)

    snapshot = ledger.latest_account_snapshot()
    assert snapshot is not None
    assert snapshot["equity"] == "100000.25"
    assert snapshot["retrieved_at"] == (NOW + timedelta(minutes=5)).isoformat()

    positions = ledger.broker_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == "50"

    orders = ledger.broker_orders()
    assert len(orders) == 1
    assert orders[0]["order_id"] == "order-1"


def test_empty_ledger_has_no_account_snapshot(ledger: SignalLedger) -> None:
    assert ledger.latest_account_snapshot() is None
    assert ledger.broker_positions() == []
    assert ledger.broker_orders() == []


def test_execution_plan_upserts_by_id_and_version(ledger: SignalLedger) -> None:
    plan = make_execution_plan()
    ledger.upsert_execution_plan(plan)
    ledger.upsert_execution_plan(replace(plan, state=PlanState.ARMED))

    active = ledger.active_execution_plans()
    assert len(active) == 1
    assert active[0].state is PlanState.ARMED
    assert active[0] == replace(plan, state=PlanState.ARMED)

    ledger.upsert_execution_plan(replace(plan, plan_version=2))
    assert len(ledger.active_execution_plans()) == 1
    assert ledger.active_execution_plans()[0].plan_version == 2


def test_active_execution_plans_exclude_terminal_states(
    ledger: SignalLedger,
) -> None:
    ledger.upsert_execution_plan(make_execution_plan(plan_id="p1", ticker="AAPL"))
    ledger.upsert_execution_plan(
        make_execution_plan(plan_id="p2", ticker="MSFT", state=PlanState.EXPIRED)
    )
    ledger.upsert_execution_plan(
        make_execution_plan(plan_id="p3", ticker="NVDA", state=PlanState.BLOCKED)
    )

    active = ledger.active_execution_plans()
    assert [plan.ticker for plan in active] == ["AAPL"]


def test_plan_events_reject_duplicate_successful_delivery(
    ledger: SignalLedger,
) -> None:
    assert ledger.record_plan_event("p1", 1, "ACTIONABLE", now=NOW) is True
    assert ledger.event_was_delivered("p1", 1, "ACTIONABLE") is True
    assert ledger.record_plan_event("p1", 1, "ACTIONABLE", now=NOW) is False

    # 不同版本或不同事件类型仍可推送
    assert ledger.record_plan_event("p1", 2, "ACTIONABLE", now=NOW) is True
    assert ledger.record_plan_event("p1", 1, "EXPIRED", now=NOW) is True
    assert ledger.event_was_delivered("p1", 1, "INVALIDATED") is False


def test_plan_event_outbox_retries_until_delivery(ledger: SignalLedger) -> None:
    card = Card(
        kind=CardKind.SIGNAL,
        title="AAPL ACTIONABLE",
        body_md="buy 1",
        sections=(CardSection("buy 1"),),
    )
    assert ledger.queue_plan_event("p1", 1, "ACTIONABLE", card, now=NOW) is True
    assert ledger.queue_plan_event("p1", 1, "ACTIONABLE", card, now=NOW) is False
    due = ledger.due_plan_events(NOW)
    assert len(due) == 1 and due[0]["card"] == card
    event_key = str(due[0]["event_key"])
    assert ledger.event_was_delivered("p1", 1, "ACTIONABLE") is False

    ledger.mark_plan_event_failed(
        event_key,
        "temporary webhook error",
        now=NOW,
        retry_at=NOW + timedelta(minutes=1),
    )
    assert ledger.due_plan_events(NOW + timedelta(seconds=30)) == []
    retried = ledger.due_plan_events(NOW + timedelta(minutes=1))
    assert retried[0]["attempts"] == 1

    ledger.mark_plan_event_sent(event_key, now=NOW + timedelta(minutes=1))
    assert ledger.event_was_delivered("p1", 1, "ACTIONABLE") is True
    assert ledger.due_plan_events(NOW + timedelta(minutes=2)) == []


def test_option_scan_and_card_are_atomic_idempotent_and_reconstructable(
    ledger: SignalLedger,
) -> None:
    snap = option_snapshot()
    expires = NOW + timedelta(minutes=45)

    assert ledger.save_option_flow_scan(
        snap, "baseline", option_card(), now=NOW, expires_at=expires
    )
    assert not ledger.save_option_flow_scan(
        snap, "baseline", option_card(), now=NOW, expires_at=expires
    )

    assert ledger.latest_option_flow_snapshot(snap.session_date) == snap
    due = ledger.due_option_flow_alerts(NOW)
    assert len(due) == 1
    assert due[0]["card"] == option_card()
    assert ledger.option_flow_alert_count(snap.session_date) == 1


def test_quiet_option_scan_is_saved_without_outbox(ledger: SignalLedger) -> None:
    snap = option_snapshot()
    assert ledger.save_option_flow_scan(snap, "quiet", None, now=NOW)
    assert ledger.latest_option_flow_snapshot(snap.session_date) == snap
    assert ledger.due_option_flow_alerts(NOW) == []


def test_expired_option_alert_is_cancelled_not_delivered(ledger: SignalLedger) -> None:
    snap = option_snapshot()
    ledger.save_option_flow_scan(
        snap,
        "change",
        option_card(),
        now=NOW,
        expires_at=NOW,
    )

    assert ledger.due_option_flow_alerts(NOW + timedelta(seconds=1)) == []
    assert ledger.option_flow_alert_status(snap.slot, "change") == "EXPIRED"


def test_option_outbox_retries_then_marks_sent(ledger: SignalLedger) -> None:
    snap = option_snapshot()
    ledger.save_option_flow_scan(
        snap,
        "change",
        option_card(),
        now=NOW,
        expires_at=NOW + timedelta(minutes=45),
    )
    event = ledger.due_option_flow_alerts(NOW)[0]
    ledger.mark_option_flow_alert_failed(
        str(event["event_key"]),
        "temporary",
        now=NOW,
        retry_at=NOW + timedelta(minutes=5),
    )
    assert ledger.due_option_flow_alerts(NOW + timedelta(minutes=4)) == []
    retry = ledger.due_option_flow_alerts(NOW + timedelta(minutes=5))[0]
    ledger.mark_option_flow_alert_sent(
        str(retry["event_key"]), now=NOW + timedelta(minutes=5)
    )
    assert ledger.due_option_flow_alerts(NOW + timedelta(minutes=6)) == []
    assert ledger.option_flow_alert_status(snap.slot, "change") == "SENT"
    assert ledger.last_option_flow_alert_at(snap.session_date) == NOW


def test_account_change_does_not_cancel_option_outbox(ledger: SignalLedger) -> None:
    snap = option_snapshot()
    ledger.save_option_flow_scan(
        snap,
        "change",
        option_card(),
        now=NOW,
        expires_at=NOW + timedelta(minutes=45),
    )
    ledger.invalidate_active_plans("ACCOUNT_CHANGED", now=NOW)
    assert len(ledger.due_option_flow_alerts(NOW)) == 1
