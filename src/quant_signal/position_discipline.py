from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from hashlib import sha256
from typing import Iterable

from quant_signal.config import PositionDisciplineSettings
from quant_signal.market_regime import Regime


MONEY = Decimal("0.01")
WEIGHT = Decimal("0.0001")


class DisciplineStatus(str, Enum):
    HOLD = "HOLD"
    WATCH = "WATCH"
    TAKE_PROFIT_DUE = "TAKE_PROFIT_DUE"
    REDUCE = "REDUCE"
    EXIT_DUE = "EXIT_DUE"


@dataclass(frozen=True)
class PositionInput:
    ticker: str
    current_price: Decimal
    quantity: Decimal | None = None
    avg_entry_price: Decimal | None = None
    pnl_pct: Decimal | None = None
    market_value: Decimal | None = None
    account_equity: Decimal | None = None
    atr: Decimal | None = None
    trend_invalidation: Decimal | None = None
    positions_complete: bool = True


@dataclass(frozen=True)
class DisciplineState:
    ticker: str
    basis_version: str
    notified_stage: int
    peak_price: Decimal
    basis_quantity: Decimal | None


@dataclass(frozen=True)
class PositionAdvice:
    ticker: str
    status: DisciplineStatus
    leverage: Decimal
    current_price: Decimal
    cost_basis: Decimal | None
    cost_quality: str
    quantity: Decimal | None
    quantity_quality: str
    pnl_pct: Decimal | None
    hard_stop_price: Decimal | None
    protection_price: Decimal | None
    next_profit_price: Decimal | None
    cumulative_sell_fraction: Decimal
    incremental_sell_fraction: Decimal
    incremental_sell_qty: Decimal | None
    financing_allowed: bool
    effective_exposure: Decimal | None
    effective_weight: Decimal | None
    warnings: tuple[str, ...]
    next_state: DisciplineState


@dataclass(frozen=True)
class PortfolioRiskSummary:
    total_effective_exposure: Decimal
    total_effective_weight: Decimal | None
    leveraged_effective_exposure: Decimal
    leveraged_effective_weight: Decimal | None
    cluster_effective_weights: dict[str, Decimal]
    warnings: tuple[str, ...]


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def _leverage_key(leverage: Decimal) -> str:
    if leverage < Decimal("1.5"):
        return "1x"
    if leverage < Decimal("2.5"):
        return "2x"
    return "3x"


def _cost(position: PositionInput) -> tuple[Decimal | None, str]:
    if position.avg_entry_price is not None and position.avg_entry_price > 0:
        return _money(position.avg_entry_price), "EXACT"
    if position.pnl_pct is not None:
        denominator = Decimal("1") + position.pnl_pct
        if denominator > 0 and position.current_price > 0:
            return _money(position.current_price / denominator), "ESTIMATED"
    return None, "MISSING"


def _basis_version(ticker: str, cost: Decimal | None) -> str:
    if cost is None:
        digest = sha256(f"{ticker}:missing".encode()).hexdigest()[:8]
        return f"{ticker}:missing:{digest}"
    return f"{ticker}:{cost.quantize(Decimal('0.0001'))}"


def _stage_cumulative(
    settings: PositionDisciplineSettings, key: str, stage: int
) -> Decimal:
    if stage <= 0:
        return Decimal("0")
    stages = settings.profit_ladders[key]
    return Decimal(str(stages[min(stage, len(stages)) - 1].cumulative_sell))


def _attained_stage(
    settings: PositionDisciplineSettings, key: str, gain: Decimal
) -> int:
    stage = 0
    for index, rule in enumerate(settings.profit_ladders[key], start=1):
        if gain >= Decimal(str(rule.gain)):
            stage = index
    return stage


def _next_profit_price(
    settings: PositionDisciplineSettings, key: str, stage: int, cost: Decimal
) -> Decimal | None:
    rules = settings.profit_ladders[key]
    if stage >= len(rules):
        return None
    return _money(cost * (Decimal("1") + Decimal(str(rules[stage].gain))))


