from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import Protocol, cast

import numpy as np
import pandas as pd
import pytest

from quant_signal.fear_dca import (
    ETFMetrics,
    FearInterpretation,
    FearMetrics,
    RecommendationDecision,
    calculate_etf_metrics,
    calculate_fear_metrics,
    interpret_fear_mas,
    recommend_qqqm,
    recommend_spy,
)


class MetricCalculator(Protocol):
    def __call__(self, closes: pd.Series[float]) -> FearMetrics | ETFMetrics: ...


class Recommender(Protocol):
    def __call__(
        self, fear_metrics: FearMetrics, etf_metrics: ETFMetrics
    ) -> RecommendationDecision: ...


def _fear(
    close: float,
    *,
    ma20: float | None = None,
    ma60: float | None = None,
    interpretation: FearInterpretation | None = None,
) -> FearMetrics:
    effective_ma20 = ma20 if ma20 is not None else close - 1.0
    effective_ma60 = ma60 if ma60 is not None else close - 2.0
    return FearMetrics(
        close=close,
        one_session_return=0.01,
        ma20=effective_ma20,
        ma60=effective_ma60,
        deviation_from_ma20=close / effective_ma20 - 1.0,
        deviation_from_ma60=close / effective_ma60 - 1.0,
        interpretation=interpretation or FearInterpretation.TREND_CONFIRMED,
    )


def _etf(*, five: float = 0.0, twenty: float = 0.0) -> ETFMetrics:
    return ETFMetrics(
        close=100.0,
        one_session_return=0.0,
        five_session_return=five,
        twenty_session_return=twenty,
    )


@pytest.mark.parametrize(
    ("vix", "expected"),
    [
        (24.999, 0.0),
        (25.0, 1.0),
        (29.999, 1.0),
        (30.0, 1.5),
        (39.999, 1.5),
        (40.0, 2.0),
        (49.999, 2.0),
        (50.0, 3.0),
        (80.0, 3.0),
    ],
)
def test_spy_vix_tier_boundaries(vix: float, expected: float) -> None:
    decision = recommend_spy(_fear(vix), _etf())

    assert decision.base_multiplier == expected
    assert decision.final_multiplier == expected


@pytest.mark.parametrize(
    ("vxn", "expected"),
    [
        (34.999, 0.0),
        (35.0, 1.0),
        (39.999, 1.0),
        (40.0, 1.5),
        (49.999, 1.5),
        (50.0, 2.0),
        (59.999, 2.0),
        (60.0, 3.0),
        (90.0, 3.0),
    ],
)
def test_qqqm_vxn_tier_boundaries(vxn: float, expected: float) -> None:
    decision = recommend_qqqm(_fear(vxn), _etf())

    assert decision.base_multiplier == expected
    assert decision.final_multiplier == expected


@pytest.mark.parametrize(
    ("five", "twenty", "expected_bonus"),
    [
        (-0.029999, -0.049999, 0.0),
        (-0.03, 0.0, 0.5),
        (0.0, -0.05, 0.5),
        (-0.03, -0.05, 0.5),
    ],
)
def test_spy_drawdown_bonus_is_inclusive_and_applied_once(
    five: float, twenty: float, expected_bonus: float
) -> None:
    decision = recommend_spy(_fear(30.0), _etf(five=five, twenty=twenty))

    assert decision.drawdown_bonus == expected_bonus
    assert decision.final_multiplier == 1.5 + expected_bonus


@pytest.mark.parametrize(
    ("five", "twenty", "expected_bonus"),
    [
        (-0.039999, -0.069999, 0.0),
        (-0.04, 0.0, 0.5),
        (0.0, -0.07, 0.5),
        (-0.04, -0.07, 0.5),
    ],
)
def test_qqqm_drawdown_bonus_is_inclusive_and_applied_once(
    five: float, twenty: float, expected_bonus: float
) -> None:
    decision = recommend_qqqm(_fear(40.0), _etf(five=five, twenty=twenty))

    assert decision.drawdown_bonus == expected_bonus
    assert decision.final_multiplier == 1.5 + expected_bonus


