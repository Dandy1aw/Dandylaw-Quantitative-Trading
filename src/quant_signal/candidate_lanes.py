from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from quant_signal.config import CandidateLaneSettings
from quant_signal.market_regime import Regime, RegimeSnapshot


MINIMUM_LANE_HISTORY = 201


class CandidateLane(str, Enum):
    TREND_CONTINUATION = "TREND_CONTINUATION"
    TREND_PULLBACK = "TREND_PULLBACK"
    RANGE_REVERSION = "RANGE_REVERSION"


@dataclass(frozen=True)
class Candidate:
    ticker: str
    lane: CandidateLane
    score: float
    price: float
    entry_low: float
    entry_high: float
    invalidation_price: float
    target_price: float
    atr: float
    history_days: int
    reasons: tuple[str, ...]
    profit_targets: tuple[float, ...] = ()
    recommended_target_stage: int = 1
    recent_buying_notional: float = 0.0
    buying_pressure_score: float = 0.0
    buying_pressure_label: str = "偏弱"
    nearby_resistance: float | None = None


@dataclass(frozen=True)
class CandidateObservation:
    ticker: str
    reason: str
    history_days: int
    price: float | None = None


@dataclass(frozen=True)
class CandidateDiscovery:
    as_of: date
    regime: Regime
    candidates: tuple[Candidate, ...]
    observations: tuple[CandidateObservation, ...]
    expected_symbols: int
    ready_symbols: int
    coverage: float
    block_reason: str | None = None


ALLOWED_LANES: dict[Regime, frozenset[CandidateLane]] = {
    Regime.TREND: frozenset(
        {CandidateLane.TREND_CONTINUATION, CandidateLane.TREND_PULLBACK}
    ),
    Regime.PULLBACK: frozenset(
        {CandidateLane.TREND_PULLBACK, CandidateLane.RANGE_REVERSION}
    ),
    Regime.RANGE: frozenset({CandidateLane.RANGE_REVERSION}),
    Regime.RISK_OFF: frozenset(),
    Regime.UNKNOWN: frozenset(),
}


def _regime(value: Regime | RegimeSnapshot) -> Regime:
    return value.regime if isinstance(value, RegimeSnapshot) else value


def _end(value: date | datetime) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    if isinstance(value, datetime):
        return stamp
    return stamp + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)


