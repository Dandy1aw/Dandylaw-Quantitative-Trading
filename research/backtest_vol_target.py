"""S4 组合级波动率目标：在生产配置(动量+仅200线闸门+防御)日收益上叠总闸。

与被证伪的"个股反波动率"不同——那个改**分配**，这个只控**总敞口**：
组合近20日实现波动(年化)超过目标时，次日整体等比降仓(exposure=target/realized,
上限1)，余下现金。标准 overlay 近似(日频、滞后1日、无成本)。
指标从日收益直接算：夏普按252交易日年化。上线条件(事前)：回撤/卡玛改善
且年化损失可接受，否则记负结果。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_trend_gate import build_portfolio, load_bars

from quant_signal.config import load_settings

REPORTS = Path(__file__).parent / "reports"
TARGETS = (0.25, 0.30, 0.35)
VOL_LOOKBACK = 20


def _metrics(r: pd.Series) -> dict[str, float]:
    r = r.fillna(0.0)
    years = (r.index[-1] - r.index[0]).days / 365.25
    total = float((1 + r).prod())
    eq = (1 + r).cumprod()
    maxdd = float(-(eq / eq.cummax() - 1).min()) * 100
    annual = (total ** (1 / years) - 1) * 100
    return {
        "annual": annual,
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
        "maxdd": maxdd,
        "calmar": annual / maxdd if maxdd > 0 else float("inf"),
    }


def main() -> None:
    settings = load_settings()
    bars = load_bars()
    pf, _, _ = build_portfolio(bars, settings, "ma_only", 0.03, weekly_exit=True)
    r = pf.returns()
    base = r if isinstance(r, pd.Series) else r.iloc[:, 0]

    rows = ["| 方案 | 年化% | Sharpe(252d) | 最大回撤% | 卡玛 | 平均敞口 |",
            "|---|---|---|---|---|---|"]
    m0 = _metrics(base)
    rows.append(f"| 基线(无vol目标) | {m0['annual']:.1f} | {m0['sharpe']:.2f} |"
                f" {m0['maxdd']:.1f} | {m0['calmar']:.2f} | 100% |")
    print("基线", {k: round(v, 2) for k, v in m0.items()})

    realized = base.rolling(VOL_LOOKBACK).std() * np.sqrt(252)
    for target in TARGETS:
        exposure = (target / realized).clip(upper=1.0).shift(1).fillna(1.0)
        scaled = base * exposure
        m = _metrics(scaled)
        rows.append(f"| vol目标 {target:.0%} | {m['annual']:.1f} | {m['sharpe']:.2f} |"
                    f" {m['maxdd']:.1f} | {m['calmar']:.2f} | {float(exposure.mean()):.0%} |")
        print(f"vt{target:.0%}", {k: round(v, 2) for k, v in m.items()},
              "avg_exp", round(float(exposure.mean()), 2))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"vol_target_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# S4 组合级波动率目标（生产配置之上叠总闸）\n\n"
        "- 基线=动量+仅200线闸门+防御(2016至今, 自选池, 生产分组名额)\n"
        f"- 敞口=min(1, 目标÷近{VOL_LOOKBACK}日实现波动)，滞后1日，余下现金；无成本\n"
        "- 夏普按252日年化；读数须叠加 S3 的 ±5pp 年化噪声带\n\n"
        + "\n".join(rows) + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