@pytest.mark.parametrize(
    ("recommend", "fear_close", "drawdown"),
    [
        (recommend_spy, 50.0, -0.03),
        (recommend_qqqm, 60.0, -0.04),
    ],
)
def test_drawdown_bonus_cannot_exceed_three_times(
    recommend: Recommender, fear_close: float, drawdown: float
) -> None:
    decision = recommend(_fear(fear_close), _etf(five=drawdown))

    assert decision.base_multiplier == 3.0
    assert decision.drawdown_bonus == 0.5
    assert decision.final_multiplier == 3.0
    assert "capped at 3x" in decision.reason


@pytest.mark.parametrize(
    ("recommend", "fear_close", "five", "twenty"),
    [
        (recommend_spy, 24.999, -0.20, -0.30),
        (recommend_qqqm, 34.999, -0.20, -0.30),
    ],
)
def test_zero_times_means_no_extra_fear_buy_not_cancel_regular_dca(
    recommend: Recommender, fear_close: float, five: float, twenty: float
) -> None:
    decision = recommend(_fear(fear_close), _etf(five=five, twenty=twenty))

    assert decision == RecommendationDecision(
        base_multiplier=0.0,
        drawdown_bonus=0.0,
        final_multiplier=0.0,
        reason="Fear threshold not reached: no extra fear buy; regular DCA continues.",
    )


@pytest.mark.parametrize(
    ("close", "ma20", "ma60", "expected"),
    [
        (30.0, 29.0, 31.0, FearInterpretation.SHORT_TERM_WARMING),
        (32.0, 29.0, 31.0, FearInterpretation.TREND_CONFIRMED),
        (28.0, 29.0, 31.0, FearInterpretation.FEAR_FALLING),
        (30.0, 31.0, 29.0, FearInterpretation.DIVERGENT),
        (30.0, 30.0, 29.0, FearInterpretation.DIVERGENT),
    ],
)
def test_fear_ma_interpretation(
    close: float,
    ma20: float,
    ma60: float,
    expected: FearInterpretation,
) -> None:
    assert interpret_fear_mas(close, ma20, ma60) == expected


def test_ma_interpretation_does_not_change_multiplier() -> None:
    falling = recommend_spy(
        _fear(
            30.0,
            ma20=31.0,
            ma60=32.0,
            interpretation=FearInterpretation.FEAR_FALLING,
        ),
        _etf(),
    )
    confirmed = recommend_spy(
        _fear(30.0, interpretation=FearInterpretation.TREND_CONFIRMED), _etf()
    )

    assert falling.final_multiplier == confirmed.final_multiplier == 1.5


def test_fear_metrics_use_exact_latest_windows() -> None:
    closes = pd.Series(
        np.arange(1.0, 62.0), index=pd.bdate_range("2026-01-01", periods=61)
    )

    metrics = calculate_fear_metrics(closes)

    assert metrics.close == 61.0
    assert metrics.one_session_return == pytest.approx(61.0 / 60.0 - 1.0)
    assert metrics.ma20 == pytest.approx(np.mean(np.arange(42.0, 62.0)))
    assert metrics.ma60 == pytest.approx(np.mean(np.arange(2.0, 62.0)))
    assert metrics.deviation_from_ma20 == pytest.approx(61.0 / metrics.ma20 - 1.0)
    assert metrics.deviation_from_ma60 == pytest.approx(61.0 / metrics.ma60 - 1.0)
    assert metrics.interpretation == FearInterpretation.TREND_CONFIRMED


def test_etf_returns_use_exact_one_five_and_twenty_session_offsets() -> None:
    closes = pd.Series(
        np.arange(100.0, 121.0), index=pd.bdate_range("2026-01-01", periods=21)
    )

    metrics = calculate_etf_metrics(closes)

    assert metrics.close == 120.0
    assert metrics.one_session_return == pytest.approx(120.0 / 119.0 - 1.0)
    assert metrics.five_session_return == pytest.approx(120.0 / 115.0 - 1.0)
    assert metrics.twenty_session_return == pytest.approx(120.0 / 100.0 - 1.0)


