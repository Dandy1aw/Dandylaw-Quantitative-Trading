"""确定性执行状态机回放: 对比旧 target-hit 口径与新 ACTIONABLE 确认口径。

注意: 用当前指数成分回放历史必然带幸存者偏差, 结果只作研究参考,
不作为正式 alpha 证据 (survivorship_biased 恒为 True)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from quant_signal.config import ExecutionPlanSettings
from quant_signal.execution import (
    TERMINAL_STATES,
    ExecutionPlan,
    PlanObservation,
    PlanState,
    advance_plan,
    apply_transition,
)

# 旧到价提醒口径: live <= target_buy * 1.002
_OLD_HIT_TOLERANCE = 1.002


@dataclass(frozen=True)
class ReplayEntry:
    ticker: str
    plan_id: str
    entry_price: float
    fwd_1d: float | None
    fwd_5d: float | None
    fwd_20d: float | None
    max_adverse_excursion: float | None


@dataclass(frozen=True)
class ReplayResult:
    old_target_hits: int
    new_actionable: int
    entry_rate: float
    entries: tuple[ReplayEntry, ...]
    survivorship_biased: bool = True


def _forward_return(closes: Sequence[float], entry: float, days: int) -> float | None:
    if len(closes) <= days:
        return None
    return float(closes[days]) / entry - 1.0


def _max_adverse_excursion(
    closes: Sequence[float], entry: float, horizon: int = 20
) -> float | None:
    window = closes[1 : horizon + 1]
    if not window:
        return None
    return min(float(close) / entry - 1.0 for close in window)


def _confirmation_price(
    plan: ExecutionPlan,
    observations: Sequence[PlanObservation],
    config: ExecutionPlanSettings,
) -> float | None:
    """按时间顺序推进状态机, 返回首次 ACTIONABLE 的确认bar收盘价。"""
    current = plan
    for observation in observations:
        if current.state in TERMINAL_STATES:
            return None
        transition = advance_plan(current, observation, config)
        if transition.state is PlanState.ACTIONABLE:
            return (
                float(observation.bar_close)
                if observation.bar_close is not None
                else observation.price
            )
        current = apply_transition(current, transition)
    return None


def replay_plans(
    plans: Sequence[ExecutionPlan],
    observations: Mapping[str, Sequence[PlanObservation]],
    daily_closes: Mapping[str, Sequence[float]],
    config: ExecutionPlanSettings,
) -> ReplayResult:
    old_hits = 0
    entries: list[ReplayEntry] = []
    for plan in plans:
        ticker_obs = observations.get(plan.ticker, ())
        if any(
            obs.price <= plan.entry_low * _OLD_HIT_TOLERANCE for obs in ticker_obs
        ):
            old_hits += 1
        entry_price = _confirmation_price(plan, ticker_obs, config)
        if entry_price is None:
            continue
        closes = daily_closes.get(plan.ticker, ())
        entries.append(
            ReplayEntry(
                ticker=plan.ticker,
                plan_id=plan.plan_id,
                entry_price=entry_price,
                fwd_1d=_forward_return(closes, entry_price, 1),
                fwd_5d=_forward_return(closes, entry_price, 5),
                fwd_20d=_forward_return(closes, entry_price, 20),
                max_adverse_excursion=_max_adverse_excursion(closes, entry_price),
            )
        )
    total = len(plans)
    return ReplayResult(
        old_target_hits=old_hits,
        new_actionable=len(entries),
        entry_rate=(len(entries) / total) if total else 0.0,
        entries=tuple(entries),
    )
