"""P1 回测：2x 杠杆 ETF 风险等价折算 vs 等权。

动机：等权 1/n 对 SNXX/MUU/RAM(2x 日内杠杆) 与 SPY 一视同仁，同一份权重承担
约 2 倍波动(按产品说明书构造，非估计值)。折算法：该类标的权重 ÷ 杠杆倍数，
再整体归一(释放的权重按比例摊给其余持仓)。

诚实前提：这些 2x 产品 2024-2025 年才成立，回测里只覆盖最近一小段(牛市段)，
样本不足以做统计显著性判断——折算的依据是"杠杆倍数是产品构造事实"这一先验，
回测只用来展示近期窗口内的实际影响方向，不作为拟合依据。

复用 backtest_momentum 的数据与选股口径(生产分组名额)。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

from backtest_momentum import load_bars

from quant_signal.config import load_settings
from quant_signal.datafeed.fx import fetch_usd_rates
from quant_signal.strategies.momentum_rotation import MomentumRotation

REPORTS = Path(__file__).parent / "reports"
START = date(2023, 7, 5)          # 与 32 池报告同窗口
LEVERAGE = {"SNXX": 2.0, "MUU": 2.0, "RAM": 2.0}


def run(bars: pd.DataFrame, adjust: bool) -> tuple[dict[str, float], int, int]:
    settings = load_settings()
    base = settings.strategies["momentum_rotation"]
    currencies = set(settings.international_tickers.values())
    fx_rates = fetch_usd_rates(currencies) if currencies else {}
    strat = MomentumRotation(
        universe=settings.universe,
        lookback_days=int(base["lookback_days"]),
        top_n=int(base["top_n"]),
        min_dollar_volume=float(base["min_dollar_volume"]),
        ticker_currency=settings.international_tickers,
        fx_rates=fx_rates,
        group_top_n=settings.momentum_group_top_n,
        asset_type=settings.asset_type,
        default_group_top_n=settings.momentum_default_group_top_n,
    )
    close = bars["close"].unstack("ticker").sort_index()
    month_ends = close.groupby(close.index.tz_localize(None).to_period("M")).tail(1).index

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    changes, months_with_lev, total_months = 0, 0, 0
    prev: set[str] = set()
    for me in month_ends:
        window = bars[bars.index.get_level_values("ts") <= me]
        picks = [s.ticker for s in strat.generate(window)]
        if not picks:
            continue
        total_months += 1
        if any(t in LEVERAGE for t in picks):
            months_with_lev += 1
        raw = {t: 1.0 / len(picks) for t in picks}
        if adjust:
            raw = {t: w / LEVERAGE.get(t, 1.0) for t, w in raw.items()}
            s = sum(raw.values())
            raw = {t: w / s for t, w in raw.items()}
        weights.loc[me:, :] = 0.0
        for t, w in raw.items():
            weights.loc[me:, t] = w
        changes += len(set(picks) - prev)
        prev = set(picks)

    pf = vbt.Portfolio.from_orders(
        close=close, size=weights, size_type="targetpercent", freq="1D",
        cash_sharing=True, call_seq="auto",
    )
    stats = pf.stats()
    years = (close.index[-1] - close.index[0]).days / 365.25
    total_ret = float(stats["Total Return [%]"]) / 100
    m = {
        "annual": ((1 + total_ret) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "maxdd": float(stats["Max Drawdown [%]"]),
        "turnover": changes / years,
    }
    return m, months_with_lev, total_months


def main() -> None:
    bars = load_bars(START)
    rows = ["| 方案 | 年化% | Sharpe | 最大回撤% | 年换手 |", "|---|---|---|---|---|"]
    lev_months = 0
    total = 0
    for adjust, label in ((False, "等权(现状)"), (True, "杠杆折算(2x权重÷2后归一)")):
        m, lev_months, total = run(bars, adjust)
        rows.append(
            f"| {label} | {m['annual']:.1f} | {m['sharpe']:.2f} | {m['maxdd']:.1f} | {m['turnover']:.1f} |"
        )
        print(label, m)
    print(f"含2x ETF的调仓月: {lev_months}/{total}")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"lev_adjust_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 2x 杠杆 ETF 风险等价折算回测（P1）\n\n"
        f"- 区间: {START} 至今；32 只生产池，月末调仓，生产分组名额；无趋势闸门\n"
        f"- 折算对象: {sorted(LEVERAGE)}（2x 日内杠杆，倍数为产品构造事实非估计值）\n"
        f"- 含 2x ETF 的调仓月: {lev_months}/{total}\n\n"
        + "\n".join(rows) + "\n\n"
        "## 结论与限制\n\n"
        "- 2x 产品 2024-2025 年才成立，样本只覆盖最近牛市段，**统计上不足以证明折算提升或降低收益**。\n"
        "- 折算的正当性来自先验（同一份权重承担约 2 倍风险是产品构造），回测仅展示近期窗口影响方向。\n"
        "- 决策建议见报告生成后的人工判读；若近期窗口内折算大幅跑输，说明这段行情 2x 敞口是收益来源，\n"
        "  折算=降杠杆，本就预期少赚——关键看回撤/夏普是否换得划算。\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
