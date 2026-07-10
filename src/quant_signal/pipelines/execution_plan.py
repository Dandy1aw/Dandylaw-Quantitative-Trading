"""PAPER 执行建议 pipeline：盘前生成确定性执行计划，盘中只推状态迁移。

数据语义分层：指数候选/核心信号 -> ticker 级聚合 -> 风险门控 -> 持久化计划。
账户来自只读 PAPER 适配器；本模块不提交任何券商订单。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.account import AccountState
from quant_signal.ai_briefing import AIBriefingContext, run_ai_briefing
from quant_signal.calendar import previous_trading_day
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
    plan_to_dict,
    portfolio_budget_from_state,
)
from quant_signal.notifier.cards import (
    execution_plan_card,
    plan_event_card,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

ET = ZoneInfo("America/New_York")

_PRICE_KEYS = ("entry_low", "entry_high", "stop_loss", "take_profit")
# 只有核心日线策略的 BUY 信号参与执行聚合; 告警类/发现类不算交易信号
_CORE_STRATEGIES = frozenset(
    {
        "momentum_rotation",
        "rsi_reversion",
        "macd_cross",
        "bollinger_breakout",
        "breakout_20d",
    }
)


@dataclasses.dataclass(frozen=True)
class _RawCandidate:
    ticker: str
    entry_low: float
    entry_high: float
    stop_loss: float
    take_profit: float
    target_weight: float | None
    score: float | None
    sources: tuple[str, ...]
    memberships: tuple[str, ...]
    currency: str = "USD"
    forced_block: str | None = None


def _has_prices(extra: Mapping[str, object]) -> bool:
    return all(isinstance(extra.get(key), (int, float)) for key in _PRICE_KEYS)


def _currency(engine: Engine, ticker: str) -> str:
    metadata = engine.settings.tickers.get(ticker)
    if metadata is not None:
        return metadata.currency
    return engine.settings.international_tickers.get(ticker, "USD")


def _index_candidates(engine: Engine, now: datetime) -> list[_RawCandidate]:
    expected_as_of = previous_trading_day(now.astimezone(ET).date())
    top_n = engine.settings.index_universe.execution_top_n
    output: list[_RawCandidate] = []
    for row in engine.ledger.latest_scan_candidates():
        if int(str(row["rank"])) > top_n:
            continue
        extra = row.get("extra")
        if not isinstance(extra, dict) or not _has_prices(extra):
            continue
        forced_block = None
        raw_block = extra.get("block_reason")
        if isinstance(raw_block, str) and raw_block:
            forced_block = raw_block
        elif str(extra.get("market_as_of", "")) != expected_as_of.isoformat():
            forced_block = "DATA_STALE"
        memberships = extra.get("memberships")
        output.append(
            _RawCandidate(
                ticker=str(row["ticker"]),
                entry_low=float(str(extra["entry_low"])),
                entry_high=float(str(extra["entry_high"])),
                stop_loss=float(str(extra["stop_loss"])),
                take_profit=float(str(extra["take_profit"])),
                target_weight=None,
                score=float(str(row["score"])),
                sources=("index_scan",),
                memberships=tuple(
                    str(item) for item in memberships
                )
                if isinstance(memberships, list)
                else (),
                currency=_currency(engine, str(row["ticker"])),
                forced_block=forced_block,
            )
        )
    return output


def _core_signals(engine: Engine, day: date) -> tuple[list[_RawCandidate], set[str]]:
    import json

    buys: list[_RawCandidate] = []
    sells: set[str] = set()
    for row in engine.ledger.signals_on(day):
        if str(row["strategy_id"]) not in _CORE_STRATEGIES:
            continue
        ticker = str(row["ticker"])
        direction = str(row["direction"])
        if direction == "sell":
            sells.add(ticker)
            continue
        if direction != "buy":
            continue
        raw_extra = row.get("extra_json")
        if not raw_extra:
            continue
        try:
            extra = json.loads(str(raw_extra))
        except json.JSONDecodeError:
            continue
        if not _has_prices(extra):
            continue
        weight = row.get("suggested_weight")
        buys.append(
            _RawCandidate(
                ticker=ticker,
                entry_low=float(extra["entry_low"]),
                entry_high=float(extra["entry_high"]),
                stop_loss=float(extra["stop_loss"]),
                take_profit=float(extra["take_profit"]),
                target_weight=float(str(weight)) if weight is not None else None,
                score=None,
                sources=(str(row["strategy_id"]),),
                memberships=(),
                currency=_currency(engine, ticker),
            )
        )
    return buys, sells


def _merge(first: _RawCandidate, second: _RawCandidate) -> _RawCandidate:
    """同向多来源合并成一个 ticker 级候选; 买入区取交集, 止损/止盈取保守值。"""
    entry_low = max(first.entry_low, second.entry_low)
    entry_high = min(first.entry_high, second.entry_high)
    stop_loss = max(first.stop_loss, second.stop_loss)
    take_profit = min(first.take_profit, second.take_profit)
    weights = [w for w in (first.target_weight, second.target_weight) if w is not None]
    conflict = entry_low > entry_high or not stop_loss < entry_low
    return _RawCandidate(
        ticker=first.ticker,
        entry_low=entry_low if not conflict else first.entry_low,
        entry_high=entry_high if not conflict else first.entry_high,
        stop_loss=stop_loss if not conflict else first.stop_loss,
        take_profit=take_profit if not conflict else first.take_profit,
        target_weight=max(weights) if weights else None,
        score=first.score if first.score is not None else second.score,
        sources=tuple(dict.fromkeys(first.sources + second.sources)),
        memberships=first.memberships or second.memberships,
        currency=first.currency,
        forced_block=first.forced_block
        or second.forced_block
        or ("BLOCKED_CONFLICT" if conflict else None),
    )


def _collect_candidates(engine: Engine, now: datetime) -> list[_RawCandidate]:
    day = now.astimezone(ET).date()
    core_buys, core_sells = _core_signals(engine, day)
    merged: dict[str, _RawCandidate] = {}
    for candidate in _index_candidates(engine, now) + core_buys:
        existing = merged.get(candidate.ticker)
        merged[candidate.ticker] = (
            _merge(existing, candidate) if existing is not None else candidate
        )
    output: list[_RawCandidate] = []
    for candidate in merged.values():
        if candidate.ticker in core_sells:
            candidate = dataclasses.replace(
                candidate, forced_block="BLOCKED_CONFLICT"
            )
        output.append(candidate)
    return output


def _force_block(plan: ExecutionPlan, reason: str) -> ExecutionPlan:
    return dataclasses.replace(
        plan,
        state=PlanState.BLOCKED,
        block_reason=reason,
        suggested_qty=None,
        suggested_notional=None,
    )


def _ai_explanation(
    engine: Engine,
    now: datetime,
    plans: list[ExecutionPlan],
    account_state: AccountState | None,
) -> str | None:
    cfg = engine.settings.ai_briefing
    if not cfg.enabled or not plans:
        return None
    account_label = (
        account_state.snapshot.source.upper() if account_state is not None else "UNAVAILABLE"
    )
    context = AIBriefingContext(
        as_of=now.isoformat(),
        output_mode="action_card",
        execution_plans=[
            {**plan_to_dict(plan), "account_label": account_label} for plan in plans
        ],
    )
    try:
        body = run_ai_briefing(cfg, context)
    except Exception as error:  # noqa: BLE001
        log.warning("execution_brief.ai_failed", error=str(error))
        return None
    return body.strip()[:300] if body else None


def run_daily(engine: Engine, now: datetime) -> None:
    cfg = engine.settings.execution_plan
    if not cfg.enabled:
        return
    account_state: AccountState | None = None
    if engine.account_provider is not None:
        try:
            account_state = engine.account_provider.snapshot(now)
        except Exception as error:  # noqa: BLE001
            log.warning("execution_brief.account_failed", error=str(error))
    if account_state is not None:
        engine.ledger.replace_account_state(account_state)

    plan_date = now.astimezone(ET).date()
    plans: list[ExecutionPlan] = []
    for raw in _collect_candidates(engine, now):
        candidate = PlanCandidate(
            ticker=raw.ticker,
            plan_date=plan_date,
            entry_low=raw.entry_low,
            entry_high=raw.entry_high,
            stop_loss=raw.stop_loss,
            take_profit=raw.take_profit,
            target_weight=raw.target_weight,
            score=raw.score,
            source_strategies=raw.sources,
            memberships=raw.memberships,
            quote_at=now,
            currency=raw.currency,
        )
        plan = build_plan(
            candidate,
            account_state.snapshot if account_state is not None else None,
            account_state.positions if account_state is not None else (),
            account_state.open_orders if account_state is not None else (),
            cfg,
            now,
            observed_positions=(
                account_state.observed_positions if account_state is not None else ()
            ),
        )
        if raw.forced_block is not None:
            plan = _force_block(plan, raw.forced_block)
        plans.append(plan)
    if account_state is not None:
        plans = apply_portfolio_limits(
            plans,
            account_state.snapshot.equity,
            cfg,
            budget=portfolio_budget_from_state(account_state, cfg),
        )
    for plan in plans:
        engine.ledger.upsert_execution_plan(plan)
    ai_summary = _ai_explanation(engine, now, plans, account_state)
    engine.notifier.send(
        execution_plan_card(account_state, plans, now, ai_summary=ai_summary)
    )
    log.info(
        "execution_brief.done",
        plans=len(plans),
        actionable_candidates=sum(
            1 for plan in plans if plan.state is PlanState.CANDIDATE
        ),
    )


def _trend_long(engine: Engine, ticker: str, now: datetime) -> bool:
    """日线趋势近似: 收盘 >= MA(ma_days)*(1-buffer)。趋势闸门关闭或数据不足时放行。"""
    cfg = engine.trend_gate_cfg
    if cfg is None:
        return True
    try:
        bars = engine.store.read_daily_bars([ticker], start=now - timedelta(days=400))
        closes = bars.xs(ticker, level="ticker")["close"].dropna()
    except Exception:  # noqa: BLE001
        return True
    if len(closes) < cfg.ma_days:
        return True
    ma = float(closes.tail(cfg.ma_days).mean())
    return float(closes.iloc[-1]) >= ma * (1 - cfg.buffer)


def _complete_bars(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    cutoff = now - timedelta(minutes=5)
    return bars[bars.index.get_level_values("ts") <= cutoff]


def _deliver_plan_events(engine: Engine, now: datetime) -> int:
    delivered = 0
    for event in engine.ledger.due_plan_events(now):
        card = event["card"]
        if engine.notifier.send(card):  # type: ignore[arg-type]
            engine.ledger.mark_plan_event_sent(str(event["event_key"]), now=now)
            delivered += 1
        else:
            engine.ledger.mark_plan_event_failed(
                str(event["event_key"]),
                "notifier returned false",
                now=now,
                retry_at=now + timedelta(minutes=1),
            )
    return delivered


def run_watch(engine: Engine, now: datetime) -> None:
    cfg = engine.settings.execution_plan
    if not cfg.enabled:
        return
    events = _deliver_plan_events(engine, now)
    plans = engine.ledger.active_execution_plans()
    if not plans:
        return
    tickers = sorted({plan.ticker for plan in plans})
    try:
        bars = engine.source.fetch_intraday_bars(tickers, lookback_days=1)
    except Exception as error:  # noqa: BLE001
        log.warning("execution_watch.bars_failed", error=str(error))
        raise

    position_qty: dict[str, float] = {}
    open_buy_tickers: set[str] = set()
    if engine.account_provider is not None:
        try:
            account_state = engine.account_provider.snapshot(now)
            engine.ledger.replace_account_state(account_state)
            for position in account_state.positions:
                position_qty[position.symbol] = float(position.qty)
            open_buy_tickers = {
                order.symbol
                for order in account_state.open_orders
                if order.side == "buy"
            }
        except Exception as error:  # noqa: BLE001
            log.warning("execution_watch.account_failed", error=str(error))

    for plan in plans:
        try:
            sub = bars.xs(plan.ticker, level="ticker").sort_index()
        except KeyError:
            continue
        if sub.empty:
            continue
        complete = _complete_bars(
            bars[bars.index.get_level_values("ticker") == plan.ticker], now
        )
        if complete.empty:
            continue
        last_complete = complete.iloc[-1]
        observation = PlanObservation(
            at=now,
            price=float(last_complete["close"]),
            bar_close=float(last_complete["close"]),
            bar_low=float(last_complete["low"]),
            bar_complete=True,
            trend_long=_trend_long(engine, plan.ticker, now),
            position_qty=position_qty.get(plan.ticker, 0.0),
            has_open_buy_order=plan.ticker in open_buy_tickers,
        )
        try:
            transition = advance_plan(plan, observation, cfg)
        except PlanTransitionError:
            continue
        if transition.state is not plan.state:
            engine.ledger.upsert_execution_plan(apply_transition(plan, transition))
        if transition.event is not None:
            engine.ledger.queue_plan_event(
                plan.plan_id,
                plan.plan_version,
                transition.event,
                plan_event_card(
                    plan, transition.event, price=observation.price, at=now
                ),
                now=now,
            )
    events += _deliver_plan_events(engine, now)
    log.info("execution_watch.done", plans=len(plans), events=events)
