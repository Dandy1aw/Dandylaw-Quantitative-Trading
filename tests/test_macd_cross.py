import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.macd_cross import MacdCross


def make_bars(ticker: str, closes: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-01-05", periods=len(closes), freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1_000_000},
        index=pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"]),
    )


def strat() -> MacdCross:
    return MacdCross(universe=["AAA"], fast=12, slow=26, signal=9)


def _run_day_by_day(closes: list[float]) -> list:
    """模拟真实用法：逐日只喂截至当天的数据，收集整个过程里出现过的所有信号。"""
    bars = make_bars("AAA", closes)
    all_ts = bars.index.get_level_values("ts").unique().sort_values()
    s = strat()
    collected = []
    for ts in all_ts:
        window = bars[bars.index.get_level_values("ts") <= ts]
        collected += s.generate(window)
    return collected


def test_downtrend_then_sharp_uptrend_triggers_golden_cross() -> None:
    down = list(np.linspace(150, 100, 40))
    up = list(np.linspace(100, 160, 20))
    closes = down + up[1:]
    signals = _run_day_by_day(closes)
    directions = {s.direction for s in signals}
    assert Direction.BUY in directions
    buy = next(s for s in signals if s.direction is Direction.BUY)
    assert buy.strategy_id == "macd_cross"
    assert "金叉" in buy.reason


def test_uptrend_then_sharp_downtrend_triggers_death_cross() -> None:
    up = list(np.linspace(100, 150, 40))
    down = list(np.linspace(150, 90, 20))
    closes = up + down[1:]
    signals = _run_day_by_day(closes)
    sell = next(s for s in signals if s.direction is Direction.SELL)
    assert "死叉" in sell.reason


def test_steady_trend_no_cross_no_signal() -> None:
    closes = list(np.linspace(100, 110, 60))  # 平稳上涨，MACD线和信号线不交叉
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []


def test_insufficient_history_returns_empty() -> None:
    closes = list(np.linspace(100, 110, 20))
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []
