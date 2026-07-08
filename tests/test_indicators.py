import numpy as np
import pandas as pd

from quant_signal.strategies.indicators import (
    atr,
    chandelier_stop,
    entry_hint,
    expected_move_target,
)


def test_entry_hint_band_below_price_not_overheated() -> None:
    idx = pd.bdate_range("2024-01-02", periods=60, tz="UTC")
    close = pd.Series(100.0 + np.arange(60) * 0.1, index=idx)   # 温和上行
    high, low = close + 1.0, close - 1.0
    hint = entry_hint(high, low, close)
    assert hint is not None
    lo, hi, overheated = hint
    assert lo <= hi == float(close.iloc[-1])   # 带上沿=现价, 下沿不高于现价
    assert lo >= float(close.iloc[-1]) - atr(high, low, close) - 1e-9
    assert overheated is False


def test_entry_hint_overheated_when_far_above_ma20() -> None:
    idx = pd.bdate_range("2024-01-02", periods=60, tz="UTC")
    base = np.full(60, 100.0)
    base[-5:] = [130, 150, 170, 190, 210]      # 末端抛物线拉升, 远离20日均线
    close = pd.Series(base, index=idx)
    high, low = close + 1.0, close - 1.0
    hint = entry_hint(high, low, close)
    assert hint is not None
    assert hint[2] is True


def test_entry_hint_insufficient_history_none() -> None:
    idx = pd.bdate_range("2024-01-02", periods=10, tz="UTC")
    close = pd.Series(100.0, index=idx)
    assert entry_hint(close + 1, close - 1, close) is None


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


def test_expected_move_target_above_price_and_scales_with_vol() -> None:
    idx = pd.bdate_range("2024-01-02", periods=120, tz="UTC")
    rng = np.random.default_rng(0)
    calm = pd.Series(100.0 * np.cumprod(1 + rng.normal(0, 0.005, 120)), index=idx)
    wild = pd.Series(100.0 * np.cumprod(1 + rng.normal(0, 0.03, 120)), index=idx)
    tp_calm = expected_move_target(calm, vol_lookback=60, horizon=20)
    tp_wild = expected_move_target(wild, vol_lookback=60, horizon=20)
    assert tp_calm is not None and tp_wild is not None
    assert tp_calm > float(calm.iloc[-1])                    # 目标在现价上方
    up_calm = tp_calm / float(calm.iloc[-1]) - 1
    up_wild = tp_wild / float(wild.iloc[-1]) - 1
    assert up_wild > up_calm                                 # 波动越大目标越高


def test_expected_move_insufficient_history_none() -> None:
    idx = pd.bdate_range("2024-01-02", periods=20, tz="UTC")
    assert expected_move_target(pd.Series(range(20), index=idx), vol_lookback=60) is None
