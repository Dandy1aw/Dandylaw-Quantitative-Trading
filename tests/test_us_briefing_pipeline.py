from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from conftest import make_test_settings
from quant_signal.account import (
    AccountSnapshot,
    AccountState,
    BrokerPosition,
    ObservedPosition,
)
from quant_signal.config import (
    CandidateLaneSettings,
    IndexUniverseSettings,
    MarketRegimeSettings,
    USBriefingSettings,
)
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.index_universe import merge_members
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.pipelines.us_briefing import (
    BriefingMode,
    _asia_context,
    _load_daily_bars,
    _account_version,
    _observed_input,
    last_completed_us_session,
    run,
)


NOW_CLOSE = datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)
NOW_ASIA = datetime(2026, 7, 15, 7, 30, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.cards: list[Card] = []
        self.results = list(results or [])

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return self.results.pop(0) if self.results else True


class FakeSource:
    def __init__(self, bars: pd.DataFrame) -> None:
        self.bars = bars

    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        mask = self.bars.index.get_level_values("ticker").isin(tickers)
        return self.bars[mask]

    def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class FakeUniverseProvider:
    def __init__(self, members: set[str]) -> None:
        self.snapshot = merge_members(
            {"nasdaq100": members}, NOW_CLOSE, as_of=date(2026, 7, 14)
        )

    def load(self, now: datetime):  # type: ignore[no-untyped-def]
        return self.snapshot


class FixedAccountProvider:
    def __init__(self, state: AccountState) -> None:
        self.state = state

    def snapshot(self, now: datetime) -> AccountState:
        return self.state


class FailingAccountProvider:
    def snapshot(self, now: datetime) -> AccountState:
        raise RuntimeError("account unavailable")


class FailingAsiaSource:
    def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
        raise TimeoutError("asia unavailable")


class FixedEarningsSource:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def next_dates(self, tickers: list[str]) -> dict[str, date]:
        self.requested = list(tickers)
        return {tickers[0]: date(2026, 7, 15)} if tickers else {}

    def recent_surprise(self, tickers: list[str], now: date) -> dict[str, float]:
        return {}


def _bars() -> tuple[pd.DataFrame, set[str]]:
    members = {f"N{i:02d}" for i in range(10)}
    ts = pd.bdate_range(end="2026-07-14", periods=220, tz="UTC")
    frames: list[pd.DataFrame] = []
    paths: dict[str, np.ndarray] = {
        ticker: 100 * np.exp(np.linspace(0, 0.18 + index * 0.01, len(ts)))
        for index, ticker in enumerate(sorted(members))
    }
    paths["QQQ"] = np.linspace(400, 520, len(ts))
    paths["SKHY"] = np.linspace(40, 43, 4)
    for ticker, close in paths.items():
        index = ts[-len(close) :]
        frames.append(
            pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": np.full(len(close), 2_000_000),
                },
                index=pd.MultiIndex.from_product(
                    [[ticker], index], names=["ticker", "ts"]
                ),
            )
        )
    return pd.concat(frames).sort_index(), members


def _account(*, partial: bool = False) -> AccountState:
    snapshot = AccountSnapshot(
        account_id="acct",
        equity=Decimal("6000"),
        cash=Decimal("1750"),
        buying_power=Decimal("2950"),
        currency="USD",
        retrieved_at=NOW_CLOSE,
        source="screenshot" if partial else "alpaca_paper",
        market_value=Decimal("700"),
        capital_limit=Decimal("6000"),
        max_financing_ratio=Decimal("0.20"),
    )
    if partial:
        return AccountState(
            snapshot=snapshot,
            positions=(),
            open_orders=(),
            recent_orders=(),
            observed_positions=(
                ObservedPosition(
                    symbol="RAM",
                    qty=None,
                    avg_entry_price=Decimal("50"),
                    current_price=Decimal("70"),
                    market_value=Decimal("700"),
                    estimated_market_value=None,
                    pnl=Decimal("200"),
                    pnl_pct=Decimal("40.00"),
                    weight_pct=Decimal("11.67"),
                    precision="PARTIAL",
                ),
            ),
            positions_partial=True,
            reported_position_count=5,
        )
    return AccountState(
        snapshot=snapshot,
        positions=(
            BrokerPosition(
                symbol="RAM",
                qty=Decimal("10"),
                side="long",
                avg_entry_price=Decimal("50"),
                market_value=Decimal("700"),
            ),
        ),
        open_orders=(),
        recent_orders=(),
    )


