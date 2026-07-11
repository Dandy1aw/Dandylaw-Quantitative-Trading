"""Durable option-flow scan, change detection, and notification pipeline."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import option_flow_card
from quant_signal.options_flow import (
    OptionFlowPolicy,
    detect_material_changes,
    top_by_side,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


class OptionFlowDataQualityError(RuntimeError):
    """The provider response is incomplete and must not produce an alert."""


def deliver(engine: "Engine", now: datetime) -> None:
    """Retry-only drain for evening jobs: no provider fetch, no new scans."""
    if now.tzinfo is None:
        raise ValueError("option flow delivery time must be timezone-aware")
    if not engine.settings.option_flow.enabled:
        return
    _deliver_option_alerts(engine, now)


def _deliver_option_alerts(engine: "Engine", now: datetime) -> None:
    """Drain only the dedicated option outbox; failed sends remain retryable.

    at-least-once 语义：send 成功但 mark_sent 前崩溃会在下轮重发一次，
    有 expires_at 兜底，属于有意取舍，勿改成 at-most-once。
    """
    for event in engine.ledger.due_option_flow_alerts(now):
        event_key = str(event["event_key"])
        card = event.get("card")
        if not isinstance(card, Card):
            engine.ledger.mark_option_flow_alert_failed(
                event_key,
                "INVALID_OPTION_FLOW_CARD",
                now=now,
                retry_at=now + timedelta(minutes=5),
            )
            log.error("option_flow.delivery_invalid", event_key=event_key)
            continue
        try:
            sent = engine.notifier.send(card)
            if not sent:
                raise RuntimeError("notifier returned false")
        except Exception as error:  # noqa: BLE001 - durable delivery boundary
            engine.ledger.mark_option_flow_alert_failed(
                event_key,
                str(error),
                now=now,
                retry_at=now + timedelta(minutes=5),
            )
            log.warning(
                "option_flow.delivery_failed",
                event_key=event_key,
                error=str(error),
            )
            continue
        engine.ledger.mark_option_flow_alert_sent(event_key, now=now)
        log.info("option_flow.delivered", event_key=event_key)


def _validate_snapshot(engine: "Engine", snapshot: object) -> None:
    from quant_signal.options_flow import OptionFlowSnapshot

    if not isinstance(snapshot, OptionFlowSnapshot):
        raise OptionFlowDataQualityError("option provider returned an invalid snapshot")
    cfg = engine.settings.option_flow
    if snapshot.venue_coverage < cfg.min_venue_coverage:
        raise OptionFlowDataQualityError(
            f"venue coverage {snapshot.venue_coverage:.0%} is below "
            f"{cfg.min_venue_coverage:.0%}"
        )
    for side in ("call", "put"):
        eligible = [
            row
            for row in top_by_side(snapshot, side, cfg.top_n)
            if row.volume >= cfg.min_volume
        ]
        if len(eligible) < cfg.top_n:
            raise OptionFlowDataQualityError(
                f"only {len(eligible)} eligible {side} contracts; "
                f"need {cfg.top_n}"
            )


def run(engine: "Engine", now: datetime, *, force_summary: bool = False) -> None:
    """Scan once and enqueue at most one baseline/change/close card."""
    if now.tzinfo is None:
        raise ValueError("option flow run time must be timezone-aware")
    cfg = engine.settings.option_flow
    if not cfg.enabled:
        return
    if engine.option_flow_source is None:
        raise RuntimeError("option flow is enabled but no source is configured")

    # Retrying old cards is independent from the new provider request.
    _deliver_option_alerts(engine, now)

    snapshot = engine.option_flow_source.fetch(now)
    _validate_snapshot(engine, snapshot)
    previous = engine.ledger.latest_option_flow_snapshot(snapshot.session_date)

    enrichment_available = engine.option_flow_enricher is not None
    if engine.option_flow_enricher is not None:
        displayed = (
            *top_by_side(snapshot, "call", cfg.top_n),
            *top_by_side(snapshot, "put", cfg.top_n),
        )
        try:
            enrichments = engine.option_flow_enricher.enrich(displayed, now)
            snapshot = replace(
                snapshot,
                rows=tuple(
                    replace(row, enrichment=enrichments[row.contract_symbol])
                    if row.contract_symbol in enrichments
                    else row
                    for row in snapshot.rows
                ),
            )
        except Exception as error:  # noqa: BLE001 - enrichment is optional
            enrichment_available = False
            log.warning("option_flow.enrichment_failed", error=str(error))

    policy = OptionFlowPolicy(
        top_n=cfg.top_n,
        rank_jump=cfg.rank_jump,
        surge_volume=cfg.surge_volume,
        zero_dte_surge_volume=cfg.zero_dte_surge_volume,
        etf_roots=frozenset(cfg.etf_roots),
    )
    changes = (
        detect_material_changes(previous, snapshot, policy)
        if previous is not None
        else ()
    )
    session = snapshot.session_date
    alert_count = engine.ledger.option_flow_alert_count(session)
    last_alert_at = engine.ledger.last_option_flow_alert_at(session)
    cooldown_ready = (
        last_alert_at is None
        or now >= last_alert_at + timedelta(minutes=cfg.cooldown_minutes)
    )

    phase = "quiet"
    should_alert = False
    if force_summary:
        phase = "close"
        should_alert = alert_count < cfg.max_alerts_per_day
    elif previous is None:
        phase = "baseline"
        should_alert = alert_count < cfg.max_alerts_per_day
    elif (
        changes
        and cooldown_ready
        and alert_count < cfg.max_alerts_per_day - 1
    ):
        phase = "change"
        should_alert = True

    card = (
        option_flow_card(
            snapshot,
            changes,
            phase,
            now,
            previous=previous,
            enrichment_available=enrichment_available,
        )
        if should_alert
        else None
    )
    expires_at = None
    if card is not None:
        expires_at = (
            now + timedelta(hours=cfg.closing_expiry_hours)
            if phase == "close"
            else now + timedelta(minutes=cfg.intraday_expiry_minutes)
        )
    created = engine.ledger.save_option_flow_scan(
        snapshot,
        phase,
        card,
        now=now,
        expires_at=expires_at,
    )
    _deliver_option_alerts(engine, now)
    log.info(
        "option_flow.completed",
        slot=snapshot.slot,
        phase=phase,
        scan_created=created,
        alert_queued=card is not None and created,
        changes=len(changes),
        venue_coverage=snapshot.venue_coverage,
        enrichment_available=enrichment_available,
    )
