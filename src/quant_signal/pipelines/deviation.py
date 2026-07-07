from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from quant_signal.notifier.cards import signal_card
from quant_signal.watch_monitor import check_deviations

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def run(engine: Engine, now: datetime) -> None:
    watch_set = engine.ledger.get_holdings(engine.momentum.strategy_id)
    if not watch_set:
        log.info("watch_deviation.skip", reason="no_holdings")
        return
    since = now - timedelta(hours=24)
    ref_prices = {
        ticker: price
        for ticker in watch_set
        if (
            price := engine.ledger.latest_signal_price(
                engine.momentum.strategy_id, ticker, since
            )
        )
        is not None
    }
    if not ref_prices:
        log.info("watch_deviation.skip", reason="no_reference_price")
        return
    live_prices = {
        ticker: price
        for ticker, price in engine._fetch_live_prices(set(ref_prices)).items()
        if price is not None
    }
    threshold = float(
        engine.settings.strategies.get("price_deviation", {}).get("threshold", 0.02)
    )
    signals = check_deviations(
        ref_prices,
        live_prices,
        now=now,
        bands=[threshold * multiplier for multiplier in (1.0, 2.5, 5.0, 10.0)],
    )
    result = engine._dedup(signals, now, channel="deviation")
    for signal in result.to_push:
        delivered = engine.notifier.send(signal_card(signal))
        engine.ledger.insert(signal, pushed=delivered, now=now)
    for signal in result.suppressed + result.overflow:
        engine.ledger.insert(signal, pushed=False, now=now)
    log.info(
        "watch_deviation.done", checked=len(ref_prices), pushed=len(result.to_push)
    )