def _engine(
    tmp_path: Path,
    *,
    delivery_mode: str = "live",
    account_provider: object | None = None,
    notifier: FakeNotifier | None = None,
) -> tuple[Engine, FakeNotifier]:
    bars, members = _bars()
    settings = make_test_settings(
        universe=["RAM"],
        leverage_factor={"RAM": 2.0},
        index_universe=IndexUniverseSettings(
            enabled=False,
            indices=["nasdaq100"],
            min_coverage=0.8,
        ),
        us_briefing=USBriefingSettings(
            enabled=True,
            delivery_mode=delivery_mode,
            min_coverage=0.8,
            market_regime=MarketRegimeSettings(
                min_history_days=201,
                high_volatility_annualized=0.5,
            ),
            candidate_lanes=CandidateLaneSettings(
                min_dollar_volume=1_000_000,
                top_n_per_lane=3,
            ),
        ),
    )
    notifier = notifier or FakeNotifier()
    engine = Engine(
        settings,
        BarStore(tmp_path / "bars.duckdb"),
        FakeSource(bars),
        SignalLedger(tmp_path / "signals.db"),
        notifier,
        index_universe_provider=FakeUniverseProvider(members),  # type: ignore[arg-type]
        account_provider=account_provider,  # type: ignore[arg-type]
    )
    engine._intl_source = FailingAsiaSource()  # type: ignore[assignment]
    return engine, notifier


def test_last_completed_session_uses_same_day_after_close_and_previous_before() -> None:
    assert last_completed_us_session(NOW_CLOSE) == date(2026, 7, 14)
    assert last_completed_us_session(NOW_ASIA) == date(2026, 7, 14)


def test_live_pipeline_discovers_nasdaq_candidates_and_applies_holding_discipline(
    tmp_path: Path,
) -> None:
    engine, notifier = _engine(
        tmp_path, account_provider=FixedAccountProvider(_account())
    )

    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)

    assert len(notifier.cards) == 1
    body = notifier.cards[0].body_md
    assert "纳指100候选" in body
    assert "RAM" in body and "卖出 7 股（累计 75%）" in body
    assert "000660.KS" not in body
    stored = engine.ledger.candidate_lane_snapshot("US_CLOSE", date(2026, 7, 14))
    assert stored
    assert all(row["ticker"] != "RAM" for row in stored)
    assert any(isinstance(row.get("suggested_qty"), int) for row in stored)
    assert "建议" in body and "股" in body
    active_plans = engine.ledger.active_execution_plans()
    assert active_plans
    assert all(plan.source_strategies[0].startswith(("TREND_", "RANGE_")) for plan in active_plans)


def test_repeated_run_does_not_resend_card(tmp_path: Path) -> None:
    engine, notifier = _engine(
        tmp_path, account_provider=FixedAccountProvider(_account())
    )
    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    assert len(notifier.cards) == 1


def test_shadow_mode_persists_without_delivery(tmp_path: Path) -> None:
    engine, notifier = _engine(
        tmp_path,
        delivery_mode="shadow",
        account_provider=FixedAccountProvider(_account()),
    )
    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    assert notifier.cards == []
    assert engine.ledger.count_us_briefing_runs() == 1
    assert engine.ledger.active_execution_plans() == []
    assert engine.ledger.position_discipline_state("RAM") is None


def test_partial_screenshot_outputs_percentage_not_invented_qty(tmp_path: Path) -> None:
    engine, notifier = _engine(
        tmp_path, account_provider=FixedAccountProvider(_account(partial=True))
    )
    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    body = notifier.cards[0].body_md
    assert "卖出 75%（股数不可用）" in body
    assert "卖出 7 股" not in body


def test_account_failure_keeps_candidate_report(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path, account_provider=FailingAccountProvider())
    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    assert len(notifier.cards) == 1
    assert "纳指100候选" in notifier.cards[0].body_md
    assert "账户数据不可用" in notifier.cards[0].body_md


def test_asia_failure_is_explicit_and_does_not_fail_report(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path)
    run(engine, NOW_ASIA, BriefingMode.ASIA_CONFIRM)
    assert len(notifier.cards) == 1
    assert "亚洲确认数据不可用" in notifier.cards[0].body_md
    assert "000660.KS" not in notifier.cards[0].body_md


