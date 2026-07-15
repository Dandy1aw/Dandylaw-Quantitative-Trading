from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import math
from typing import Iterable

import numpy as np
import pandas as pd

from quant_signal.config import MarketRegimeSettings


class Regime(str, Enum):
    UNKNOWN = "UNKNOWN"
    TREND = "TREND"
    PULLBACK = "PULLBACK"
    RANGE = "RANGE"
    RISK_OFF = "RISK_OFF"


@dataclass(frozen=True)
class RegimeSnapshot:
    as_of: date
    regime: Regime
    coverage: float
    ready_symbols: int
    expected_symbols: int
    benchmark_price: float | None
    benchmark_ma20: float | None
    benchmark_ma50: float | None
    benchmark_ma200: float | None
    benchmark_slope20: float | None
    benchmark_slope50: float | None
    breadth_above_20d: float | None
    breadth_above_50d: float | None
    breadth_above_200d: float | None
    participation_5d: float | None
    new_high_20d: float | None
    new_low_20d: float | None
    realized_volatility: float | None
    atr_pct: float | None
    reasons: tuple[str, ...]
    data_quality: str


def _as_utc_end(value: date | datetime) -> pd.Timestamp:
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
        return pd.DataFrame(columns=["high", "low", "close", "volume"])
    try:
        frame = bars.xs(ticker, level="ticker").sort_index()
    except KeyError:
        return pd.DataFrame(columns=bars.columns)
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    frame = frame.copy()
    frame.index = index
    frame = frame.loc[index <= end]
    if "close" not in frame.columns:
        return pd.DataFrame(columns=bars.columns)
    close = pd.to_numeric(frame["close"], errors="coerce")
    filtered: pd.DataFrame = frame.loc[np.isfinite(close)].copy()
    return filtered


def _mean(values: Iterable[bool]) -> float | None:
    rows = list(values)
    return float(sum(rows) / len(rows)) if rows else None


def _atr_pct(frame: pd.DataFrame, period: int) -> float | None:
    if len(frame) < period + 1:
        return None
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    previous = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous).abs(), (low - previous).abs()], axis=1
    ).max(axis=1)
    price = float(close.iloc[-1])
    value = float(true_range.tail(period).mean())
    if not math.isfinite(value) or price <= 0:
        return None
    return value / price


