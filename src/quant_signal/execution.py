from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import math
from typing import Sequence, cast
from zoneinfo import ZoneInfo

from quant_signal.account import (
    AccountSnapshot,
    AccountState,
    BrokerOrder,
    BrokerPosition,
    ObservedPosition,
)
from quant_signal.config import ExecutionPlanSettings

ET = ZoneInfo("America/New_York")
RULE_VERSION = "exec-v1"

# 首版只在正常交易日的入场窗口评估状态迁移; 半日市由调用方缩短 session_close。
ACTION_WINDOW_START = time(9, 45)
ACTION_WINDOW_END = time(15, 45)


class PlanTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortfolioBudget:
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    current_exposure: Decimal
    capital_limit: Decimal
    max_financing_ratio: Decimal
    exposure_by_symbol: dict[str, Decimal] = field(default_factory=dict)
    cluster_by_symbol: dict[str, str] = field(default_factory=dict)
    cluster_exposure: dict[str, Decimal] = field(default_factory=dict)

    @property
    def max_gross_exposure(self) -> Decimal:
        return self.capital_limit * (Decimal("1") + self.max_financing_ratio)

    @property
    def available_new_notional(self) -> Decimal:
        financing = self.capital_limit * self.max_financing_ratio
        return max(
            Decimal("0"),
            min(
                self.buying_power,
                self.cash + financing,
                self.max_gross_exposure - self.current_exposure,
            ),
        )


def portfolio_budget_from_state(
    state: AccountState,
    config: ExecutionPlanSettings,
) -> PortfolioBudget:
    snapshot = state.snapshot
    capital_limit = snapshot.capital_limit or snapshot.equity
    exposure_by_symbol: dict[str, Decimal] = {}
    if state.observed_positions:
        exposure_by_symbol = {
            position.symbol: position.exposure_value
            for position in state.observed_positions
        }
    else:
        exposure_by_symbol = {
            position.symbol: position.market_value for position in state.positions
        }
    current_exposure = snapshot.market_value
    if current_exposure is None:
        current_exposure = sum(exposure_by_symbol.values(), Decimal("0"))
    cluster_by_symbol = {
        symbol: cluster
        for cluster, symbols in config.risk_clusters.items()
        for symbol in symbols
    }
    cluster_exposure: dict[str, Decimal] = {}
    for symbol, value in exposure_by_symbol.items():
        cluster = cluster_by_symbol.get(symbol)
        if cluster is not None:
            cluster_exposure[cluster] = cluster_exposure.get(cluster, Decimal("0")) + value
    return PortfolioBudget(
        equity=min(snapshot.equity, capital_limit),
        cash=snapshot.cash,
        buying_power=snapshot.buying_power,
        current_exposure=current_exposure,
        capital_limit=capital_limit,
        max_financing_ratio=snapshot.max_financing_ratio,
        exposure_by_symbol=exposure_by_symbol,
        cluster_by_symbol=cluster_by_symbol,
        cluster_exposure=cluster_exposure,
    )


class PlanState(str, Enum):
    CANDIDATE = "CANDIDATE"
    ARMED = "ARMED"
    IN_ENTRY_ZONE = "IN_ENTRY_ZONE"
    ACTIONABLE = "ACTIONABLE"
    AWAITING_FILL = "AWAITING_FILL"
    OPEN = "OPEN"
    EXIT_DUE = "EXIT_DUE"
    CLOSED = "CLOSED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


TERMINAL_STATES = frozenset(
    {PlanState.INVALIDATED, PlanState.EXPIRED, PlanState.CLOSED, PlanState.BLOCKED}
)
_TERMINAL_STATES = TERMINAL_STATES
_PRE_ENTRY_STATES = frozenset(
    {
        PlanState.CANDIDATE,
        PlanState.ARMED,
        PlanState.IN_ENTRY_ZONE,
        PlanState.ACTIONABLE,
        PlanState.AWAITING_FILL,
    }
)


@dataclass(frozen=True)
class PlanCandidate:
    ticker: str
    plan_date: date
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    target_weight: float | None
    score: float | None
    source_strategies: tuple[str, ...]
    memberships: tuple[str, ...]
    quote_at: datetime
    currency: str = "USD"


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    plan_version: int
    plan_date: date
    ticker: str
    currency: str
    source_strategies: tuple[str, ...]
    memberships: tuple[str, ...]
    score: float | None
    entry_low: float
    entry_high: float
    limit_price: float
    stop_loss: float
    take_profit: float
    target_weight: float | None
    gap_qty: int | None
    risk_qty: int | None
    cash_qty: int | None
    cap_qty: int | None
    suggested_qty: int | None
    suggested_notional: float | None
    valid_from: datetime
    expires_at: datetime
    quote_at: datetime
    account_at: datetime | None
    state: PlanState
    block_reason: str | None
    rule_version: str


