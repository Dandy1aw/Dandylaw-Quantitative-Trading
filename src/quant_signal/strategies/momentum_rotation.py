from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class MomentumRotation(Strategy):
    strategy_id = "momentum_rotation"
    schedule = "daily_premarket"

    def __init__(
        self,
        universe: list[str],
        lookback_days: int = 60,
        top_n: int = 3,
        min_dollar_volume: float = 50_000_000,
    ) -> None:
        self.universe = universe
        self.lookback_days = lookback_days
        self.top_n = top_n
        self.min_dollar_volume = min_dollar_volume

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close = bars["close"].unstack("ticker").sort_index()
        volume = bars["volume"].unstack("ticker").sort_index()
        close = close[[t for t in self.universe if t in close.columns]]
        if len(close) < self.lookback_days + 1:
            return []

        momentum = close.iloc[-1] / close.iloc[-1 - self.lookback_days] - 1.0
        dollar_vol_20d = (close * volume).tail(20).mean()
        eligible = momentum[dollar_vol_20d >= self.min_dollar_volume].dropna()
        top = eligible.sort_values(ascending=False).head(self.top_n)

        last_ts = close.index[-1].to_pydatetime()
        weight = round(1.0 / self.top_n, 4) if self.top_n else None
        return [
            Signal(
                ticker=str(t),
                direction=Direction.BUY,
                price=float(close[t].iloc[-1]),
                reason=f"{self.lookback_days}日动量 {mom:+.1%}，排名第{i}",
                strategy_id=self.strategy_id,
                ts=last_ts,
                suggested_weight=weight,
            )
            for i, (t, mom) in enumerate(top.items(), start=1)
        ]
