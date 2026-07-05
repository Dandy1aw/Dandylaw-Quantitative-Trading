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


def test_survives_mismatched_trading_calendar(daily_bars: pd.DataFrame) -> None:
    """美股假期休市但国际标的照常交易时，不能让全体美股标的的动量被判定为 NaN。"""
    ts = daily_bars.index.get_level_values("ts").unique().sort_values()
    extra_day = ts[-1] + pd.Timedelta(days=1)  # 模拟"美股休市但港股/韩股照常交易"的那一天
    extra_row = pd.DataFrame(
        {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1_000_000},
        index=pd.MultiIndex.from_tuples([("KRT", extra_day)], names=["ticker", "ts"]),
    )
    bars_with_gap = pd.concat([daily_bars, extra_row]).sort_index()

    strat = MomentumRotation(universe=["AAA", "BBB", "CCC", "DDD", "KRT"], top_n=2)
    signals = strat.generate(bars_with_gap)
    tickers = {s.ticker for s in signals}
    assert tickers == {"AAA", "BBB"}   # AAA/BBB 不该因 KRT 多出的一天而消失


def test_group_ranking_isolates_market_groups(daily_bars: pd.DataFrame) -> None:
    """港股/韩股组的动量再夸张，也不该挤占默认组(美股)的名额，各自独立排名。"""
    ts = daily_bars.index.get_level_values("ts")
    n = len(ts.unique())
    krt_close = 500_000.0 * np.cumprod(np.full(n, 1.03))  # 极端动量，KRW计价
    krt = pd.DataFrame(
        {"open": krt_close, "high": krt_close, "low": krt_close, "close": krt_close, "volume": 100_000},
        index=pd.MultiIndex.from_product([["KRT"], ts.unique()], names=["ticker", "ts"]),
    )
    bars = pd.concat([daily_bars, krt]).sort_index()

    strat = MomentumRotation(
        universe=[*UNIVERSE, "KRT"], top_n=2, min_dollar_volume=50_000_000,
        ticker_currency={"KRT": "KRW"}, fx_rates={"KRW": 1300.0},
        group_top_n={"KRW": 1},
    )
    signals = strat.generate(bars)
    tickers = {s.ticker for s in signals}
    assert tickers == {"AAA", "BBB", "KRT"}   # 默认组仍是AAA/BBB，KRT占自己的韩股组名额

    krt_signal = next(s for s in signals if s.ticker == "KRT")
    assert "韩股组" in krt_signal.reason
    aaa_signal = next(s for s in signals if s.ticker == "AAA")
    assert "美股组" in aaa_signal.reason
    assert all(s.suggested_weight == round(1 / 3, 4) for s in signals)   # 3只均分


def test_rank_is_within_group_not_global(daily_bars: pd.DataFrame) -> None:
    """名次按组内算，每组都从第1起——不是全局名次。否则分组标签配全局名次会自相
    矛盾（美股组标签却写着'第3'，韩股组第1只标的却写着'第2'）。"""
    ts = daily_bars.index.get_level_values("ts")
    n = len(ts.unique())
    krt_close = 500_000.0 * np.cumprod(np.full(n, 1.03))  # 全局动量最高
    krt = pd.DataFrame(
        {"open": krt_close, "high": krt_close, "low": krt_close, "close": krt_close, "volume": 100_000},
        index=pd.MultiIndex.from_product([["KRT"], ts.unique()], names=["ticker", "ts"]),
    )
    bars = pd.concat([daily_bars, krt]).sort_index()
    strat = MomentumRotation(
        universe=[*UNIVERSE, "KRT"], top_n=2, min_dollar_volume=50_000_000,
        ticker_currency={"KRT": "KRW"}, fx_rates={"KRW": 1300.0}, group_top_n={"KRW": 1},
    )
    signals = {s.ticker: s for s in strat.generate(bars)}
    # KRT 全局动量第1，韩股组内也是第1
    assert "韩股组第1" in signals["KRT"].reason
    # AAA/BBB 全局是第2/第3，但美股组内应是第1/第2
    assert "美股组第1" in signals["AAA"].reason
    assert "美股组第2" in signals["BBB"].reason
    assert signals["AAA"].extra is not None and signals["AAA"].extra["rank"] == 1
    assert signals["BBB"].extra is not None and signals["BBB"].extra["rank"] == 2


