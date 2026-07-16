from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Mapping

import pandas as pd

from quant_signal.candidate_lanes import Candidate, discover_candidates
from quant_signal.config import CandidateLaneSettings, MarketRegimeSettings
from quant_signal.market_regime import classify_market_regime


@dataclass
class _OpenPosition:
    candidate: Candidate
    units: float
    entry_price: float
    entry_index: int
    holding_sessions: int = 1


def _number(bar: pd.Series, field: str) -> float:
    value = float(bar[field])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"invalid {field}")
    return value


def entry_fill_price(bar: pd.Series, candidate: Candidate) -> float | None:
    """Return a next-session zone fill; never use the signal session close."""
    try:
        opening = _number(bar, "open")
        high = _number(bar, "high")
        low = _number(bar, "low")
    except (KeyError, TypeError, ValueError):
        return None
    if opening <= candidate.invalidation_price:
        return None
    if low > candidate.entry_high or high < candidate.entry_low:
        return None
    if candidate.entry_low <= opening <= candidate.entry_high:
        return opening
    if opening > candidate.entry_high:
        return candidate.entry_high
    return candidate.entry_low


def exit_fill_price(
    bar: pd.Series,
    *,
    stop: float,
    target: float,
    time_exit: bool,
) -> tuple[float, str] | None:
    """Conservative daily-bar exit: gaps, then stop before target, then time exit."""
    try:
        opening = _number(bar, "open")
        high = _number(bar, "high")
        low = _number(bar, "low")
        close = _number(bar, "close")
    except (KeyError, TypeError, ValueError):
        return None
    if opening <= stop:
        return opening, "STOP"
    if opening >= target:
        return opening, "TARGET"
    if low <= stop:
        return stop, "STOP"
    if high >= target:
        return target, "TARGET"
    if time_exit:
        return close, "TIME"
    return None


def entry_session_exit_price(
    bar: pd.Series,
    candidate: Candidate,
    *,
    time_exit: bool,
) -> tuple[float, str] | None:
    """Avoid crediting a target that traded before a gap-down zone entry."""
    try:
        opening = _number(bar, "open")
        low = _number(bar, "low")
        close = _number(bar, "close")
    except (KeyError, TypeError, ValueError):
        return None
    if opening > candidate.entry_high:
        if low <= candidate.invalidation_price:
            return candidate.invalidation_price, "STOP"
        return (close, "TIME") if time_exit else None
    return exit_fill_price(
        bar,
        stop=candidate.invalidation_price,
        target=candidate.target_price,
        time_exit=time_exit,
    )


def _bar(bars: pd.DataFrame, ticker: str, session: pd.Timestamp) -> pd.Series | None:
    try:
        row = bars.loc[(ticker, session)]
    except KeyError:
        return None
    return row if isinstance(row, pd.Series) else None


