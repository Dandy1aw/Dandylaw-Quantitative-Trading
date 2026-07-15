from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from quant_signal.candidate_lanes import discover_candidates
from quant_signal.config import CandidateLaneSettings, MarketRegimeSettings
from quant_signal.market_regime import classify_market_regime


def _portfolio_return(
    close: pd.DataFrame, current: pd.Timestamp, following: pd.Timestamp, symbols: set[str]
) -> float:
    if not symbols:
        return 0.0
    returns: list[float] = []
    for symbol in symbols:
        try:
            before = float(close.loc[current, symbol])
            after = float(close.loc[following, symbol])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(before) and math.isfinite(after) and before > 0:
            returns.append(after / before - 1.0)
    return float(np.mean(returns)) if returns else 0.0


def _turnover(previous: set[str], current: set[str]) -> float:
    previous_weight = 1.0 / len(previous) if previous else 0.0
    current_weight = 1.0 / len(current) if current else 0.0
    symbols = previous | current
    return 0.5 * sum(
        abs(
            (current_weight if symbol in current else 0.0)
            - (previous_weight if symbol in previous else 0.0)
        )
        for symbol in symbols
    )


def replay_candidate_lanes(
    bars: pd.DataFrame,
    members: set[str],
    *,
    regime_settings: MarketRegimeSettings,
    lane_settings: CandidateLaneSettings,
    transaction_cost_bps: float = 5.0,
    point_in_time_members: Mapping[date, set[str]] | None = None,
) -> dict[str, float | int | bool]:
    """One-session-delay replay that reuses production regime and lane functions."""
    if bars.empty:
        raise ValueError("bars must not be empty")
    close = bars["close"].unstack("ticker").sort_index()
    sessions = list(close.index)
    daily: list[float] = []
    qqq_daily: list[float] = []
    turnovers: list[float] = []
    active_returns: list[float] = []
    previous: set[str] = set()
    trade_count = 0
    first_index = max(regime_settings.min_history_days - 1, 200)
    for index in range(first_index, len(sessions) - 1):
        current = pd.Timestamp(sessions[index])
        following = pd.Timestamp(sessions[index + 1])
        day = current.date()
        current_members = (
            set(point_in_time_members.get(day, set()))
            if point_in_time_members is not None
            else set(members)
        )
        if not current_members:
            continue
        cutoff = bars.index.get_level_values("ts") <= current
        history = bars.loc[cutoff]
        regime = classify_market_regime(
            history,
            current_members,
            as_of=day,
            settings=regime_settings,
            min_coverage=0.98,
        )
        discovery = discover_candidates(
            history,
            current_members,
            regime,
            as_of=day,
            settings=lane_settings,
        )
        selected = {candidate.ticker for candidate in discovery.candidates}
        turnover = _turnover(previous, selected)
        gross = _portfolio_return(close, current, following, selected)
        net = gross - turnover * transaction_cost_bps / 10_000
        qqq = _portfolio_return(close, current, following, {"QQQ"})
        daily.append(net)
        qqq_daily.append(qqq)
        turnovers.append(turnover)
        if selected:
            active_returns.append(net)
        trade_count += len(selected - previous)
        previous = selected
    if not daily:
        raise ValueError("not enough replay sessions after the required lookback")
    returns = pd.Series(daily, dtype=float)
    equity = (1.0 + returns).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    annualized = float((1.0 + total_return) ** (252 / len(returns)) - 1.0)
    drawdown = equity / equity.cummax() - 1.0
    volatility = float(returns.std())
    sharpe = float(returns.mean() / volatility * math.sqrt(252)) if volatility > 0 else 0.0
    years = len(returns) / 252
    qqq_return = float(np.prod(1.0 + np.asarray(qqq_daily)) - 1.0)
    return {
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": float(drawdown.min()),
        "sharpe": sharpe,
        "annual_turnover": float(sum(turnovers) / years) if years > 0 else 0.0,
        "win_rate": (
            float(sum(value > 0 for value in active_returns) / len(active_returns))
            if active_returns
            else 0.0
        ),
        "trade_count": trade_count,
        "qqq_return": qqq_return,
        "relative_return": total_return - qqq_return,
        "survivorship_bias": point_in_time_members is None,
        "transaction_cost_bps": transaction_cost_bps,
    }


def render_markdown(result: Mapping[str, float | int | bool]) -> str:
    bias = "是" if bool(result["survivorship_bias"]) else "否"
    return (
        "# 纳指100分赛道回放\n\n"
        f"- 总收益：{float(result['total_return']):+.2%}\n"
        f"- 年化收益：{float(result['annualized_return']):+.2%}\n"
        f"- 最大回撤：{float(result['max_drawdown']):.2%}\n"
        f"- Sharpe：{float(result['sharpe']):.2f}\n"
        f"- 年换手：{float(result['annual_turnover']):.2f}\n"
        f"- 活跃日胜率：{float(result['win_rate']):.1%}\n"
        f"- 新进入交易数：{int(result['trade_count'])}\n"
        f"- QQQ收益：{float(result['qqq_return']):+.2%}\n"
        f"- 相对QQQ：{float(result['relative_return']):+.2%}\n\n"
        f"执行口径：信号后下一交易日执行；交易成本 {float(result['transaction_cost_bps']):g} bps。\n\n"
        f"幸存者偏差：{bias}。使用当前成分时，本结果不能作为上线收益证明。\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars-csv", required=True, type=Path)
    parser.add_argument("--members", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    args = parser.parse_args()
    flat = pd.read_csv(args.bars_csv, parse_dates=["ts"])
    bars = flat.set_index(["ticker", "ts"]).sort_index()
    members = {
        line.strip().upper()
        for line in args.members.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    result = replay_candidate_lanes(
        bars,
        members,
        regime_settings=MarketRegimeSettings(),
        lane_settings=CandidateLaneSettings(),
        transaction_cost_bps=args.cost_bps,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "report.md").write_text(render_markdown(result), encoding="utf-8")


if __name__ == "__main__":
    main()
