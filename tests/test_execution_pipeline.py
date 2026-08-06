from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_test_settings

from quant_signal.account import (
    AccountFetchError,
    AccountSnapshot,
    AccountState,
    BrokerOrder,
    BrokerPosition,
    ObservedPosition,
)
from quant_signal.config import (
    AIBriefingSettings,
    ExecutionPlanSettings,
    IndexUniverseSettings,
    NotifySettings,
)
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.execution import PlanCandidate, PlanState, build_plan
from quant_signal.ledger import SignalLedger
from quant_signal.portfolio_import import (
    ExtractedAccount,
    PortfolioExtraction,
    validate_extraction,
)

BRIEF_NOW = datetime(2026, 7, 10, 12, 15, tzinfo=timezone.utc)  # 08:15 ET
WATCH_NOW = datetime(2026, 7, 10, 14, 30, tzinfo=timezone.utc)  # 10:30 ET
MARKET_AS_OF = date(2026, 7, 9)


class FakeNotifier:
    def __init__(self, success: bool = True) -> None:
        self.cards: list[object] = []
        self.success = success

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return self.success


class FakeAccountProvider:
    def __init__(self, state: AccountState | None = None, fail: bool = False) -> None:
        self._state = state
        self._fail = fail

    def snapshot(self, now: datetime) -> AccountState:
        if self._fail or self._state is None:
            raise AccountFetchError("alpaca paper account GET /v2/account failed")
        return replace(
            self._state,
            snapshot=replace(self._state.snapshot, retrieved_at=now),
        )


def paper_account(
    positions: tuple[BrokerPosition, ...] = (),
    open_orders: tuple[BrokerOrder, ...] = (),
    recent_orders: tuple[BrokerOrder, ...] = (),
) -> AccountState:
    return AccountState(
        snapshot=AccountSnapshot(
            account_id="paper-1",
            equity=Decimal("100000"),
            cash=Decimal("50000"),
            buying_power=Decimal("100000"),
            currency="USD",
            retrieved_at=BRIEF_NOW,
        ),
        positions=positions,
        open_orders=open_orders,
        recent_orders=recent_orders,
    )


def screenshot_account() -> AccountState:
    observed = tuple(
        ObservedPosition(
            symbol=symbol,
            qty=None,
            avg_entry_price=None,
            current_price=None,
            market_value=None,
            estimated_market_value=Decimal(value),
            pnl=None,
            pnl_pct=None,
            weight_pct=None,
            precision="ESTIMATED",
        )
        for symbol, value in {
            "DRAM": "887.34",
            "MU": "991.06",
            "RAM": "363.93",
            "SMH": "1226.68",
            "SNXX": "774.62",
        }.items()
    )
    return AccountState(
        snapshot=AccountSnapshot(
            account_id="screenshot:abc",
            equity=Decimal("5995.52"),
            cash=Decimal("1751.13"),
            buying_power=Decimal("3474.15"),
            currency="USD",
            retrieved_at=BRIEF_NOW,
            source="screenshot",
            market_value=Decimal("4244.15"),
            capital_limit=Decimal("6000"),
            max_financing_ratio=Decimal("0.20"),
        ),
        positions=(),
        open_orders=(),
        recent_orders=(),
        observed_positions=observed,
        positions_partial=True,
        reported_position_count=6,
    )


def scan_extra(**overrides: object) -> dict[str, object]:
    extra: dict[str, object] = {
        "memberships": ["nasdaq100", "sp500"],
        "market_as_of": MARKET_AS_OF.isoformat(),
        "momentum_60d": 0.25,
        "high20_proximity": 0.99,
        "volume_ratio": 1.4,
        "entry_low": 100.0,
        "entry_high": 102.0,
        "target_buy": 100.0,
        "stop_loss": 95.0,
        "take_profit": 115.0,
    }
    extra.update(overrides)
    return extra


def make_engine(
    tmp_path: Path,
    account_provider: object,
    source: object | None = None,
    **settings_overrides: object,
) -> tuple[Engine, FakeNotifier, SignalLedger]:
    overrides: dict[str, object] = {
        "index_universe": IndexUniverseSettings(enabled=False),
        "execution_plan": ExecutionPlanSettings(enabled=True),
    }
    overrides.update(settings_overrides)
    settings = make_test_settings(**overrides)
    store = BarStore(tmp_path / "bars.duckdb")
    ledger = SignalLedger(tmp_path / "signals.db")
    notifier = FakeNotifier()
    engine = Engine(
        settings,
        store,
        source if source is not None else _EmptySource(),
        ledger,
        notifier,
        account_provider=account_provider,  # type: ignore[arg-type]
    )
    return engine, notifier, ledger