def advise_position(
    position: PositionInput,
    *,
    leverage: float | Decimal,
    prior: DisciplineState | None,
    settings: PositionDisciplineSettings,
    regime: Regime = Regime.TREND,
) -> PositionAdvice:
    """Build deterministic advice; advancing notification state prevents alert spam."""
    leverage_value = Decimal(str(leverage))
    if leverage_value < 1:
        raise ValueError("leverage must be at least one")
    if position.current_price <= 0:
        raise ValueError("current_price must be positive")

    warnings: list[str] = []
    cost, cost_quality = _cost(position)
    quantity = position.quantity if position.quantity is not None and position.quantity >= 0 else None
    quantity_quality = "EXACT" if quantity is not None else "MISSING"
    if not position.positions_complete:
        warnings.append("POSITIONS_PARTIAL")
    if cost is None:
        warnings.append("COST_BASIS_MISSING")

    basis_version = _basis_version(position.ticker, cost)
    valid_prior = prior if prior is not None and prior.basis_version == basis_version else None
    previous_stage = valid_prior.notified_stage if valid_prior is not None else 0
    basis_quantity = (
        valid_prior.basis_quantity
        if valid_prior is not None and valid_prior.basis_quantity is not None
        else quantity
    )
    peak_price = max(
        position.current_price,
        valid_prior.peak_price if valid_prior is not None else position.current_price,
    )

    key = _leverage_key(leverage_value)
    pnl_pct: Decimal | None = None
    hard_stop: Decimal | None = None
    protection: Decimal | None = None
    next_profit: Decimal | None = None
    attained_stage = 0
    if cost is not None:
        pnl_pct = position.current_price / cost - Decimal("1")
        attained_stage = _attained_stage(settings, key, pnl_pct)
        loss_cap = Decimal(str(settings.hard_loss_caps[key]))
        hard_stop = _money(cost * (Decimal("1") - loss_cap))
        protection_candidates = [hard_stop]
        if (
            position.trend_invalidation is not None
            and hard_stop < position.trend_invalidation < position.current_price
        ):
            protection_candidates.append(_money(position.trend_invalidation))
        effective_stage = max(previous_stage, attained_stage)
        if pnl_pct >= Decimal(str(settings.breakeven_trigger)):
            protection_candidates.append(_money(cost))
        if effective_stage >= 1:
            protection_candidates.append(
                _money(cost * (Decimal("1") + Decimal(str(settings.stage_one_profit_lock))))
            )
        if effective_stage >= 2:
            protection_candidates.append(
                _money(cost * (Decimal("1") + Decimal(str(settings.stage_two_profit_lock))))
            )
        if effective_stage >= 3 and position.atr is not None and position.atr > 0:
            multiple = (
                settings.trailing_atr_multiple_1x
                if key == "1x"
                else settings.trailing_atr_multiple_leveraged
            )
            protection_candidates.append(
                _money(peak_price - position.atr * Decimal(str(multiple)))
            )
        protection = max(protection_candidates)
        next_profit = _next_profit_price(settings, key, max(previous_stage, attained_stage), cost)

    next_stage = max(previous_stage, attained_stage)
    previous_cumulative = _stage_cumulative(settings, key, previous_stage)
    cumulative = _stage_cumulative(settings, key, next_stage)
    incremental_fraction = (
        cumulative - previous_cumulative if attained_stage > previous_stage else Decimal("0")
    )
    incremental_qty: Decimal | None
    if quantity is None or basis_quantity is None or cost is None:
        incremental_qty = None
    else:
        incremental_qty = (basis_quantity * incremental_fraction).to_integral_value(
            rounding=ROUND_DOWN
        )

    exposure: Decimal | None = None
    if position.market_value is not None:
        exposure = abs(position.market_value) * leverage_value
    elif quantity is not None:
        exposure = position.current_price * abs(quantity) * leverage_value
    effective_exposure = _money(exposure) if exposure is not None else None
    effective_weight: Decimal | None = None
    if (
        effective_exposure is not None
        and position.account_equity is not None
        and position.account_equity > 0
    ):
        effective_weight = (effective_exposure / position.account_equity).quantize(WEIGHT)
        if effective_weight > Decimal(str(settings.max_single_effective_weight)):
            warnings.append("SINGLE_EFFECTIVE_EXPOSURE_HIGH")

    financing_allowed = not (
        regime == Regime.RISK_OFF
        or (leverage_value > 1 and not settings.allow_financing_for_leveraged)
    )
    if leverage_value > 1:
        warnings.append("LEVERAGED_PRODUCT_PATH_RISK")
    if not financing_allowed:
        warnings.append("FINANCING_DISABLED")

    if hard_stop is not None and position.current_price <= hard_stop:
        status = DisciplineStatus.EXIT_DUE
    elif incremental_fraction > 0:
        status = DisciplineStatus.TAKE_PROFIT_DUE
    elif (
        pnl_pct is not None
        and pnl_pct <= -Decimal(str(settings.hard_loss_caps[key])) / Decimal("2")
    ):
        status = DisciplineStatus.WATCH
    elif "SINGLE_EFFECTIVE_EXPOSURE_HIGH" in warnings:
        status = DisciplineStatus.REDUCE
    else:
        status = DisciplineStatus.HOLD

    state = DisciplineState(
        ticker=position.ticker,
        basis_version=basis_version,
        notified_stage=next_stage,
        peak_price=_money(peak_price),
        basis_quantity=basis_quantity,
    )
    return PositionAdvice(
        ticker=position.ticker,
        status=status,
        leverage=leverage_value,
        current_price=_money(position.current_price),
        cost_basis=cost,
        cost_quality=cost_quality,
        quantity=quantity,
        quantity_quality=quantity_quality,
        pnl_pct=pnl_pct,
        hard_stop_price=hard_stop,
        protection_price=protection,
        next_profit_price=next_profit,
        cumulative_sell_fraction=cumulative,
        incremental_sell_fraction=incremental_fraction,
        incremental_sell_qty=incremental_qty,
        financing_allowed=financing_allowed,
        effective_exposure=effective_exposure,
        effective_weight=effective_weight,
        warnings=tuple(dict.fromkeys(warnings)),
        next_state=state,
    )


