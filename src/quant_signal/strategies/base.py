from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import math

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
    # 去重键后缀：同标的同方向同策略下再细分（如偏离监控按 2%/5%/10% 档位分开，
    # 升到更高档=新键=会再推一次"升级告警"，同档在窗口内仍压制）
    dedup_suffix: str | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.price):
            raise ValueError(f"Signal.price must be finite: {self.ticker}={self.price}")


def dedup_key(s: Signal) -> str:
    base = f"{s.ticker}|{s.direction.value}|{s.strategy_id}"
    return f"{base}|{s.dedup_suffix}" if s.dedup_suffix else base


class Strategy:
    strategy_id: str
    schedule: str             # "daily_premarket" | "intraday_5min"
    universe: list[str]

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        """bars: MultiIndex(ticker, ts) OHLCV。纯函数，无副作用，无 IO。"""
        raise NotImplementedError