def test_asia_context_includes_us_futures_and_vix_without_trade_candidates(
    tmp_path: Path,
) -> None:
    engine, _ = _engine(tmp_path)
    symbols = ["^KS11", "^KQ11", "NQ=F", "ES=F", "^VIX"]
    ts = pd.to_datetime(["2026-07-14", "2026-07-15"], utc=True)
    frames = []
    for index, symbol in enumerate(symbols):
        close = np.array([100.0, 101.0 + index])
        frames.append(
            pd.DataFrame(
                {
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": [1_000_000, 1_000_000],
                },
                index=pd.MultiIndex.from_product(
                    [[symbol], ts], names=["ticker", "ts"]
                ),
            )
        )
    engine._intl_source = FakeSource(pd.concat(frames))  # type: ignore[assignment]

    context, text = _asia_context(engine, NOW_ASIA)

    assert {"NQ=F", "ES=F", "^VIX"} <= context.keys()
    assert "纳指期货" in text and "标普期货" in text and "VIX" in text


def test_pipeline_removes_candidates_inside_earnings_blackout(tmp_path: Path) -> None:
    engine, notifier = _engine(tmp_path)
    earnings = FixedEarningsSource()
    engine.earnings_source = earnings

    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)

    stored = engine.ledger.candidate_lane_snapshot("US_CLOSE", date(2026, 7, 14))
    assert earnings.requested
    assert len(stored) == 2
    assert earnings.requested[0] not in {str(row["ticker"]) for row in stored}
    assert "财报窗口 1" in notifier.cards[0].body_md


def test_failed_delivery_retries_without_consuming_profit_stage(tmp_path: Path) -> None:
    notifier = FakeNotifier([False, True])
    engine, _ = _engine(
        tmp_path,
        account_provider=FixedAccountProvider(_account()),
        notifier=notifier,
    )

    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    first_run = engine.ledger.us_briefing_run(
        next(iter(_run_ids(engine.ledger)))
    )
    assert first_run is not None and first_run["status"] == "FAILED"
    assert engine.ledger.position_discipline_state("RAM") is None
    assert engine.ledger.active_execution_plans() == []

    run(engine, NOW_CLOSE, BriefingMode.US_CLOSE)
    assert len(notifier.cards) == 2
    assert all("卖出 7 股（累计 75%）" in card.body_md for card in notifier.cards)
    state = engine.ledger.position_discipline_state("RAM")
    assert state is not None and state.notified_stage == 3
    assert engine.ledger.active_execution_plans()


def test_screenshot_pnl_pct_is_always_converted_from_percentage_points() -> None:
    account = _account(partial=True)
    observed = ObservedPosition(
        symbol="SMH",
        qty=None,
        avg_entry_price=None,
        current_price=Decimal("100"),
        market_value=Decimal("1000"),
        estimated_market_value=None,
        pnl=Decimal("9.50"),
        pnl_pct=Decimal("0.95"),
        weight_pct=Decimal("16.67"),
        precision="PARTIAL",
    )

    position = _observed_input(observed, account, pd.DataFrame())

    assert position is not None
    assert position.pnl_pct == Decimal("0.0095")


def test_account_version_ignores_retrieval_time_only_changes() -> None:
    account = _account()
    later = replace(
        account,
        snapshot=replace(
            account.snapshot,
            retrieved_at=account.snapshot.retrieved_at + timedelta(minutes=1),
        ),
    )

    assert _account_version(account) == _account_version(later)


def test_partial_volume_primary_uses_full_volume_fallback(tmp_path: Path) -> None:
    bars, members = _bars()

    class PartialPrimary(FakeSource):
        partial_market_volume = True

        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            raise AssertionError("partial primary must not drive liquidity filters")

    class FullVolume(FakeSource):
        def __init__(self, data: pd.DataFrame) -> None:
            super().__init__(data)
            self.calls = 0

        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            self.calls += 1
            return super().fetch_daily_bars(tickers, start, end)

    engine, _ = _engine(tmp_path)
    engine.source = PartialPrimary(bars)
    full = FullVolume(bars)
    engine._intl_source = full  # type: ignore[assignment]

    loaded = _load_daily_bars(engine, members, date(2026, 7, 14))

    assert not loaded.empty
    assert full.calls > 0


def _run_ids(ledger: SignalLedger) -> set[str]:
    with ledger._lock:  # type: ignore[attr-defined]
        rows = ledger._con.execute("SELECT run_id FROM us_briefing_runs").fetchall()  # type: ignore[attr-defined]
    return {str(row["run_id"]) for row in rows}
