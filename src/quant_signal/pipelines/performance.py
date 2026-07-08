"""P2 绩效闭环 pipeline：每周重放近 90 天已推送信号出策略绩效卡。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from quant_signal.performance import build_round_trips, performance_card, strategy_summary

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

WINDOW_DAYS = 90
BENCH = "SPY"
BENCH_DAYS = 28


def _last_close(engine: Engine, tickers: list[str], now: datetime) -> dict[str, float]:
    if not tickers:
        return {}
    daily = engine.store.read_daily_bars(tickers, start=now - timedelta(days=14))
    marks: dict[str, float] = {}
    for ticker in tickers:
        if ticker in daily.index.get_level_values("ticker"):
            series = daily.xs(ticker, level="ticker")["close"].dropna()
            if not series.empty:
                marks[ticker] = float(series.iloc[-1])
    return marks


def _benchmark_note(engine: Engine, now: datetime) -> str | None:
    daily = engine.store.read_daily_bars([BENCH], start=now - timedelta(days=BENCH_DAYS + 7))
    if daily.empty or BENCH not in daily.index.get_level_values("ticker"):
        return None
    series = daily.xs(BENCH, level="ticker")["close"].dropna()
    if len(series) < 2:
        return None
    ret = float(series.iloc[-1] / series.iloc[0] - 1.0)
    return f"参照：同期约 {BENCH_DAYS} 天 {BENCH} {ret:+.1%}（策略列为单笔均值，口径不同仅供感知）"


def run(engine: Engine, now: datetime) -> None:
    rows = engine.ledger.pushed_signals(now - timedelta(days=WINDOW_DAYS))
    trades = build_round_trips(rows)
    open_tickers = sorted({t.ticker for t in trades if not t.closed})
    marks = _last_close(engine, open_tickers, now)
    card = performance_card(
        strategy_summary(trades, marks), WINDOW_DAYS, _benchmark_note(engine, now)
    )
    engine.notifier.send(card)
    log.info(
        "performance.done",
        trades=len(trades),
        open=len(open_tickers),
        strategies=len({t.strategy_id for t in trades}),
    )
