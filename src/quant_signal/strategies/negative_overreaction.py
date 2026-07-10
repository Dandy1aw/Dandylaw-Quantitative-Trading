"""Pure price rules for a conservative negative-overreaction setup."""

from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd


@dataclass(frozen=True)
class ShockEvent:
    ticker: str
    shock_at: pd.Timestamp
    pre_close: float
    event_close: float
    event_low: float
    atr20: float
    drop: float
    volume_ratio: float


@dataclass(frozen=True)
class ReversalConfirmation:
    ticker: str
    shock_at: pd.Timestamp
    confirmed_at: pd.Timestamp
    entry_at: pd.Timestamp
    entry_price: float
    stop_price: float


def _atr_before(bars: pd.DataFrame, position: int, period: int = 20) -> float:
    prior = bars.iloc[position - period : position]
    previous_close = bars["close"].shift(1).iloc[position - period : position]
    tr = pd.concat(
        [
            prior["high"] - prior["low"],
            (prior["high"] - previous_close).abs(),
            (prior["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.mean())


def detect_shock(
    ticker: str,
    bars: pd.DataFrame,
    at: pd.Timestamp,
    *,
    min_drop: float = 0.08,
    atr_multiple: float = 2.0,
    volume_multiple: float = 1.8,
    min_dollar_volume: float = 50_000_000,
) -> ShockEvent | None:
    sub = bars.sort_index()
    if at not in sub.index:
        return None
    position = int(sub.index.get_indexer(pd.Index([at]))[0])
    if position < 200:
        return None
    pre_close = float(sub["close"].iloc[position - 1])
    sma200 = float(sub["close"].iloc[position - 200 : position].mean())
    if not math.isfinite(pre_close) or pre_close <= sma200 or pre_close < 5.0:
        return None
    atr20 = _atr_before(sub, position)
    event_close = float(sub["close"].iloc[position])
    drop = event_close / pre_close - 1.0
    threshold = max(min_drop, atr_multiple * atr20 / pre_close)
    if drop > -threshold:
        return None
    prior_volume = float(sub["volume"].iloc[position - 20 : position].mean())
    volume_ratio = float(sub["volume"].iloc[position]) / prior_volume if prior_volume > 0 else 0.0
    dollar_volume = float(
        (sub["close"] * sub["volume"]).iloc[position - 20 : position].mean()
    )
    if volume_ratio < volume_multiple or dollar_volume < min_dollar_volume:
        return None
    return ShockEvent(
        ticker=ticker,
        shock_at=pd.Timestamp(at),
        pre_close=pre_close,
        event_close=event_close,
        event_low=float(sub["low"].iloc[position]),
        atr20=atr20,
        drop=drop,
        volume_ratio=volume_ratio,
    )


def confirm_reversal(
    bars: pd.DataFrame,
    shock: ShockEvent,
    *,
    min_wait: int = 2,
    max_wait: int = 5,
) -> ReversalConfirmation | None:
    sub = bars.sort_index()
    if shock.shock_at not in sub.index:
        return None
    position = int(sub.index.get_indexer(pd.Index([shock.shock_at]))[0])
    for offset in range(min_wait, max_wait + 1):
        current = position + offset
        entry = current + 1
        if entry >= len(sub):
            break
        recent = sub.iloc[current - 1 : current + 1]
        if float(recent["close"].min()) < shock.event_low:
            continue
        close = float(sub["close"].iloc[current])
        sma5 = float(sub["close"].iloc[max(0, current - 4) : current + 1].mean())
        recovery = (close - shock.event_low) / (shock.pre_close - shock.event_low)
        if close <= sma5 and recovery < 0.35:
            continue
        entry_price = float(sub["open"].iloc[entry])
        if not math.isfinite(entry_price):
            continue
        return ReversalConfirmation(
            ticker=shock.ticker,
            shock_at=shock.shock_at,
            confirmed_at=pd.Timestamp(sub.index[current]),
            entry_at=pd.Timestamp(sub.index[entry]),
            entry_price=entry_price,
            stop_price=shock.event_low,
        )
    return None