@dataclass(frozen=True)
class PlanObservation:
    at: datetime
    price: float
    bar_close: float | None = None
    bar_low: float | None = None
    bar_complete: bool = False
    trend_long: bool = True
    position_qty: float = 0.0
    has_open_buy_order: bool = False
    session_close_et: time = ACTION_WINDOW_END


@dataclass(frozen=True)
class PlanTransition:
    state: PlanState
    event: str | None
    reason: str | None = None


def _plan_id(candidate: PlanCandidate) -> str:
    payload = "|".join(
        [
            candidate.plan_date.isoformat(),
            candidate.ticker,
            f"{candidate.entry_low:.4f}",
            f"{candidate.entry_high:.4f}",
            f"{candidate.stop_loss:.4f}",
            f"{candidate.take_profit:.4f}",
            RULE_VERSION,
        ]
    )
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _default_expiry(plan_date: date) -> datetime:
    return datetime.combine(plan_date, ACTION_WINDOW_END, tzinfo=ET)


def _prices_are_finite_positive(candidate: PlanCandidate) -> bool:
    prices = (
        candidate.entry_low,
        candidate.entry_high,
        candidate.stop_loss,
        candidate.take_profit,
    )
    return all(math.isfinite(price) and price > 0 for price in prices)


def _validate_prices(candidate: PlanCandidate, config: ExecutionPlanSettings) -> str | None:
    if not _prices_are_finite_positive(candidate):
        return "INVALID_PRICES"
    if candidate.entry_low > candidate.entry_high:
        return "INVALID_PRICES"
    if candidate.take_profit <= candidate.entry_high:
        return "INVALID_PRICES"
    limit_price = candidate.entry_high
    stop_distance = (limit_price - candidate.stop_loss) / limit_price
    if stop_distance < config.min_stop_distance:
        return "STOP_TOO_TIGHT"
    if stop_distance > config.max_stop_distance:
        return "STOP_TOO_WIDE"
    if candidate.stop_loss >= candidate.entry_low:
        return "INVALID_PRICES"
    return None


_OPEN_ORDER_STATUSES = frozenset(
    {"new", "accepted", "pending_new", "partially_filled", "held"}
)


def _is_open_buy(order: BrokerOrder) -> bool:
    return order.side == "buy" and order.status in _OPEN_ORDER_STATUSES


def _is_open_sell(order: BrokerOrder) -> bool:
    return order.side == "sell" and order.status in _OPEN_ORDER_STATUSES


