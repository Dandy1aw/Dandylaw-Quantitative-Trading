from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import math
from typing import TYPE_CHECKING, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.account import AccountState, BrokerPosition, ObservedPosition
from quant_signal.ai_briefing import (
    USBriefingAIContext,
    run_ai_briefing,
    validate_us_briefing_output,
)
from quant_signal.calendar import is_trading_day, previous_trading_day, session_close_utc
from quant_signal.candidate_lanes import (
    Candidate,
    CandidateDiscovery,
    CandidateObservation,
    discover_candidates,
)
from quant_signal.execution import (
    ExecutionPlan,
    PlanCandidate,
    apply_portfolio_limits,
    build_plan,
    portfolio_budget_from_state,
)
from quant_signal.market_regime import RegimeSnapshot, classify_market_regime
from quant_signal.notifier.cards import us_briefing_card
from quant_signal.position_discipline import (
    PortfolioRiskSummary,
    PositionAdvice,
    PositionInput,
    advise_position,
    summarize_portfolio_risk,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine


log = structlog.get_logger()
ET = ZoneInfo("America/New_York")
ASIA_CONTEXT_SYMBOLS = ("^KS11", "^KQ11")


class BriefingMode(str, Enum):
    US_CLOSE = "US_CLOSE"
    ASIA_CONFIRM = "ASIA_CONFIRM"


def last_completed_us_session(now: datetime) -> date:
    now_utc = now.astimezone(timezone.utc)
    day = now_utc.astimezone(ET).date()
    close = session_close_utc(day) if is_trading_day(day) else None
    if close is not None and now_utc >= close.astimezone(timezone.utc):
        return day
    return previous_trading_day(day)


def _plain(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return _plain(dataclasses.asdict(value))  # type: ignore[arg-type]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _mapping(value: object) -> dict[str, object]:
    plain = _plain(value)
    if not isinstance(plain, dict):
        raise TypeError("expected mapping payload")
    return plain


def _nasdaq_members(snapshot: object) -> set[str]:
    raw_members = getattr(snapshot, "members", {})
    return {
        str(ticker)
        for ticker, member in raw_members.items()
        if "nasdaq100" in getattr(member, "memberships", ())
    }


def _load_daily_bars(
    engine: Engine, members: set[str], as_of: date
) -> pd.DataFrame:
    symbols = sorted(members | {engine.settings.us_briefing.market_regime.benchmark, "SKHY"})
    start = as_of - timedelta(days=420)
    end = as_of + timedelta(days=1)
    full_volume_fallback = bool(
        getattr(engine.source, "partial_market_volume", False)
    )
    daily_source = engine._intl_source if full_volume_fallback else engine.source
    source_name = "yfinance" if full_volume_fallback else engine.settings.data_source
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(symbols), 100):
        chunk = symbols[offset : offset + 100]
        try:
            fetched = daily_source.fetch_daily_bars(chunk, start, end)
        except Exception as error:  # noqa: BLE001
            log.warning("us_briefing.daily_chunk_failed", offset=offset, error=str(error))
            continue
        if not fetched.empty:
            frames.append(fetched)
    for frame in frames:
        engine.store.write_daily_bars(frame, source=source_name)
    end_time = datetime.combine(as_of, datetime.max.time(), tzinfo=timezone.utc)
    return engine.store.read_daily_bars(
        symbols,
        start=end_time - timedelta(days=420),
        end=end_time,
    )


def _bars_version(bars: pd.DataFrame, universe_hash: str, as_of: date) -> str:
    rows: list[str] = [universe_hash, as_of.isoformat()]
    if not bars.empty:
        latest = bars.reset_index().sort_values("ts").groupby("ticker").tail(1)
        for row in latest.sort_values("ticker").itertuples(index=False):
            rows.append(f"{row.ticker}|{row.ts}|{float(str(row.close)):.8f}")
    return sha256("\n".join(rows).encode("utf-8")).hexdigest()[:20]


def _account_version(account: AccountState | None) -> str:
    if account is None:
        return "account-unavailable"
    rows = [
        account.snapshot.retrieved_at.astimezone(timezone.utc).isoformat(),
        str(account.snapshot.equity),
        str(account.positions_partial),
    ]
    rows.extend(
        f"{row.symbol}|{row.qty}|{row.avg_entry_price}|{row.market_value}"
        for row in sorted(account.positions, key=lambda item: item.symbol)
    )
    rows.extend(
        f"{row.symbol}|{row.qty}|{row.avg_entry_price}|{row.current_price}|{row.pnl_pct}"
        for row in sorted(account.observed_positions, key=lambda item: item.symbol)
    )
    return sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _ticker_frame(bars: pd.DataFrame, ticker: str) -> pd.DataFrame:
    try:
        frame = cast(pd.DataFrame, bars.xs(ticker, level="ticker")).sort_index()
        return frame
    except (KeyError, TypeError):
        return pd.DataFrame()


def _latest_atr(bars: pd.DataFrame, ticker: str) -> Decimal | None:
    frame = _ticker_frame(bars, ticker)
    if len(frame) < 15:
        return None
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    value = float(true_range.tail(14).mean())
    return Decimal(str(value)) if math.isfinite(value) and value > 0 else None


def _broker_input(
    position: BrokerPosition, account: AccountState, bars: pd.DataFrame
) -> PositionInput | None:
    quantity = abs(position.qty)
    current: Decimal | None = None
    if quantity > 0:
        current = abs(position.market_value) / quantity
    if current is None or current <= 0:
        frame = _ticker_frame(bars, position.symbol)
        if not frame.empty:
            current = Decimal(str(float(frame["close"].iloc[-1])))
    if current is None or current <= 0:
        return None
    return PositionInput(
        ticker=position.symbol,
        current_price=current,
        quantity=quantity,
        avg_entry_price=position.avg_entry_price,
        market_value=abs(position.market_value),
        account_equity=account.snapshot.equity,
        atr=_latest_atr(bars, position.symbol),
        positions_complete=not account.positions_partial,
    )


def _observed_input(
    position: ObservedPosition, account: AccountState, bars: pd.DataFrame
) -> PositionInput | None:
    current = position.current_price
    if current is None and position.qty not in (None, Decimal("0")):
        value = position.market_value or position.estimated_market_value
        if value is not None:
            current = abs(value) / abs(position.qty or Decimal("1"))
    if current is None:
        frame = _ticker_frame(bars, position.symbol)
        if not frame.empty:
            current = Decimal(str(float(frame["close"].iloc[-1])))
    if current is None or current <= 0:
        return None
    return PositionInput(
        ticker=position.symbol,
        current_price=current,
        quantity=abs(position.qty) if position.qty is not None else None,
        avg_entry_price=position.avg_entry_price,
        pnl_pct=(
            position.pnl_pct / Decimal("100")
            if position.pnl_pct is not None
            else None
        ),
        market_value=position.market_value or position.estimated_market_value,
        account_equity=account.snapshot.equity,
        atr=_latest_atr(bars, position.symbol),
        positions_complete=not account.positions_partial,
    )


def _discipline(
    engine: Engine,
    account: AccountState | None,
    bars: pd.DataFrame,
    regime: RegimeSnapshot,
    now: datetime,
) -> tuple[list[PositionAdvice], PortfolioRiskSummary | None]:
    if account is None:
        return [], None
    inputs: list[PositionInput] = []
    if account.positions:
        inputs.extend(
            item
            for row in account.positions
            if (item := _broker_input(row, account, bars)) is not None
        )
    else:
        inputs.extend(
            item
            for row in account.observed_positions
            if (item := _observed_input(row, account, bars)) is not None
        )
    advice: list[PositionAdvice] = []
    for position in inputs:
        leverage = engine.settings.leverage_factor.get(position.ticker, 1.0)
        advice_item = advise_position(
            position,
            leverage=leverage,
            prior=engine.ledger.position_discipline_state(position.ticker),
            settings=engine.settings.us_briefing.position_discipline,
            regime=regime.regime,
        )
        advice.append(advice_item)
    summary = summarize_portfolio_risk(
        advice,
        equity=account.snapshot.equity,
        clusters=engine.settings.execution_plan.risk_clusters,
    )
    return advice, summary


def _upcoming_us_session(now: datetime) -> date:
    now_utc = now.astimezone(timezone.utc)
    day = now_utc.astimezone(ET).date()
    close = session_close_utc(day) if is_trading_day(day) else None
    if close is not None and now_utc < close.astimezone(timezone.utc):
        return day
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return day


def _size_candidates(
    engine: Engine,
    candidates: Sequence[Candidate],
    account: AccountState | None,
    now: datetime,
) -> tuple[list[dict[str, object]], list[ExecutionPlan]]:
    payloads = [_mapping(candidate) for candidate in candidates]
    if account is None:
        for payload in payloads:
            payload.update(
                {
                    "suggested_qty": None,
                    "suggested_notional": None,
                    "plan_state": "BLOCKED",
                    "block_reason": "NO_ACCOUNT",
                }
            )
        return payloads, []
    plan_date = _upcoming_us_session(now)
    plans = [
        build_plan(
            PlanCandidate(
                ticker=candidate.ticker,
                plan_date=plan_date,
                entry_low=candidate.entry_low,
                entry_high=candidate.entry_high,
                stop_loss=candidate.invalidation_price,
                take_profit=candidate.target_price,
                target_weight=None,
                score=candidate.score,
                source_strategies=(candidate.lane.value,),
                memberships=("nasdaq100",),
                quote_at=now,
                currency="USD",
            ),
            account.snapshot,
            account.positions,
            account.open_orders,
            engine.settings.execution_plan,
            now,
            observed_positions=account.observed_positions,
        )
        for candidate in candidates
    ]
    plans = apply_portfolio_limits(
        plans,
        account.snapshot.equity,
        engine.settings.execution_plan,
        budget=portfolio_budget_from_state(account, engine.settings.execution_plan),
    )
    for payload, plan in zip(payloads, plans):
        payload.update(
            {
                "suggested_qty": plan.suggested_qty,
                "suggested_notional": plan.suggested_notional,
                "plan_state": plan.state.value,
                "block_reason": plan.block_reason,
                "valid_session": plan.plan_date.isoformat(),
            }
        )
    return payloads, plans


def _skhy_observation(
    bars: pd.DataFrame, existing: Sequence[CandidateObservation]
) -> CandidateObservation | None:
    if any(item.ticker == "SKHY" for item in existing):
        return None
    frame = _ticker_frame(bars, "SKHY")
    days = (
        len(pd.to_numeric(frame["close"], errors="coerce").dropna())
        if not frame.empty and "close" in frame
        else 0
    )
    if days >= 201:
        return None
    price = float(frame["close"].iloc[-1]) if days else None
    return CandidateObservation("SKHY", "INSUFFICIENT_HISTORY", days, price)


def _asia_context(engine: Engine, now: datetime) -> tuple[dict[str, float], str]:
    try:
        bars = engine._intl_source.fetch_daily_bars(
            list(ASIA_CONTEXT_SYMBOLS), now.date() - timedelta(days=10), now.date() + timedelta(days=1)
        )
    except Exception as error:  # noqa: BLE001
        log.warning("us_briefing.asia_context_failed", error=str(error))
        return {}, "亚洲确认数据不可用；不据此推断方向"
    output: dict[str, float] = {}
    for ticker in ASIA_CONTEXT_SYMBOLS:
        frame = _ticker_frame(bars, ticker)
        close = (
            pd.to_numeric(frame["close"], errors="coerce").dropna()
            if not frame.empty and "close" in frame
            else pd.Series(dtype=float)
        )
        if len(close) >= 2:
            output[ticker] = float(close.iloc[-1] / close.iloc[-2] - 1.0)
    if not output:
        return {}, "亚洲确认数据不可用；不据此推断方向"
    text = "、".join(f"{ticker} {value:+.1%}" for ticker, value in output.items())
    return output, f"亚洲非交易上下文：{text}"


def _payloads(
    regime: RegimeSnapshot,
    discovery: CandidateDiscovery,
    advice: Sequence[PositionAdvice],
    risk: PortfolioRiskSummary | None,
) -> tuple[
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    dict[str, object],
    list[dict[str, object]],
]:
    regime_payload = _mapping(regime)
    candidate_payload = [_mapping(item) for item in discovery.candidates]
    discipline_payload = [_mapping(item) for item in advice]
    risk_payload = _mapping(risk) if risk is not None else {}
    observation_payload = [_mapping(item) for item in discovery.observations]
    return (
        regime_payload,
        candidate_payload,
        discipline_payload,
        risk_payload,
        observation_payload,
    )


def run(engine: Engine, now: datetime, mode: BriefingMode) -> None:
    settings = engine.settings.us_briefing
    if not settings.enabled:
        return
    provider = engine.index_universe_provider
    if provider is None:
        log.warning("us_briefing.skip", reason="index_universe_unavailable")
        return
    as_of = last_completed_us_session(now)
    try:
        universe = provider.load(now)
    except Exception as error:  # noqa: BLE001
        log.warning("us_briefing.skip", reason="index_universe_failed", error=str(error))
        return
    members = _nasdaq_members(universe)
    bars = _load_daily_bars(engine, members, as_of)
    regime = classify_market_regime(
        bars,
        members,
        as_of=as_of,
        settings=settings.market_regime,
        min_coverage=settings.min_coverage,
    )
    discovery = discover_candidates(
        bars,
        members,
        regime,
        as_of=as_of,
        settings=settings.candidate_lanes,
    )
    skhy = _skhy_observation(bars, discovery.observations)
    if skhy is not None:
        discovery = dataclasses.replace(
            discovery, observations=(*discovery.observations, skhy)
        )
    account: AccountState | None = None
    data_quality = [f"纳指100覆盖率 {regime.coverage:.1%}"]
    if engine.account_provider is not None:
        try:
            account = engine.account_provider.snapshot(now)
            engine.ledger.replace_account_state(account)
        except Exception as error:  # noqa: BLE001
            log.warning("us_briefing.account_failed", error=str(error))
    if account is None:
        data_quality.append("账户数据不可用；不输出精确卖出股数")
    elif account.positions_partial:
        data_quality.append("持仓截图不完整；仅按可见持仓和比例给建议")
    advice, risk = _discipline(engine, account, bars, regime, now)
    regime_payload, candidates, discipline, portfolio_risk, observations = _payloads(
        regime, discovery, advice, risk
    )
    candidates, execution_plans = _size_candidates(
        engine,
        discovery.candidates,
        account,
        now,
    )
    if mode == BriefingMode.ASIA_CONFIRM:
        asia, asia_quality = _asia_context(engine, now)
        regime_payload["asia_context"] = asia
        data_quality.append(asia_quality)
    universe_hash = str(getattr(universe, "content_hash", "unknown"))
    data_version = sha256(
        f"{_bars_version(bars, universe_hash, as_of)}|{_account_version(account)}".encode()
    ).hexdigest()[:20]
    report_kind = mode.value
    report_run = engine.ledger.begin_us_briefing_run(
        report_kind, as_of, data_version, now=now
    )
    if not report_run.created and report_run.status in {"DELIVERED", "SHADOWED"}:
        log.info("us_briefing.duplicate", run_id=report_run.run_id)
        return
    engine.ledger.save_market_regime_snapshot(report_kind, regime_payload, now=now)
    engine.ledger.replace_candidate_lane_snapshot(
        report_kind, as_of, candidates, now=now
    )
    ai_summary: str | None = None
    ai_context = USBriefingAIContext(
        report_kind=report_kind,  # type: ignore[arg-type]
        as_of=as_of.isoformat(),
        regime=regime_payload,
        candidates=candidates,
        discipline=discipline,
        portfolio_risk=portfolio_risk,
        observations=observations,
        data_quality=data_quality,
    )
    if engine.settings.ai_briefing.enabled:
        try:
            raw_ai = run_ai_briefing(engine.settings.ai_briefing, ai_context)
            ai_summary = (
                validate_us_briefing_output(raw_ai, ai_context) if raw_ai else None
            )
        except Exception as error:  # noqa: BLE001
            log.warning("us_briefing.ai_failed", error=str(error))
    card = us_briefing_card(
        report_kind=report_kind,
        as_of=as_of.isoformat(),
        regime=regime_payload,
        candidates=candidates,
        discipline=discipline,
        portfolio_risk=portfolio_risk,
        observations=observations,
        data_quality=data_quality,
        ai_summary=ai_summary,
    )
    delivered = False
    if settings.delivery_mode == "live":
        delivered = engine.notifier.send(card)
    shadowed = settings.delivery_mode == "shadow"
    if delivered:
        for plan in execution_plans:
            engine.ledger.upsert_execution_plan(plan)
        for item in advice:
            engine.ledger.save_position_discipline_state(item.next_state, now=now)
    payload: dict[str, object] = {
        "regime": regime_payload,
        "candidates": candidates,
        "discipline": discipline,
        "portfolio_risk": portfolio_risk,
        "observations": observations,
        "data_quality": data_quality,
        "card_title": card.title,
    }
    engine.ledger.complete_us_briefing_run(
        report_run.run_id,
        payload=payload,
        delivered=delivered,
        shadowed=shadowed,
        now=now,
    )
    log.info(
        "us_briefing.done",
        mode=report_kind,
        regime=regime.regime.value,
        candidates=len(candidates),
        positions=len(discipline),
        delivered=delivered,
    )
