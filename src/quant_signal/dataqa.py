"""S6 双源数据校验：Alpaca(IEX) vs yfinance 收盘价对比。

IEX 免费源只含约 2-3% 成交量，日线 close 与全市场有偏差——动量计算的输入
需要这道体检。对每个标的取**两源共同拥有的最新交易日**收盘对比，偏差超阈值
即告警。纯函数，取数由 pipeline 负责。
"""

from __future__ import annotations

import pandas as pd


def latest_common_closes(
    a: pd.DataFrame, b: pd.DataFrame
) -> dict[str, tuple[float, float]]:
    """两个 (ticker,ts) MultiIndex 日线表 → {ticker: (a_close, b_close)}，
    仅取两源共同拥有的最新交易日；任一源缺该标的则跳过。"""
    out: dict[str, tuple[float, float]] = {}
    if a.empty or b.empty:
        return out
    tickers = set(a.index.get_level_values("ticker")) & set(b.index.get_level_values("ticker"))
    for ticker in sorted(tickers):
        sa = a.xs(ticker, level="ticker")["close"].dropna()
        sb = b.xs(ticker, level="ticker")["close"].dropna()
        common = sa.index.intersection(sb.index)
        if len(common) == 0:
            continue
        ts = common.max()
        out[ticker] = (float(sa.loc[ts]), float(sb.loc[ts]))
    return out


def divergences(
    pairs: dict[str, tuple[float, float]], threshold: float = 0.005
) -> list[tuple[str, float, float, float]]:
    """返回 [(ticker, a, b, 偏差)]，偏差=|a/b−1| 超过 threshold 才列出，按偏差降序。"""
    hits: list[tuple[str, float, float, float]] = []
    for ticker, (pa, pb) in pairs.items():
        if pb <= 0:
            continue
        pct = abs(pa / pb - 1.0)
        if pct > threshold:
            hits.append((ticker, pa, pb, pct))
    hits.sort(key=lambda item: item[3], reverse=True)
    return hits
