"""组合集中度：目标持仓间的收益相关簇与权重警示（P1 展示层）。

动机：池子里 MU/SMH/SNXX/RAM/MUU 等标的名义上分散，实为同一条存储/半导体链——
名义 5 个仓位实际是一注。用近 lookback 日收益相关性把高相关标的聚成簇，
最大簇合计权重超过 cap 时在榜单卡追加一行警示。纯展示，不改选股与权重。
"""

from __future__ import annotations

import pandas as pd


def correlation_clusters(
    close: pd.DataFrame,
    tickers: list[str],
    lookback: int = 60,
    threshold: float = 0.7,
) -> list[list[str]]:
    """对给定标的按日收益相关性(> threshold)做贪心聚簇，返回 ≥2 成员的簇(按大小降序)。"""
    present = [t for t in tickers if t in close.columns]
    if len(present) < 2:
        return []
    rets = close[present].pct_change(fill_method=None).tail(lookback).dropna(how="all")
    corr = rets.corr()

    parent: dict[str, str] = {t: t for t in present}

    def find(t: str) -> str:
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    for i, a in enumerate(present):
        for b in present[i + 1 :]:
            value = float(corr[a].loc[b]) if pd.notna(corr[a].loc[b]) else None
            if value is not None and value > threshold:
                parent[find(b)] = find(a)

    groups: dict[str, list[str]] = {}
    for t in present:
        groups.setdefault(find(t), []).append(t)
    clusters = [sorted(members) for members in groups.values() if len(members) >= 2]
    clusters.sort(key=len, reverse=True)
    return clusters


def cluster_weight_warning(
    clusters: list[list[str]],
    weights: dict[str, float],
    cap: float = 0.5,
) -> str | None:
    """最大簇合计权重超过 cap 时给出一行 markdown 警示，否则 None。"""
    worst: tuple[float, list[str]] | None = None
    for members in clusters:
        total = sum(weights.get(t, 0.0) for t in members)
        if worst is None or total > worst[0]:
            worst = (total, members)
    if worst is None or worst[0] <= cap:
        return None
    total, members = worst
    return (
        f"⚠ **集中度提示**：{'/'.join(members)} 近60日高相关(≥0.7)，"
        f"合计权重 {total:.0%}——名义分散实为一注，注意单一主题回撤风险。"
    )
