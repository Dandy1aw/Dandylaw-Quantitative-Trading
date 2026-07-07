from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.notifier.cards import alert_card, signal_card

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def intraday_snapshot(
    daily: pd.DataFrame, intraday: pd.DataFrame, day: date
) -> pd.DataFrame:
    frames = [daily]
    day_start = pd.Timestamp(day, tz="UTC")
    current = intraday[intraday.index.get_level_values("ts") >= day_start]
    for ticker in current.index.get_level_values("ticker").unique():
        ticker_bars = current.xs(ticker, level="ticker").sort_index()
        if ticker_bars.empty:
            continue
        row = pd.DataFrame(
            {
                "open": [float(ticker_bars["open"].iloc[0])],
                "high": [float(ticker_bars["high"].max())],
                "low": [float(ticker_bars["low"].min())],
                "close": [float(ticker_bars["close"].iloc[-1])],
                "volume": [float(ticker_bars["volume"].sum())],
            },
            index=pd.MultiIndex.from_tuples(
                [(ticker, ticker_bars.index[-1])], names=["ticker", "ts"]
            ),
        )
        frames.append(row)
    return pd.concat(frames).sort_index()


def run(engine: Engine, now: datetime) -> None:
    intraday = engine.source.fetch_intraday_bars(engine.settings.watchlist)
    engine.store.write_intraday_bars(intraday, source=engine.settings.data_source)
    daily = engine.store.read_daily_bars(
        engine.settings.watchlist, start=now - pd.Timedelta(days=60)
    )
    bars = intraday_snapshot(daily, intraday, now.date())
    result = engine._dedup(
        engine.breakout.generate(bars), now, channel="intraday"
    )
    delayed = engine.settings.data_source == "yfinance"
    for signal in result.to_push:
        delivered = engine.notifier.send(signal_card(signal, delayed=delayed))
        engine.ledger.insert(signal, pushed=delivered, now=now)
    for signal in result.suppressed:
        engine.ledger.insert(signal, pushed=False, now=now)
    if result.overflow:
        for signal in result.overflow:
            engine.ledger.insert(signal, pushed=False, now=now)
        names = ", ".join(
            f"{signal.ticker}({signal.direction.value})" for signal in result.overflow
        )
        engine.notifier.send(
            alert_card(
                "信号限流汇总",
                f"1 小时配额已满，以下 {len(result.overflow)} 条合并：{names}",
            )
        )
    log.info(
        "intraday.done", pushed=len(result.to_push), overflow=len(result.overflow)
    )
