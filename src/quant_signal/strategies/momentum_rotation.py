from __future__ import annotations

from collections import defaultdict

import pandas as pd

from quant_signal.strategies.base import Direction, Signal, Strategy

_GROUP_LABELS = {"HKD": "港股组", "KRW": "韩股组"}
_DEFAULT_GROUP_LABEL = "美股组"


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
        group_top_n: dict[str, int] | None = None,
    ) -> None:
        self.universe = universe
        self.lookback_days = lookback_days
        self.top_n = top_n
        self.min_dollar_volume = min_dollar_volume
        # 非美元计价标的的成交额换算：ticker -> 币种、币种 -> 1美元兑换数量
        self.ticker_currency = ticker_currency or {}
        self.fx_rates = fx_rates or {}
        # 按币种分组的独立名额（如 {"HKD": 1, "KRW": 1}）；未配置的币种
        # （含所有 USD 标的）归入默认组，用 top_n。空字典 = 全局统一排名。
        self.group_top_n = group_top_n or {}

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

        # 按币种分组，组内独立排名取各自名额——组之间互不挤占彼此的名额
        groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for t, m in eligible.items():
            ccy = self.ticker_currency.get(t)
            key = ccy if ccy in self.group_top_n else "_default"
            groups[key].append((t, m))

        selected: list[tuple[str, float, str]] = []
        for key, items in groups.items():
            n = self.group_top_n[key] if key != "_default" else self.top_n
            label = _GROUP_LABELS.get(key, _DEFAULT_GROUP_LABEL)
            top_in_group = sorted(items, key=lambda kv: kv[1], reverse=True)[:n]
            selected += [(t, m, label) for t, m in top_in_group]

        selected.sort(key=lambda x: x[1], reverse=True)  # 仅用于展示排名，不影响入选

        last_ts = close.index[-1].to_pydatetime()
        weight = round(1.0 / len(selected), 4) if selected else None
        return [
            Signal(
                ticker=t,
                direction=Direction.BUY,
                price=last_price[t],
                reason=f"{self.lookback_days}日动量 {mom:+.1%}，{label}第{i}",
                strategy_id=self.strategy_id,
                ts=last_ts,
                suggested_weight=weight,
                extra={"momentum_60d": mom, "rank": i},
            )
            for i, (t, mom, label) in enumerate(selected, start=1)
        ]
