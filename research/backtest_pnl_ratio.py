"""盈亏比回测：对当前上线策略(动量+仅200线闸门+防御)算 per-trade 盈亏比。

一笔交易 = 某标的从进入持仓到清仓的一次完整往返（close-to-close）。直接从每日
目标权重矩阵里按"连续持有段"切分，避免 vectorbt 对 targetpercent 组合逐日再平衡
产生的碎片交易污染统计。

盈亏比给三个口径，避免单看一个被误导：
  - payoff(平均盈亏比) = 平均盈利% / |平均亏损%|
  - profit_factor(盈利因子) = 总盈利 / 总亏损
  - 配合胜率 + 期望值(每笔平均收益%) 才有意义
对照：基线(无闸门) vs 上线配置(仅200线+防御)。
复用 backtest_trend_gate.build_weights，保证与主回测是同一个策略。
研究脚本，靠真实缓存数据跑通。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest_trend_gate import REPORTS, START, build_weights, load_bars  # noqa: E402

from quant_signal.config import load_settings  # noqa: E402

VARIANTS = [("none", "基线(无闸门)"), ("ma_only", "上线配置(仅200线+防御)")]


def round_trips(weights: pd.DataFrame, close: pd.DataFrame) -> tuple[list[float], int]:
    """切出每个标的的连续持有段，返回 (已平仓往返收益率列表, 未平仓段数)。"""
    closed: list[float] = []
    open_n = 0
    for t in weights.columns:
        w = weights[t].to_numpy()
        c = close[t].to_numpy()
        held = w > 1e-9
        n = len(w)
        i = 0
        while i < n:
            if not held[i]:
                i += 1
                continue
            start = i
            while i < n and held[i]:
                i += 1
            entry = c[start]
            if i < n:                         # 已在第 i 天清仓，按当日收盘卖出
                exit_p, is_open = c[i], False
            else:                             # 持有到区间末尾，仍未平仓
                exit_p, is_open = c[n - 1], True
            if not (np.isnan(entry) or np.isnan(exit_p)) and entry > 0:
                if is_open:
                    open_n += 1
                else:
                    closed.append(exit_p / entry - 1.0)
    return closed, open_n


def trade_metrics(rets: list[float], open_n: int) -> dict[str, float]:
    a = np.array(rets, dtype=float)
    n = len(a)
    wins, losses = a[a > 0], a[a < 0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0   # 负数
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    win_rate = len(wins) / n if n else 0.0
    payoff = (avg_win / abs(avg_loss)) if avg_loss != 0 else float("inf")
    pf_ratio = (gross_win / gross_loss) if gross_loss != 0 else float("inf")
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    return {
        "trades": n, "open": open_n, "win_rate": win_rate * 100,
        "avg_win": avg_win * 100, "avg_loss": avg_loss * 100,
        "payoff": payoff, "profit_factor": pf_ratio, "expectancy": expectancy * 100,
        "best": float(a.max()) * 100 if n else 0.0,
        "worst": float(a.min()) * 100 if n else 0.0,
        "median": float(np.median(a)) * 100 if n else 0.0,
    }


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    print("回测标的:", sorted(bars.index.get_level_values("ticker").unique()))

    rows = []
    for gate, label in VARIANTS:
        weights, close, _ = build_weights(bars, settings, gate, buffer=0.03, weekly_exit=True)
        rets, open_n = round_trips(weights, close)
        m = trade_metrics(rets, open_n)
        print(f"\n=== {label} ===")
        for k, v in m.items():
            print(f"  {k}: {v:.2f}")
        rows.append((label, m))

    hdr = ("| 方案 | 已平仓交易 | 胜率% | 平均盈% | 平均亏% | 盈亏比(payoff) | "
           "盈利因子 | 期望值%/笔 | 中位数% | 最好% | 最差% | 未平仓 |")
    sep = "|" + "---|" * 12
    lines = [hdr, sep]
    for label, m in rows:
        lines.append(
            f"| {label} | {m['trades']:.0f} | {m['win_rate']:.1f} | {m['avg_win']:.1f} | "
            f"{m['avg_loss']:.1f} | {m['payoff']:.2f} | {m['profit_factor']:.2f} | "
            f"{m['expectancy']:.2f} | {m['median']:.1f} | {m['best']:.1f} | {m['worst']:.1f} | "
            f"{m['open']:.0f} |"
        )

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"pnl_ratio_from{START:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 盈亏比回测报告（per-trade 往返）\n\n"
        f"- 区间: {START} 至今，universe={settings.universe}\n"
        "- 一笔交易 = 某标的从建仓到清仓的一次完整往返（含轮动换出/闸门切防御触发的平仓），"
        "close-to-close\n"
        "- 含防御腿(BIL/TLT/GLD)的持有段；未平仓段单列，不计入盈亏比统计\n"
        "- 盈亏比(payoff)=平均盈利/|平均亏损|；盈利因子=总盈利/总亏损；需配合胜率与期望值一起看\n\n"
        + "\n".join(lines) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"\n报告已生成: {out}")


if __name__ == "__main__":
    main()
