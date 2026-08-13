from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quant_signal.holding_alert_rate_limit import (
    AlertDisposition,
    HoldingAlertDecision,
    PriorHoldingAlert,
    SuppressionReason,
    select_holding_alerts,
)
from quant_signal.strategies.base import Direction, Signal

NOW = datetime(2026, 8, 14, 14, 0, tzinfo=UTC)


def _signal(
    ticker: str,
    *,
    direction: Direction = Direction.BUY,
    severity: object = 1,
    strength: object = 1.0,
    window: str = "1m",
) -> Signal:
    return Signal(
        ticker=ticker,
        direction=direction,
        price=100.0,
        reason="price move",
        strategy_id="holding_price_alert",
        ts=NOW,
        extra={
            "severity": severity,
            "strength_score": strength,
            "window": window,
        },
    )


def _prior(
    ticker: str,
    *,
    direction: Direction = Direction.BUY,
    severity: int = 1,
    strength: float = 1.0,
    minutes_ago: int = 1,
    kind: AlertDisposition = AlertDisposition.FIRST,
) -> PriorHoldingAlert:
    return PriorHoldingAlert(
        ticker=ticker,
        direction=direction,
        severity=severity,
        strength_score=strength,
        pushed_at=NOW - timedelta(minutes=minutes_ago),
        alert_kind=kind,
    )


def _by_ticker(
    decisions: tuple[HoldingAlertDecision, ...],
) -> dict[str, HoldingAlertDecision]:
    return {decision.signal.ticker: decision for decision in decisions}


def test_first_threshold_crossing_is_sendable() -> None:
    decision = select_holding_alerts([_signal("AAA")], [])[0]

    assert decision.should_send is True
    assert decision.disposition is AlertDisposition.FIRST
    assert decision.suppression_reason is None
    assert decision.ticker_alert_number == 1


def test_detection_window_change_alone_is_not_an_upgrade() -> None:
    decision = select_holding_alerts(
        [_signal("AAA", severity=1, strength=1.4, window="15m")],
        [_prior("AAA", severity=1, strength=1.0)],
    )[0]

    assert decision.should_send is False
    assert decision.disposition is None
    assert decision.suppression_reason is SuppressionReason.NO_MEANINGFUL_UPGRADE
    assert decision.ticker_alert_number == 2


