from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_signal.account import AccountSnapshot, BrokerOrder, BrokerPosition
from quant_signal.config import ExecutionPlanSettings
from quant_signal.execution import (
    ExecutionPlan,
    PlanCandidate,
    PlanObservation,
    PlanState,
    PlanTransitionError,
    advance_plan,
    apply_portfolio_limits,
    apply_transition,
    build_plan,
)

CONFIG = ExecutionPlanSettings()
NOW = datetime(2026, 7, 10, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
PRE_WINDOW = datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc)  # 09:30 ET


def candidate(**overrides: object) -> PlanCandidate:
    values: dict[str, object] = {
        "ticker": "AAPL",
        "plan_date": NOW.date(),
        "entry_low": 100.0,
        "entry_high": 102.0,
        "stop_loss": 95.0,
        "take_profit": 115.0,
        "target_weight": 0.10,
        "score": 0.9,
        "source_strategies": ("index_scan",),
        "memberships": ("nasdaq100", "sp500"),
        "quote_at": NOW,
    }
    values.update(overrides)
    return PlanCandidate(**values)  # type: ignore[arg-type]


def account(**overrides: object) -> AccountSnapshot:
    values: dict[str, object] = {
        "account_id": "paper-1",
        "equity": Decimal("100000"),
        "cash": Decimal("50000"),
        "buying_power": Decimal("100000"),
        "currency": "USD",
        "retrieved_at": NOW,
    }
    values.update(overrides)
    return AccountSnapshot(**values)  # type: ignore[arg-type]


def open_buy(qty: str, limit: str, symbol: str = "AAPL") -> BrokerOrder:
    return BrokerOrder(
        order_id="o-1",
        symbol=symbol,
        side="buy",
        status="new",
        qty=Decimal(qty),
        limit_price=Decimal(limit),
        submitted_at=NOW,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )


def position(symbol: str, qty: str, value: str) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        qty=Decimal(qty),
        side="long",
        avg_entry_price=Decimal("100"),
        market_value=Decimal(value),
    )


# ---------------------------------------------------------------- sizing


def test_quantity_is_minimum_of_gap_risk_cash_and_cap() -> None:
    plan = build_plan(candidate(), account(), positions=(), orders=(), config=CONFIG, now=NOW)
    assert plan.state is PlanState.CANDIDATE
    assert plan.suggested_qty == min(plan.gap_qty, plan.risk_qty, plan.cash_qty, plan.cap_qty)
    # equity=100k, limit=102, stop=95: risk_qty=floor(500/7)=71 是这里的最小值
    assert plan.suggested_qty == 71
    assert plan.limit_price == 102.0
    assert plan.suggested_notional == pytest.approx(71 * 102.0)


def test_existing_position_and_open_orders_reduce_gap() -> None:
    plan = build_plan(
        candidate(),
        account(),
        positions=(position("AAPL", "50", "5100"),),
        orders=(open_buy("20", "101"),),
        config=CONFIG,
        now=NOW,
    )
    # target_qty=floor(10000/102)=98, gap=98-50-20=28
    assert plan.gap_qty == 28
    assert plan.suggested_qty == 28


def test_cash_reserve_and_frozen_cash_limit_quantity() -> None:
    plan = build_plan(
        candidate(target_weight=0.9),
        account(cash=Decimal("22000")),
        positions=(),
        orders=(open_buy("10", "100", symbol="MSFT"),),
        config=CONFIG,
        now=NOW,
    )
    # cash_qty = floor((22000 - 20000 reserve - 1000 frozen)/102) = 9
    assert plan.cash_qty == 9
    assert plan.suggested_qty == min(plan.risk_qty, 9)


def test_max_position_weight_caps_quantity() -> None:
    config = ExecutionPlanSettings(risk_per_trade=0.05, max_position_weight=0.05)
    plan = build_plan(
        candidate(target_weight=0.5),
        account(),
        positions=(position("AAPL", "20", "2040"),),
        orders=(),
        config=config,
        now=NOW,
    )
    # cap = floor((100000*0.05 - 2040)/102) = floor(2960/102) = 29
    assert plan.cap_qty == 29


def test_candidate_without_target_weight_uses_risk_and_cap() -> None:
    plan = build_plan(
        candidate(target_weight=None),
        account(),
        positions=(),
        orders=(),
        config=CONFIG,
        now=NOW,
    )
    assert plan.gap_qty is None
    assert plan.suggested_qty == min(plan.risk_qty, plan.cash_qty, plan.cap_qty)


