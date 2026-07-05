from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class BollingerBreakout(Strategy):
    """布林带突破：波动率基础的区间突破/跌破，含当日在滚动窗口内（标准布林带惯例，
    与 breakout_20d 的"不含当日"口径不同——两者是不同指标，各自遵循自己的惯例）。"""

    strategy_id = "bollinger_breakout"
    schedule = "daily_premarket"

    def __init__(
        self,
        universe: list[str],
        period: int = 20,
        num_std: float = 2.0,
    ) -> None:
        self.universe = universe
        self.period = period
        self.num_std = num_std

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close_all = bars["close"].unstack("ticker").sort_index()
        signals: list[Signal] = []
        for t in self.universe:
            if t not in close_all.columns:
                continue
            series = close_all[t].dropna()
            if len(series) < self.period:
                continue
            window = series.tail(self.period)
            mid = float(window.mean())
            std = float(window.std())
            upper = mid + self.num_std * std
            lower = mid - self.num_std * std
            price = float(series.iloc[-1])
            ts = series.index[-1].to_pydatetime()
            extra: dict[str, object] = {"upper": upper, "mid": mid, "lower": lower}
            if price > upper:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.BUY,
                        price=price,
                        reason=f"突破布林上轨 {upper:.2f}（{self.period}日,{self.num_std:.0f}倍标准差）",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra=extra,
                    )
                )
            elif price < lower:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.SELL,
                        price=price,
                        reason=f"跌破布林下轨 {lower:.2f}（{self.period}日,{self.num_std:.0f}倍标准差）",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra=extra,
                    )
                )
        return signals
