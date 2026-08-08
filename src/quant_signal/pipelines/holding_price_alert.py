from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.holding_price_alert import STRATEGY_ID, evaluate_holding_price_alerts
from quant_signal.notifier.cards import holding_price_alert_card
from quant_signal.price_move_research import research_price_move_causes
from quant_signal.strategies.base import Signal, dedup_key

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")


def _monitored_positions(engine: Engine) -> list[dict[str, object]]:
    positions: list[dict[str, object]] = []
    for row in engine.ledger.active_observed_positions(exact_only=True):
        try:
            quantity = Decimal(str(row.get("qty", "0")))
        except (InvalidOperation, ValueError):
            continue
        if quantity > 0 and str(row.get("symbol", "")).strip():
            item = dict(row)
            item["monitor_origin"] = "holding"
            positions.append(item)
    held = {str(row["symbol"]).strip().upper() for row in positions}
    manual_loader = getattr(engine.ledger, "active_manual_monitors", None)
    manual = manual_loader() if callable(manual_loader) else []
    for ticker in manual:
        symbol = str(ticker).strip().upper()
        if symbol and symbol not in held:
            positions.append(
                {
                    "symbol": symbol,
                    "qty": None,
                    "avg_entry_price": None,
                    "monitor_origin": "manual",
                }
            )
    return positions


def _minute_fetcher(engine: Engine) -> Callable[[list[str], int], pd.DataFrame]:
    fetch = getattr(engine.source, "fetch_minute_bars", None)
    if not callable(fetch):
        fetch = getattr(engine._intl_source, "fetch_minute_bars", None)
    if not callable(fetch):
        raise TypeError("configured market data source has no 1-minute bar support")
    return cast(Callable[[list[str], int], pd.DataFrame], fetch)


def _severity(signal: object) -> int:
    extra = getattr(signal, "extra", None)
    raw = extra.get("severity", 1) if isinstance(extra, dict) else 1
    return int(raw) if isinstance(raw, (int, float)) else 1


def _recent_news(
    engine: Engine,
    tickers: list[str],
    now: datetime,
    lookback_hours: int,
) -> dict[str, list[dict[str, object]]]:
    if engine.news_source is None or not tickers:
        return {}
    try:
        articles = engine.news_source.fetch(
            tickers,
            now - timedelta(hours=lookback_hours),
            now + timedelta(minutes=5),
        )
    except Exception as error:  # noqa: BLE001 - 新闻不能吞掉价格告警
        log.warning("holding_price_alert.news_failed", error=str(error))
        return {}
    output: dict[str, list[dict[str, object]]] = {ticker: [] for ticker in tickers}
    for article in articles:
        row: dict[str, object] = {
            "created_at": article.created_at.isoformat(),
            "headline": article.headline,
            "summary": article.summary,
            "source": article.source,
            "url": article.url,
        }
        for ticker in set(article.symbols) & set(tickers):
            output[ticker].append(row)
    return {ticker: rows[-5:] for ticker, rows in output.items() if rows}


def run(engine: Engine, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("holding price alert time must be timezone-aware")
    settings = engine.settings.holding_price_alert
    if not settings.enabled:
        return
    monitored = _monitored_positions(engine)
    holdings = [
        row for row in monitored if row.get("monitor_origin") == "holding"
    ]
    manuals = [row for row in monitored if row.get("monitor_origin") == "manual"]
    manual_capacity = max(0, settings.max_tickers - len(holdings))
    positions = holdings + manuals[:manual_capacity]
    if not positions:
        log.info("holding_price_alert.skip", reason="no_monitored_symbols")
        return

    tickers = sorted({str(row["symbol"]).strip().upper() for row in positions})
    bars = _minute_fetcher(engine)(tickers, settings.lookback_minutes)
    etf_symbols = frozenset(
        ticker
        for ticker in tickers
        if engine.settings.asset_type.get(ticker) == "ETF"
        or ticker in engine.settings.option_flow.etf_roots
    )
    evaluation = evaluate_holding_price_alerts(
        bars,
        positions,
        now,
        settings,
        etf_symbols,
    )
    signals = list(evaluation.signals)

    cooldown_since = now - timedelta(minutes=settings.cooldown_minutes)
    recently_pushed = engine.ledger.last_push_by_key(cooldown_since)
    local_now = now.astimezone(_ET)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    quota_used = engine.ledger.pushed_count_since(
        local_midnight.astimezone(UTC), {STRATEGY_ID}
    )
    pushed = 0
    suppressed = 0
    overflow = 0
    ready: list[Signal] = []
    for signal in sorted(
        signals,
        key=_severity,
        reverse=True,
    ):
        if dedup_key(signal) in recently_pushed:
            engine.ledger.insert(signal, pushed=False, now=now)
            suppressed += 1
            continue
        if quota_used + len(ready) >= settings.max_alerts_per_day:
            engine.ledger.insert(signal, pushed=False, now=now)
            overflow += 1
            continue
        ready.append(signal)

    search_settings = settings.cause_search
    seed_news = _recent_news(
        engine,
        [signal.ticker for signal in ready],
        now,
        search_settings.lookback_hours,
    )
    causes = (
        research_price_move_causes(
            ready,
            search_settings,
            now=now,
            seed_news=seed_news,
        )
        if ready
        else {}
    )
    for signal in ready:
        extra = dict(signal.extra or {})
        cause = causes.get(signal.ticker)
        if cause is not None:
            extra["price_move_cause"] = cause.as_dict()
        enriched = replace(signal, extra=extra)
        delivered = engine.notifier.send(holding_price_alert_card(enriched))
        engine.ledger.insert(enriched, pushed=delivered, now=now)
        if delivered:
            pushed += 1

    log.info(
        "holding_price_alert.done",
        requested=evaluation.requested,
        available=evaluation.available,
        evaluated=evaluation.evaluated,
        stale=evaluation.stale,
        future=evaluation.future,
        insufficient=evaluation.insufficient,
        max_bar_age_seconds=(
            round(evaluation.max_bar_age_seconds, 1)
            if evaluation.max_bar_age_seconds is not None
            else None
        ),
        triggered=len(signals),
        pushed=pushed,
        suppressed=suppressed,
        overflow=overflow,
        feed=str(bars.attrs.get("feed", "unknown")),
    )
