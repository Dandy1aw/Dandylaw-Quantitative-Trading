from __future__ import annotations

from collections import defaultdict

import pandas as pd
import structlog

from quant_signal.strategies.base import Direction, Signal, Strategy

_GROUP_LABELS = {"HKD": "港股组", "KRW": "韩股组"}
_DEFAULT_GROUP_LABEL = "美股组"
log = structlog.get_logger()


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
        asset_type: dict[str, str] | None = None,
        default_group_top_n: dict[str, int] | None = None,
        leverage_factor: dict[str, float] | None = None,
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
        # 默认组(USD)内部再按资产类型细分：ticker -> "ETF"|"STOCK"，
        # default_group_top_n 如 {"ETF": 2, "STOCK": 3}。不传就不细分，
        # 默认组仍是整体用 top_n（向后兼容原有行为）。
        self.asset_type = asset_type or {}
        self.default_group_top_n = default_group_top_n or {}
        # P1 风险等价折算：ticker -> 杠杆倍数(如 2x 日内杠杆 ETF 为 2.0)。
        # 建议权重 = (等权 ÷ 倍数) 整体归一——同一份权重承担的风险与 1x 标的对齐。
        # 倍数是产品构造事实(说明书)，非拟合参数；回测见 research/backtest_lev_adjust.py。
        self.leverage_factor = leverage_factor or {}

    def _compute(
        self, bars: pd.DataFrame
    ) -> tuple[dict[str, float], dict[str, float], pd.Timestamp | None]:
        """算出通过成交额过滤的标的动量与最新价，返回 (eligible动量, 最新价, 最新bar时间)。"""
        close = bars["close"].unstack("ticker").sort_index()
        volume = bars["volume"].unstack("ticker").sort_index()
        close = close[[t for t in self.universe if t in close.columns]]
        if close.empty:
            return {}, {}, None

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
            currency = self.ticker_currency.get(t, "USD")
            if currency != "USD" and currency not in self.fx_rates:
                log.warning("momentum.missing_fx", ticker=t, currency=currency)
                continue
            fx = self.fx_rates.get(currency, 1.0)
            dollar_vol_usd[t] = native_dv / fx

        eligible = {t: m for t, m in momentum.items() if dollar_vol_usd.get(t, 0.0) >= self.min_dollar_volume}
        return eligible, last_price, close.index[-1]

    def rank(self, bars: pd.DataFrame) -> list[tuple[str, float, float]]:
        """全池按动量降序：返回 (ticker, 动量, 最新价)，供 Top5买/Top3卖 榜单用。
        与 generate 用同一套动量/成交额口径，只是不做分组截断、返回全部合格标的。"""
        eligible, last_price, _ = self._compute(bars)
        return sorted(
            ((t, m, last_price[t]) for t, m in eligible.items()),
            key=lambda x: x[1], reverse=True,
        )

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        eligible, last_price, last_bar_ts = self._compute(bars)
        if not eligible or last_bar_ts is None:
            return []

        # 按币种分组，组内独立排名取各自名额——组之间互不挤占彼此的名额。
        # 默认组(USD)若配置了 default_group_top_n，内部再按资产类型细分。
        groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
        labels: dict[str, str] = {}
        for t, m in eligible.items():
            ccy = self.ticker_currency.get(t)
            if ccy in self.group_top_n:
                key = ccy
                labels[key] = _GROUP_LABELS.get(ccy, ccy)
            elif self.default_group_top_n:
                atype = self.asset_type.get(t, "STOCK")
                key = f"_default:{atype}"
                labels[key] = "美股ETF组" if atype == "ETF" else "美股个股组"
            else:
                key = "_default"
                labels[key] = _DEFAULT_GROUP_LABEL
            groups[key].append((t, m))

        # 每组内独立按动量排名取名额，名次(rank)按"组内"算——各组都从第1起，
        # 与"分组标签"一致；不用全局名次（否则'美股组'标签却配全局'第3'会自相矛盾）。
        selected: list[tuple[str, float, str, int]] = []
        for key, items in groups.items():
            if key in self.group_top_n:
                n = self.group_top_n[key]
            elif key.startswith("_default:"):
                n = self.default_group_top_n.get(key.split(":", 1)[1], 0)
            else:
                n = self.top_n
            top_in_group = sorted(items, key=lambda kv: kv[1], reverse=True)[:n]
            for gi, (t, m) in enumerate(top_in_group, start=1):
                selected.append((t, m, labels[key], gi))

        # 展示顺序仍按动量降序（卡片读起来高→低），但每条的名次是组内名次
        selected.sort(key=lambda x: x[1], reverse=True)

        last_ts = last_bar_ts.to_pydatetime()
        # 等权基础上做杠杆折算(无杠杆配置时 raw 全相等，退化为原等权行为)
        raw = {t: 1.0 / self.leverage_factor.get(t, 1.0) for t, _, _, _ in selected}
        total = sum(raw.values())
        weights = {t: round(v / total, 4) for t, v in raw.items()} if total else {}
        return [
            Signal(
                ticker=t,
                direction=Direction.BUY,
                price=last_price[t],
                reason=f"{self.lookback_days}日动量 {mom:+.1%}，{label}第{gi}",
                strategy_id=self.strategy_id,
                ts=last_ts,
                suggested_weight=weights.get(t),
                extra={"momentum_60d": mom, "rank": gi},
            )
            for t, mom, label, gi in selected
        ]
