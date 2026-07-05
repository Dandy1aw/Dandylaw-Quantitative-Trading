import numpy as np
import pandas as pd

from quant_signal.strategies.indicators import atr, chandelier_stop


def _ohlc(n, base=100.0, rng=2.0):  # type: ignore[no-untyped-def]
    idx = pd.bdate_range("2024-01-02", periods=n, tz="UTC")
    close = pd.Series(base + np.arange(n) * 1.0, index=idx)
    high = close + rng
    low = close - rng
    return high, low, close


def test_atr_constant_range() -> None:
    high, low, close = _ohlc(60, rng=2.0)
    # 每日 high-low=4，close 稳步上行使 TR≈4~5；ATR 应为正且量级接近 4~5
    a = atr(high, low, close, period=14)
    assert 3.5 < a < 6.0


def test_chandelier_below_recent_high() -> None:
    high, low, close = _ohlc(60, rng=2.0)
    stop = chandelier_stop(high, low, close, lookback=22, atr_period=14, mult=3.0)
    assert stop is not None
    recent_high = float(high.tail(22).max())
    assert stop < recent_high            # 吊灯止损在近期高点下方
    assert stop < float(close.iloc[-1])  # 且低于现价（保护位）


def test_chandelier_insufficient_history_none() -> None:
    high, low, close = _ohlc(10)
    assert chandelier_stop(high, low, close, lookback=22, atr_period=14, mult=3.0) is None
