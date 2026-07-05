"""按单只标的统计 BUY 信号的胜率/收益，选出表现最好的几只。

只统计 RSI回归/MACD金叉/布林带突破 三个"逐标的独立判断"的策略——
动量轮动是相对排名的组合策略（选不选中取决于跟其他标的比排名），
不存在"这只股票自己的胜率"这个概念，所以不纳入本统计。

复用 backtest_new_strategies.py 里已验证的 collect_events()，只是不再
把所有标的的信号合并算一个整体胜率，而是按 ticker 分组分别统计。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from quant_signal.config import load_settings
from quant_signal.strategies.base import Direction, Strategy
from quant_signal.strategies.bollinger_breakout import BollingerBreakout
from quant_signal.strategies.macd_cross import MacdCross
from quant_signal.strategies.rsi_reversion import RsiReversion

from backtest_new_strategies import collect_events, load_bars

REPORTS = Path(__file__).parent / "reports"
HORIZON = 20          # 统一用20日持有期，跟之前几份报告的口径一致
MIN_SIGNALS = 100      # 少于这个信号数的标的不参与排名，避免小样本/短历史标的的噪音干扰


def per_ticker_stats(
    events: list[tuple[pd.Timestamp, str, float, Direction]],
    close: pd.DataFrame,
    strategy_name: str,
) -> list[dict[str, object]]:
    all_ts = close.index
    by_ticker: dict[str, list[float]] = {}
    for ts, ticker, price, d in events:
        if d != Direction.BUY or ticker not in close.columns:
            continue
        pos = all_ts.get_loc(ts)
        if pos + HORIZON >= len(all_ts):
            continue
        exit_px = float(close[ticker].iloc[pos + HORIZON])
        by_ticker.setdefault(ticker, []).append(exit_px / price - 1.0)

    rows = []
    for ticker, rets in by_ticker.items():
        if len(rets) < MIN_SIGNALS:
            continue
        sr = pd.Series(rets)
        rows.append(
            {
                "ticker": ticker,
                "strategy": strategy_name,
                "signals": len(sr),
                "win_rate": float((sr > 0).mean()),
                "avg_return": float(sr.mean()),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=date.fromisoformat, default=date(2016, 1, 1))
    args = parser.parse_args()

    settings = load_settings()
    bars = load_bars(args.start)
    close = bars["close"].unstack("ticker").sort_index()

    rp = settings.strategies["rsi_reversion"]
    mc = settings.strategies["macd_cross"]
    bb = settings.strategies["bollinger_breakout"]
    strategies = {
        "RSI回归": RsiReversion(settings.universe, int(rp["period"]), float(rp["oversold"]), float(rp["overbought"])),
        "MACD金叉": MacdCross(settings.universe, int(mc["fast"]), int(mc["slow"]), int(mc["signal"])),
        "布林带突破": BollingerBreakout(settings.universe, int(bb["period"]), float(bb["num_std"])),
    }

    all_rows: list[dict[str, object]] = []
    for name, strat in strategies.items():
        events = collect_events(strat, bars)
        all_rows += per_ticker_stats(events, close, name)

    if not all_rows:
        print(f"没有任何标的达到最少 {MIN_SIGNALS} 次信号的门槛，无法排名")
        return

    by_win = sorted(all_rows, key=lambda r: r["win_rate"], reverse=True)  # type: ignore[arg-type,return-value]
    by_ret = sorted(all_rows, key=lambda r: r["avg_return"], reverse=True)  # type: ignore[arg-type,return-value]

    def fmt_rows(rows: list[dict[str, object]]) -> str:
        lines = ["| 标的 | 策略 | 信号数 | 胜率 | 平均收益(20日) |", "|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['ticker']} | {r['strategy']} | {r['signals']} |"
                f" {r['win_rate']:.0%} | {r['avg_return']:+.2%} |"
            )
        return "\n".join(lines)

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"per_ticker_from{args.start:%Y%m%d}_run{datetime.now(timezone.utc):%Y%m%d}.md"
    out.write_text(
        "# 按标的统计的 BUY 信号表现（20日持有期）\n\n"
        f"- 数据: {args.start} 至今，universe={settings.universe}\n"
        f"- 只统计 RSI回归/MACD金叉/布林带突破 三个逐标的独立判断的策略"
        f"（动量轮动是相对排名的组合策略，不适用'单只股票胜率'这个概念）\n"
        f"- 少于 {MIN_SIGNALS} 次信号的标的不参与排名（样本太小，数字没有意义）\n\n"
        "## 胜率最高 Top 3\n\n"
        + fmt_rows(by_win[:3])
        + "\n\n## 平均收益最高 Top 3\n\n"
        + fmt_rows(by_ret[:3])
        + "\n\n## 全部标的完整数据\n\n"
        + fmt_rows(sorted(all_rows, key=lambda r: (r["ticker"], r["strategy"])))  # type: ignore[arg-type,return-value]
        + "\n\n> 回测结果仅供评估，不构成投资建议。同一标的可能被多个策略分别统计，"
        "策略之间信号不叠加。\n",
        encoding="utf-8",
    )
    print(f"报告已生成: {out}")
    print("\n胜率Top3:")
    for r in by_win[:3]:
        print(f"  {r['ticker']} ({r['strategy']}): 胜率{r['win_rate']:.0%}, 信号数{r['signals']}")
    print("收益Top3:")
    for r in by_ret[:3]:
        print(f"  {r['ticker']} ({r['strategy']}): 平均收益{r['avg_return']:+.2%}, 信号数{r['signals']}")


if __name__ == "__main__":
    main()
