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
        if close.empty:
            return []

        # 按各标的自身的有效数据取"最新一行"，不用全市场统一的行位置——
        # 否则不同交易日历的标的（如美股假期但港股/韩股照常交易）会让
        # 缺当日数据的标的被错误判定为 NaN，动量排名整体失真。
        momentum: dict[str, float] = {}
        last_price: dict[str, float] = {}
        dollar_vol_usd: dict[str, float] = {}
        for t in close.columns:
            series = close[t].dropna()
            if len(series) < self.lookback_days + 1:
                continue
            momentum[t] = float(series.iloc[-1] / series.iloc[-1 - self.lookback_days] - 1.0)
            last_price[t] = float(series.iloc[-1])
            vol = volume[t].reindex(series.index)
            native_dv = float((series * vol).tail(20).mean())
            fx = self.fx_rates.get(self.ticker_currency.get(t, "USD"), 1.0)
            dollar_vol_usd[t] = native_dv / fx

        eligible = {t: m for t, m in momentum.items() if dollar_vol_usd.get(t, 0.0) >= self.min_dollar_volume}
        top = sorted(eligible.items(), key=lambda kv: kv[1], reverse=True)[: self.top_n]

        last_ts = close.index[-1].to_pydatetime()
        weight = round(1.0 / self.top_n, 4) if self.top_n else None
        return [
            Signal(
                ticker=t,
                direction=Direction.BUY,
                price=last_price[t],
                reason=f"{self.lookback_days}日动量 {mom:+.1%}，排名第{i}",
                strategy_id=self.strategy_id,
                ts=last_ts,
                suggested_weight=weight,
            )
            for i, (t, mom) in enumerate(top, start=1)
        ]
