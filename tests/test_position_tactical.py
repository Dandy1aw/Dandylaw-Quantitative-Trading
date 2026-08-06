from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import pandas as pd

from quant_signal.position_tactical import (
    OpexContext,
    OptionStructure,
    PositionTacticalPolicy,
    WeeklyTechnical,
    classify_confluence,
    compute_opex_context,
    compute_option_structure,
    compute_weekly_technical,
)


def _weekly_daily_frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-02", periods=len(closes), freq="W-FRI", tz="UTC")
    return pd.DataFrame(
        {
            "open": [value + 1.0 for value in closes],
            "high": [value + 2.0 for value in closes],
            "low": [value - 2.0 for value in closes],
            "close": closes,
            "volume": 1_000_000,
        },
        index=index,
    )


def test_weekly_technical_detects_oversold_lower_band_setup() -> None:
    daily = _weekly_daily_frame([100.0] * 23 + [80.0])

    technical = compute_weekly_technical(daily, as_of=date(2026, 6, 12))

    assert technical.weekly_rsi is not None
    assert technical.weekly_rsi <= 35
    assert technical.bollinger_lower is not None
    assert technical.bollinger_mid is not None
    assert technical.bollinger_upper is not None
    assert (
        technical.bollinger_lower
        < technical.bollinger_mid
        < technical.bollinger_upper
    )
    assert technical.weekly_atr is not None and technical.weekly_atr > 0
    assert technical.state == "OVERSOLD_AT_LOWER_BAND"
    assert technical.history_weeks == 24


def test_weekly_technical_fails_closed_with_short_history() -> None:
    daily = _weekly_daily_frame([100.0] * 20)

    technical = compute_weekly_technical(daily, as_of=date(2026, 5, 15))

    assert technical.state == "DATA_INSUFFICIENT"
    assert technical.weekly_rsi is None
    assert technical.bollinger_lower is None
    assert technical.bollinger_mid is None
    assert technical.bollinger_upper is None
    assert technical.weekly_atr is None
    assert technical.history_weeks == 20


@dataclass(frozen=True)
class _Contract:
    side: str
    expiration: date
    strike: Decimal
    open_interest: int | None
    bid: Decimal | None = Decimal("1.00")
    ask: Decimal | None = Decimal("1.20")
    gamma: float | None = 0.02


def _wall_contracts() -> tuple[_Contract, ...]:
    expiry = date(2026, 7, 17)
    return (
        _Contract("put", expiry, Decimal("95"), 6_000),
        _Contract("put", expiry, Decimal("100"), 1_000),
        _Contract("call", expiry, Decimal("100"), 1_000),
        _Contract("call", expiry, Decimal("105"), 6_000),
    )


def test_option_structure_computes_directional_walls_and_unsigned_gamma_pin() -> None:
    structure = compute_option_structure(
        _wall_contracts(),
        spot=Decimal("100"),
        session=date(2026, 7, 17),
        truncated=False,
        policy=PositionTacticalPolicy(),
    )

    assert structure.put_wall == Decimal("95")
    assert structure.call_wall == Decimal("105")
    assert structure.max_pain == Decimal("100")
    assert structure.reliability == "HIGH"
    assert structure.gamma_pin_score is not None
    assert structure.dealer_gamma_state == "UNKNOWN"


def test_option_structure_never_uses_wrong_side_of_spot_as_wall() -> None:
    expiry = date(2026, 7, 17)
    structure = compute_option_structure(
        (
            _Contract("put", expiry, Decimal("105"), 9_000),
            _Contract("call", expiry, Decimal("95"), 9_000),
        ),
        spot=Decimal("100"),
        session=date(2026, 7, 17),
        truncated=False,
        policy=PositionTacticalPolicy(),
    )

    assert structure.put_wall is None
    assert structure.call_wall is None
    assert structure.reliability == "LOW"


