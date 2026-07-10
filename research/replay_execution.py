"""回放执行状态机: 旧 target-hit 口径 vs 新 ACTIONABLE 确认口径。

用法: uv run python research/replay_execution.py [--days 20]

对台账里已持久化的执行计划, 拉取计划日的 Alpaca IEX 5 分钟 bar 逐bar回放,
输出两种口径的提醒数量、入场率、前向 1/5/20 日收益与最大不利变动。
显式标注: 使用当前成分与已存计划, 结果带幸存者偏差, 仅供研究。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from quant_signal.config import load_settings
from quant_signal.datafeed.alpaca_source import AlpacaSource
from quant_signal.datafeed.store import BarStore
from quant_signal.execution import PlanObservation
from quant_signal.execution_replay import replay_plans
from quant_signal.ledger import SignalLedger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=20, help="回放最近 N 个日历日的计划")
    args = parser.parse_args()

    settings = load_settings()
    ledger = SignalLedger(settings.db_path / "signals.db")
    store = BarStore(settings.db_path / "bars.duckdb")
    source = AlpacaSource(settings.alpaca_key, settings.alpaca_secret)

    plans = ledger.active_execution_plans()
    if not plans:
        print("台账中没有活跃执行计划, 无可回放对象。")
        return
    tickers = sorted({plan.ticker for plan in plans})
    intraday = source.fetch_intraday_bars(tickers, lookback_days=args.days)

    observations: dict[str, list[PlanObservation]] = {}
    for ticker in tickers:
        try:
            sub = intraday.xs(ticker, level="ticker").sort_index()
        except KeyError:
            continue
        observations[ticker] = [
            PlanObservation(
                at=ts.to_pydatetime(),
                price=float(row["close"]),
                bar_close=float(row["close"]),
                bar_low=float(row["low"]),
                bar_complete=True,
            )
            for ts, row in sub.iterrows()
        ]

    now = datetime.now(timezone.utc)
    daily: dict[str, list[float]] = {}
    bars = store.read_daily_bars(tickers, start=now - timedelta(days=60))
    for ticker in tickers:
        try:
            daily[ticker] = [
                float(v) for v in bars.xs(ticker, level="ticker")["close"].tolist()
            ]
        except KeyError:
            daily[ticker] = []

    result = replay_plans(plans, observations, daily, settings.execution_plan)
    print(f"计划总数: {len(plans)}")
    print(f"旧口径 target-hit 提醒: {result.old_target_hits}")
    print(f"新口径 ACTIONABLE 确认: {result.new_actionable}")
    print(f"入场率: {result.entry_rate:.1%}")
    for entry in result.entries:
        print(
            f"  {entry.ticker}: 入场 {entry.entry_price:.2f}"
            f" 1d {entry.fwd_1d if entry.fwd_1d is not None else 'n/a'}"
            f" 5d {entry.fwd_5d if entry.fwd_5d is not None else 'n/a'}"
            f" 20d {entry.fwd_20d if entry.fwd_20d is not None else 'n/a'}"
            f" MAE {entry.max_adverse_excursion if entry.max_adverse_excursion is not None else 'n/a'}"
        )
    print("⚠ 使用当前成分/已存计划回放, 结果带幸存者偏差, 仅供研究参考。")


if __name__ == "__main__":
    main()