def summarize_portfolio_risk(
    advice: Iterable[PositionAdvice],
    *,
    equity: Decimal,
    clusters: dict[str, list[str]],
) -> PortfolioRiskSummary:
    rows = list(advice)
    total = sum(
        (row.effective_exposure or Decimal("0") for row in rows), Decimal("0")
    )
    leveraged = sum(
        (
            row.effective_exposure or Decimal("0")
            for row in rows
            if row.leverage > 1
        ),
        Decimal("0"),
    )
    by_symbol = {
        row.ticker: row.effective_exposure or Decimal("0") for row in rows
    }
    cluster_weights: dict[str, Decimal] = {}
    for cluster, symbols in clusters.items():
        exposure = sum((by_symbol.get(symbol, Decimal("0")) for symbol in symbols), Decimal("0"))
        if equity > 0:
            cluster_weights[cluster] = (exposure / equity).quantize(WEIGHT)
    total_weight = (total / equity).quantize(WEIGHT) if equity > 0 else None
    leveraged_weight = (leveraged / equity).quantize(WEIGHT) if equity > 0 else None
    warnings: list[str] = []
    if total_weight is not None and total_weight > Decimal("1.20"):
        warnings.append("TOTAL_EFFECTIVE_EXPOSURE_HIGH")
    if leveraged_weight is not None and leveraged_weight > Decimal("0.50"):
        warnings.append("LEVERAGED_EFFECTIVE_EXPOSURE_HIGH")
    if any(weight > Decimal("0.35") for weight in cluster_weights.values()):
        warnings.append("CLUSTER_EFFECTIVE_EXPOSURE_HIGH")
    return PortfolioRiskSummary(
        total_effective_exposure=_money(total),
        total_effective_weight=total_weight,
        leveraged_effective_exposure=_money(leveraged),
        leveraged_effective_weight=leveraged_weight,
        cluster_effective_weights=cluster_weights,
        warnings=tuple(warnings),
    )
