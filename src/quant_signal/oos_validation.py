"""滚动样本外切分与排名缓冲候选选择的纯函数。"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class RollingSplit:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


def rolling_splits(
    index: pd.DatetimeIndex,
    *,
    train_size: int,
    test_size: int,
    step_size: int,
) -> list[RollingSplit]:
    """按交易日数量生成训练在前、测试在后的滚动窗口。"""
    if min(train_size, test_size, step_size) < 1:
        raise ValueError("split sizes must be positive")
    dates = pd.DatetimeIndex(index.unique()).sort_values()
    out: list[RollingSplit] = []
    for start in range(0, len(dates) - train_size - test_size + 1, step_size):
        test_start = start + train_size
        out.append(
            RollingSplit(
                train_start=pd.Timestamp(dates[start]),
                train_end=pd.Timestamp(dates[test_start - 1]),
                test_start=pd.Timestamp(dates[test_start]),
                test_end=pd.Timestamp(dates[test_start + test_size - 1]),
            )
        )
    return out


def buffered_selection(
    ranking: list[tuple[str, float]],
    current: set[str],
    *,
    top_n: int,
    rank_buffer: int,
    min_momentum: float = float("-inf"),
) -> list[str]:
    """保留缓冲区内的原持仓，再按排名补满；低于绝对动量门槛的不强选。"""
    if top_n < 1 or rank_buffer < 0:
        raise ValueError("top_n must be positive and rank_buffer non-negative")
    eligible = [(ticker, score) for ticker, score in ranking if score >= min_momentum]
    ranks = {ticker: rank for rank, (ticker, _) in enumerate(eligible)}
    retained = sorted(
        (
            ticker
            for ticker in current
            if ticker in ranks and ranks[ticker] < top_n + rank_buffer
        ),
        key=ranks.__getitem__,
    )[:top_n]
    selected = list(retained)
    for ticker, _ in eligible:
        if ticker not in selected:
            selected.append(ticker)
        if len(selected) == top_n:
            break
    return sorted(selected, key=ranks.__getitem__)
