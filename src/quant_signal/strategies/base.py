from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd


class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    REDUCE = "reduce"


@dataclass(frozen=True)
class Signal:
    ticker: str
    direction: Direction
    price: float              # 触发时参考价
    reason: str               # 人类可读，将出现在飞书卡片
    strategy_id: str
    ts: datetime              # UTC aware
    suggested_weight: float | None = None   # 建议仓位占比 0~1
    extra: dict[str, object] | None = None


def dedup_key(s: Signal) -> str:
    return f"{s.ticker}|{s.direction.value}|{s.strategy_id}"


class Strategy:
    strategy_id: str
    schedule: str             # "daily_premarket" | "intraday_5min"
    universe: list[str]

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        """bars: MultiIndex(ticker, ts) OHLCV。纯函数，无副作用，无 IO。"""
        raise NotImplementedError
