from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from quant_signal.enrichment import run_uzi_analysis
from quant_signal.notifier.cards import build_enrichment_card

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def run(engine: Engine, now: datetime) -> None:
    cfg = engine.settings.enrichment
    if not cfg.enabled:
        return
    held = set(engine.ledger.get_holdings(engine.momentum.strategy_id))
    today_buys = {
        str(row["ticker"])
        for row in engine.ledger.signals_on(now.date())
        if row["direction"] == "buy"
    }
    watch_set = sorted(
        (held | today_buys) - set(engine.settings.international_tickers)
    )[: cfg.max_tickers]
    if not watch_set:
        log.info("enrichment.skip", reason="empty_watch_set")
        return
    results = []
    for ticker in watch_set:
        result = run_uzi_analysis(
            ticker,
            Path(cfg.uzi_run_py),
            cfg.python_exe,
            cfg.depth,
            cfg.timeout_seconds,
        )
        if result is not None:
            results.append(result)
    if not results:
        log.info("enrichment.skip", reason="no_results", attempted=len(watch_set))
        return
    engine.notifier.send(build_enrichment_card(results, held))
    log.info("enrichment.done", attempted=len(watch_set), succeeded=len(results))
