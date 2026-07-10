from __future__ import annotations

import time

import structlog

from quant_signal.config import load_settings
from quant_signal.datafeed.base import get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.logging_setup import setup_logging
from quant_signal.notifier.feishu import get_notifier
from quant_signal.scheduler import build_scheduler

log = structlog.get_logger()


def main() -> None:
    setup_logging()
    settings = load_settings()
    store = BarStore(settings.db_path / "bars.duckdb")
    ledger = SignalLedger(settings.db_path / "signals.db")
    notifier = get_notifier(settings)
    from quant_signal.datafeed.earnings import YFinanceEarnings
    from quant_signal.datafeed.fundamentals import YFinanceFundamentals
    from quant_signal.datafeed.news import AlpacaNewsSource
    from quant_signal.news_store import NewsStore

    engine = Engine(
        settings, store, get_source(settings), ledger, notifier,
        earnings_source=YFinanceEarnings(),
        fundamentals_source=YFinanceFundamentals(),
        news_source=(
            AlpacaNewsSource(settings.alpaca_key, settings.alpaca_secret)
            if settings.alpaca_key and settings.alpaca_secret
            else None
        ),
        news_store=NewsStore(settings.db_path / "news.db"),
    )
    sched = build_scheduler(engine, ledger, store, notifier)
    sched.start()
    log.info(
        "scheduler.started",
        jobs=[j.id for j in sched.get_jobs()],
        data_source=settings.data_source,
        notifier=type(notifier).__name__,
    )
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        sched.shutdown()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    main()
