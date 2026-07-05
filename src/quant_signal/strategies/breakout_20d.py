from __future__ import annotations

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy


class Breakout20d(Strategy):
    strategy_id = "breakout_20d"
    schedule = "intraday_5min"

    def __init__(
        self,
        universe: list[str],
        high_lookback_days: int = 20,
        volume_multiplier: float = 1.5,
    ) -> None:
        self.universe = universe
        self.high_lookback_days = high_lookback_days
        self.volume_multiplier = volume_multiplier

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        signals: list[Signal] = []
        n = self.high_lookback_days
        for ticker in self.universe:
            if ticker not in bars.index.get_level_values("ticker"):
                continue
            tb = bars.xs(ticker, level="ticker").sort_index()
            if len(tb) < n + 1:
                continue
            today = tb.iloc[-1]          # 当日进行中 bar
            window = tb.iloc[-1 - n : -1]  # 之前 20 根，不含当日
            prior_high = float(window["high"].max())
            avg_vol = float(window["volume"].mean())
            price = float(today["close"])
            vol = float(today["volume"])
            if price > prior_high and vol > self.volume_multiplier * avg_vol:
                signals.append(
                    Signal(
                        ticker=ticker,
                        direction=Direction.BUY,
                        price=price,
                        reason=(
                            f"突破{n}日高点 {prior_high:.2f}，"
                            f"量能 {vol / avg_vol:.1f}x 于均量"
                        ),
                        strategy_id=self.strategy_id,
                        ts=tb.index[-1].to_pydatetime(),
                        extra={"prior_high": prior_high, "volume_ratio": vol / avg_vol},
                    )
                )
        return signals
