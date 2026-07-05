from __future__ import annotations

import pandas as pd


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """平均真实波幅：TR = max(高−低, |高−昨收|, |低−昨收|)，取最近 period 均值。"""
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    return float(tr.tail(period).mean())


def chandelier_stop(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    lookback: int = 22,
    atr_period: int = 14,
    mult: float = 3.0,
) -> float | None:
    """ATR 吊灯移动止损：近 lookback 日最高价 − mult × ATR。数据不足返回 None。

    趋势跟踪的经典移动止损，贴近价格并随波动自适应，比 200 日线更适合当"参考卖出价"。
    """
    if len(close.dropna()) < max(lookback, atr_period) + 1:
        return None
    highest = float(high.tail(lookback).max())
    return highest - mult * atr(high, low, close, atr_period)


def expected_move_target(
    close: pd.Series, vol_lookback: int = 60, horizon: int = 20
) -> float | None:
    """波动率期望位止盈目标：现价 ×(1 + 日波动率 × √horizon)，即未来 horizon 日约 1σ 上沿。

    热门高波动股目标自然更高，随波动自适应；数据不足返回 None。
    """
    c = close.dropna()
    if len(c) < vol_lookback + 1:
        return None
    daily_std = float(c.pct_change().dropna().tail(vol_lookback).std())
    return float(float(c.iloc[-1]) * (1.0 + daily_std * (horizon ** 0.5)))
