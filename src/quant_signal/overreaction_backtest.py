"""Shared, execution-realistic event trade simulation."""

from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd

from quant_signal.strategies.negative_overreaction import (
    ReversalConfirmation,
    ShockEvent,
)


@dataclass(frozen=True)
class EventTrade:
    ticker: str
    shock_at: pd.Timestamp
    entry_at: pd.Timestamp
    entry_price: float
    exit_at: pd.Timestamp
    exit_price: float
    exit_reason: str
    max_adverse_excursion: float
    max_favorable_excursion: float

    @property
    def return_pct(self) -> float:
        return self.exit_price / self.entry_price - 1.0


def simulate_trade(
    bars: pd.DataFrame,
    shock: ShockEvent,
    confirmation: ReversalConfirmation,
    *,
    max_hold: int = 40,
    repair_fraction: float = 0.75,
) -> EventTrade:
    """Close-confirmed stop/repair decisions execute at the following open."""
    sub = bars.sort_index()
    entry_pos = int(sub.index.get_indexer(pd.Index([confirmation.entry_at]))[0])
    target = shock.event_low + repair_fraction * (shock.pre_close - shock.event_low)
    for held in range(1, max_hold + 1):
        decision_pos = entry_pos + held
        exit_pos = decision_pos + 1
        if exit_pos >= len(sub):
            break
        decision_close = float(sub["close"].iloc[decision_pos])
        reason = ""
        if decision_close < shock.event_low:
            reason = "stop"
        elif decision_close >= target:
            reason = "repair"
        elif held == max_hold:
            reason = "time"
        if reason:
            exit_price = float(sub["open"].iloc[exit_pos])
            path = sub.iloc[entry_pos : exit_pos + 1]
            return EventTrade(
                shock.ticker,
                shock.shock_at,
                confirmation.entry_at,
                confirmation.entry_price,
                pd.Timestamp(sub.index[exit_pos]),
                exit_price,
                reason,
                float(path["low"].min()) / confirmation.entry_price - 1.0,
                float(path["high"].max()) / confirmation.entry_price - 1.0,
            )
    last_pos = len(sub) - 1
    path = sub.iloc[entry_pos : last_pos + 1]
    return EventTrade(
        shock.ticker,
        shock.shock_at,
        confirmation.entry_at,
        confirmation.entry_price,
        pd.Timestamp(sub.index[last_pos]),
        float(sub["close"].iloc[last_pos]),
        "end",
        float(path["low"].min()) / confirmation.entry_price - 1.0,
        float(path["high"].max()) / confirmation.entry_price - 1.0,
    )


def summarize_trades(trades: list[EventTrade]) -> dict[str, float]:
    returns = [trade.return_pct for trade in trades]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": float(len(trades)),
        "win_rate": len(wins) / len(returns) if returns else 0.0,
        "avg_return": sum(returns) / len(returns) if returns else 0.0,
        "median_return": float(pd.Series(returns).median()) if returns else 0.0,
        "profit_factor": gross_profit / gross_loss if gross_loss else (math.inf if wins else 0.0),
        "avg_mae": (
            sum(trade.max_adverse_excursion for trade in trades) / len(trades)
            if trades else 0.0
        ),
    }