def load_point_in_time_members(path: Path) -> dict[date, set[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("membership JSON must be an object keyed by YYYY-MM-DD")
    output: dict[date, set[str]] = {}
    for key, value in raw.items():
        if not isinstance(value, list):
            raise ValueError("membership JSON values must be ticker lists")
        output[date.fromisoformat(str(key))] = {
            str(ticker).strip().upper() for ticker in value if str(ticker).strip()
        }
    return output


def replay_candidate_lanes(
    bars: pd.DataFrame,
    members: set[str],
    *,
    regime_settings: MarketRegimeSettings,
    lane_settings: CandidateLaneSettings,
    transaction_cost_bps: float = 5.0,
    point_in_time_members: Mapping[date, set[str]] | None = None,
    max_hold_sessions: int = 20,
    max_positions: int = 6,
) -> dict[str, float | int | bool]:
    """Event replay using next-session entries and production regime/lane functions."""
    if bars.empty:
        raise ValueError("bars must not be empty")
    if max_hold_sessions < 1 or max_positions < 1:
        raise ValueError("max_hold_sessions and max_positions must be positive")
    sessions = sorted(set(pd.DatetimeIndex(bars.index.get_level_values("ts"))))
    daily: list[float] = []
    equity_curve: list[float] = []
    trade_returns: list[float] = []
    positions: dict[str, _OpenPosition] = {}
    pending: list[tuple[Candidate, int]] = []
    cash = 1.0
    previous_equity = 1.0
    traded_notional = 0.0
    candidate_signals = 0
    no_fills = 0
    trade_count = 0
    stop_exits = 0
    target_exits = 0
    time_exits = 0
    entry_delays: list[int] = []
    cost_rate = transaction_cost_bps / 10_000
    first_index = max(regime_settings.min_history_days - 1, 200)
    if len(sessions) <= first_index + 1:
        raise ValueError("not enough replay sessions after the required lookback")

    for index in range(first_index, len(sessions)):
        current = pd.Timestamp(sessions[index])
        day = current.date()

        for ticker, position in list(positions.items()):
            row = _bar(bars, ticker, current)
            if row is None:
                continue
            position.holding_sessions += 1
            exit_result = exit_fill_price(
                row,
                stop=position.candidate.invalidation_price,
                target=position.candidate.target_price,
                time_exit=position.holding_sessions >= max_hold_sessions,
            )
            if exit_result is None:
                continue
            exit_price, reason = exit_result
            proceeds = position.units * exit_price * (1.0 - cost_rate)
            cash += proceeds
            traded_notional += position.units * exit_price
            trade_returns.append(
                exit_price * (1.0 - cost_rate)
                / (position.entry_price * (1.0 + cost_rate))
                - 1.0
            )
            stop_exits += int(reason == "STOP")
            target_exits += int(reason == "TARGET")
            time_exits += int(reason == "TIME")
            del positions[ticker]

        candidate_signals += len(pending)
        for candidate, signal_index in pending:
            if candidate.ticker in positions or len(positions) >= max_positions:
                continue
            row = _bar(bars, candidate.ticker, current)
            fill = entry_fill_price(row, candidate) if row is not None else None
            if fill is None:
                no_fills += 1
                continue
            opening_equity = cash
            for ticker, position in positions.items():
                held_bar = _bar(bars, ticker, current)
                if held_bar is not None:
                    opening_equity += position.units * _number(held_bar, "open")
            allocation = min(cash, opening_equity / max_positions)
            if allocation <= 0:
                continue
            units = allocation / (fill * (1.0 + cost_rate))
            cash -= allocation
            traded_notional += units * fill
            trade_count += 1
            entry_delays.append(index - signal_index)
            position = _OpenPosition(candidate, units, fill, index)
            immediate_exit = entry_session_exit_price(
                row,
                candidate,
                time_exit=max_hold_sessions == 1,
            )
            if immediate_exit is None:
                positions[candidate.ticker] = position
                continue
            exit_price, reason = immediate_exit
            cash += units * exit_price * (1.0 - cost_rate)
            traded_notional += units * exit_price
            trade_returns.append(
                exit_price * (1.0 - cost_rate) / (fill * (1.0 + cost_rate)) - 1.0
            )
            stop_exits += int(reason == "STOP")
            target_exits += int(reason == "TARGET")
            time_exits += int(reason == "TIME")

        equity = cash
        for ticker, position in positions.items():
            row = _bar(bars, ticker, current)
            if row is not None:
                equity += position.units * _number(row, "close")
        daily.append(equity / previous_equity - 1.0)
        equity_curve.append(equity)
        previous_equity = equity

        if index >= len(sessions) - 1:
            pending = []
            continue
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
        pending = [(candidate, index) for candidate in discovery.candidates]

    returns = pd.Series(daily, dtype=float)
    equity_series = pd.Series(equity_curve, dtype=float)
    total_return = float(equity_series.iloc[-1] - 1.0)
    annualized = float((1.0 + total_return) ** (252 / len(returns)) - 1.0)
    drawdown = equity_series / equity_series.cummax() - 1.0
    volatility = float(returns.std())
    sharpe = float(returns.mean() / volatility * math.sqrt(252)) if volatility > 0 else 0.0
    years = len(returns) / 252
    qqq_start = _bar(bars, "QQQ", pd.Timestamp(sessions[first_index + 1]))
    qqq_end = _bar(bars, "QQQ", pd.Timestamp(sessions[-1]))
    qqq_return = (
        _number(qqq_end, "close") / _number(qqq_start, "open") - 1.0
        if qqq_start is not None and qqq_end is not None
        else 0.0
    )
    average_equity = float(equity_series.mean()) if not equity_series.empty else 1.0
    return {
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": float(drawdown.min()),
        "sharpe": sharpe,
        "annual_turnover": (
            traded_notional / 2.0 / average_equity / years
            if years > 0 and average_equity > 0
            else 0.0
        ),
        "win_rate": (
            float(sum(value > 0 for value in trade_returns) / len(trade_returns))
            if trade_returns
            else 0.0
        ),
        "trade_count": trade_count,
        "qqq_return": qqq_return,
        "relative_return": total_return - qqq_return,
        "survivorship_bias": point_in_time_members is None,
        "transaction_cost_bps": transaction_cost_bps,
        "same_close_entries": 0,
        "min_entry_delay_sessions": min(entry_delays) if entry_delays else 0,
        "fill_rate": trade_count / candidate_signals if candidate_signals else 0.0,
        "no_fill_signals": no_fills,
        "stop_exits": stop_exits,
        "target_exits": target_exits,
        "time_exits": time_exits,
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
        f"- 成交率：{float(result.get('fill_rate', 0)):.1%}\n"
        f"- 同收盘成交：{int(result.get('same_close_entries', 0))}\n"
        f"- 最短入场延迟：{int(result.get('min_entry_delay_sessions', 0))} 个交易日\n"
        f"- 止损/目标/到期退出：{int(result.get('stop_exits', 0))}/"
        f"{int(result.get('target_exits', 0))}/{int(result.get('time_exits', 0))}\n\n"
        f"执行口径：信号后下一交易日执行（开盘后按买入区间）；同日止损与目标均触及时按止损优先；"
        f"双边交易成本 {float(result['transaction_cost_bps']):g} bps。\n\n"
        f"幸存者偏差：{bias}。使用当前成分时，本结果不能作为上线收益证明。\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars-csv", required=True, type=Path)
    parser.add_argument("--members", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument("--membership-json", type=Path)
    parser.add_argument("--max-hold-sessions", type=int, default=20)
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
        point_in_time_members=(
            load_point_in_time_members(args.membership_json)
            if args.membership_json is not None
            else None
        ),
        max_hold_sessions=args.max_hold_sessions,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "report.md").write_text(render_markdown(result), encoding="utf-8")


if __name__ == "__main__":
    main()
