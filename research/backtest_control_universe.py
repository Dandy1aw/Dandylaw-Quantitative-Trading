"""S1 对照池实验：同一动量策略跑 纳指100近似成分池 vs 自选池 vs 基准买入持有。

目的：回答"alpha 是否真实存在"。自选池是手工挑过的赢家(幸存者偏差)，回测数字
必然高估；若策略逻辑真有效，在没挑过的对照池上也应显著跑赢 SPY/QQQ 买入持有。

诚实声明：
- 对照池用**当前**纳指100近似成分(~70只)，仍含"入指数即幸存"偏差，非 point-in-time；
  它消除的是"手工挑赢家"偏差。绝对数字仍偏乐观，只用于相对判断。
- 部分成分(ABNB/CEG/GEHC/CRWD等)上市晚，早期窗口自动因历史不足跳过。
- 两池用**完全相同**配置：60日动量、全局 top5、5000万美元成交额门槛、月末调仓等权。
- Sharpe 为 vectorbt 365日历日年化口径(偏高)，仅作组内相对比较。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

from backtest_momentum import run_backtest

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource

REPORTS = Path(__file__).parent / "reports"

# 纳指100 当前成分近似(大市值流动票为主, 2026年初口径)
CONTROL = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX",
    "AMD", "PEP", "LIN", "ADBE", "CSCO", "TMUS", "QCOM", "INTU", "AMAT", "TXN",
    "CMCSA", "ISRG", "HON", "BKNG", "AMGN", "VRTX", "ADP", "PANW", "GILD", "SBUX",
    "MU", "LRCX", "ADI", "MDLZ", "REGN", "KLAC", "SNPS", "CDNS", "PYPL", "CRWD",
    "MAR", "CSX", "ABNB", "ORLY", "NXPI", "CTAS", "PCAR", "MNST", "ROP", "FTNT",
    "WDAY", "CEG", "DXCM", "TEAM", "IDXX", "ROST", "KDP", "EA", "VRSK", "FAST",
    "CTSH", "KHC", "LULU", "ODFL", "MRVL", "ON", "CPRT", "ADSK", "BIIB", "EXC",
    "XEL", "GEHC",
]
BENCH = ["SPY", "QQQ"]
START_LONG = date(2016, 1, 1)
START_SHORT = date(2023, 7, 5)


def load_all_bars() -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = sorted(set(CONTROL) | set(settings.universe) | set(BENCH))
    missing = [t for t in tickers if store.daily_bar_count(t) < 100]
    if missing:
        print(f"fetching {len(missing)} tickers from yfinance...")
        df = YFinanceSource().fetch_daily_bars(
            missing, START_LONG, datetime.now(timezone.utc).date()
        )
        store.write_daily_bars(df, source="yfinance")
    start_dt = datetime(START_LONG.year, START_LONG.month, START_LONG.day, tzinfo=timezone.utc)
    return store.read_daily_bars(tickers, start=start_dt)


def buy_hold(bars: pd.DataFrame, ticker: str, start: date) -> dict[str, float]:
    close = bars["close"].unstack("ticker").sort_index()
    close = close.loc[close.index >= pd.Timestamp(start, tz="UTC"), ticker].dropna()
    pf = vbt.Portfolio.from_holding(close, freq="1D")
    stats = pf.stats()
    years = (close.index[-1] - close.index[0]).days / 365.25
    total = float(stats["Total Return [%]"]) / 100
    return {
        "annual_return": ((1 + total) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "max_drawdown": float(stats["Max Drawdown [%]"]),
        "turnover_per_year": 0.0,
    }


def window(bars: pd.DataFrame, start: date) -> pd.DataFrame:
    return bars[bars.index.get_level_values("ts") >= pd.Timestamp(start, tz="UTC")]


def main() -> None:
    settings = load_settings()
    bars = load_all_bars()
    have = sorted(set(bars.index.get_level_values("ticker")))
    ctrl = [t for t in CONTROL if t in have]
    print(f"对照池可用 {len(ctrl)}/{len(CONTROL)}")

    sections: list[str] = []
    for start, label in ((START_LONG, "2016 至今"), (START_SHORT, "2023-07 至今")):
        wb = window(bars, start)
        rows = [
            f"### 窗口：{label}",
            "",
            "| 组合 | 年化% | Sharpe(365d口径) | 最大回撤% | 年换手 |",
            "|---|---|---|---|---|",
        ]
        for name, m in (
            ("对照池(纳指100近似, top5)", run_backtest(
                wb, 60, 5, 50e6, {}, universe=ctrl,
                group_top_n={}, default_group_top_n={})),
            ("自选池(同配置 top5)", run_backtest(
                wb, 60, 5, 50e6, {}, universe=list(settings.universe),
                group_top_n={}, default_group_top_n={})),
            ("SPY 买入持有", buy_hold(bars, "SPY", start)),
            ("QQQ 买入持有", buy_hold(bars, "QQQ", start)),
        ):
            rows.append(
                f"| {name} | {m['annual_return']:.1f} | {m['sharpe']:.2f} |"
                f" {m['max_drawdown']:.1f} | {m['turnover_per_year']:.1f} |"
            )
            print(label, name, {k: round(v, 2) for k, v in m.items()})
        sections.append("\n".join(rows))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"control_universe_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# S1 对照池实验：动量策略的 alpha 是否真实存在\n\n"
        f"- 对照池：纳指100当前成分近似({len(ctrl)}只可用)；与自选池用完全相同配置"
        "(60日动量/全局top5/5千万美元门槛/月末调仓等权，无分组名额、无趋势闸门)\n"
        "- **诚实声明**：当前成分仍含'入指数即幸存'偏差(非 point-in-time)；上市晚的成分"
        "在早期窗口因历史不足自动缺席；Sharpe 为 vectorbt 365日口径仅供组内相对比较\n\n"
        + "\n\n".join(sections) + "\n\n"
        "## 判读标准(事前写定)\n\n"
        "- 对照池显著跑赢 SPY/QQQ 买入持有 → 动量策略逻辑可信，自选池数字的一部分是真 alpha\n"
        "- 对照池不及/仅追平基准 → 自选池回测的超额主要来自'挑对了票'，系统应定位为"
        "'自选池择时器'，优化重心转向风控\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
