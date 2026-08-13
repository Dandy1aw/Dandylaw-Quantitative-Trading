"""Pure daily quota decisions for holding price alerts.

Only alerts confirmed as delivered belong in ``prior_alerts``.  A caller can
therefore retry selection after a delivery failure with the same history and
the remaining candidates; no quota is consumed until ``as_prior`` is appended
after a successful delivery.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Final, cast

from quant_signal.strategies.base import Direction, Signal

_MIN_TRIGGER_STRENGTH: Final = 1.0
_MIN_UPGRADE_STRENGTH: Final = 1.5


class AlertDisposition(str, Enum):
    """Sendable alert kinds, in descending business priority."""

    FIRST = "FIRST"
    UPGRADE = "UPGRADE"
    REVERSAL = "REVERSAL"


class SuppressionReason(str, Enum):
    """Stable audit values for alerts that must not be sent."""

    TICKER_DAILY_CAP = "TICKER_DAILY_CAP"
    NO_MEANINGFUL_UPGRADE = "NO_MEANINGFUL_UPGRADE"
    GLOBAL_REGULAR_CAP = "GLOBAL_REGULAR_CAP"
    GLOBAL_DAILY_CAP = "GLOBAL_DAILY_CAP"
    INVALID_CANDIDATE = "INVALID_CANDIDATE"


def _is_valid_severity(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3


def _is_valid_strength(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True)
class PriorHoldingAlert:
    """One successfully delivered alert in the current US trading day."""

    ticker: str
    direction: Direction
    severity: int
    strength_score: float
    pushed_at: datetime
    alert_kind: AlertDisposition

    def __post_init__(self) -> None:
        if not isinstance(self.ticker, str) or not self.ticker.strip():
            raise ValueError("PriorHoldingAlert.ticker must be non-empty")
        if self.direction not in (Direction.BUY, Direction.SELL):
            raise ValueError("PriorHoldingAlert.direction must be BUY or SELL")
        if not _is_valid_severity(self.severity):
            raise ValueError(
                "PriorHoldingAlert.severity must be an integer from 1 to 3"
            )
        if not _is_valid_strength(self.strength_score):
            raise ValueError(
                "PriorHoldingAlert.strength_score must be finite and positive"
            )
        if not isinstance(self.pushed_at, datetime) or not _is_aware(self.pushed_at):
            raise ValueError("PriorHoldingAlert.pushed_at must be timezone-aware")
        if not isinstance(self.alert_kind, AlertDisposition):
            raise TypeError("PriorHoldingAlert.alert_kind is invalid")


@dataclass(frozen=True)
class HoldingAlertDecision:
    """The send or suppression decision for exactly one input signal."""

    signal: Signal
    disposition: AlertDisposition | None
    should_send: bool
    suppression_reason: SuppressionReason | None
    ticker_alert_number: int

    def __post_init__(self) -> None:
        if self.ticker_alert_number < 1:
            raise ValueError("ticker_alert_number must be positive")
        if self.should_send:
            if self.disposition is None or self.suppression_reason is not None:
                raise ValueError(
                    "sendable decisions require a disposition and no suppression"
                )
        elif self.suppression_reason is None:
            raise ValueError("suppressed decisions require a suppression reason")

    def as_prior(self, pushed_at: datetime) -> PriorHoldingAlert:
        """Convert a successfully delivered decision into durable history."""
        if not self.should_send or self.disposition is None:
            raise ValueError(
                "only a successfully delivered sendable decision can become history"
            )
        fields = _candidate_fields(self.signal)
        if fields is None:
            raise ValueError("decision contains an invalid candidate")
        severity, strength = fields
        return PriorHoldingAlert(
            ticker=self.signal.ticker,
            direction=self.signal.direction,
            severity=severity,
            strength_score=strength,
            pushed_at=pushed_at,
            alert_kind=self.disposition,
        )


def _candidate_fields(signal: Signal) -> tuple[int, float] | None:
    if (
        not isinstance(signal.ticker, str)
        or not signal.ticker.strip()
        or signal.direction not in (Direction.BUY, Direction.SELL)
        or not isinstance(signal.extra, dict)
    ):
        return None
    severity = signal.extra.get("severity")
    strength = signal.extra.get("strength_score")
    if not _is_valid_severity(severity) or not _is_valid_strength(strength):
        return None
    return cast(int, severity), float(cast(int | float, strength))


def _history_for_ticker(
    history: Sequence[PriorHoldingAlert],
    ticker: str,
) -> list[PriorHoldingAlert]:
    return [alert for alert in history if alert.ticker == ticker]


def _classify(
    signal: Signal,
    history: Sequence[PriorHoldingAlert],
) -> tuple[AlertDisposition | None, SuppressionReason | None, int, float]:
    ticker_history = _history_for_ticker(history, signal.ticker)
    alert_number = len(ticker_history) + 1
    fields = _candidate_fields(signal)
    if fields is None:
        return None, SuppressionReason.INVALID_CANDIDATE, alert_number, 0.0
    severity, strength = fields
    if not ticker_history:
        if strength >= _MIN_TRIGGER_STRENGTH:
            return AlertDisposition.FIRST, None, alert_number, strength
        return None, SuppressionReason.NO_MEANINGFUL_UPGRADE, alert_number, strength

    latest = max(
        enumerate(ticker_history),
        key=lambda indexed: (indexed[1].pushed_at, indexed[0]),
    )[1]
    if signal.direction != latest.direction:
        if strength >= _MIN_TRIGGER_STRENGTH:
            return AlertDisposition.REVERSAL, None, alert_number, strength
        return None, SuppressionReason.NO_MEANINGFUL_UPGRADE, alert_number, strength

    highest_severity = max(alert.severity for alert in ticker_history)
    if severity > highest_severity and strength >= _MIN_UPGRADE_STRENGTH:
        return AlertDisposition.UPGRADE, None, alert_number, strength
    return None, SuppressionReason.NO_MEANINGFUL_UPGRADE, alert_number, strength


def _priority(disposition: AlertDisposition | None) -> int:
    return {
        AlertDisposition.REVERSAL: 0,
        AlertDisposition.UPGRADE: 1,
        AlertDisposition.FIRST: 2,
        None: 3,
    }[disposition]


def _validate_quotas(regular_slots: int, daily_cap: int, per_ticker_cap: int) -> None:
    values = (regular_slots, daily_cap, per_ticker_cap)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("quota values must be integers")
    if daily_cap < 1 or per_ticker_cap < 1 or regular_slots < 0:
        raise ValueError("quota values are out of range")
    if regular_slots > daily_cap:
        raise ValueError("regular_slots cannot exceed daily_cap")


def select_holding_alerts(
    candidates: Sequence[Signal],
    prior_alerts: Sequence[PriorHoldingAlert],
    *,
    regular_slots: int = 4,
    daily_cap: int = 5,
    per_ticker_cap: int = 2,
) -> tuple[HoldingAlertDecision, ...]:
    """Return one deterministic decision per candidate.

    Selection simulates successful delivery for quota allocation.  A caller
    that encounters a delivery failure should discard that candidate and call
    this function again with unchanged ``prior_alerts`` and the remaining
    candidates.  After success, append ``decision.as_prior(pushed_at)`` before
    selecting again.
    """
    _validate_quotas(regular_slots, daily_cap, per_ticker_cap)
    if any(not isinstance(alert, PriorHoldingAlert) for alert in prior_alerts):
        raise ValueError("prior_alerts must contain PriorHoldingAlert values")
    if any(not isinstance(signal, Signal) for signal in candidates):
        raise ValueError("candidates must contain Signal values")

    simulated_history = list(prior_alerts)
    remaining = list(enumerate(candidates))
    decisions: list[HoldingAlertDecision] = []

    while remaining:
        classified: list[
            tuple[
                int,
                Signal,
                AlertDisposition | None,
                SuppressionReason | None,
                int,
                float,
            ]
        ] = []
        for index, signal in remaining:
            disposition, reason, alert_number, strength = _classify(
                signal,
                simulated_history,
            )
            classified.append(
                (index, signal, disposition, reason, alert_number, strength)
            )

        classified.sort(
            key=lambda item: (
                _priority(item[2]),
                -item[5],
                item[1].ticker,
                item[0],
            )
        )
        index, signal, disposition, reason, alert_number, _ = classified[0]
        remaining = [item for item in remaining if item[0] != index]

        if reason is SuppressionReason.INVALID_CANDIDATE:
            decision = HoldingAlertDecision(signal, None, False, reason, alert_number)
        elif len(simulated_history) >= daily_cap:
            decision = HoldingAlertDecision(
                signal,
                None,
                False,
                SuppressionReason.GLOBAL_DAILY_CAP,
                alert_number,
            )
        elif alert_number > per_ticker_cap:
            decision = HoldingAlertDecision(
                signal,
                None,
                False,
                SuppressionReason.TICKER_DAILY_CAP,
                alert_number,
            )
        elif disposition is None:
            decision = HoldingAlertDecision(
                signal,
                None,
                False,
                reason or SuppressionReason.NO_MEANINGFUL_UPGRADE,
                alert_number,
            )
        elif (
            len(simulated_history) >= regular_slots
            and disposition is AlertDisposition.FIRST
        ):
            decision = HoldingAlertDecision(
                signal,
                None,
                False,
                SuppressionReason.GLOBAL_REGULAR_CAP,
                alert_number,
            )
        else:
            decision = HoldingAlertDecision(
                signal,
                disposition,
                True,
                None,
                alert_number,
            )
            simulated_history.append(decision.as_prior(signal.ts))
        decisions.append(decision)

    return tuple(decisions)
