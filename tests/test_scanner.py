import numpy as np
import pandas as pd

from quant_signal.scanner import liquidity_filter, robust_factor_scores, scan_scores


def _bars(spec: dict[str, dict[str, float]], n: int = 140) -> pd.DataFrame:
    """spec: ticker -> {price0, drift, vol}；构造 n 天日线。"""
    frames = []
    idx = pd.bdate_range("2026-01-01", periods=n, tz="UTC")
    for ticker, cfg in spec.items():
        close = cfg["price0"] * np.cumprod(np.full(n, 1 + cfg.get("drift", 0.0)))
        df = pd.DataFrame(
            {"open": close, "high": close * 1.01, "low": close * 0.99,
             "close": close, "volume": cfg.get("vol", 1e6)},
            index=pd.MultiIndex.from_product([[ticker], idx], names=["ticker", "ts"]),
        )
        frames.append(df)
    return pd.concat(frames)


def test_liquidity_filter_drops_cheap_and_thin() -> None:
    bars = _bars({
        "GOOD": {"price0": 50.0, "vol": 2e6},      # ~1亿美元/日
        "CHEAP": {"price0": 2.0, "vol": 5e7},      # 价格<5 排除
        "THIN": {"price0": 100.0, "vol": 1e3},     # 成交额太小 排除
    }, n=6)
    assert liquidity_filter(bars, min_dollar_volume=20e6, min_price=5.0) == ["GOOD"]


def test_liquidity_filter_top_k_by_dollar_volume() -> None:
    bars = _bars({"A": {"price0": 50.0, "vol": 2e6}, "B": {"price0": 50.0, "vol": 9e6}}, n=6)
    assert liquidity_filter(bars, min_dollar_volume=1e6, top_k=1) == ["B"]


def test_scan_scores_rank_momentum_leader_first() -> None:
    bars = _bars({
        "HOT": {"price0": 50.0, "drift": 0.01},    # 强动量+贴近新高
        "FLAT": {"price0": 50.0, "drift": 0.0},
        "COLD": {"price0": 50.0, "drift": -0.005},
    })
    results = scan_scores(bars)
    assert [r.ticker for r in results][0] == "HOT"
    assert results[0].momentum_60d > 0.5
    assert results[0].high20_proximity > 0.95


def test_scan_scores_exclude_short_history() -> None:
    full = _bars({"OK": {"price0": 50.0, "drift": 0.01}})
    short = _bars({"NEW": {"price0": 50.0, "drift": 0.05}}, n=60)
    results = scan_scores(pd.concat([full, short]))
    assert [r.ticker for r in results] == ["OK"]


def test_scan_scores_use_bounded_robust_percentiles() -> None:
    bars = _bars({
        "A": {"price0": 50.0, "drift": 0.003, "vol": 1e6},
        "B": {"price0": 50.0, "drift": 0.002, "vol": 2e6},
        "OUTLIER": {"price0": 50.0, "drift": 0.001, "vol": 1e12},
    })

    results = scan_scores(bars)

    assert all(-0.5 <= result.score <= 0.5 for result in results)
    assert results[0].ticker == "A"


def test_robust_factor_scores_clip_outlier_and_center_ranks() -> None:
    scores = robust_factor_scores({"A": 1.0, "B": 2.0, "C": 3.0, "X": 1e12})

    assert set(scores) == {"A", "B", "C", "X"}
    assert min(scores.values()) >= -0.5
    assert max(scores.values()) <= 0.5
    assert scores["A"] < scores["B"] < scores["C"] <= scores["X"]
