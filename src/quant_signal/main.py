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
from quant_signal.options_flow import OptionFlowEnricher, OptionFlowSource
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

    option_flow_source: OptionFlowSource | None = None
    option_flow_enricher: OptionFlowEnricher | None = None
    if settings.option_flow.enabled:
        from quant_signal.datafeed.alpaca_options import AlpacaOptionEnricher
        from quant_signal.datafeed.cboe_options import CboeOptionFlowSource

        option_flow_source = CboeOptionFlowSource(
            venues=tuple(settings.option_flow.venues),
            discovery_limit=settings.option_flow.discovery_limit,
            top_n=settings.option_flow.top_n,
            min_venue_coverage=settings.option_flow.min_venue_coverage,
            circuit_breaker_failures=settings.option_flow.circuit_breaker_failures,
            circuit_breaker_cooldown_minutes=(
                settings.option_flow.circuit_breaker_cooldown_minutes
            ),
            excluded_index_roots=frozenset(
                settings.option_flow.excluded_index_roots
            ),
        )
        if settings.alpaca_key and settings.alpaca_secret:
            option_flow_enricher = AlpacaOptionEnricher(
                settings.alpaca_key,
                settings.alpaca_secret,
                feed=settings.option_flow.feed,
            )

    option_chain_source = None
    if settings.option_intel.enabled or settings.feishu_bot.enabled:
        if settings.alpaca_key and settings.alpaca_secret:
            from quant_signal.datafeed.alpaca_options import AlpacaOptionChainSource

            option_chain_source = AlpacaOptionChainSource(
                settings.alpaca_key, settings.alpaca_secret
            )
        elif settings.option_intel.enabled:
            log.warning(
                "option_intel.credentials_missing",
                hint="config/.env 需要 ALPACA_KEY/ALPACA_SECRET",
            )

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
        option_flow_source=option_flow_source,
        option_flow_enricher=option_flow_enricher,
        option_chain_source=option_chain_source,
    )
    from quant_signal.scheduler import JobRuntime

    runtime = JobRuntime(ledger=ledger)
    if settings.feishu_bot.enabled:
        if settings.feishu_app_id and settings.feishu_app_secret:
            import threading

            from quant_signal.feishu_bot import (
                FeishuBotService,
                LarkTransport,
                run_ws_forever,
            )

            bot = FeishuBotService(
                ledger,
                settings,
                LarkTransport(
                    settings.feishu_app_id,
                    settings.feishu_app_secret,
                    settings.feishu_proxy,
                ),
                runtime=runtime,
                engine=engine,
            )
            bot.start()
            threading.Thread(
                target=run_ws_forever,
                args=(
                    bot,
                    settings.feishu_app_id,
                    settings.feishu_app_secret,
                    settings.feishu_proxy,
                ),
                name="feishu-bot-ws",
                daemon=True,
            ).start()
            log.info("feishu_bot.started")
        else:
            log.warning(
                "feishu_bot.credentials_missing",
                hint="config/.env 需要 FEISHU_APP_ID/FEISHU_APP_SECRET",
            )

    sched = build_scheduler(engine, ledger, store, notifier, runtime=runtime)
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
