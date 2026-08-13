"""Pure metrics and recommendation rules for fear-index DCA reminders."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


class FearInterpretation(str, Enum):
    """Human-readable interpretation of a fear index against its moving averages."""

    SHORT_TERM_WARMING = "short-term fear warming"
    TREND_CONFIRMED = "fear trend confirmed"
    FEAR_FALLING = "fear falling"
    DIVERGENT = "divergent fear environment"


@dataclass(frozen=True)
class FearMetrics:
    """Latest fear-index metrics calculated from completed sessions."""

    close: float
    one_session_return: float
    ma20: float
    ma60: float
    deviation_from_ma20: float
    deviation_from_ma60: float
    interpretation: FearInterpretation


@dataclass(frozen=True)
class ETFMetrics:
    """Latest ETF close and exact session-offset returns."""

    close: float
    one_session_return: float
    five_session_return: float
    twenty_session_return: float


@dataclass(frozen=True)
class RecommendationDecision:
    """Persistable breakdown of one fear-based DCA recommendation."""

    base_multiplier: float
    drawdown_bonus: float
    final_multiplier: float
    reason: str


def _validated_window(
    closes: pd.Series[float], *, sessions: int, label: str
) -> pd.Series[float]:
    """Return the required trailing close window or raise a clear validation error."""
    if len(closes) < sessions:
        raise ValueError(
            f"{label} requires at least {sessions} sessions; got {len(closes)}"
        )

    numeric = pd.to_numeric(closes.tail(sessions), errors="coerce").astype(float)
    values = numeric.to_numpy(dtype=float)
    if not bool(np.all(np.isfinite(values) & (values > 0.0))):
        raise ValueError(f"{label} must contain finite positive numbers")
    return numeric


def _validate_positive_values(label: str, *values: float) -> None:
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise ValueError(f"{label} must contain finite positive numbers")


def _validate_finite_values(label: str, *values: float) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError(f"{label} must contain finite numbers")


def interpret_fear_mas(close: float, ma20: float, ma60: float) -> FearInterpretation:
    """Interpret moving-average position without changing any DCA multiplier."""
    _validate_positive_values("fear close and moving averages", close, ma20, ma60)
    if close > ma20 and close > ma60:
        return FearInterpretation.TREND_CONFIRMED
    if close > ma20:
        return FearInterpretation.SHORT_TERM_WARMING
    if close < ma20 and close < ma60:
        return FearInterpretation.FEAR_FALLING
    return FearInterpretation.DIVERGENT


def calculate_fear_metrics(closes: pd.Series[float]) -> FearMetrics:
    """Calculate VIX/VXN metrics from the last 60 completed sessions."""
    window = _validated_window(closes, sessions=60, label="fear closes")
    close = float(window.iloc[-1])
    previous_close = float(window.iloc[-2])
    ma20 = float(window.tail(20).mean())
    ma60 = float(window.mean())
    return FearMetrics(
        close=close,
        one_session_return=close / previous_close - 1.0,
        ma20=ma20,
        ma60=ma60,
        deviation_from_ma20=close / ma20 - 1.0,
        deviation_from_ma60=close / ma60 - 1.0,
        interpretation=interpret_fear_mas(close, ma20, ma60),
    )


def calculate_etf_metrics(closes: pd.Series[float]) -> ETFMetrics:
    """Calculate exact 1-, 5-, and 20-session ETF close returns."""
    window = _validated_window(closes, sessions=21, label="ETF closes")
    close = float(window.iloc[-1])
    return ETFMetrics(
        close=close,
        one_session_return=close / float(window.iloc[-2]) - 1.0,
        five_session_return=close / float(window.iloc[-6]) - 1.0,
        twenty_session_return=close / float(window.iloc[-21]) - 1.0,
    )


def _tier(value: float, thresholds: tuple[float, float, float, float]) -> float:
    _validate_positive_values("fear close", value)
    first, second, third, fourth = thresholds
    if value < first:
        return 0.0
    if value < second:
        return 1.0
    if value < third:
        return 1.5
    if value < fourth:
        return 2.0
    return 3.0


def _recommend(
    *,
    fear_name: str,
    fear_close: float,
    etf_name: str,
    etf_metrics: ETFMetrics,
    thresholds: tuple[float, float, float, float],
    five_session_threshold: float,
    twenty_session_threshold: float,
) -> RecommendationDecision:
    _validate_finite_values(
        f"{etf_name} returns",
        etf_metrics.one_session_return,
        etf_metrics.five_session_return,
        etf_metrics.twenty_session_return,
    )
    _validate_positive_values(f"{etf_name} close", etf_metrics.close)
    base = _tier(fear_close, thresholds)
    if base == 0.0:
        return RecommendationDecision(
            base_multiplier=0.0,
            drawdown_bonus=0.0,
            final_multiplier=0.0,
            reason="Fear threshold not reached: no extra fear buy; regular DCA continues.",
        )

    five_triggered = etf_metrics.five_session_return <= five_session_threshold
    twenty_triggered = etf_metrics.twenty_session_return <= twenty_session_threshold
    bonus = 0.5 if five_triggered or twenty_triggered else 0.0
    final = min(3.0, base + bonus)

    reasons = [f"{fear_name} {fear_close:.2f} sets base {base:g}x"]
    if bonus:
        triggered_windows = []
        if five_triggered:
            triggered_windows.append("5-session")
        if twenty_triggered:
            triggered_windows.append("20-session")
        reasons.append(
            f"{etf_name} {' and '.join(triggered_windows)} drawdown adds 0.5x once"
        )
    else:
        reasons.append("no drawdown bonus")
    if base + bonus > 3.0:
        reasons.append("capped at 3x")
    reasons.append(f"final {final:g}x")
    return RecommendationDecision(
        base_multiplier=base,
        drawdown_bonus=bonus,
        final_multiplier=final,
        reason="; ".join(reasons) + ".",
    )


def recommend_spy(
    fear_metrics: FearMetrics, etf_metrics: ETFMetrics
) -> RecommendationDecision:
    """Recommend the SPY fear-buy multiplier from VIX and SPY metrics."""
    return _recommend(
        fear_name="VIX",
        fear_close=fear_metrics.close,
        etf_name="SPY",
        etf_metrics=etf_metrics,
        thresholds=(25.0, 30.0, 40.0, 50.0),
        five_session_threshold=-0.03,
        twenty_session_threshold=-0.05,
    )


def recommend_qqqm(
    fear_metrics: FearMetrics, etf_metrics: ETFMetrics
) -> RecommendationDecision:
    """Recommend the QQQM fear-buy multiplier from VXN and QQQM metrics."""
    return _recommend(
        fear_name="VXN",
        fear_close=fear_metrics.close,
        etf_name="QQQM",
        etf_metrics=etf_metrics,
        thresholds=(35.0, 40.0, 50.0, 60.0),
        five_session_threshold=-0.04,
        twenty_session_threshold=-0.07,
    )