class _EmptySource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        return pd.DataFrame()

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        return pd.DataFrame()


def _intraday_frame(ticker: str, closes: list[float], end_utc: datetime) -> pd.DataFrame:
    ts = pd.date_range(
        end=end_utc, periods=len(closes), freq="5min", tz="UTC"
    )
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": 10_000,
        },
        index=pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"]),
    )


class FakeIntradaySource(_EmptySource):
    def __init__(self, bars: pd.DataFrame) -> None:
        self._bars = bars

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        mask = self._bars.index.get_level_values("ticker").isin(tickers)
        return self._bars[mask]


class FailingIntradaySource(_EmptySource):
    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise TimeoutError("market data unavailable")


def seed_candidates(ledger: SignalLedger, extra: dict[str, object]) -> None:
    ledger.replace_scan_candidates(
        BRIEF_NOW.date(),
        [
            {
                "ticker": "AAPL",
                "rank": 1,
                "score": 0.9,
                "price": 103.0,
                "extra": extra,
            }
        ],
        as_of=MARKET_AS_OF,
    )


def seed_screenshot_account(ledger: SignalLedger, *, observed_at: datetime) -> None:
    extraction = PortfolioExtraction(
        account=ExtractedAccount(
            equity=Decimal("5995.52"),
            market_value=Decimal("0"),
            cash=Decimal("5995.52"),
            buying_power=Decimal("5995.52"),
            reported_position_count=0,
            observed_at=observed_at,
        ),
        positions=(),
    )
    record = validate_extraction(
        extraction,
        image_sha256="a" * 64,
        uploaded_at=observed_at,
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )
    assert ledger.save_portfolio_import(record) is True


def seed_active_plan(
    ledger: SignalLedger, state: PlanState = PlanState.IN_ENTRY_ZONE
) -> None:
    candidate = PlanCandidate(
        ticker="AAPL",
        plan_date=BRIEF_NOW.date(),
        entry_low=100.0,
        entry_high=102.0,
        stop_loss=95.0,
        take_profit=115.0,
        target_weight=None,
        score=0.9,
        source_strategies=("index_scan",),
        memberships=("sp500",),
        quote_at=BRIEF_NOW,
    )
    plan = build_plan(
        candidate,
        paper_account().snapshot,
        (),
        (),
        ExecutionPlanSettings(enabled=True),
        BRIEF_NOW,
    )
    ledger.upsert_execution_plan(replace(plan, state=state))


# ---------------------------------------------------------------- daily brief


def test_daily_brief_sends_paper_card_and_persists_plans(tmp_path: Path) -> None:
    account = paper_account(
        positions=(
            BrokerPosition(
                symbol="MSFT",
                qty=Decimal("10"),
                side="long",
                avg_entry_price=Decimal("400"),
                market_value=Decimal("4300"),
            ),
        ),
        open_orders=(
            BrokerOrder(
                order_id="o-1",
                symbol="NVDA",
                side="buy",
                status="new",
                qty=Decimal("5"),
                limit_price=Decimal("120"),
                submitted_at=BRIEF_NOW,
                filled_qty=Decimal("0"),
                filled_avg_price=None,
            ),
        ),
        recent_orders=(
            BrokerOrder(
                order_id="o-0",
                symbol="MSFT",
                side="buy",
                status="filled",
                qty=Decimal("10"),
                limit_price=None,
                submitted_at=BRIEF_NOW - timedelta(days=1),
                filled_qty=Decimal("10"),
                filled_avg_price=Decimal("400"),
            ),
        ),
    )
    engine, notifier, ledger = make_engine(tmp_path, FakeAccountProvider(account))
    seed_screenshot_account(
        ledger, observed_at=BRIEF_NOW - timedelta(hours=100)
    )
    seed_candidates(ledger, scan_extra())

    assert engine.run_execution_brief(BRIEF_NOW) is True

    assert len(notifier.cards) == 1
    card = notifier.cards[0]
    body = card.body_md  # type: ignore[attr-defined]
    assert "PAPER" in card.title or "PAPER" in body  # type: ignore[attr-defined]
    # 账户区
    assert "$100,000.00" in body and "$50,000.00" in body
    assert "MSFT" in body  # 持仓
    assert "NVDA" in body  # 未成交订单
    assert "o-0" in body or "400" in body  # 最近成交
    assert "账户快照已" not in body
    assert "从未导入截图账户" not in body
    # 计划区: 限价/股数/金额/止损/止盈/有效期
    assert "102" in body and "95" in body and "115" in body
    assert "71" in body  # risk_qty=floor(500/7)
    assert "$7,242.00" in body  # notional
    assert "15:45" in body  # 有效期
    # 账户时间
    assert BRIEF_NOW.astimezone().strftime("%H:%M") in body or "08:15" in body or "账户时间" in body

    active = ledger.active_execution_plans()
    assert len(active) == 1
    assert active[0].ticker == "AAPL"
    assert active[0].suggested_qty == 71
    # 账户状态已落库
    assert ledger.latest_account_snapshot() is not None