def test_group_with_no_eligible_candidate_does_not_backfill(daily_bars: pd.DataFrame) -> None:
    """某组没有标的通过流动性门槛时，总选中数相应减少，不从其他组补位。"""
    ts = daily_bars.index.get_level_values("ts")
    krt_close = 500_000.0 * np.cumprod(np.full(len(ts.unique()), 1.03))
    krt = pd.DataFrame(
        {"open": krt_close, "high": krt_close, "low": krt_close, "close": krt_close, "volume": 1},  # 成交量极低，过不了流动性门槛
        index=pd.MultiIndex.from_product([["KRT"], ts.unique()], names=["ticker", "ts"]),
    )
    bars = pd.concat([daily_bars, krt]).sort_index()

    strat = MomentumRotation(
        universe=[*UNIVERSE, "KRT"], top_n=2, min_dollar_volume=50_000_000,
        ticker_currency={"KRT": "KRW"}, fx_rates={"KRW": 1300.0},
        group_top_n={"KRW": 1},
    )
    signals = strat.generate(bars)
    tickers = {s.ticker for s in signals}
    assert tickers == {"AAA", "BBB"}   # 韩股组空缺，不补给默认组


def test_empty_group_top_n_preserves_original_global_ranking(daily_bars: pd.DataFrame) -> None:
    """不传 group_top_n（默认空字典）时，行为跟修改前完全一致——全局统一排名。"""
    strat = MomentumRotation(universe=UNIVERSE, top_n=2, min_dollar_volume=50_000_000)
    signals = strat.generate(daily_bars)
    assert [s.ticker for s in signals] == ["AAA", "BBB"]
    assert all("美股组" in s.reason for s in signals)


def test_default_group_splits_etf_vs_stock(daily_bars: pd.DataFrame) -> None:
    """默认组内 ETF 和个股各自独立排名取各自名额，不互相挤占。"""
    strat = MomentumRotation(
        universe=UNIVERSE,
        top_n=2,   # 未配置 default_group_top_n 时的兜底值，本例不应被使用
        min_dollar_volume=50_000_000,
        asset_type={"AAA": "ETF", "BBB": "ETF", "CCC": "STOCK", "DDD": "STOCK"},
        default_group_top_n={"ETF": 1, "STOCK": 1},
    )
    signals = strat.generate(daily_bars)
    tickers = {s.ticker for s in signals}
    # AAA(动量最高的ETF) 和 CCC(个股里流动性合格、动量最高的) 各占一个名额
    # DDD 动量最高但被流动性过滤，BBB 是ETF组里动量较低的，应该都不入选
    assert tickers == {"AAA", "CCC"}
    aaa_signal = next(s for s in signals if s.ticker == "AAA")
    ccc_signal = next(s for s in signals if s.ticker == "CCC")
    assert "ETF组" in aaa_signal.reason
    assert "个股组" in ccc_signal.reason


def test_no_default_group_top_n_preserves_original_behavior(daily_bars: pd.DataFrame) -> None:
    """不传 default_group_top_n 时，即使传了 asset_type 也不影响原有全局排名。"""
    strat = MomentumRotation(
        universe=UNIVERSE, top_n=2, min_dollar_volume=50_000_000,
        asset_type={"AAA": "ETF", "BBB": "STOCK", "CCC": "STOCK", "DDD": "STOCK"},
    )
    signals = strat.generate(daily_bars)
    assert {s.ticker for s in signals} == {"AAA", "BBB"}


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
