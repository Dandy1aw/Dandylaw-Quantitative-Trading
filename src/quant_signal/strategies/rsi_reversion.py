from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class RsiReversion(Strategy):
    """RSI 均值回归：超卖看涨、超买看跌。与动量轮动互补（追强势 vs 抓短期超跌反弹/超涨回调）。"""

    strategy_id = "rsi_reversion"
    schedule = "daily_premarket"

    def __init__(
        self,
        universe: list[str],
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
    ) -> None:
        self.universe = universe
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close_all = bars["close"].unstack("ticker").sort_index()
        signals: list[Signal] = []
        for t in self.universe:
            if t not in close_all.columns:
                continue
            series = close_all[t].dropna()
            if len(series) < self.period + 1:
                continue
            delta = series.diff().dropna()
            gain = delta.clip(lower=0.0)
            loss = -delta.clip(upper=0.0)
            avg_gain = float(gain.rolling(self.period).mean().iloc[-1])
            avg_loss = float(loss.rolling(self.period).mean().iloc[-1])
            rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

            price = float(series.iloc[-1])
            ts = series.index[-1].to_pydatetime()
            if rsi < self.oversold:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.BUY,
                        price=price,
                        reason=f"RSI({self.period})={rsi:.1f}，超卖(<{self.oversold:.0f})，均值回归看涨",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra={"rsi": rsi, "target_buy": price},  # 超卖即买点,目标价=触发价
                    )
                )
            elif rsi > self.overbought:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.SELL,
                        price=price,
                        reason=f"RSI({self.period})={rsi:.1f}，超买(>{self.overbought:.0f})，均值回归看跌",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra={"rsi": rsi},
                    )
                )
        return signals
