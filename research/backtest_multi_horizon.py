"""S2 多周期动量集成回测：score=mean(63/126/252日动量) vs 单60日。

假设：单周期是点估计含"选参运气"；多周期合成更稳。上线条件(事前定)：
对照池与自选池**同时**不差于单周期(夏普/回撤)，才改生产配置。
注意：252日要求使多周期变体前一年不可交易，窗口内有效期更短；两变体
都从各自可交易起点算年化，对比以 Sharpe/回撤为主。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from backtest_control_universe import CONTROL, load_all_bars, window
from backtest_momentum import run_backtest

from quant_signal.config import load_settings

REPORTS = Path(__file__).parent / "reports"
MULTI = [63, 126, 252]


def main() -> None:
    settings = load_settings()
    bars = load_all_bars()
    have = set(bars.index.get_level_values("ticker"))
    ctrl = [t for t in CONTROL if t in have]
    pools = [("对照池", ctrl), ("自选池", list(settings.universe))]

    sections: list[str] = []
    for start, wlabel in ((date(2016, 1, 1), "2016 至今"), (date(2023, 7, 5), "2023-07 至今")):
        wb = window(bars, start)
        rows = [f"### 窗口：{wlabel}", "",
                "| 池子 | 变体 | 年化% | Sharpe | 最大回撤% | 年换手 |", "|---|---|---|---|---|---|"]
        for plabel, uni in pools:
            for vlabel, lbs in (("单60日", None), ("63/126/252合成", MULTI)):
                m = run_backtest(wb, 60, 5, 50e6, {}, universe=uni,
                                 lookbacks=lbs, group_top_n={}, default_group_top_n={})
                rows.append(
                    f"| {plabel} | {vlabel} | {m['annual_return']:.1f} | {m['sharpe']:.2f} |"
                    f" {m['max_drawdown']:.1f} | {m['turnover_per_year']:.1f} |"
                )
                print(wlabel, plabel, vlabel, {k: round(v, 2) for k, v in m.items()})
        sections.append("\n".join(rows))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"multi_horizon_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# S2 多周期动量集成 vs 单60日\n\n"
        "- 两池均为全局 top5、5千万门槛、月末调仓等权、无闸门；Sharpe 为 vbt 365日口径(组内相对比较)\n"
        "- 多周期要求 252 日历史，窗口前段不可交易；年化各自从可交易起点算\n\n"
        + "\n\n".join(sections) + "\n\n"
        "## 上线条件(事前写定)\n\n两池同时不差于单周期(夏普/回撤)才改生产。\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
