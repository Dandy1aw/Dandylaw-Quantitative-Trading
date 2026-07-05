from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class MacdCross(Strategy):
    """MACD 金叉/死叉：趋势跟踪，跟动量轮动（相对排名）不同视角，看的是绝对趋势拐点。"""

    strategy_id = "macd_cross"
    schedule = "daily_premarket"

    def __init__(
        self,
        universe: list[str],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> None:
        self.universe = universe
        self.fast = fast
        self.slow = slow
        self.signal_period = signal

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        close_all = bars["close"].unstack("ticker").sort_index()
        signals: list[Signal] = []
        min_len = self.slow + self.signal_period + 1
        for t in self.universe:
            if t not in close_all.columns:
                continue
            series = close_all[t].dropna()
            if len(series) < min_len:
                continue
            ema_fast = series.ewm(span=self.fast, adjust=False).mean()
            ema_slow = series.ewm(span=self.slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=self.signal_period, adjust=False).mean()
            diff_today = float(macd_line.iloc[-1] - signal_line.iloc[-1])
            diff_yesterday = float(macd_line.iloc[-2] - signal_line.iloc[-2])

            price = float(series.iloc[-1])
            ts = series.index[-1].to_pydatetime()
            extra: dict[str, object] = {
                "macd": float(macd_line.iloc[-1]),
                "signal": float(signal_line.iloc[-1]),
            }
            if diff_yesterday <= 0 < diff_today:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.BUY,
                        price=price,
                        reason=f"MACD金叉({self.fast}/{self.slow}/{self.signal_period})",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra=extra,
                    )
                )
            elif diff_yesterday >= 0 > diff_today:
                signals.append(
                    Signal(
                        ticker=t,
                        direction=Direction.SELL,
                        price=price,
                        reason=f"MACD死叉({self.fast}/{self.slow}/{self.signal_period})",
                        strategy_id=self.strategy_id,
                        ts=ts,
                        extra=extra,
                    )
                )
        return signals