def test_truncated_or_missing_oi_chain_cannot_be_high_reliability() -> None:
    truncated = compute_option_structure(
        _wall_contracts(),
        spot=Decimal("100"),
        session=date(2026, 7, 17),
        truncated=True,
        policy=PositionTacticalPolicy(),
    )
    no_oi = compute_option_structure(
        tuple(
            _Contract(
                item.side,
                item.expiration,
                item.strike,
                None,
                gamma=item.gamma,
            )
            for item in _wall_contracts()
        ),
        spot=Decimal("100"),
        session=date(2026, 7, 17),
        truncated=False,
        policy=PositionTacticalPolicy(),
    )

    assert truncated.reliability != "HIGH"
    assert no_oi.reliability == "LOW"
    assert no_oi.put_wall is None and no_oi.call_wall is None


def test_opex_context_classifies_window_day_and_reset() -> None:
    window = compute_opex_context(date(2026, 7, 15))
    expiry = compute_opex_context(date(2026, 7, 17))
    reset = compute_opex_context(date(2026, 7, 20))

    assert window.state == "OPEX_WINDOW"
    assert window.trading_days_to_expiry == 2
    assert expiry.state == "OPEX_DAY"
    assert expiry.trading_days_to_expiry == 0
    assert reset.state == "POST_OPEX_RESET"


def _technical(
    *,
    state: str = "OVERSOLD_AT_LOWER_BAND",
    reversal: bool | None = True,
) -> WeeklyTechnical:
    return WeeklyTechnical(
        state=state,  # type: ignore[arg-type]
        weekly_rsi=28.0,
        bollinger_lower=80.0,
        bollinger_mid=110.0,
        bollinger_upper=140.0,
        middle_slope_4w=-0.01,
        weekly_atr=4.0,
        reversal_confirmed=reversal,
        latest_week_partial=False,
        recent_four_week_low=90.0,
        history_weeks=30,
    )


def _structure(
    *,
    reliability: str = "MEDIUM",
    put_buffer: float | None = 0.05,
) -> OptionStructure:
    return OptionStructure(
        expiry=date(2026, 8, 7),
        put_wall=Decimal("95"),
        call_wall=Decimal("105"),
        max_pain=Decimal("100"),
        put_buffer_pct=put_buffer,
        call_upside_pct=0.05,
        gamma_pin_score=0.4,
        dealer_gamma_state="UNKNOWN",
        reliability=reliability,  # type: ignore[arg-type]
        oi_coverage=0.75,
        quote_coverage=0.70,
        truncated=False,
    )


def test_confluence_classifies_all_risk_states_and_price_references() -> None:
    opex = OpexContext(date(2026, 8, 21), 10, "NORMAL")
    tactical = classify_confluence(
        "MU",
        Decimal("100"),
        _technical(),
        _structure(),
        opex,
    )

    assert tactical.conclusion == "TACTICAL_REBOUND"
    assert tactical.invalidation_price == Decimal("93.0000")
    assert tactical.target_1 == Decimal("105.0000")
    assert tactical.target_2 == Decimal("110.0000")

    assert classify_confluence(
        "MU", Decimal("100"), _technical(reversal=False), _structure(), opex
    ).conclusion == "WAIT_REVERSAL_CONFIRMATION"
    assert classify_confluence(
        "MU",
        Decimal("100"),
        _technical(),
        _structure(put_buffer=0.005),
        opex,
    ).conclusion == "PUT_WALL_BREAK_RISK"
    assert classify_confluence(
        "MU",
        Decimal("100"),
        _technical(state="DOWNTREND_CONTINUATION_RISK"),
        _structure(),
        opex,
    ).conclusion == "FALLING_KNIFE_RISK"
    assert classify_confluence(
        "MU",
        Decimal("100"),
        _technical(),
        _structure(reliability="LOW"),
        opex,
    ).conclusion == "TECHNICAL_ONLY"
    assert classify_confluence(
        "MU",
        Decimal("100"),
        _technical(state="DATA_INSUFFICIENT", reversal=None),
        _structure(reliability="LOW"),
        opex,
    ).conclusion == "DATA_INSUFFICIENT"


def test_low_reliability_uses_technical_invalidation() -> None:
    tactical = classify_confluence(
        "MU",
        Decimal("100"),
        _technical(),
        replace(_structure(), reliability="LOW"),
        OpexContext(date(2026, 8, 21), 10, "NORMAL"),
    )

    assert tactical.invalidation_price == Decimal("88.0000")