def build_plan(
    candidate: PlanCandidate,
    account: AccountSnapshot | None,
    positions: Sequence[BrokerPosition],
    orders: Sequence[BrokerOrder],
    config: ExecutionPlanSettings,
    now: datetime,
    observed_positions: Sequence[ObservedPosition] = (),
) -> ExecutionPlan:
    """确定性生成单 ticker 执行计划; 所有中间上限都保留在计划里以便审计。"""
    limit_price = candidate.entry_high
    base = ExecutionPlan(
        plan_id=_plan_id(candidate),
        plan_version=1,
        plan_date=candidate.plan_date,
        ticker=candidate.ticker,
        currency=candidate.currency,
        source_strategies=candidate.source_strategies,
        memberships=candidate.memberships,
        score=candidate.score,
        entry_low=candidate.entry_low,
        entry_high=candidate.entry_high,
        limit_price=limit_price,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        target_weight=candidate.target_weight,
        gap_qty=None,
        risk_qty=None,
        cash_qty=None,
        cap_qty=None,
        suggested_qty=None,
        suggested_notional=None,
        valid_from=now,
        expires_at=_default_expiry(candidate.plan_date),
        quote_at=candidate.quote_at,
        account_at=account.retrieved_at if account is not None else None,
        state=PlanState.CANDIDATE,
        block_reason=None,
        rule_version=RULE_VERSION,
    )

    price_block = _validate_prices(candidate, config)
    if price_block is not None:
        return dataclasses.replace(base, state=PlanState.BLOCKED, block_reason=price_block)

    if candidate.currency != "USD":
        return dataclasses.replace(
            base, state=PlanState.BLOCKED, block_reason="UNSUPPORTED_MARKET"
        )

    quote_age = (now - candidate.quote_at).total_seconds()
    if quote_age > config.quote_max_age_seconds:
        return dataclasses.replace(base, state=PlanState.BLOCKED, block_reason="STALE_QUOTE")
    if account is None:
        return dataclasses.replace(base, state=PlanState.BLOCKED, block_reason="NO_ACCOUNT")
    account_age = (now - account.retrieved_at).total_seconds()
    account_max_age = (
        config.screenshot_max_age_hours * 3600
        if account.source == "screenshot"
        else config.account_max_age_seconds
    )
    if account_age > account_max_age:
        return dataclasses.replace(
            base, state=PlanState.BLOCKED, block_reason="STALE_ACCOUNT"
        )

    capital_limit = account.capital_limit or account.equity
    effective_equity = min(account.equity, capital_limit)
    equity = float(effective_equity)
    cash = float(account.cash)
    current_qty = sum(
        float(position.qty) for position in positions if position.symbol == candidate.ticker
    )
    exact_current_value = sum(
        float(position.market_value)
        for position in positions
        if position.symbol == candidate.ticker
    )
    observed_for_ticker = [
        position for position in observed_positions if position.symbol == candidate.ticker
    ]
    if observed_for_ticker and any(position.qty is None for position in observed_for_ticker):
        return dataclasses.replace(
            base, state=PlanState.BLOCKED, block_reason="POSITION_QTY_UNKNOWN"
        )
    observed_current_value = sum(
        float(position.exposure_value) for position in observed_for_ticker
    )
    current_value = observed_current_value or exact_current_value
    open_buys = [order for order in orders if _is_open_buy(order)]
    open_buy_qty = sum(
        float(order.qty or 0) for order in open_buys if order.symbol == candidate.ticker
    )
    open_sell_qty = sum(
        float(order.qty or 0)
        for order in orders
        if order.symbol == candidate.ticker and _is_open_sell(order)
    )
    frozen_cash = sum(
        float(order.qty or 0) * float(order.limit_price or 0) for order in open_buys
    )
    open_buy_notional = sum(
        float(order.qty or 0) * float(order.limit_price or 0)
        for order in open_buys
        if order.symbol == candidate.ticker
    )

    gap_qty: int | None = None
    if candidate.target_weight is not None:
        target_qty = math.floor(equity * candidate.target_weight / limit_price)
        gap_qty = int(target_qty - current_qty - open_buy_qty + open_sell_qty)
    risk_qty = math.floor(
        equity * config.risk_per_trade / (limit_price - candidate.stop_loss)
    )
    if account.source == "screenshot" or account.capital_limit is not None:
        current_exposure = float(account.market_value or 0)
        max_financing = float(capital_limit * account.max_financing_ratio)
        remaining_gross = float(account.max_gross_exposure) - current_exposure
        available_funds = min(
            float(account.buying_power),
            cash + max_financing,
            remaining_gross,
        )
        cash_qty = math.floor((available_funds - frozen_cash) / limit_price)
    else:
        cash_qty = math.floor(
            (cash - equity * config.cash_reserve - frozen_cash) / limit_price
        )
    cap_qty = math.floor(
        (float(capital_limit) * config.max_position_weight - current_value - open_buy_notional)
        / limit_price
    )

    quantities = [qty for qty in (gap_qty, risk_qty, cash_qty, cap_qty) if qty is not None]
    suggested_qty = max(0, min(quantities))
    plan = dataclasses.replace(
        base,
        gap_qty=gap_qty,
        risk_qty=risk_qty,
        cash_qty=cash_qty,
        cap_qty=cap_qty,
        suggested_qty=suggested_qty,
        suggested_notional=round(suggested_qty * limit_price, 2) if suggested_qty else None,
    )
    if suggested_qty <= 0:
        return dataclasses.replace(
            plan,
            state=PlanState.BLOCKED,
            block_reason="ZERO_QTY",
            suggested_notional=None,
        )
    return plan


