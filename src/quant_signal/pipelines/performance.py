"""P2 绩效闭环 pipeline：每周重放近 90 天已推送信号出策略绩效卡。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.performance import (
    build_horizon_trades,
    build_round_trips,
    performance_card,
    strategy_summary,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

WINDOW_DAYS = 90
BENCH = "SPY"
BENCH_DAYS = 28


def _scan_bars(
    engine: Engine, rows: list[dict[str, object]], now: datetime
) -> pd.DataFrame:
    scan_rows = [row for row in rows if str(row["strategy_id"]) == "market_scan"]
    tickers = sorted({str(row["ticker"]) for row in scan_rows})
    if not tickers:
        return pd.DataFrame()
    starts = [datetime.fromisoformat(str(row["pushed_at"])) for row in scan_rows]
    start = min(starts) - timedelta(days=2)
    source = (
        engine._intl_source
        if bool(getattr(engine.source, "partial_market_volume", False))
        else engine.source
    )
    source_name = "yfinance" if source is engine._intl_source else engine.settings.data_source
    try:
        fresh = source.fetch_daily_bars(
            tickers, start.date(), (now + timedelta(days=1)).date()
        )
        engine.store.write_daily_bars(fresh, source=source_name)
    except Exception as error:  # noqa: BLE001
        log.warning("performance.scan_bars_failed", error=str(error))
    return engine.store.read_daily_bars(tickers, start=start)


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
    scan_bars = _scan_bars(engine, rows, now)
    trades = build_round_trips(rows) + build_horizon_trades(rows, scan_bars)
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
