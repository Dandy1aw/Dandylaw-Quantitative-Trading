import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.bollinger_breakout import BollingerBreakout


def make_bars(ticker: str, closes: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-01-05", periods=len(closes), freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1_000_000},
        index=pd.MultiIndex.from_product([[ticker], ts], names=["ticker", "ts"]),
    )


def strat() -> BollingerBreakout:
    return BollingerBreakout(universe=["AAA"], period=20, num_std=2.0)


def test_spike_above_band_triggers_buy() -> None:
    closes = [100.0] * 19 + [130.0]   # 前19天平稳，最后一天暴涨
    signals = strat().generate(make_bars("AAA", closes))
    assert len(signals) == 1
    assert signals[0].direction is Direction.BUY
    assert signals[0].strategy_id == "bollinger_breakout"
    assert "上轨" in signals[0].reason


def test_spike_below_band_triggers_sell() -> None:
    closes = [100.0] * 19 + [70.0]
    signals = strat().generate(make_bars("AAA", closes))
    assert len(signals) == 1
    assert signals[0].direction is Direction.SELL
    assert "下轨" in signals[0].reason


def test_within_band_no_signal() -> None:
    closes = [100.0, 101.0, 99.0, 100.5, 99.5] * 4
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []


def test_insufficient_history_returns_empty() -> None:
    closes = [100.0] * 10
    signals = strat().generate(make_bars("AAA", closes))
    assert signals == []
