from datetime import date

import numpy as np
import pandas as pd

from quant_signal.config import MarketRegimeSettings
from quant_signal.market_regime import Regime, classify_market_regime


DAY = date(2026, 7, 14)
MEMBERS = {f"N{i:02d}" for i in range(10)}
CFG = MarketRegimeSettings(
    min_history_days=201,
    trend_breadth_min=0.60,
    risk_off_breadth_max=0.35,
    high_volatility_annualized=0.50,
)


def _frame(paths: dict[str, np.ndarray]) -> pd.DataFrame:
    ts = pd.date_range(end=DAY, periods=220, freq="B", tz="UTC")
    frames: list[pd.DataFrame] = []
    for ticker, close in paths.items():
        close = np.asarray(close, dtype=float)
        high = close * 1.01
        low = close * 0.99
        frames.append(
            pd.DataFrame(
                {
                    "open": close * 0.999,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": np.full(len(close), 2_000_000),
                },
                index=pd.MultiIndex.from_product(
                    [[ticker], ts[-len(close) :]], names=["ticker", "ts"]
                ),
            )
        )
    return pd.concat(frames).sort_index()


def _trend_paths() -> dict[str, np.ndarray]:
    base = np.linspace(80.0, 140.0, 220)
    paths = {ticker: base * (1 + index / 200) for index, ticker in enumerate(MEMBERS)}
    paths["QQQ"] = np.linspace(400.0, 520.0, 220)
    return paths


def test_healthy_uptrend_is_trend() -> None:
    snapshot = classify_market_regime(
        _frame(_trend_paths()), MEMBERS, as_of=DAY, settings=CFG
    )

    assert snapshot.regime == Regime.TREND
    assert snapshot.coverage == 1.0
    assert snapshot.breadth_above_50d > 0.7
    assert snapshot.data_quality == "COMPLETE"


def test_broken_long_trend_is_risk_off() -> None:
    paths = _trend_paths()
    paths["QQQ"][-50:] = np.linspace(500.0, 300.0, 50)
    for ticker in MEMBERS:
        paths[ticker][-80:] = np.linspace(130.0, 70.0, 80)

    snapshot = classify_market_regime(
        _frame(paths), MEMBERS, as_of=DAY, settings=CFG
    )

    assert snapshot.regime == Regime.RISK_OFF
    assert "QQQ_BELOW_200D" in snapshot.reasons


def test_long_uptrend_with_short_pullback_is_pullback() -> None:
    paths = _trend_paths()
    paths["QQQ"][-12:] = np.linspace(520.0, 498.0, 12)
    for ticker in list(MEMBERS)[:5]:
        paths[ticker][-12:] = np.linspace(paths[ticker][-13], paths[ticker][-13] * 0.93, 12)

    snapshot = classify_market_regime(
        _frame(paths), MEMBERS, as_of=DAY, settings=CFG
    )

    assert snapshot.regime == Regime.PULLBACK
    assert "SHORT_TERM_DETERIORATION" in snapshot.reasons


def test_sideways_market_is_range() -> None:
    x = np.linspace(0, 12 * np.pi, 220)
    paths = {ticker: 100 + np.sin(x + i) * 2 for i, ticker in enumerate(MEMBERS)}
    paths["QQQ"] = 450 + np.sin(x) * 4

    snapshot = classify_market_regime(
        _frame(paths), MEMBERS, as_of=DAY, settings=CFG
    )

    assert snapshot.regime == Regime.RANGE


def test_insufficient_coverage_returns_unknown() -> None:
    paths = _trend_paths()
    for ticker in list(MEMBERS)[2:]:
        paths[ticker] = paths[ticker][-40:]

    snapshot = classify_market_regime(
        _frame(paths), MEMBERS, as_of=DAY, settings=CFG, min_coverage=0.98
    )

    assert snapshot.data_quality == "INCOMPLETE"
    assert snapshot.regime == Regime.UNKNOWN
    assert snapshot.coverage == 0.2
    assert "COVERAGE_BELOW_MINIMUM" in snapshot.reasons


def test_future_and_non_finite_bars_are_not_used() -> None:
    bars = _frame(_trend_paths())
    future = bars.xs("QQQ", level="ticker").tail(1).copy()
    future.index = pd.DatetimeIndex([pd.Timestamp("2026-07-15", tz="UTC")])
    future["close"] = np.inf
    future.index = pd.MultiIndex.from_product(
        [["QQQ"], future.index], names=["ticker", "ts"]
    )

    snapshot = classify_market_regime(
        pd.concat([bars, future]), MEMBERS, as_of=DAY, settings=CFG
    )

    assert snapshot.regime == Regime.TREND
    assert snapshot.benchmark_price == 520.0