def _history(bars: pd.DataFrame, ticker: str, end: pd.Timestamp) -> pd.DataFrame:
    if bars.empty or not isinstance(bars.index, pd.MultiIndex):
        return pd.DataFrame()
    try:
        frame = bars.xs(ticker, level="ticker").sort_index().copy()
    except KeyError:
        return pd.DataFrame()
    index = pd.DatetimeIndex(frame.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    frame.index = index
    if "close" not in frame:
        return pd.DataFrame()
    frame = frame.loc[index <= end]
    finite = np.isfinite(pd.to_numeric(frame["close"], errors="coerce"))
    filtered: pd.DataFrame = frame.loc[finite].copy()
    return filtered


def _atr(frame: pd.DataFrame, period: int = 14) -> float:
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    return float(true_range.tail(period).mean())


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff().tail(period)
    gains = delta.clip(lower=0).mean()
    losses = -delta.clip(upper=0).mean()
    if losses == 0:
        return 100.0
    strength = float(gains / losses)
    return 100.0 - 100.0 / (1.0 + strength)


def _buying_pressure(frame: pd.DataFrame) -> tuple[float, float, str, int]:
    """Estimate recent buying turnover from daily OHLCV; this is not trade flow."""
    recent = frame.tail(5)
    close = pd.to_numeric(frame["close"], errors="coerce")
    recent_close = close.tail(5)
    previous_close = close.shift(1).tail(5)
    high = pd.to_numeric(recent["high"], errors="coerce")
    low = pd.to_numeric(recent["low"], errors="coerce")
    volume = pd.to_numeric(recent["volume"], errors="coerce")
    turnover = recent_close * volume
    daily_range = (high - low).replace(0, np.nan)
    close_location = ((recent_close - low) / daily_range).clip(0, 1).fillna(0.5)
    up_day = recent_close > previous_close
    buy_weight = pd.Series(
        np.where(up_day, 0.5 + 0.5 * close_location, 0.25 * close_location),
        index=recent.index,
        dtype=float,
    )
    buying_notional = float((turnover * buy_weight).sum())
    baseline_turnover = float(
        (close.tail(20) * pd.to_numeric(frame["volume"], errors="coerce").tail(20)).mean()
        * len(recent)
    )
    score = (
        min(max(buying_notional / baseline_turnover, 0.0), 1.0)
        if math.isfinite(baseline_turnover) and baseline_turnover > 0
        else 0.0
    )
    if score >= 0.65:
        return buying_notional, score, "强", 3
    if score >= 0.40:
        return buying_notional, score, "中等", 2
    return buying_notional, score, "偏弱", 1


def _nearby_resistance(frame: pd.DataFrame, entry_high: float) -> float | None:
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    history = close.iloc[-125:-5]
    if len(history) < 3:
        return None
    swing_highs = history[(history >= history.shift(1)) & (history > history.shift(-1))]
    candidates = swing_highs[swing_highs >= entry_high * 1.02]
    if candidates.empty:
        return None
    return round(float(candidates.min()), 4)


def _levels(
    price: float,
    ma20: float,
    ma50: float,
    atr: float,
    lane: CandidateLane,
    settings: CandidateLaneSettings,
) -> tuple[float, float, float, float]:
    if lane == CandidateLane.TREND_CONTINUATION:
        entry_low = max(ma20, price - atr)
        entry_high = price
    elif lane == CandidateLane.TREND_PULLBACK:
        entry_low = max(ma50, price - atr * 0.5)
        entry_high = price + atr * 0.15
    else:
        entry_low = price
        entry_high = price + atr * 0.25
    entry_low = min(entry_low, entry_high)
    invalidation = min(entry_low - atr * settings.stop_atr_multiple, ma50 - atr * 0.25)
    invalidation = max(0.01, invalidation)
    target = entry_high * 1.05
    return tuple(round(value, 4) for value in (entry_low, entry_high, invalidation, target))  # type: ignore[return-value]


def _candidate(
    ticker: str,
    frame: pd.DataFrame,
    lane: CandidateLane,
    score: float,
    reasons: tuple[str, ...],
    settings: CandidateLaneSettings,
) -> Candidate | None:
    close = pd.to_numeric(frame["close"], errors="coerce").dropna()
    price = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    atr = _atr(frame)
    if not math.isfinite(atr) or atr <= 0:
        return None
    entry_low, entry_high, invalidation, target = _levels(
        price, ma20, ma50, atr, lane, settings
    )
    profit_targets = tuple(
        round(entry_high * (1.0 + stage * 0.05), 4) for stage in (1, 2, 3)
    )
    buying_notional, pressure_score, pressure_label, target_stage = _buying_pressure(
        frame
    )
    resistance = _nearby_resistance(frame, entry_high)
    if resistance is not None:
        stages_before_resistance = [
            stage
            for stage, target_value in enumerate(profit_targets, start=1)
            if target_value <= resistance
        ]
        resistance_stage = max(stages_before_resistance, default=1)
        target_stage = min(target_stage, resistance_stage)
    target = profit_targets[target_stage - 1]
    if not (0 < invalidation < entry_low <= entry_high < target):
        return None
    return Candidate(
        ticker=ticker,
        lane=lane,
        score=round(score, 8),
        price=round(price, 4),
        entry_low=entry_low,
        entry_high=entry_high,
        invalidation_price=invalidation,
        target_price=target,
        atr=round(atr, 4),
        history_days=len(close),
        reasons=reasons,
        profit_targets=profit_targets,
        recommended_target_stage=target_stage,
        recent_buying_notional=round(buying_notional, 2),
        buying_pressure_score=round(pressure_score, 4),
        buying_pressure_label=pressure_label,
        nearby_resistance=resistance,
    )


def discover_candidates(
    bars: pd.DataFrame,
    members: set[str] | frozenset[str],
    regime: Regime | RegimeSnapshot,
    *,
    as_of: date | datetime,
    settings: CandidateLaneSettings,
    earnings_blocked: set[str] | frozenset[str] = frozenset(),
    risk_clusters: Mapping[str, Sequence[str]] | None = None,
    holdings: set[str] | frozenset[str] | None = None,
) -> CandidateDiscovery:
    """Discover Nasdaq candidates; `holdings` is intentionally ignored by design."""
    del holdings
    current_regime = _regime(regime)
    as_of_day = as_of.date() if isinstance(as_of, datetime) else as_of
    allowed = ALLOWED_LANES[current_regime]
    if not allowed:
        reason = (
            "REGIME_RISK_OFF"
            if current_regime == Regime.RISK_OFF
            else "REGIME_DATA_INCOMPLETE"
        )
        return CandidateDiscovery(
            as_of=as_of_day,
            regime=current_regime,
            candidates=(),
            observations=(),
            expected_symbols=len(members),
            ready_symbols=0,
            coverage=0.0,
            block_reason=reason,
        )

    end = _end(as_of)
    observations: list[CandidateObservation] = []
    by_lane: dict[CandidateLane, list[Candidate]] = {lane: [] for lane in allowed}
    ready = 0
    for ticker in sorted(members):
        frame = _history(bars, ticker, end)
        close = pd.to_numeric(frame["close"], errors="coerce").dropna() if not frame.empty else pd.Series(dtype=float)
        price = float(close.iloc[-1]) if not close.empty else None
        if len(close) < MINIMUM_LANE_HISTORY:
            observations.append(
                CandidateObservation(ticker, "INSUFFICIENT_HISTORY", len(close), price)
            )
            continue
        ready += 1
        if ticker in earnings_blocked:
            observations.append(
                CandidateObservation(ticker, "EARNINGS_WINDOW", len(close), price)
            )
            continue
        assert price is not None
        dollar_volume = float(
            (close.tail(20) * pd.to_numeric(frame["volume"], errors="coerce").tail(20)).mean()
        )
        if price < settings.minimum_price or dollar_volume < settings.min_dollar_volume:
            observations.append(
                CandidateObservation(ticker, "LIQUIDITY_FILTER", len(close), price)
            )
            continue
        atr = _atr(frame)
        if not math.isfinite(atr) or atr <= 0:
            observations.append(
                CandidateObservation(ticker, "INVALID_VOLATILITY", len(close), price)
            )
            continue
        ma20 = float(close.tail(20).mean())
        ma50 = float(close.tail(50).mean())
        ma200 = float(close.tail(200).mean())
        ma200_previous = float(close.iloc[-221:-21].mean()) if len(close) >= 221 else float(close.iloc[:200].mean())
        return5 = price / float(close.iloc[-6]) - 1.0
        return20 = price / float(close.iloc[-21]) - 1.0
        return60 = price / float(close.iloc[-61]) - 1.0
        return120 = price / float(close.iloc[-121]) - 1.0
        distance20_atr = (price - ma20) / atr
        rsi = _rsi(close)
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        volume_ratio = float(volume.iloc[-1] / volume.tail(20).mean())

        if distance20_atr > settings.overheat_atr_multiple:
            observations.append(CandidateObservation(ticker, "OVERHEATED", len(close), price))
            continue

        long_trend = price > ma200 and ma50 > ma200 and ma200 >= ma200_previous * 0.995
        near_high = price >= float(close.tail(20).max()) * 0.97
        if CandidateLane.TREND_CONTINUATION in allowed and (
            long_trend
            and price > ma50
            and return20 > 0
            and return60 > 0
            and return120 > 0
            and near_high
        ):
            score = return20 * 0.25 + return60 * 0.45 + return120 * 0.30 + min(volume_ratio, 2.0) * 0.01
            row = _candidate(
                ticker,
                frame,
                CandidateLane.TREND_CONTINUATION,
                score,
                ("LONG_TREND", "RELATIVE_STRENGTH", "NOT_OVERHEATED"),
                settings,
            )
            if row is not None:
                by_lane[CandidateLane.TREND_CONTINUATION].append(row)
                continue

        controlled_pullback = (
            long_trend
            and price <= ma20
            and price >= ma50 - atr * settings.pullback_atr_multiple
            and return5 < 0
            and 20 <= rsi <= 60
        )
        if CandidateLane.TREND_PULLBACK in allowed and controlled_pullback:
            score = return120 * 0.5 + return60 * 0.25 + max(0.0, -return5) * 0.25
            row = _candidate(
                ticker,
                frame,
                CandidateLane.TREND_PULLBACK,
                score,
                ("LONG_TREND_INTACT", "CONTROLLED_PULLBACK"),
                settings,
            )
            if row is not None:
                by_lane[CandidateLane.TREND_PULLBACK].append(row)
                continue

        reversal_confirmed = len(close) >= 3 and close.iloc[-1] > close.iloc[-2] > close.iloc[-3]
        range_reversion = (
            price < ma20
            and distance20_atr <= -0.5
            and rsi <= 45
            and reversal_confirmed
        )
        if CandidateLane.RANGE_REVERSION in allowed and range_reversion:
            score = abs(distance20_atr) * 0.5 + (45 - rsi) / 100 + max(0.0, -return5)
            row = _candidate(
                ticker,
                frame,
                CandidateLane.RANGE_REVERSION,
                score,
                ("ATR_DEVIATION", "REVERSAL_CONFIRMED"),
                settings,
            )
            if row is not None:
                by_lane[CandidateLane.RANGE_REVERSION].append(row)

    selected: list[Candidate] = []
    seen: set[str] = set()
    cluster_by_ticker: dict[str, str] = {}
    for configured_cluster, symbols in (risk_clusters or {}).items():
        for symbol in symbols:
            cluster_by_ticker.setdefault(str(symbol).upper(), configured_cluster)
    cluster_counts: dict[str, int] = {}
    lane_order = (
        CandidateLane.TREND_CONTINUATION,
        CandidateLane.TREND_PULLBACK,
        CandidateLane.RANGE_REVERSION,
    )
    for lane in lane_order:
        ranked = sorted(by_lane.get(lane, []), key=lambda row: (-row.score, row.ticker))
        for row in ranked:
            if row.ticker in seen:
                continue
            cluster_name = cluster_by_ticker.get(row.ticker)
            if (
                cluster_name is not None
                and cluster_counts.get(cluster_name, 0)
                >= settings.max_candidates_per_cluster
            ):
                observations.append(
                    CandidateObservation(
                        row.ticker, "CLUSTER_CAP", row.history_days, row.price
                    )
                )
                continue
            selected.append(row)
            seen.add(row.ticker)
            if cluster_name is not None:
                cluster_counts[cluster_name] = cluster_counts.get(cluster_name, 0) + 1
            if sum(candidate.lane == lane for candidate in selected) >= settings.top_n_per_lane:
                break

    expected = len(members)
    return CandidateDiscovery(
        as_of=as_of_day,
        regime=current_regime,
        candidates=tuple(selected),
        observations=tuple(observations),
        expected_symbols=expected,
        ready_symbols=ready,
        coverage=round(ready / expected, 6) if expected else 0.0,
    )
