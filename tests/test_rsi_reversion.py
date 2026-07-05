import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.rsi_reversion import RsiReversion


def make_bars(ticker: str, closes: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-01-05", periods=len(closes), freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1_000_000},
        index=pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"]),
    )


def strat() -> RsiReversion:
    return RsiReversion(universe=["AAA"], period=14, oversold=30.0, overbought=70.0)


def test_all_down_days_triggers_oversold_buy() -> None:
    closes = list(np.linspace(200, 100, 20))  # 持续下跌
    signals = strat().generate(make_bars("AAA", closes))
    assert len(signals) == 1
    assert signals[0].direction is Direction.BUY
    assert signals[0].strategy_id == "rsi_reversion"
    assert signals[0].extra["rsi"] < 30.0


def test_all_up_days_triggers_overbought_sell() -> None:
    closes = list(np.linspace(100, 200, 20))  # 持续上涨
    signals = strat().generate(make_bars("AAA", closes))
    assert len(signals) == 1
    assert signals[0].direction is Direction.SELL
    assert signals[0].extra["rsi"] > 70.0


def test_choppy_flat_no_signal() -> None:
    closes = [100.0 + (2 if i % 2 == 0 else -2) for i in range(20)]
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []


def test_insufficient_history_returns_empty() -> None:
    closes = list(np.linspace(200, 100, 10))  # 少于 period+1
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []
