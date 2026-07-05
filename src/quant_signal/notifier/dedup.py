from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from quant_signal.strategies.base import Signal, dedup_key


@dataclass
class DedupResult:
    to_push: list[Signal] = field(default_factory=list)
    suppressed: list[Signal] = field(default_factory=list)   # 4h 窗口内重复
    overflow: list[Signal] = field(default_factory=list)     # 超小时限流，待汇总


def apply_dedup(
    signals: list[Signal],
    now: datetime,
    last_push_by_key: dict[str, datetime],
    pushed_in_last_hour: int,
    dedup_hours: int = 4,
    hourly_limit: int = 10,
) -> DedupResult:
    result = DedupResult()
    window = timedelta(hours=dedup_hours)
    quota = max(0, hourly_limit - pushed_in_last_hour)
    for s in signals:
        last = last_push_by_key.get(dedup_key(s))
        if last is not None and now - last < window:
            result.suppressed.append(s)
        elif len(result.to_push) < quota:
            result.to_push.append(s)
        else:
            result.overflow.append(s)
    return result