def classify_market_regime(
    bars: pd.DataFrame,
    members: set[str] | frozenset[str],
    *,
    as_of: date | datetime,
    settings: MarketRegimeSettings,
    min_coverage: float = 0.98,
) -> RegimeSnapshot:
    """Classify the market from complete daily bars without reading account state."""
    as_of_day = as_of.date() if isinstance(as_of, datetime) else as_of
    end = _as_utc_end(as_of)
    histories = {ticker: _history(bars, ticker, end) for ticker in sorted(members)}
    ready = {
        ticker: frame
        for ticker, frame in histories.items()
        if len(frame) >= settings.min_history_days
    }
    expected = len(members)
    coverage = len(ready) / expected if expected else 0.0
    benchmark = _history(bars, settings.benchmark, end)

    empty = RegimeSnapshot(
        as_of=as_of_day,
        regime=Regime.UNKNOWN,
        coverage=round(coverage, 6),
        ready_symbols=len(ready),
        expected_symbols=expected,
        benchmark_price=None,
        benchmark_ma20=None,
        benchmark_ma50=None,
        benchmark_ma200=None,
        benchmark_slope20=None,
        benchmark_slope50=None,
        breadth_above_20d=None,
        breadth_above_50d=None,
        breadth_above_200d=None,
        participation_5d=None,
        new_high_20d=None,
        new_low_20d=None,
        realized_volatility=None,
        atr_pct=None,
        reasons=("COVERAGE_BELOW_MINIMUM",),
        data_quality="INCOMPLETE",
    )
    if expected == 0 or coverage < min_coverage:
        return empty
    if len(benchmark) < settings.min_history_days:
        return RegimeSnapshot(
            **{
                **empty.__dict__,
                "reasons": ("BENCHMARK_HISTORY_INSUFFICIENT",),
            }
        )

    benchmark_close = pd.to_numeric(benchmark["close"], errors="coerce").dropna()
    price = float(benchmark_close.iloc[-1])
    ma20 = float(benchmark_close.tail(20).mean())
    ma50 = float(benchmark_close.tail(50).mean())
    ma200 = float(benchmark_close.tail(200).mean())
    slope20 = ma20 / float(benchmark_close.iloc[-21:-1].mean()) - 1.0
    slope50 = ma50 / float(benchmark_close.iloc[-51:-1].mean()) - 1.0

    def closes(frame: pd.DataFrame) -> pd.Series:
        return pd.to_numeric(frame["close"], errors="coerce").dropna()

    above20 = _mean(float(closes(frame).iloc[-1]) > float(closes(frame).tail(20).mean()) for frame in ready.values())
    above50 = _mean(float(closes(frame).iloc[-1]) > float(closes(frame).tail(50).mean()) for frame in ready.values())
    above200 = _mean(float(closes(frame).iloc[-1]) > float(closes(frame).tail(200).mean()) for frame in ready.values())
    participation5 = _mean(float(closes(frame).iloc[-1]) > float(closes(frame).iloc[-6]) for frame in ready.values())
    new_high20 = _mean(float(closes(frame).iloc[-1]) >= float(closes(frame).tail(20).max()) for frame in ready.values())
    new_low20 = _mean(float(closes(frame).iloc[-1]) <= float(closes(frame).tail(20).min()) for frame in ready.values())
    returns = benchmark_close.pct_change().dropna().tail(settings.volatility_days)
    realized_volatility = float(returns.std() * math.sqrt(252))
    atr_percentage = _atr_pct(benchmark, settings.atr_days)

    reasons: list[str] = []
    long_trend_broken = price < ma200 * 0.99
    breadth_broken = above50 is not None and above50 <= settings.risk_off_breadth_max
    volatility_broken = (
        math.isfinite(realized_volatility)
        and realized_volatility >= settings.high_volatility_annualized
    )
    if long_trend_broken:
        reasons.append("QQQ_BELOW_200D")
    if breadth_broken:
        reasons.append("BREADTH_RISK_OFF")
    if volatility_broken:
        reasons.append("VOLATILITY_RISK_OFF")

    if reasons:
        regime = Regime.RISK_OFF
    else:
        long_trend_intact = price > ma200 and ma50 > ma200 and slope50 > 0
        healthy_long_trend = long_trend_intact and price > ma50
        healthy_short_trend = price >= ma20 and slope20 > 0
        healthy_breadth = above50 is not None and above50 >= settings.trend_breadth_min
        if healthy_long_trend and healthy_short_trend and healthy_breadth:
            regime = Regime.TREND
            reasons.append("TREND_AND_BREADTH_HEALTHY")
        elif long_trend_intact and (price < ma20 or not healthy_breadth or slope20 <= 0):
            regime = Regime.PULLBACK
            reasons.append("SHORT_TERM_DETERIORATION")
        else:
            regime = Regime.RANGE
            reasons.append("NO_DIRECTIONAL_EDGE")

    return RegimeSnapshot(
        as_of=as_of_day,
        regime=regime,
        coverage=round(coverage, 6),
        ready_symbols=len(ready),
        expected_symbols=expected,
        benchmark_price=price,
        benchmark_ma20=ma20,
        benchmark_ma50=ma50,
        benchmark_ma200=ma200,
        benchmark_slope20=slope20,
        benchmark_slope50=slope50,
        breadth_above_20d=above20,
        breadth_above_50d=above50,
        breadth_above_200d=above200,
        participation_5d=participation5,
        new_high_20d=new_high20,
        new_low_20d=new_low20,
        realized_volatility=realized_volatility,
        atr_pct=atr_percentage,
        reasons=tuple(reasons),
        data_quality="COMPLETE",
    )