@pytest.mark.parametrize(
    ("calculator", "length", "message"),
    [
        (calculate_fear_metrics, 59, "fear closes requires at least 60 sessions"),
        (calculate_etf_metrics, 20, "ETF closes requires at least 21 sessions"),
    ],
)
def test_metric_windows_must_be_complete(
    calculator: MetricCalculator, length: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        calculator(pd.Series(np.arange(1.0, length + 1.0)))


@pytest.mark.parametrize("bad_value", [np.nan, np.inf, -np.inf, 0.0, -1.0])
@pytest.mark.parametrize("calculator", [calculate_fear_metrics, calculate_etf_metrics])
def test_metric_inputs_must_be_finite_positive(
    calculator: MetricCalculator, bad_value: float
) -> None:
    closes = pd.Series(np.arange(1.0, 62.0))
    closes.iloc[-10] = bad_value

    with pytest.raises(ValueError, match="finite positive numbers"):
        calculator(closes)


@pytest.mark.parametrize("calculator", [calculate_fear_metrics, calculate_etf_metrics])
@pytest.mark.parametrize(
    "index",
    [
        pd.Index([*range(60), 59]),
        pd.Index([*range(60), -1]),
    ],
    ids=["duplicate", "non-monotonic"],
)
def test_metric_inputs_require_unique_chronological_index(
    calculator: MetricCalculator, index: pd.Index[int]
) -> None:
    closes = pd.Series(np.arange(1.0, 62.0), index=index)

    with pytest.raises(
        ValueError, match="index must be unique and monotonically increasing"
    ):
        calculator(closes)


@pytest.mark.parametrize("recommend", [recommend_spy, recommend_qqqm])
@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(replace(_fear(40.0), close=np.nan), id="close"),
        pytest.param(
            replace(_fear(40.0), one_session_return=np.inf), id="one-session-return"
        ),
        pytest.param(replace(_fear(40.0), ma20=0.0), id="ma20"),
        pytest.param(replace(_fear(40.0), ma60=-np.inf), id="ma60"),
        pytest.param(
            replace(_fear(40.0), deviation_from_ma20=np.nan), id="ma20-deviation"
        ),
        pytest.param(
            replace(_fear(40.0), deviation_from_ma60=np.inf), id="ma60-deviation"
        ),
    ],
)
def test_recommendations_reject_nonfinite_or_nonpositive_fear_metrics(
    recommend: Recommender, malformed: FearMetrics
) -> None:
    with pytest.raises(ValueError, match="fear metrics"):
        recommend(malformed, _etf())


@pytest.mark.parametrize("recommend", [recommend_spy, recommend_qqqm])
@pytest.mark.parametrize(
    ("malformed", "field"),
    [
        (replace(_fear(40.0), deviation_from_ma20=0.123), "ma20"),
        (replace(_fear(40.0), deviation_from_ma60=0.123), "ma60"),
    ],
)
def test_recommendations_reject_inconsistent_ma_deviations(
    recommend: Recommender, malformed: FearMetrics, field: str
) -> None:
    with pytest.raises(ValueError, match=f"deviation_from_{field} is inconsistent"):
        recommend(malformed, _etf())


@pytest.mark.parametrize("recommend", [recommend_spy, recommend_qqqm])
def test_recommendations_reject_invalid_interpretation_type(
    recommend: Recommender,
) -> None:
    malformed = replace(
        _fear(40.0),
        interpretation=cast(FearInterpretation, "fear trend confirmed"),
    )

    with pytest.raises(TypeError, match="interpretation must be a FearInterpretation"):
        recommend(malformed, _etf())


@pytest.mark.parametrize("recommend", [recommend_spy, recommend_qqqm])
def test_recommendations_reject_interpretation_inconsistent_with_mas(
    recommend: Recommender,
) -> None:
    malformed = replace(_fear(40.0), interpretation=FearInterpretation.FEAR_FALLING)

    with pytest.raises(ValueError, match="interpretation is inconsistent"):
        recommend(malformed, _etf())


@pytest.mark.parametrize(
    ("value", "attribute"),
    [
        (_fear(30.0), "close"),
        (_etf(), "close"),
        (RecommendationDecision(1.0, 0.5, 1.5, "reason"), "reason"),
    ],
)
def test_metric_and_decision_value_objects_are_frozen(
    value: object, attribute: str
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, attribute, 999.0)
