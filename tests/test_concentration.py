import numpy as np
import pandas as pd

from quant_signal.concentration import cluster_weight_warning, correlation_clusters


def _close(n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    idx = pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC")
    base = rng.normal(0, 0.02, n)
    a = 100 * np.cumprod(1 + base)
    b = 100 * np.cumprod(1 + base + rng.normal(0, 0.002, n))     # 与 A 高相关
    c = 100 * np.cumprod(1 + rng.normal(0, 0.02, n))             # 独立
    return pd.DataFrame({"AAA": a, "BBB": b, "CCC": c}, index=idx)


def test_clusters_group_highly_correlated_tickers() -> None:
    clusters = correlation_clusters(_close(), ["AAA", "BBB", "CCC"], lookback=60, threshold=0.7)
    assert ["AAA", "BBB"] in [sorted(c) for c in clusters]
    assert all("CCC" not in c for c in clusters)      # 独立标的不入簇


def test_clusters_ignore_missing_tickers() -> None:
    clusters = correlation_clusters(_close(), ["AAA", "BBB", "ZZZ"], lookback=60, threshold=0.7)
    assert [sorted(c) for c in clusters] == [["AAA", "BBB"]]


def test_cluster_warning_fires_above_cap() -> None:
    md = cluster_weight_warning([["AAA", "BBB"]], {"AAA": 0.4, "BBB": 0.3, "CCC": 0.3}, cap=0.5)
    assert md is not None and "AAA" in md and "70%" in md


def test_cluster_warning_none_below_cap() -> None:
    assert cluster_weight_warning([["AAA", "BBB"]], {"AAA": 0.2, "BBB": 0.2}, cap=0.5) is None
    assert cluster_weight_warning([], {"AAA": 1.0}, cap=0.5) is None
