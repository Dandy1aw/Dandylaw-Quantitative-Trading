import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.breakout_20d import Breakout20d


def make_bars(last_close: float, last_volume: float, n: int = 30) -> pd.DataFrame:
    """前 n-1 天 close/high 固定 100/101，最后一天（当日）close=last_close。"""
    ts = pd.date_range("2026-05-01", periods=n, freq="B", tz="UTC")
    close = np.full(n, 100.0)
    high = np.full(n, 101.0)
    vol = np.full(n, 1_000_000.0)
    close[-1] = last_close
    high[-1] = max(101.0, last_close)
    vol[-1] = last_volume
    return pd.DataFrame(
        {"open": close, "high": high, "low": close - 1, "close": close, "volume": vol},
        index=pd.MultiIndex.from_product([["NVDA"], ts], names=["ticker", "ts"]),
    )


def strat() -> Breakout20d:
    return Breakout20d(universe=["NVDA"], high_lookback_days=20, volume_multiplier=1.5)


def test_breakout_with_volume_triggers_buy() -> None:
    bars = make_bars(last_close=102.0, last_volume=2_000_000)  # 破 101 高点 + 2x 均量
    signals = strat().generate(bars)
    assert len(signals) == 1
    s = signals[0]
    assert s.ticker == "NVDA" and s.direction is Direction.BUY
    assert s.price == 102.0
    assert s.strategy_id == "breakout_20d"


def test_no_signal_without_volume_confirm() -> None:
    bars = make_bars(last_close=102.0, last_volume=1_200_000)  # 量能不足 1.5x
    assert strat().generate(bars) == []


def test_no_signal_below_prior_high() -> None:
    bars = make_bars(last_close=100.5, last_volume=2_000_000)  # 未破前高 101
    assert strat().generate(bars) == []


def test_prior_high_excludes_current_day() -> None:
    """当日自身新高不能作为被突破的前高。"""
    bars = make_bars(last_close=150.0, last_volume=2_000_000)
    signals = strat().generate(bars)
    assert len(signals) == 1
    assert signals[0].extra is not None
    assert signals[0].extra["prior_high"] == 101.0   # 而非当日的 150


def test_insufficient_history() -> None:
    bars = make_bars(last_close=102.0, last_volume=2_000_000, n=10)
    assert strat().generate(bars) == []
