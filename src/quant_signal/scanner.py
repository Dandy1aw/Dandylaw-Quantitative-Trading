"""T2 全市场扫描（纯函数层）：流动性初筛 + 可解释三因子打分。

score = 0.4×z(60日动量) + 0.3×z(距20日高点近度) + 0.3×z(5日量比)。
全部为可解释规则、无拟合参数；历史不足 130 日的次新直接排除。
诚实定位：候选发现器，Top1 会写入台账接受绩效周报检验。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

MIN_HISTORY = 130


def liquidity_filter(
    bars: pd.DataFrame,
    min_dollar_volume: float = 20_000_000,
    min_price: float = 5.0,
    top_k: int = 500,
) -> list[str]:
    """近几日日线 → 过滤低价/低流动性，按日均成交额降序取前 top_k。"""
    out: list[tuple[str, float]] = []
    if bars.empty:
        return []
    for ticker in bars.index.get_level_values("ticker").unique():
        sub = bars.xs(ticker, level="ticker")
        close = sub["close"].dropna()
        if close.empty or float(close.iloc[-1]) < min_price:
            continue
        dollar = float((sub["close"] * sub["volume"]).dropna().mean())
        if dollar >= min_dollar_volume:
            out.append((str(ticker), dollar))
    out.sort(key=lambda item: item[1], reverse=True)
    return [ticker for ticker, _ in out[:top_k]]


@dataclass(frozen=True)
class ScanResult:
    ticker: str
    score: float
    momentum_60d: float
    high20_proximity: float   # 现价/20日最高, 越接近1越强
    volume_ratio: float       # 近5日均量/近60日均量
    price: float


def _zscores(values: dict[str, float]) -> dict[str, float]:
    s = pd.Series(values, dtype=float)
    std = float(s.std())
    if std == 0 or len(s) < 2:
        return {k: 0.0 for k in values}
    mean = float(s.mean())
    return {str(k): (float(v) - mean) / std for k, v in s.items()}


def scan_scores(bars: pd.DataFrame) -> list[ScanResult]:
    """130日+日线 → 按综合分降序的扫描结果(历史不足的标的排除)。"""
    mom: dict[str, float] = {}
    prox: dict[str, float] = {}
    volr: dict[str, float] = {}
    price: dict[str, float] = {}
    if bars.empty:
        return []
    for ticker in bars.index.get_level_values("ticker").unique():
        sub = bars.xs(ticker, level="ticker").sort_index()
        close = sub["close"].dropna()
        if len(close) < MIN_HISTORY:
            continue
        t = str(ticker)
        last = float(close.iloc[-1])
        mom[t] = last / float(close.iloc[-61]) - 1.0
        high20 = float(sub["high"].dropna().tail(20).max())
        prox[t] = last / high20 if high20 > 0 else 0.0
        vol = sub["volume"].dropna()
        base_vol = float(vol.tail(60).mean())
        volr[t] = float(vol.tail(5).mean()) / base_vol if base_vol > 0 else 0.0
        price[t] = last
    zm, zp, zv = _zscores(mom), _zscores(prox), _zscores(volr)
    results = [
        ScanResult(
            ticker=t,
            score=0.4 * zm[t] + 0.3 * zp[t] + 0.3 * zv[t],
            momentum_60d=mom[t],
            high20_proximity=prox[t],
            volume_ratio=volr[t],
            price=price[t],
        )
        for t in mom
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results