def test_zero_quantity_blocks_plan() -> None:
    plan = build_plan(
        candidate(),
        account(cash=Decimal("20000")),  # 全部现金都是保留金
        positions=(),
        orders=(),
        config=CONFIG,
        now=NOW,
    )
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "ZERO_QTY"
    assert plan.suggested_qty == 0


def test_too_tight_stop_blocks_buy() -> None:
    plan = build_plan(
        candidate(entry_low=100, entry_high=101, stop_loss=100),
        account(),
        (),
        (),
        CONFIG,
        NOW,
    )
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "STOP_TOO_TIGHT"


def test_too_wide_stop_blocks_buy() -> None:
    plan = build_plan(
        candidate(stop_loss=60.0), account(), (), (), CONFIG, NOW
    )
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "STOP_TOO_WIDE"


def test_invalid_price_ordering_blocks_buy() -> None:
    plan = build_plan(
        candidate(take_profit=101.0), account(), (), (), CONFIG, NOW
    )
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "INVALID_PRICES"


def test_stale_account_omits_quantities() -> None:
    stale = account(retrieved_at=NOW - timedelta(seconds=120))
    plan = build_plan(candidate(), stale, (), (), CONFIG, NOW)
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "STALE_ACCOUNT"
    assert plan.suggested_qty is None
    assert plan.suggested_notional is None
    # 观察价位保留
    assert plan.entry_low == 100.0


def test_stale_quote_omits_quantities() -> None:
    plan = build_plan(
        candidate(quote_at=NOW - timedelta(seconds=500)),
        account(),
        (),
        (),
        CONFIG,
        NOW,
    )
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "STALE_QUOTE"
    assert plan.suggested_qty is None


def test_missing_account_omits_quantities() -> None:
    plan = build_plan(candidate(), None, (), (), CONFIG, NOW)
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "NO_ACCOUNT"
    assert plan.suggested_qty is None


def test_plan_id_is_stable_and_version_starts_at_one() -> None:
    plan_a = build_plan(candidate(), account(), (), (), CONFIG, NOW)
    plan_b = build_plan(candidate(), account(), (), (), CONFIG, NOW)
    plan_c = build_plan(candidate(entry_high=103.0), account(), (), (), CONFIG, NOW)
    assert plan_a.plan_id == plan_b.plan_id
    assert plan_a.plan_id != plan_c.plan_id
    assert plan_a.plan_version == 1


def test_portfolio_limits_cap_new_positions_and_daily_risk() -> None:
    config = ExecutionPlanSettings(max_new_positions_per_day=1)
    plans = [
        build_plan(candidate(ticker="AAPL"), account(), (), (), config, NOW),
        build_plan(candidate(ticker="MSFT"), account(), (), (), config, NOW),
    ]
    limited = apply_portfolio_limits(plans, account().equity, config)
    assert limited[0].state is PlanState.CANDIDATE
    assert limited[1].state is PlanState.BLOCKED
    assert limited[1].block_reason == "MAX_NEW_POSITIONS"


def test_portfolio_limits_block_when_daily_risk_exhausted() -> None:
    config = ExecutionPlanSettings(risk_per_trade=0.005, max_daily_new_risk=0.005)
    plans = [
        build_plan(candidate(ticker="AAPL"), account(), (), (), config, NOW),
        build_plan(candidate(ticker="MSFT"), account(), (), (), config, NOW),
    ]
    limited = apply_portfolio_limits(plans, account().equity, config)
    assert limited[0].state is PlanState.CANDIDATE
    assert limited[1].block_reason == "DAILY_RISK_EXHAUSTED"


# ---------------------------------------------------------------- transitions


def make_plan(**overrides: object) -> ExecutionPlan:
    plan = build_plan(candidate(), account(), (), (), CONFIG, NOW)
    if overrides:
        import dataclasses

        plan = dataclasses.replace(plan, **overrides)  # type: ignore[arg-type]
    return plan


def obs(**overrides: object) -> PlanObservation:
    values: dict[str, object] = {
        "at": NOW,
        "price": 105.0,
        "bar_close": None,
        "bar_low": None,
        "bar_complete": False,
        "trend_long": True,
        "position_qty": 0.0,
        "has_open_buy_order": False,
    }
    values.update(overrides)
    return PlanObservation(**values)  # type: ignore[arg-type]


