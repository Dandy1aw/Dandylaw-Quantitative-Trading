"""S3 调仓时点运气：月度锚点错位的年化离散度 + 分批错峰合成 + 每日评估版对照。

- 锚点变体：每月最后交易日往前挪 0/5/10/15 个交易日作为调仓日，其余口径同 S1/S2
  (全局 top5, 60日动量, 等权, 无闸门)。四个变体的差异 = 纯"哪天调仓"的运气。
- 错峰合成：资金均分 4 份分别按四个锚点跑，日收益取均值——分散时点运气的标准解法。
- 每日评估版：生产实际是每天评估、排名变化即换，单独跑一版对照回测口径差异。
- 本报告统一用日收益直接算指标：夏普按 252 交易日年化(与 vbt 365 口径不同,组内一致)。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt

from backtest_control_universe import CONTROL, load_all_bars, window

from quant_signal.config import load_settings
from quant_signal.strategies.momentum_rotation import MomentumRotation

REPORTS = Path(__file__).parent / "reports"
START = date(2016, 1, 1)
OFFSETS = (0, 5, 10, 15)


def _strategy(universe: list[str]) -> MomentumRotation:
    return MomentumRotation(universe=universe, lookback_days=60, top_n=5,
                            min_dollar_volume=50e6)


def _weights_for_anchors(bars: pd.DataFrame, universe: list[str],
                         anchors: pd.DatetimeIndex) -> pd.DataFrame:
    strat = _strategy(universe)
    close = bars["close"].unstack("ticker").sort_index()
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for ts in anchors:
        picks = {s.ticker for s in strat.generate(bars[bars.index.get_level_values("ts") <= ts])}
        if picks:
            weights.loc[ts:, :] = 0.0
            weights.loc[ts:, list(picks)] = 1.0 / len(picks)
    return weights


def _returns(bars: pd.DataFrame, weights: pd.DataFrame) -> pd.Series:
    close = bars["close"].unstack("ticker").sort_index()
    pf = vbt.Portfolio.from_orders(close=close, size=weights, size_type="targetpercent",
                                   freq="1D", cash_sharing=True, call_seq="auto")
    r = pf.returns()
    return r if isinstance(r, pd.Series) else r.iloc[:, 0]


def _metrics(r: pd.Series) -> dict[str, float]:
    r = r.fillna(0.0)
    years = (r.index[-1] - r.index[0]).days / 365.25
    total = float((1 + r).prod())
    eq = (1 + r).cumprod()
    return {
        "annual": (total ** (1 / years) - 1) * 100,
        "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0,
        "maxdd": float(-(eq / eq.cummax() - 1).min()) * 100,
    }


def _anchor_dates(close_index: pd.DatetimeIndex, offset: int) -> pd.DatetimeIndex:
    positions = []
    series = pd.Series(range(len(close_index)), index=close_index)
    month_end_pos = series.groupby(close_index.tz_localize(None).to_period("M")).max()
    for pos in month_end_pos:
        positions.append(max(int(pos) - offset, 0))
    return close_index[sorted(set(positions))]


def main() -> None:
    settings = load_settings()
    bars = window(load_all_bars(), START)
    have = set(bars.index.get_level_values("ticker"))
    ctrl = [t for t in CONTROL if t in have]
    close_index = pd.DatetimeIndex(bars["close"].unstack("ticker").sort_index().index)

    sections: list[str] = []
    for plabel, uni in (("自选池", list(settings.universe)), ("对照池", ctrl)):
        rows = [f"### {plabel}（2016 至今, 全局top5）", "",
                "| 变体 | 年化% | Sharpe(252d) | 最大回撤% |", "|---|---|---|---|"]
        rets: list[pd.Series] = []
        annuals: list[float] = []
        for off in OFFSETS:
            w = _weights_for_anchors(bars, uni, _anchor_dates(close_index, off))
            r = _returns(bars, w)
            m = _metrics(r)
            rets.append(r)
            annuals.append(m["annual"])
            rows.append(f"| 锚点=月末-{off}日 | {m['annual']:.1f} | {m['sharpe']:.2f} | {m['maxdd']:.1f} |")
            print(plabel, f"off={off}", {k: round(v, 2) for k, v in m.items()})
        combined = pd.concat(rets, axis=1).fillna(0.0).mean(axis=1)
        mc = _metrics(combined)
        rows.append(f"| **4份错峰合成** | {mc['annual']:.1f} | {mc['sharpe']:.2f} | {mc['maxdd']:.1f} |")
        spread = max(annuals) - min(annuals)
        rows.append("")
        rows.append(f"- 时点运气(四锚点年化 max−min)：**{spread:.1f}pp**")
        print(plabel, "tranche", {k: round(v, 2) for k, v in mc.items()}, "spread", round(spread, 1))
        sections.append("\n".join(rows))

    # 每日评估版(生产口径, 只跑自选池控制耗时)
    uni = list(settings.universe)
    strat = _strategy(uni)
    close = bars["close"].unstack("ticker").sort_index()
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    prev: set[str] = set()
    for i, ts in enumerate(close.index):
        if i < 61:
            continue
        picks = {s.ticker for s in strat.generate(bars[bars.index.get_level_values("ts") <= ts])}
        if picks and picks != prev:
            weights.loc[ts:, :] = 0.0
            weights.loc[ts:, list(picks)] = 1.0 / len(picks)
            prev = picks
    md = _metrics(_returns(bars, weights))
    print("每日评估版(自选池)", {k: round(v, 2) for k, v in md.items()})
    daily_row = (f"### 每日评估版（自选池，生产实际口径）\n\n"
                 f"| 变体 | 年化% | Sharpe(252d) | 最大回撤% |\n|---|---|---|---|\n"
                 f"| 每日评估·排名变化即换 | {md['annual']:.1f} | {md['sharpe']:.2f} | {md['maxdd']:.1f} |")

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"timing_luck_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# S3 调仓时点运气 + 分批错峰 + 每日评估口径\n\n"
        "- 同一策略只挪调仓日(月末-0/5/10/15交易日)；差异=纯时点运气\n"
        "- 夏普按252交易日年化(与S1/S2的vbt 365口径数值不可直接互比)\n\n"
        + "\n\n".join(sections) + "\n\n" + daily_row + "\n\n"
        "> 回测结果仅供评估，不构成投资建议。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")


if __name__ == "__main__":
    main()
