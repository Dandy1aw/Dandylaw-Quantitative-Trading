from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.holding_alert_rate_limit import (
    AlertDisposition,
    HoldingAlertDecision,
    PriorHoldingAlert,
    SuppressionReason,
    select_holding_alerts,
)
from quant_signal.holding_price_alert import STRATEGY_ID, evaluate_holding_price_alerts
from quant_signal.notifier.cards import holding_price_alert_card
from quant_signal.price_move_research import PriceMoveCause, research_price_move_causes
from quant_signal.strategies.base import Direction, Signal

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")


def _monitored_positions(engine: Engine) -> list[dict[str, object]]:
    positions: list[dict[str, object]] = []
    for row in engine.ledger.active_observed_positions(exact_only=True):
        try:
            quantity = Decimal(str(row.get("qty", "0")))
        except (InvalidOperation, ValueError):
            continue
        if quantity > 0 and str(row.get("symbol", "")).strip():
            item = dict(row)
            item["monitor_origin"] = "holding"
            positions.append(item)
    held = {str(row["symbol"]).strip().upper() for row in positions}
    manual_loader = getattr(engine.ledger, "active_manual_monitors", None)
    manual = manual_loader() if callable(manual_loader) else []
    for ticker in manual:
        symbol = str(ticker).strip().upper()
        if symbol and symbol not in held:
            positions.append(
                {
                    "symbol": symbol,
                    "qty": None,
                    "avg_entry_price": None,
                    "monitor_origin": "manual",
                }
            )
    return positions