def test_full_confirmation_path_to_actionable() -> None:
    plan = make_plan()

    armed = advance_plan(plan, obs(price=105.0), CONFIG)
    assert armed.state is PlanState.ARMED
    assert armed.event is None
    plan = apply_transition(plan, armed)

    in_zone = advance_plan(plan, obs(price=101.0), CONFIG)
    assert in_zone.state is PlanState.IN_ENTRY_ZONE
    assert in_zone.event is None
    plan = apply_transition(plan, in_zone)

    confirmed = advance_plan(
        plan,
        obs(price=101.0, bar_close=101.0, bar_low=100.5, bar_complete=True),
        CONFIG,
    )
    assert confirmed.state is PlanState.ACTIONABLE
    assert confirmed.event == "ACTIONABLE"


def test_incomplete_bar_does_not_confirm() -> None:
    plan = make_plan(state=PlanState.IN_ENTRY_ZONE)
    result = advance_plan(
        plan, obs(price=101.0, bar_close=101.0, bar_low=100.5, bar_complete=False), CONFIG
    )
    assert result.state is PlanState.IN_ENTRY_ZONE
    assert result.event is None


def test_no_action_before_0945_et() -> None:
    plan = make_plan()
    result = advance_plan(
        plan,
        obs(at=PRE_WINDOW, price=101.0, bar_close=101.0, bar_low=100.5, bar_complete=True),
        CONFIG,
    )
    assert result.state is PlanState.CANDIDATE
    assert result.event is None


def test_price_below_stop_invalidates() -> None:
    plan = make_plan(state=PlanState.IN_ENTRY_ZONE)
    result = advance_plan(plan, obs(price=94.0), CONFIG)
    assert result.state is PlanState.INVALIDATED
    assert result.event == "INVALIDATED"


def test_trend_flat_invalidates() -> None:
    plan = make_plan(state=PlanState.ARMED)
    result = advance_plan(plan, obs(price=105.0, trend_long=False), CONFIG)
    assert result.state is PlanState.INVALIDATED
    assert result.event == "INVALIDATED"


def test_expiry_wins() -> None:
    plan = make_plan(state=PlanState.ARMED)
    late = plan.expires_at + timedelta(minutes=1)
    result = advance_plan(plan, obs(at=late, price=105.0), CONFIG)
    assert result.state is PlanState.EXPIRED
    assert result.event == "EXPIRED"


def test_duplicate_open_buy_order_blocks_confirmation() -> None:
    plan = make_plan(state=PlanState.IN_ENTRY_ZONE)
    result = advance_plan(
        plan,
        obs(
            price=101.0,
            bar_close=101.0,
            bar_low=100.5,
            bar_complete=True,
            has_open_buy_order=True,
        ),
        CONFIG,
    )
    assert result.state is PlanState.IN_ENTRY_ZONE
    assert result.event is None


def test_repeated_actionable_observation_emits_no_event() -> None:
    plan = make_plan(state=PlanState.ACTIONABLE)
    result = advance_plan(
        plan, obs(price=101.0, bar_close=101.0, bar_low=100.5, bar_complete=True), CONFIG
    )
    assert result.state is PlanState.ACTIONABLE
    assert result.event is None


def test_position_detected_opens_plan() -> None:
    plan = make_plan(state=PlanState.ACTIONABLE)
    result = advance_plan(plan, obs(price=101.0, position_qty=71.0), CONFIG)
    assert result.state is PlanState.OPEN
    assert result.event == "OPEN"


def test_open_position_stop_breach_and_take_profit() -> None:
    plan = make_plan(state=PlanState.OPEN)
    stop_hit = advance_plan(plan, obs(price=94.0, position_qty=71.0), CONFIG)
    assert stop_hit.state is PlanState.EXIT_DUE
    assert stop_hit.event == "STOP_BREACH"

    tp_hit = advance_plan(plan, obs(price=116.0, position_qty=71.0), CONFIG)
    assert tp_hit.state is PlanState.EXIT_DUE
    assert tp_hit.event == "TAKE_PROFIT"


def test_open_position_closed_when_position_gone() -> None:
    plan = make_plan(state=PlanState.OPEN)
    result = advance_plan(plan, obs(price=105.0, position_qty=0.0), CONFIG)
    assert result.state is PlanState.CLOSED
    assert result.event == "CLOSED"


@pytest.mark.parametrize(
    "terminal",
    [PlanState.INVALIDATED, PlanState.EXPIRED, PlanState.CLOSED, PlanState.BLOCKED],
)
def test_terminal_states_reject_transitions(terminal: PlanState) -> None:
    plan = make_plan(state=terminal)
    with pytest.raises(PlanTransitionError):
        advance_plan(plan, obs(), CONFIG)
