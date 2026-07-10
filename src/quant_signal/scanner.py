"""T2 全市场扫描（纯函数层）：流动性初筛 + 稳健三因子打分。

score = 0.4×rank(60日动量) + 0.3×rank(距20日高点近度) + 0.3×rank(5日量比)。
各因子先按横截面 5%/95% 分位截尾，再转成居中的百分位排名。
全部为可解释规则、无拟合参数；历史不足 130 日的次新直接排除。
诚实定位：候选发现器，Top1 会写入台账接受绩效周报检验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import cast

import numpy as np
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
        dollar = float((sub["close"] * sub["volume"]).dropna().tail(20).mean())
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


@dataclass(frozen=True)
class ScanValidation:
    bars: pd.DataFrame
    valid_symbols: tuple[str, ...]
    coverage: float
    rejected: Mapping[str, str]


def _valid_ohlcv(bars: pd.DataFrame) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    if bars.empty or any(column not in bars.columns for column in required):
        return bars.iloc[0:0].copy()
    numeric = bars[required].apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(numeric).all(axis=1)
    positive_prices = (numeric[["open", "high", "low", "close"]] > 0).all(axis=1)
    nonnegative_volume = numeric["volume"] >= 0
    return cast(
        pd.DataFrame,
        bars.loc[finite & positive_prices & nonnegative_volume].sort_index(),
    )


def validate_scan_bars(
    bars: pd.DataFrame,
    symbols: Sequence[str],
    as_of: date,
    *,
    min_history: int = MIN_HISTORY,
) -> ScanValidation:
    requested = tuple(sorted(set(symbols)))
    clean = _valid_ohlcv(bars)
    available = (
        set(map(str, clean.index.get_level_values("ticker").unique()))
        if not clean.empty
        else set()
    )
    valid: list[str] = []
    rejected: dict[str, str] = {}
    frames: list[pd.DataFrame] = []
    for ticker in requested:
        if ticker not in available:
            rejected[ticker] = "MISSING"
            continue
        sub = cast(
            pd.DataFrame,
            clean.xs(ticker, level="ticker", drop_level=False).sort_index(),
        )
        if len(sub) < min_history:
            rejected[ticker] = "SHORT_HISTORY"
            continue
        latest = pd.Timestamp(sub.index.get_level_values("ts").max()).date()
        if latest != as_of:
            rejected[ticker] = "STALE"
            continue
        valid.append(ticker)
        frames.append(sub)
    selected = pd.concat(frames).sort_index() if frames else clean.iloc[0:0].copy()
    coverage = len(valid) / len(requested) if requested else 0.0
    return ScanValidation(
        bars=selected,
        valid_symbols=tuple(valid),
        coverage=coverage,
        rejected=MappingProxyType(dict(sorted(rejected.items()))),
    )


def robust_factor_scores(values: dict[str, float]) -> dict[str, float]:
    """横截面 5%/95% 截尾后转成 [-0.5, 0.5] 的百分位排名。"""
    s = pd.Series(values, dtype=float)
    if len(s) < 2:
        return {k: 0.0 for k in values}
    clipped = s.clip(lower=float(s.quantile(0.05)), upper=float(s.quantile(0.95)))
    ranked = clipped.rank(method="average", pct=True) - 0.5
    return {str(k): float(v) for k, v in ranked.items()}


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
    zm = robust_factor_scores(mom)
    zp = robust_factor_scores(prox)
    zv = robust_factor_scores(volr)
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
    results.sort(key=lambda r: (-r.score, r.ticker))
    return results