def apply_portfolio_limits(
    plans: Sequence[ExecutionPlan],
    equity: Decimal | float,
    config: ExecutionPlanSettings,
    *,
    budget: PortfolioBudget | None = None,
) -> list[ExecutionPlan]:
    """按顺序套用当日组合级限制: 新仓数量与当日新增风险额度。"""
    risk_budget = float(equity) * config.max_daily_new_risk
    used_risk = 0.0
    accepted = 0
    remaining_notional = (
        float(budget.available_new_notional) if budget is not None else math.inf
    )
    cluster_exposure = (
        {key: float(value) for key, value in budget.cluster_exposure.items()}
        if budget is not None
        else {}
    )
    limited: list[ExecutionPlan] = []
    for plan in plans:
        if plan.state is not PlanState.CANDIDATE or not plan.suggested_qty:
            limited.append(plan)
            continue
        affordable_qty = (
            max(0, math.floor(remaining_notional / plan.limit_price))
            if budget is not None
            else plan.suggested_qty
        )
        suggested_qty = min(plan.suggested_qty, affordable_qty)
        exhausted_reason = "PORTFOLIO_BUDGET_EXHAUSTED"
        cluster = budget.cluster_by_symbol.get(plan.ticker) if budget is not None else None
        if cluster is not None and budget is not None:
            cluster_room = (
                float(budget.capital_limit) * config.max_cluster_weight
                - cluster_exposure.get(cluster, 0.0)
            )
            cluster_qty = max(0, math.floor(cluster_room / plan.limit_price))
            if cluster_qty < suggested_qty:
                suggested_qty = cluster_qty
                exhausted_reason = "CLUSTER_WEIGHT_EXCEEDED"
        if suggested_qty <= 0:
            limited.append(
                dataclasses.replace(
                    plan,
                    state=PlanState.BLOCKED,
                    block_reason=exhausted_reason,
                    suggested_qty=None,
                    suggested_notional=None,
                )
            )
            continue
        if suggested_qty != plan.suggested_qty:
            plan = dataclasses.replace(
                plan,
                suggested_qty=suggested_qty,
                suggested_notional=round(suggested_qty * plan.limit_price, 2),
            )
        plan_risk = suggested_qty * (plan.limit_price - plan.stop_loss)
        if accepted >= config.max_new_positions_per_day:
            limited.append(
                dataclasses.replace(
                    plan, state=PlanState.BLOCKED, block_reason="MAX_NEW_POSITIONS"
                )
            )
            continue
        if used_risk + plan_risk > risk_budget * (1 + 1e-9):
            limited.append(
                dataclasses.replace(
                    plan, state=PlanState.BLOCKED, block_reason="DAILY_RISK_EXHAUSTED"
                )
            )
            continue
        used_risk += plan_risk
        accepted += 1
        notional = suggested_qty * plan.limit_price
        remaining_notional -= notional
        if cluster is not None:
            cluster_exposure[cluster] = cluster_exposure.get(cluster, 0.0) + notional
        limited.append(plan)
    return limited


def _in_action_window(at: datetime, session_close_et: time) -> bool:
    local = at.astimezone(ET)
    if local.weekday() >= 5:
        return False
    close = min(session_close_et, ACTION_WINDOW_END)
    return ACTION_WINDOW_START <= local.time() <= close


def _in_zone(plan: ExecutionPlan, price: float) -> bool:
    return plan.entry_low <= price <= plan.entry_high


def advance_plan(
    plan: ExecutionPlan,
    observation: PlanObservation,
    config: ExecutionPlanSettings,
) -> PlanTransition:
    """纯状态迁移: 同一状态的重复观测不产生事件, 终态拒绝继续演进。"""
    if plan.state in _TERMINAL_STATES:
        raise PlanTransitionError(
            f"plan {plan.plan_id} in terminal state {plan.state.value}"
        )

    if plan.state is PlanState.OPEN:
        if observation.position_qty <= 0:
            return PlanTransition(PlanState.CLOSED, "CLOSED", "POSITION_GONE")
        if observation.price <= plan.stop_loss:
            return PlanTransition(PlanState.EXIT_DUE, "STOP_BREACH")
        if observation.price >= plan.take_profit:
            return PlanTransition(PlanState.EXIT_DUE, "TAKE_PROFIT")
        return PlanTransition(PlanState.OPEN, None)

    if plan.state is PlanState.EXIT_DUE:
        if observation.position_qty <= 0:
            return PlanTransition(PlanState.CLOSED, "CLOSED", "POSITION_GONE")
        return PlanTransition(PlanState.EXIT_DUE, None)

    assert plan.state in _PRE_ENTRY_STATES

    if observation.at > plan.expires_at:
        return PlanTransition(PlanState.EXPIRED, "EXPIRED")
    if observation.position_qty > 0 and plan.state in (
        PlanState.ACTIONABLE,
        PlanState.AWAITING_FILL,
    ):
        return PlanTransition(PlanState.OPEN, "OPEN", "POSITION_DETECTED")
    if not observation.trend_long:
        return PlanTransition(PlanState.INVALIDATED, "INVALIDATED", "TREND_FLAT")
    breached = observation.price <= plan.stop_loss or (
        observation.bar_complete
        and observation.bar_low is not None
        and observation.bar_low <= plan.stop_loss
    )
    if breached:
        return PlanTransition(
            PlanState.INVALIDATED, "INVALIDATED", "STOP_BREACHED_PRE_ENTRY"
        )

    if not _in_action_window(observation.at, observation.session_close_et):
        return PlanTransition(plan.state, None)

    if plan.state in (PlanState.ACTIONABLE, PlanState.AWAITING_FILL):
        return PlanTransition(plan.state, None)

    if plan.state is PlanState.CANDIDATE:
        if observation.price > plan.entry_high:
            return PlanTransition(PlanState.ARMED, None)
        if _in_zone(plan, observation.price):
            return PlanTransition(PlanState.IN_ENTRY_ZONE, None)
        return PlanTransition(PlanState.CANDIDATE, None)

    if plan.state is PlanState.ARMED:
        if _in_zone(plan, observation.price):
            return PlanTransition(PlanState.IN_ENTRY_ZONE, None)
        return PlanTransition(PlanState.ARMED, None)

    # IN_ENTRY_ZONE: 只有完整 5 分钟 bar 收在买区内且未破止损才允许 ACTIONABLE。
    if observation.price < plan.entry_low:
        return PlanTransition(PlanState.ARMED, None)
    confirmed = (
        observation.bar_complete
        and observation.bar_close is not None
        and _in_zone(plan, observation.bar_close)
        and (observation.bar_low is None or observation.bar_low > plan.stop_loss)
        and not observation.has_open_buy_order
        and bool(plan.suggested_qty)
    )
    if confirmed:
        return PlanTransition(PlanState.ACTIONABLE, "ACTIONABLE")
    return PlanTransition(PlanState.IN_ENTRY_ZONE, None)


