"""动量轮动策略 vectorbt 回测。

用与实时完全相同的 MomentumRotation.generate 逐月生成目标组合，
按月末调仓构建持仓矩阵，vectorbt 计算组合绩效。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

from quant_signal.config import load_settings
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.momentum_rotation import MomentumRotation

REPORTS = Path(__file__).parent / "reports"


def load_bars(start: date) -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = settings.universe
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(
            tickers, start, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    # 缓存里可能有比 start 更早的历史（比如之前跑过全量回测），必须显式按
    # start 过滤，否则 --start 只在首次拉数据时有效，缓存命中后就是空操作。
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    return store.read_daily_bars(tickers, start=start_dt)


def run_backtest(
    bars: pd.DataFrame,
    lookback_days: int,
    top_n: int,
    min_dollar_volume: float,
    fx_rates: dict[str, float],
    universe: list[str] | None = None,
) -> dict[str, float]:
    settings = load_settings()
    # 默认用 settings.universe，但调用方可以显式传入不同的标的池（比如探索性
    # 回测里根本不在正式 universe 里的标的）——否则 self.universe 和 bars 的
    # 交集会是空集，策略永远选不出任何标的却不报错，非常隐蔽。
    strat = MomentumRotation(
        universe=universe if universe is not None else settings.universe,
        lookback_days=lookback_days,
        top_n=top_n,
        min_dollar_volume=min_dollar_volume,
        ticker_currency=settings.international_tickers,
        fx_rates=fx_rates,
        group_top_n=settings.momentum_group_top_n,
        asset_type=settings.asset_type,
        default_group_top_n=settings.momentum_default_group_top_n,
    )
    close = bars["close"].unstack("ticker").sort_index()
    month_ends = close.groupby(close.index.tz_localize(None).to_period("M")).tail(1).index

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    changes = 0
    prev: set[str] = set()
    for me in month_ends:
        window = bars[bars.index.get_level_values("ts") <= me]
        picks = {s.ticker for s in strat.generate(window)}
        if picks:
            weights.loc[me:, :] = 0.0
            weights.loc[me:, list(picks)] = 1.0 / len(picks)
            changes += len(picks - prev)
            prev = picks

    if not prev:
        raise ValueError(
            "策略在整个回测区间从未选出任何标的——通常是 universe 跟 bars 里"
            "实际包含的标的没有交集，检查 run_backtest 的 universe 参数是否传对"
        )

    pf = vbt.Portfolio.from_orders(
        close=close,
        size=weights,
        size_type="targetpercent",
        freq="1D",
        cash_sharing=True,
        call_seq="auto",
    )
    stats = pf.stats()
    years = (close.index[-1] - close.index[0]).days / 365.25
    total_ret = float(stats["Total Return [%]"]) / 100
    return {
        "annual_return": ((1 + total_ret) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "max_drawdown": float(stats["Max Drawdown [%]"]),
        "turnover_per_year": changes / years,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    bars = load_bars(args.start)
    settings = load_settings()
    base = settings.strategies["momentum_rotation"]
    currencies = set(settings.international_tickers.values())
    fx_rates = fetch_usd_rates(currencies) if currencies else {}

    rows = [
        "| lookback | top_n | 年化% | Sharpe | 最大回撤% | 年换手次数 |",
        "|---|---|---|---|---|---|",
    ]
    for lookback in (30, 60, 90, 120, 252):
        for top_n in (2, 3):
            m = run_backtest(bars, lookback, top_n, float(base["min_dollar_volume"]), fx_rates)
            marker = (
                " ←当前参数"
                if (lookback, top_n) == (int(base["lookback_days"]), int(base["top_n"]))
                else ""
            )
            rows.append(
                f"| {lookback} | {top_n} | {m['annual_return']:.1f} | {m['sharpe']:.2f} |"
                f" {m['max_drawdown']:.1f} | {m['turnover_per_year']:.1f}{marker} |"
            )

    fx_note = ""
    if fx_rates:
        fx_str = "、".join(f"1 USD = {v:,.2f} {k}" for k, v in fx_rates.items())
        fx_note = (
            f"- 国际标的（{', '.join(settings.international_tickers)}）成交额换算使用当前实时汇率"
            f"（{fx_str}）统一套用到全部历史区间，非逐日历史汇率，为近似简化\n"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"momentum_from{args.start:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 动量轮动回测报告\n\n"
        f"- 数据: {args.start} 至今，universe={settings.universe}\n"
        f"- 月末调仓，等权持有 top_n\n"
        f"{fx_note}\n" + "\n".join(rows) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。参数修改由用户决定。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