def test_execution_brief_coalesces_recent_scheduled_run(tmp_path: Path) -> None:
    engine, notifier, ledger = make_engine(
        tmp_path, FakeAccountProvider(paper_account())
    )
    seed_candidates(ledger, scan_extra())

    assert engine.run_execution_brief(BRIEF_NOW) is True
    assert (
        engine.run_execution_brief(
            BRIEF_NOW + timedelta(minutes=5),
            skip_if_run_within=timedelta(minutes=5),
        )
        is True
    )

    assert len(notifier.cards) == 1


def test_execution_brief_reports_delivery_failure(tmp_path: Path) -> None:
    engine, notifier, ledger = make_engine(
        tmp_path, FakeAccountProvider(paper_account())
    )
    seed_candidates(ledger, scan_extra())
    notifier.success = False

    assert engine.run_execution_brief(BRIEF_NOW) is False
    assert len(notifier.cards) == 1


def test_daily_brief_does_not_warn_when_screenshot_account_is_fresh(
    tmp_path: Path,
) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        screenshot_max_age_hours=24,
    )
    engine, notifier, ledger = make_engine(
        tmp_path, None, execution_plan=settings
    )
    seed_screenshot_account(
        ledger, observed_at=BRIEF_NOW - timedelta(hours=24)
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "账户快照已" not in body
    assert "从未导入截图账户" not in body


def test_daily_brief_warns_when_screenshot_account_is_stale(tmp_path: Path) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        screenshot_max_age_hours=24,
    )
    engine, notifier, ledger = make_engine(
        tmp_path, None, execution_plan=settings
    )
    seed_screenshot_account(
        ledger,
        observed_at=BRIEF_NOW - timedelta(hours=25, minutes=59),
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert (
        "⚠️ 账户快照已 25 小时未更新（阈值 24h），"
        "股数建议基于旧账本，请发送新截图"
    ) in body


def test_daily_brief_warns_when_screenshot_account_was_never_imported(
    tmp_path: Path,
) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        screenshot_max_age_hours=24,
    )
    engine, notifier, ledger = make_engine(
        tmp_path, None, execution_plan=settings
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "⚠️ 从未导入截图账户，请发送账户截图" in body


def test_daily_brief_warns_without_crashing_when_screenshot_time_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        screenshot_max_age_hours=24,
    )
    engine, notifier, ledger = make_engine(
        tmp_path, None, execution_plan=settings
    )

    def invalid_observed_account() -> dict[str, object]:
        return {"observed_at": "not-a-timestamp"}

    monkeypatch.setattr(
        ledger, "latest_observed_account", invalid_observed_account
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "⚠️ 账户快照时间无效，请发送新截图" in body


def test_daily_brief_without_account_shows_no_quantity(tmp_path: Path) -> None:
    engine, notifier, ledger = make_engine(
        tmp_path, FakeAccountProvider(None, fail=True)
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "NO_ACCOUNT" in body or "账户数据不足" in body
    # 无账户时只汇总阻断原因，不伪造数量
    assert "NO_ACCOUNT" in body
    # 计划落库为 BLOCKED, 不产生可执行数量
    assert ledger.active_execution_plans() == []


def test_daily_brief_blocks_stale_market_data(tmp_path: Path) -> None:
    engine, notifier, ledger = make_engine(
        tmp_path, FakeAccountProvider(paper_account())
    )
    seed_candidates(
        ledger, scan_extra(market_as_of=(MARKET_AS_OF - timedelta(days=3)).isoformat())
    )

    engine.run_execution_brief(BRIEF_NOW)

    assert ledger.active_execution_plans() == []
    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "DATA_STALE" in body


def test_daily_brief_conflicting_sell_signal_blocks_plan(tmp_path: Path) -> None:
    from quant_signal.strategies.base import Direction, Signal

    engine, notifier, ledger = make_engine(
        tmp_path, FakeAccountProvider(paper_account())
    )
    seed_candidates(ledger, scan_extra())
    ledger.insert(
        Signal(
            ticker="AAPL",
            direction=Direction.SELL,
            price=103.0,
            reason="轮动调出",
            strategy_id="momentum_rotation",
            ts=BRIEF_NOW,
        ),
        pushed=True,
        now=BRIEF_NOW,
    )

    engine.run_execution_brief(BRIEF_NOW)

    assert ledger.active_execution_plans() == []
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "BLOCKED_CONFLICT" in body


def test_daily_brief_disabled_flag_is_noop(tmp_path: Path) -> None:
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        execution_plan=ExecutionPlanSettings(enabled=False),
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    assert notifier.cards == []
    assert ledger.active_execution_plans() == []


def test_daily_brief_sizes_unheld_aapl_from_screenshot_account(tmp_path: Path) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        risk_clusters={
            "semiconductor_memory": ["DRAM", "MU", "RAM", "SMH", "SNXX", "AMD"]
        },
    )
    engine, _notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(screenshot_account()),
        execution_plan=settings,
    )
    seed_candidates(
        ledger,
        scan_extra(
            entry_low=307.26,
            entry_high=316.22,
            stop_loss=290.49,
            take_profit=341.68,
        ),
    )

    engine.run_execution_brief(BRIEF_NOW)

    active = ledger.active_execution_plans()
    assert len(active) == 1
    assert active[0].ticker == "AAPL"
    assert active[0].suggested_qty == 1
    assert active[0].suggested_notional == 316.22