def _minute_fetcher(engine: Engine) -> Callable[[list[str], int], pd.DataFrame]:
    fetch = getattr(engine.source, "fetch_minute_bars", None)
    if not callable(fetch):
        fetch = getattr(engine._intl_source, "fetch_minute_bars", None)
    if not callable(fetch):
        raise TypeError("configured market data source has no 1-minute bar support")
    return cast(Callable[[list[str], int], pd.DataFrame], fetch)


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def _finite_absolute(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        normalized = abs(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def _prior_alerts_from_rows(
    rows: Sequence[Mapping[str, object]],
    fallback_time: datetime,
) -> tuple[list[PriorHoldingAlert], set[str], list[str]]:
    """Conservatively rebuild successful daily quota history.

    Every returned ledger row consumes a global slot.  Fields that are not
    needed for identity are replaced with the most restrictive valid values;
    an ambiguous direction or timestamp additionally blocks that ticker for
    the rest of the day because reversal/latest-direction cannot be proven.
    """
    if fallback_time.tzinfo is None or fallback_time.utcoffset() is None:
        raise ValueError("fallback_time must be timezone-aware")

    alerts: list[PriorHoldingAlert] = []
    blocked_tickers: set[str] = set()
    issues: list[str] = []
    for index, row in enumerate(rows):
        raw_ticker = row.get("ticker")
        ticker = str(raw_ticker).strip().upper() if raw_ticker is not None else ""
        if not ticker:
            ticker = f"__INVALID_HISTORY_{index:04d}"
            issues.append(f"row[{index}]:invalid_ticker")

        raw_direction = row.get("direction")
        try:
            direction = (
                raw_direction
                if isinstance(raw_direction, Direction)
                else Direction(str(raw_direction))
            )
            if direction not in (Direction.BUY, Direction.SELL):
                raise ValueError
        except (TypeError, ValueError):
            direction = Direction.BUY
            if not ticker.startswith("__INVALID_HISTORY_"):
                blocked_tickers.add(ticker)
            issues.append(f"row[{index}]:invalid_direction")

        raw_pushed_at = row.get("pushed_at")
        try:
            pushed_at = (
                raw_pushed_at
                if isinstance(raw_pushed_at, datetime)
                else datetime.fromisoformat(str(raw_pushed_at))
            )
            if pushed_at.tzinfo is None or pushed_at.utcoffset() is None:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            pushed_at = fallback_time + timedelta(microseconds=index)
            if not ticker.startswith("__INVALID_HISTORY_"):
                blocked_tickers.add(ticker)
            issues.append(f"row[{index}]:invalid_pushed_at")

        raw_extra = row.get("extra")
        extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
        extra_valid = row.get("extra_valid") is True
        if not extra_valid:
            issues.append(f"row[{index}]:invalid_extra")

        severity_raw = extra.get("severity") if extra_valid else None
        if (
            isinstance(severity_raw, int)
            and not isinstance(severity_raw, bool)
            and 1 <= severity_raw <= 3
        ):
            severity = severity_raw
        else:
            severity = 3
            issues.append(f"row[{index}]:conservative_severity")

        strength = (
            _finite_positive(extra.get("strength_score")) if extra_valid else None
        )
        if strength is None and extra_valid:
            move = _finite_absolute(extra.get("move_pct"))
            threshold = _finite_positive(extra.get("threshold_pct"))
            if move is not None and threshold is not None:
                try:
                    derived_strength = move / threshold
                except (OverflowError, ZeroDivisionError):
                    derived_strength = math.inf
                strength = (
                    derived_strength
                    if math.isfinite(derived_strength) and derived_strength > 0
                    else None
                )
        if strength is None or not math.isfinite(strength) or strength <= 0:
            strength = 1.5
            issues.append(f"row[{index}]:conservative_strength")

        raw_kind = extra.get("alert_kind") if extra_valid else None
        try:
            alert_kind = (
                AlertDisposition.FIRST
                if raw_kind is None
                else AlertDisposition(str(raw_kind))
            )
        except ValueError:
            alert_kind = AlertDisposition.FIRST
            issues.append(f"row[{index}]:invalid_alert_kind")

        alerts.append(
            PriorHoldingAlert(
                ticker=ticker,
                direction=direction,
                severity=severity,
                strength_score=strength,
                pushed_at=pushed_at,
                alert_kind=alert_kind,
            )
        )

    alerts.sort(key=lambda alert: alert.pushed_at)
    return alerts, blocked_tickers, issues


def _with_decision_audit(
    decision: HoldingAlertDecision,
    *,
    suppression_reason: SuppressionReason | None = None,
    send_error: str | None = None,
    history_issue: str | None = None,
) -> Signal:
    extra = dict(decision.signal.extra or {})
    extra["alert_kind"] = (
        decision.disposition.value if decision.disposition is not None else None
    )
    extra["ticker_alert_number"] = decision.ticker_alert_number
    if decision.severity is not None:
        extra["severity"] = decision.severity
    if decision.strength_score is not None:
        extra["strength_score"] = decision.strength_score
    if suppression_reason is not None:
        extra["suppression_reason"] = suppression_reason.value
    if send_error is not None:
        extra["send_error"] = send_error
    if history_issue is not None:
        extra["history_reconstruction_reason"] = history_issue
    return replace(decision.signal, extra=extra)


def _candidate_snapshot(signal: Signal) -> tuple[int | None, float | None]:
    extra = signal.extra
    if not isinstance(extra, dict):
        return None, None
    severity = extra.get("severity")
    strength = _finite_positive(extra.get("strength_score"))
    if (
        not isinstance(severity, int)
        or isinstance(severity, bool)
        or not 1 <= severity <= 3
        or strength is None
    ):
        return None, None
    return severity, strength


def _recent_news(
    engine: Engine,
    tickers: list[str],
    now: datetime,
    lookback_hours: int,
) -> dict[str, list[dict[str, object]]]:
    if engine.news_source is None or not tickers:
        return {}
    try:
        articles = engine.news_source.fetch(
            tickers,
            now - timedelta(hours=lookback_hours),
            now + timedelta(minutes=5),
        )
    except Exception as error:  # noqa: BLE001 - 新闻不能吞掉价格告警
        log.warning("holding_price_alert.news_failed", error=str(error))
        return {}
    output: dict[str, list[dict[str, object]]] = {ticker: [] for ticker in tickers}
    for article in articles:
        row: dict[str, object] = {
            "created_at": article.created_at.isoformat(),
            "headline": article.headline,
            "summary": article.summary,
            "source": article.source,
            "url": article.url,
        }
        for ticker in set(article.symbols) & set(tickers):
            output[ticker].append(row)
    return {ticker: rows[-5:] for ticker, rows in output.items() if rows}


def run(engine: Engine, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("holding price alert time must be timezone-aware")
    settings = engine.settings.holding_price_alert
    if not settings.enabled:
        return
    monitored = _monitored_positions(engine)
    holdings = [
        row for row in monitored if row.get("monitor_origin") == "holding"
    ]
    manuals = [row for row in monitored if row.get("monitor_origin") == "manual"]
    manual_capacity = max(0, settings.max_tickers - len(holdings))
    positions = holdings + manuals[:manual_capacity]
    if not positions:
        log.info("holding_price_alert.skip", reason="no_monitored_symbols")
        return

    tickers = sorted({str(row["symbol"]).strip().upper() for row in positions})
    bars = _minute_fetcher(engine)(tickers, settings.lookback_minutes)
    etf_symbols = frozenset(
        ticker
        for ticker in tickers
        if engine.settings.asset_type.get(ticker) == "ETF"
        or ticker in engine.settings.option_flow.etf_roots
    )
    evaluation = evaluate_holding_price_alerts(
        bars,
        positions,
        now,
        settings,
        etf_symbols,
    )
    signals = list(evaluation.signals)

    local_now = now.astimezone(_ET)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_midnight.astimezone(UTC)
    history_rows = engine.ledger.pushed_strategy_signals_since(
        STRATEGY_ID,
        day_start,
    )
    history, blocked_tickers, history_issues = _prior_alerts_from_rows(
        history_rows,
        day_start,
    )
    pushed = 0
    send_failed = 0
    pushed_kinds: Counter[str] = Counter()
    suppression_counts: Counter[str] = Counter()
    search_settings = settings.cause_search
    researched_signal_ids: set[int] = set()
    cause_cache: dict[int, PriceMoveCause] = {}
    remaining: list[Signal] = []
    history_count_by_ticker = Counter(alert.ticker for alert in history)
    for signal in signals:
        if signal.ticker not in blocked_tickers:
            remaining.append(signal)
            continue
        severity, strength = _candidate_snapshot(signal)
        decision = HoldingAlertDecision(
            signal=signal,
            disposition=None,
            should_send=False,
            suppression_reason=SuppressionReason.TICKER_DAILY_CAP,
            ticker_alert_number=history_count_by_ticker[signal.ticker] + 1,
            severity=severity,
            strength_score=strength,
        )
        audited = _with_decision_audit(
            decision,
            suppression_reason=SuppressionReason.TICKER_DAILY_CAP,
            history_issue="ambiguous_direction_or_time",
        )
        engine.ledger.insert(audited, pushed=False, now=now)
        suppression_counts[SuppressionReason.TICKER_DAILY_CAP.value] += 1

    while remaining:
        decisions = select_holding_alerts(
            remaining,
            history,
            regular_slots=settings.regular_alert_slots,
            daily_cap=settings.max_alerts_per_day,
            per_ticker_cap=settings.max_alerts_per_ticker_per_day,
            upgrade_score=settings.meaningful_upgrade_score,
        )
        newly_approved = [
            item.signal
            for item in decisions
            if item.should_send and id(item.signal) not in researched_signal_ids
        ]
        if newly_approved:
            researched_signal_ids.update(id(signal) for signal in newly_approved)
            seed_news = _recent_news(
                engine,
                [signal.ticker for signal in newly_approved],
                now,
                search_settings.lookback_hours,
            )
            try:
                researched_causes = research_price_move_causes(
                    newly_approved,
                    search_settings,
                    now=now,
                    seed_news=seed_news,
                )
            except Exception as error:  # noqa: BLE001 - 查因失败不能吞掉价格告警
                log.warning("holding_price_alert.research_failed", error=str(error))
                researched_causes = {}
            for signal in newly_approved:
                cause = researched_causes.get(signal.ticker)
                if cause is not None:
                    cause_cache[id(signal)] = cause

        decision = decisions[0]
        remaining.remove(decision.signal)
        if not decision.should_send:
            reason = decision.suppression_reason or SuppressionReason.INVALID_CANDIDATE
            audited = _with_decision_audit(decision, suppression_reason=reason)
            engine.ledger.insert(audited, pushed=False, now=now)
            suppression_counts[reason.value] += 1
            continue

        audited = _with_decision_audit(decision)
        cause = cause_cache.get(id(decision.signal))
        if cause is not None:
            extra = dict(audited.extra or {})
            extra["price_move_cause"] = cause.as_dict()
            audited = replace(audited, extra=extra)

        send_error: str | None = None
        try:
            delivered = engine.notifier.send(holding_price_alert_card(audited))
            if not delivered:
                send_error = "notifier returned false"
        except Exception as error:  # noqa: BLE001 - 失败事件必须写审计账本
            delivered = False
            send_error = f"{type(error).__name__}: {error}"
            log.warning(
                "holding_price_alert.send_failed",
                ticker=audited.ticker,
                error=send_error,
            )
        if send_error is not None:
            audited = _with_decision_audit(decision, send_error=send_error)
            if cause is not None:
                extra = dict(audited.extra or {})
                extra["price_move_cause"] = cause.as_dict()
                audited = replace(audited, extra=extra)
            send_failed += 1
        engine.ledger.insert(audited, pushed=delivered, now=now)
        if delivered:
            history.append(decision.as_prior(now))
            pushed += 1
            if decision.disposition is not None:
                pushed_kinds[decision.disposition.value] += 1

    log.info(
        "holding_price_alert.done",
        requested=evaluation.requested,
        available=evaluation.available,
        evaluated=evaluation.evaluated,
        stale=evaluation.stale,
        future=evaluation.future,
        insufficient=evaluation.insufficient,
        max_bar_age_seconds=(
            round(evaluation.max_bar_age_seconds, 1)
            if evaluation.max_bar_age_seconds is not None
            else None
        ),
        triggered=len(signals),
        pushed=pushed,
        first=pushed_kinds[AlertDisposition.FIRST.value],
        upgrade=pushed_kinds[AlertDisposition.UPGRADE.value],
        reversal=pushed_kinds[AlertDisposition.REVERSAL.value],
        suppressed=sum(suppression_counts.values()),
        suppression_reasons=dict(suppression_counts),
        send_failed=send_failed,
        history_reconstruction_issues=history_issues,
        feed=str(bars.attrs.get("feed", "unknown")),
    )