@pytest.mark.parametrize(
    ("severity", "strength"),
    [(1, 1.5), (2, 1.49)],
)
def test_same_direction_requires_both_higher_severity_and_1_5_strength(
    severity: int,
    strength: float,
) -> None:
    decision = select_holding_alerts(
        [_signal("AAA", severity=severity, strength=strength)],
        [_prior("AAA", severity=1)],
    )[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.NO_MEANINGFUL_UPGRADE


def test_same_direction_higher_severity_at_1_5_strength_is_upgrade() -> None:
    decision = select_holding_alerts(
        [_signal("AAA", severity=2, strength=1.5)],
        [_prior("AAA", severity=1)],
    )[0]

    assert decision.should_send is True
    assert decision.disposition is AlertDisposition.UPGRADE
    assert decision.ticker_alert_number == 2


def test_upgrade_must_beat_highest_severity_across_all_directions() -> None:
    decision = select_holding_alerts(
        [_signal("AAA", severity=2, strength=1.5)],
        [
            _prior(
                "AAA",
                direction=Direction.SELL,
                severity=3,
                minutes_ago=2,
                kind=AlertDisposition.REVERSAL,
            ),
            _prior("AAA", severity=1, minutes_ago=1),
        ],
        per_ticker_cap=3,
    )[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.NO_MEANINGFUL_UPGRADE


def test_opposite_to_latest_successful_direction_is_reversal() -> None:
    decision = select_holding_alerts(
        [_signal("AAA", direction=Direction.SELL, severity=1, strength=1.0)],
        [_prior("AAA", direction=Direction.BUY, severity=3, strength=2.0)],
    )[0]

    assert decision.should_send is True
    assert decision.disposition is AlertDisposition.REVERSAL
    assert decision.ticker_alert_number == 2


def test_below_threshold_candidate_cannot_be_first_or_reversal() -> None:
    first = select_holding_alerts([_signal("AAA", strength=0.99)], [])[0]
    reversal = select_holding_alerts(
        [_signal("BBB", direction=Direction.SELL, strength=0.99)],
        [_prior("BBB", direction=Direction.BUY)],
    )[0]

    assert first.suppression_reason is SuppressionReason.NO_MEANINGFUL_UPGRADE
    assert reversal.suppression_reason is SuppressionReason.NO_MEANINGFUL_UPGRADE


def test_third_ticker_alert_is_always_suppressed() -> None:
    decision = select_holding_alerts(
        [_signal("AAA", direction=Direction.BUY, severity=3, strength=3.0)],
        [
            _prior("AAA", direction=Direction.BUY, minutes_ago=2),
            _prior(
                "AAA",
                direction=Direction.SELL,
                minutes_ago=1,
                kind=AlertDisposition.REVERSAL,
            ),
        ],
    )[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.TICKER_DAILY_CAP
    assert decision.ticker_alert_number == 3


def test_first_four_global_slots_accept_first_alerts() -> None:
    decisions = select_holding_alerts(
        [_signal("DDD"), _signal("BBB"), _signal("AAA"), _signal("CCC")],
        [],
    )

    assert [decision.signal.ticker for decision in decisions] == [
        "AAA",
        "BBB",
        "CCC",
        "DDD",
    ]
    assert all(decision.should_send for decision in decisions)


def test_fifth_slot_rejects_an_ordinary_first_alert() -> None:
    history = [_prior(f"P{i}") for i in range(4)]
    decision = select_holding_alerts([_signal("NEW")], history)[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.GLOBAL_REGULAR_CAP


def test_fifth_slot_accepts_upgrade_then_daily_cap_suppresses_remainder() -> None:
    history = [
        _prior("UP", severity=1),
        _prior("P1"),
        _prior("P2"),
        _prior("P3"),
    ]
    decisions = select_holding_alerts(
        [
            _signal("NEW", severity=3, strength=3.0),
            _signal("UP", severity=2, strength=1.5),
        ],
        history,
    )

    assert [decision.signal.ticker for decision in decisions] == ["UP", "NEW"]
    assert decisions[0].disposition is AlertDisposition.UPGRADE
    assert decisions[0].should_send is True
    assert decisions[1].suppression_reason is SuppressionReason.GLOBAL_DAILY_CAP


def test_daily_cap_suppresses_every_kind() -> None:
    history = [
        _prior("UP", severity=1),
        _prior("REV", direction=Direction.BUY),
        _prior("P1"),
        _prior("P2"),
        _prior("P3"),
    ]
    decisions = select_holding_alerts(
        [
            _signal("UP", severity=2, strength=1.5),
            _signal("REV", direction=Direction.SELL),
            _signal("NEW"),
        ],
        history,
    )

    assert all(not decision.should_send for decision in decisions)
    assert all(
        decision.suppression_reason is SuppressionReason.GLOBAL_DAILY_CAP
        for decision in decisions
    )


def test_batch_order_is_kind_then_strength_then_ticker() -> None:
    history = [
        _prior("REV-A", direction=Direction.BUY),
        _prior("REV-B", direction=Direction.BUY),
        _prior("UP-A", severity=1),
        _prior("UP-B", severity=1),
    ]
    candidates = [
        _signal("FIRST", strength=3.0),
        _signal("UP-B", severity=2, strength=2.0),
        _signal("REV-B", direction=Direction.SELL, strength=1.0),
        _signal("UP-A", severity=2, strength=2.0),
        _signal("REV-A", direction=Direction.SELL, strength=2.0),
    ]

    decisions = select_holding_alerts(
        candidates,
        history,
        daily_cap=20,
        regular_slots=20,
        per_ticker_cap=3,
    )

    assert [decision.signal.ticker for decision in decisions] == [
        "REV-A",
        "REV-B",
        "UP-A",
        "UP-B",
        "FIRST",
    ]


def test_selector_returns_a_decision_for_every_candidate() -> None:
    candidates = [_signal("A"), _signal("B"), _signal("C")]

    decisions = select_holding_alerts(candidates, [], daily_cap=1, regular_slots=1)

    assert len(decisions) == len(candidates)
    assert set(_by_ticker(decisions)) == {"A", "B", "C"}


def test_failed_delivery_can_be_removed_and_selection_retried_without_using_slot() -> (
    None
):
    history = [_prior("P1"), _prior("P2"), _prior("P3")]
    first_pass = select_holding_alerts(
        [_signal("A", strength=2.0), _signal("B", strength=1.0)],
        history,
    )
    assert first_pass[0].signal.ticker == "A"
    assert first_pass[0].should_send is True
    assert first_pass[1].suppression_reason is SuppressionReason.GLOBAL_REGULAR_CAP

    after_a_failed = select_holding_alerts([_signal("B", strength=1.0)], history)

    assert after_a_failed[0].should_send is True


def test_successful_decision_converts_to_immutable_prior_for_next_selection() -> None:
    decision = select_holding_alerts([_signal("AAA")], [])[0]

    prior = decision.as_prior(NOW)

    assert prior == PriorHoldingAlert(
        ticker="AAA",
        direction=Direction.BUY,
        severity=1,
        strength_score=1.0,
        pushed_at=NOW,
        alert_kind=AlertDisposition.FIRST,
    )


@pytest.mark.parametrize("strength", [float("nan"), float("inf"), -1.0, 0.0, True])
def test_invalid_candidate_strength_fails_closed(strength: object) -> None:
    decision = select_holding_alerts([_signal("AAA", strength=strength)], [])[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.INVALID_CANDIDATE


@pytest.mark.parametrize("severity", [0, 4, 1.5, True, "2"])
def test_invalid_candidate_severity_fails_closed(severity: object) -> None:
    decision = select_holding_alerts([_signal("AAA", severity=severity)], [])[0]

    assert decision.should_send is False
    assert decision.suppression_reason is SuppressionReason.INVALID_CANDIDATE


@pytest.mark.parametrize(
    ("severity", "strength"),
    [(0, 1.0), (4, 1.0), (1, float("nan")), (1, 0.0)],
)
def test_malformed_prior_history_is_rejected(severity: int, strength: float) -> None:
    with pytest.raises(ValueError):
        PriorHoldingAlert(
            ticker="AAA",
            direction=Direction.BUY,
            severity=severity,
            strength_score=strength,
            pushed_at=NOW,
            alert_kind=AlertDisposition.FIRST,
        )


@pytest.mark.parametrize(
    ("regular_slots", "daily_cap", "per_ticker_cap"),
    [(-1, 5, 2), (6, 5, 2), (4, 0, 2), (4, 5, 0)],
)
def test_invalid_quota_configuration_is_rejected(
    regular_slots: int,
    daily_cap: int,
    per_ticker_cap: int,
) -> None:
    with pytest.raises(ValueError):
        select_holding_alerts(
            [_signal("AAA")],
            [],
            regular_slots=regular_slots,
            daily_cap=daily_cap,
            per_ticker_cap=per_ticker_cap,
        )
