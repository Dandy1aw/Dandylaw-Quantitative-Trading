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
        ticker_currency: dict[str, str] | None = None,
        fx_rates: dict[str, float] | None = None,
    ) -> None:
        self.universe = universe
        self.lookback_days = lookback_days
        self.top_n = top_n
        self.min_dollar_volume = min_dollar_volume
        # 非美元计价标的的成交额换算：ticker -> 币种、币种 -> 1美元兑换数量
        self.ticker_currency = ticker_currency or {}
        self.fx_rates = fx_rates or {}

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close = bars["close"].unstack("ticker").sort_index()
        volume = bars["volume"].unstack("ticker").sort_index()
        close = close[[t for t in self.universe if t in close.columns]]
        if len(close) < self.lookback_days + 1:
            return []

        momentum = close.iloc[-1] / close.iloc[-1 - self.lookback_days] - 1.0
        dollar_vol_native = (close * volume).tail(20).mean()
        fx_divisor = pd.Series(
            {t: self.fx_rates.get(self.ticker_currency.get(t, "USD"), 1.0) for t in close.columns}
        )
        dollar_vol_usd = dollar_vol_native / fx_divisor
        eligible = momentum[dollar_vol_usd >= self.min_dollar_volume].dropna()
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
