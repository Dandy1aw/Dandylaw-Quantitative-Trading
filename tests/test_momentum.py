import numpy as np
import pandas as pd

from quant_signal.strategies.base import Direction
from quant_signal.strategies.momentum_rotation import MomentumRotation

UNIVERSE = ["AAA", "BBB", "CCC", "DDD"]


def make(top_n: int = 2) -> MomentumRotation:
    return MomentumRotation(
        universe=UNIVERSE, lookback_days=60, top_n=top_n, min_dollar_volume=50_000_000
    )


def test_picks_top_momentum_and_filters_low_volume(daily_bars: pd.DataFrame) -> None:
    signals = make(top_n=2).generate(daily_bars)
    tickers = [s.ticker for s in signals]
    assert tickers == ["AAA", "BBB"]          # DDD 动量最高但被成交额过滤
    assert all(s.direction is Direction.BUY for s in signals)
    assert all(s.suggested_weight == 0.5 for s in signals)
    assert all(s.strategy_id == "momentum_rotation" for s in signals)


def test_signal_ts_is_last_bar_ts(daily_bars: pd.DataFrame) -> None:
    signals = make().generate(daily_bars)
    last_ts = daily_bars.index.get_level_values("ts").max()
    assert all(s.ts == last_ts for s in signals)


def test_insufficient_history_returns_empty(daily_bars: pd.DataFrame) -> None:
    ts = daily_bars.index.get_level_values("ts").unique().sort_values()
    short = daily_bars[daily_bars.index.get_level_values("ts") <= ts[30]]
    assert make().generate(short) == []


def test_no_lookahead_truncation(daily_bars: pd.DataFrame) -> None:
    """同一策略实例先看过全量数据后，对截断数据的输出必须与新实例一致（无内部状态泄漏未来信息）。"""
    ts = daily_bars.index.get_level_values("ts").unique().sort_values()
    upto_90 = daily_bars[daily_bars.index.get_level_values("ts") <= ts[89]]
    reused = make()
    reused.generate(daily_bars)          # 先喂全量（含"未来"10 天）
    assert reused.generate(upto_90) == make().generate(upto_90)


def test_dollar_volume_filter_converts_foreign_currency() -> None:
    """非美元计价标的的成交额过滤必须换算成美元，否则原始数字会误判流动性达标。"""
    ts = pd.date_range("2025-08-01", periods=65, freq="B", tz="UTC")
    n = len(ts)
    aaa_close = 100.0 * np.cumprod(np.full(n, 1.008))
    aaa = pd.DataFrame(
        {"open": aaa_close, "high": aaa_close, "low": aaa_close, "close": aaa_close, "volume": 5_000_000},
        index=pd.MultiIndex.from_product([["AAA"], ts], names=["ticker", "ts"]),
    )
    krt_close = 500_000.0 * np.cumprod(np.full(n, 1.01))  # KRW 计价，动量比 AAA 更强
    krt = pd.DataFrame(
        {"open": krt_close, "high": krt_close, "low": krt_close, "close": krt_close, "volume": 200},
        index=pd.MultiIndex.from_product([["KRT"], ts], names=["ticker", "ts"]),
    )
    bars = pd.concat([aaa, krt]).sort_index()

    strat_no_fx = MomentumRotation(
        universe=["AAA", "KRT"], lookback_days=60, top_n=2, min_dollar_volume=50_000_000
    )
    assert "KRT" in {s.ticker for s in strat_no_fx.generate(bars)}  # 不换算时被原始KRW数字误判达标

    strat_with_fx = MomentumRotation(
        universe=["AAA", "KRT"],
        lookback_days=60,
        top_n=2,
        min_dollar_volume=50_000_000,
        ticker_currency={"KRT": "KRW"},
        fx_rates={"KRW": 1300.0},
    )
    signals = strat_with_fx.generate(bars)
    tickers = {s.ticker for s in signals}
    assert "KRT" not in tickers   # 换算成美元后流动性不足，正确剔除
    assert "AAA" in tickers