def apply_transition(plan: ExecutionPlan, transition: PlanTransition) -> ExecutionPlan:
    return dataclasses.replace(plan, state=transition.state)


def plan_to_dict(plan: ExecutionPlan) -> dict[str, object]:
    payload = dataclasses.asdict(plan)
    payload["plan_date"] = plan.plan_date.isoformat()
    payload["valid_from"] = plan.valid_from.isoformat()
    payload["expires_at"] = plan.expires_at.isoformat()
    payload["quote_at"] = plan.quote_at.isoformat()
    payload["account_at"] = (
        plan.account_at.isoformat() if plan.account_at is not None else None
    )
    payload["state"] = plan.state.value
    payload["source_strategies"] = list(plan.source_strategies)
    payload["memberships"] = list(plan.memberships)
    return payload


def plan_from_dict(payload: dict[str, object]) -> ExecutionPlan:
    def _float(key: str) -> float:
        return float(str(payload[key]))

    def _opt_int(key: str) -> int | None:
        value = payload.get(key)
        return int(str(value)) if value is not None else None

    account_at = payload.get("account_at")
    target_weight = payload.get("target_weight")
    score = payload.get("score")
    suggested_notional = payload.get("suggested_notional")
    return ExecutionPlan(
        plan_id=str(payload["plan_id"]),
        plan_version=int(str(payload["plan_version"])),
        plan_date=date.fromisoformat(str(payload["plan_date"])),
        ticker=str(payload["ticker"]),
        currency=str(payload["currency"]),
        source_strategies=tuple(
            str(item) for item in cast("list[object]", payload["source_strategies"])
        ),
        memberships=tuple(
            str(item) for item in cast("list[object]", payload["memberships"])
        ),
        score=float(str(score)) if score is not None else None,
        entry_low=_float("entry_low"),
        entry_high=_float("entry_high"),
        limit_price=_float("limit_price"),
        stop_loss=_float("stop_loss"),
        take_profit=_float("take_profit"),
        target_weight=float(str(target_weight)) if target_weight is not None else None,
        gap_qty=_opt_int("gap_qty"),
        risk_qty=_opt_int("risk_qty"),
        cash_qty=_opt_int("cash_qty"),
        cap_qty=_opt_int("cap_qty"),
        suggested_qty=_opt_int("suggested_qty"),
        suggested_notional=(
            float(str(suggested_notional)) if suggested_notional is not None else None
        ),
        valid_from=datetime.fromisoformat(str(payload["valid_from"])),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        quote_at=datetime.fromisoformat(str(payload["quote_at"])),
        account_at=(
            datetime.fromisoformat(str(account_at)) if account_at is not None else None
        ),
        state=PlanState(str(payload["state"])),
        block_reason=(
            str(payload["block_reason"]) if payload.get("block_reason") is not None else None
        ),
        rule_version=str(payload["rule_version"]),
    )
