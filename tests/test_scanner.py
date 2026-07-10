from datetime import date
import time

import numpy as np
import pandas as pd

from quant_signal.scanner import (
    liquidity_filter,
    robust_factor_scores,
    scan_scores,
    validate_scan_bars,
)


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


def test_liquidity_filter_uses_recent_twenty_sessions() -> None:
    bars = _bars({"FADED": {"price0": 50.0, "vol": 1.0}}, n=40)
    bars.loc[("FADED", slice(None)), "volume"] = np.r_[
        np.full(20, 10_000_000),
        np.full(20, 1_000),
    ]

    assert liquidity_filter(bars, min_dollar_volume=1_000_000) == []


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


def test_validate_scan_bars_checks_history_freshness_and_ohlcv() -> None:
    fresh = _bars({"FRESH": {"price0": 50.0, "drift": 0.001}}, n=140)
    as_of = fresh.index.get_level_values("ts").max().date()
    stale = _bars({"STALE": {"price0": 50.0, "drift": 0.001}}, n=139)
    short = _bars({"SHORT": {"price0": 50.0, "drift": 0.001}}, n=60)
    invalid = _bars({"INVALID": {"price0": 50.0, "drift": 0.001}}, n=140)
    invalid.loc[("INVALID", invalid.index.get_level_values("ts").max()), "close"] = -1

    validation = validate_scan_bars(
        pd.concat([fresh, stale, short, invalid]),
        ["FRESH", "STALE", "SHORT", "INVALID", "MISSING"],
        as_of,
    )

    assert validation.valid_symbols == ("FRESH",)
    assert validation.coverage == 0.2
    assert validation.rejected == {
        "INVALID": "STALE",
        "MISSING": "MISSING",
        "SHORT": "SHORT_HISTORY",
        "STALE": "STALE",
    }
    assert (validation.bars[["open", "high", "low", "close"]] > 0).all().all()


def test_scan_scores_handles_600_symbols_locally_under_ten_seconds() -> None:
    bars = _bars(
        {
            f"S{i:03d}": {
                "price0": 20.0 + i / 10,
                "drift": (i % 17) / 100_000,
                "vol": 2_000_000 + i,
            }
            for i in range(600)
        }
    )

    started = time.perf_counter()
    results = scan_scores(bars)
    elapsed = time.perf_counter() - started

    assert len(results) == 600
    assert elapsed < 10.0
