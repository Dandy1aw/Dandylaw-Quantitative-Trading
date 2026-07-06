"""统一回测指标：收益/风险/风险收益/稳定性/交易/成本。研究脚本共用。

- 组合指标从日收益序列算(rf=0)：累计收益、年化CAGR、年化波动、夏普、最大回撤、卡玛、超额(vs基准)。
- 成本：按目标权重变化的两边换手 × 每边成本(bps)扣减，得净年化与成本拖累——
  比对 targetpercent 组合逐日再平衡收手续费更真实(只在调仓/止损日计成本)。
- 交易稳定性指标(胜率/盈利因子/交易数)由 backtest_pnl_ratio 的往返统计给出。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _total_cagr(daily: pd.Series, years: float) -> tuple[float, float]:
    total = float((1.0 + daily).prod() - 1.0)
    cagr = ((1.0 + total) ** (1.0 / years) - 1.0) * 100 if years > 0 else 0.0
    return total * 100, cagr


def _max_drawdown(daily: pd.Series) -> float:
    eq = (1.0 + daily).cumprod()
    return float(-(eq / eq.cummax() - 1.0).min()) * 100


def _sharpe(daily: pd.Series) -> float:
    sd = float(daily.std())
    return float(daily.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else 0.0


def portfolio_metrics(
    gross_daily: pd.Series,
    weights: pd.DataFrame,
    bench_close: pd.Series,
    cost_per_side: float,
) -> dict[str, float]:
    """gross_daily: 组合日收益(未扣成本)；weights: 每日目标权重；bench_close: 基准收盘。"""
    r = gross_daily.fillna(0.0)
    idx = r.index
    years = (idx[-1] - idx[0]).days / 365.25

    total, cagr = _total_cagr(r, years)
    vol = float(r.std() * np.sqrt(TRADING_DAYS)) * 100
    sharpe = _sharpe(r)
    maxdd = _max_drawdown(r)
    calmar = cagr / maxdd if maxdd > 0 else float("inf")

    bench_daily = bench_close.pct_change(fill_method=None).reindex(idx).fillna(0.0)
    _, bench_cagr = _total_cagr(bench_daily, years)
    excess = cagr - bench_cagr

    # 两边换手(目标权重变化的绝对值之和)，只在调仓/止损改变目标时产生
    turn_daily = weights.diff().abs().sum(axis=1).reindex(idx).fillna(0.0)
    ann_turnover = float(turn_daily.sum() / years) if years > 0 else 0.0
    net_daily = r - turn_daily * cost_per_side
    _, net_cagr = _total_cagr(net_daily, years)
    net_sharpe = _sharpe(net_daily)
    cost_drag = cagr - net_cagr

    return {
        "total_return": total, "cagr": cagr, "excess": excess,
        "maxdd": maxdd, "vol": vol, "sharpe": sharpe, "calmar": calmar,
        "ann_turnover": ann_turnover, "net_cagr": net_cagr,
        "net_sharpe": net_sharpe, "cost_drag": cost_drag,
    }