def test_daily_brief_blocks_existing_semiconductor_cluster_addition(tmp_path: Path) -> None:
    settings = ExecutionPlanSettings(
        enabled=True,
        account_provider="screenshot",
        cash_reserve=0,
        risk_clusters={
            "semiconductor_memory": ["DRAM", "MU", "RAM", "SMH", "SNXX", "AMD"]
        },
    )
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(screenshot_account()),
        execution_plan=settings,
    )
    ledger.replace_scan_candidates(
        BRIEF_NOW.date(),
        [{"ticker": "AMD", "rank": 1, "score": 0.9, "price": 103.0, "extra": scan_extra()}],
        as_of=MARKET_AS_OF,
    )

    engine.run_execution_brief(BRIEF_NOW)

    assert ledger.active_execution_plans() == []
    assert "CLUSTER_WEIGHT_EXCEEDED" in notifier.cards[0].body_md  # type: ignore[attr-defined]


def test_daily_brief_propagates_currency_and_blocks_non_usd_quantity(tmp_path: Path) -> None:
    settings = ExecutionPlanSettings(enabled=True, cash_reserve=0)
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(screenshot_account()),
        execution_plan=settings,
        universe=["000660.KS"],
        international_tickers={"000660.KS": "KRW"},
    )
    ledger.replace_scan_candidates(
        BRIEF_NOW.date(),
        [{"ticker": "000660.KS", "rank": 1, "score": 0.9, "price": 103.0, "extra": scan_extra()}],
        as_of=MARKET_AS_OF,
    )

    engine.run_execution_brief(BRIEF_NOW)

    assert ledger.active_execution_plans() == []
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "UNSUPPORTED_MARKET" in body
    assert "KRW" in body


def test_daily_brief_embeds_one_short_ai_summary_in_same_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_ai(settings, context):  # type: ignore[no-untyped-def]
        seen["mode"] = context.output_mode
        seen["label"] = context.execution_plans[0]["account_label"]
        seen["holdings"] = context.holdings
        seen["ranking"] = context.ranking
        return "观" * 500

    monkeypatch.setattr(
        "quant_signal.pipelines.execution_plan.run_ai_briefing", fake_ai
    )
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(screenshot_account()),
        execution_plan=ExecutionPlanSettings(
            enabled=True, account_provider="screenshot", cash_reserve=0
        ),
        ai_briefing=AIBriefingSettings(enabled=True),
        notify=NotifySettings(action_card_only=True),
    )
    seed_candidates(ledger, scan_extra())

    engine.run_execution_brief(BRIEF_NOW)

    assert len(notifier.cards) == 1
    assert seen["mode"] == "action_card"
    assert seen["label"] == "SCREENSHOT"
    assert set(seen["holdings"]) == {"DRAM", "MU", "RAM", "SMH", "SNXX"}
    assert seen["ranking"][0]["ticker"] == "AAPL"
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "AI简评" in body
    assert "观" * 301 not in body


# ---------------------------------------------------------------- intraday watch


def test_watch_normal_move_emits_no_card(tmp_path: Path) -> None:
    bars = _intraday_frame("AAPL", [104.0, 104.5, 105.0], WATCH_NOW)
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(bars),
    )
    seed_active_plan(ledger, state=PlanState.ARMED)

    engine.run_execution_watch(WATCH_NOW)

    assert notifier.cards == []


def test_watch_confirmed_entry_emits_actionable_once(tmp_path: Path) -> None:
    bars = _intraday_frame("AAPL", [103.0, 101.5, 101.0], WATCH_NOW)
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(bars),
    )
    seed_active_plan(ledger, state=PlanState.IN_ENTRY_ZONE)

    engine.run_execution_watch(WATCH_NOW)
    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md  # type: ignore[attr-defined]
    assert "ACTIONABLE" in body or "ACTIONABLE" in notifier.cards[0].title  # type: ignore[attr-defined]
    assert "PAPER" not in notifier.cards[0].title and "PAPER" not in body  # type: ignore[attr-defined]

    active = ledger.active_execution_plans()
    assert len(active) == 1
    assert active[0].state is PlanState.ACTIONABLE

    # 同样的观测再跑一次: 状态不变, 不再推送
    engine.run_execution_watch(WATCH_NOW + timedelta(minutes=5))
    assert len(notifier.cards) == 1


def test_watch_retries_failed_notification_from_outbox(tmp_path: Path) -> None:
    bars = _intraday_frame("AAPL", [103.0, 101.5, 101.0], WATCH_NOW)
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(bars),
    )
    notifier.success = False
    seed_active_plan(ledger, state=PlanState.IN_ENTRY_ZONE)

    engine.run_execution_watch(WATCH_NOW)

    assert len(notifier.cards) == 1
    assert ledger.event_was_delivered(
        ledger.active_execution_plans()[0].plan_id, 1, "ACTIONABLE"
    ) is False

    notifier.success = True
    engine.run_execution_watch(WATCH_NOW + timedelta(minutes=5))

    assert len(notifier.cards) == 2
    assert ledger.event_was_delivered(
        ledger.active_execution_plans()[0].plan_id, 1, "ACTIONABLE"
    ) is True


def test_watch_queues_event_before_plan_state_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = _intraday_frame("AAPL", [103.0, 101.5, 101.0], WATCH_NOW)
    engine, _notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(bars),
    )
    seed_active_plan(ledger, state=PlanState.IN_ENTRY_ZONE)

    def fail_update(plan: object) -> None:
        raise RuntimeError("simulated state write failure")

    monkeypatch.setattr(ledger, "upsert_execution_plan", fail_update)
    with pytest.raises(RuntimeError, match="state write failure"):
        engine.run_execution_watch(WATCH_NOW)

    assert len(ledger.due_plan_events(WATCH_NOW)) == 1


def test_watch_stop_breach_invalidates_and_notifies(tmp_path: Path) -> None:
    bars = _intraday_frame("AAPL", [96.0, 94.5, 94.0], WATCH_NOW)
    engine, notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(bars),
    )
    seed_active_plan(ledger, state=PlanState.IN_ENTRY_ZONE)

    engine.run_execution_watch(WATCH_NOW)

    assert len(notifier.cards) == 1
    assert ledger.active_execution_plans() == []


def test_watch_without_active_plans_is_noop(tmp_path: Path) -> None:
    engine, notifier, _ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FakeIntradaySource(_intraday_frame("AAPL", [100.0], WATCH_NOW)),
    )

    engine.run_execution_watch(WATCH_NOW)

    assert notifier.cards == []


def test_watch_market_data_failure_propagates_to_job_health(tmp_path: Path) -> None:
    engine, _notifier, ledger = make_engine(
        tmp_path,
        FakeAccountProvider(paper_account()),
        source=FailingIntradaySource(),
    )
    seed_active_plan(ledger, state=PlanState.ARMED)

    with pytest.raises(TimeoutError, match="market data unavailable"):
        engine.run_execution_watch(WATCH_NOW)
