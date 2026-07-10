"""Post-market observer for repairable negative-news price shocks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.news_classifier import classify_news
from quant_signal.notifier.cards import negative_overreaction_card
from quant_signal.strategies.negative_overreaction import confirm_reversal, detect_shock

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()
ET = ZoneInfo("America/New_York")


def run(engine: Engine, now: datetime) -> None:
    if engine.news_source is None or engine.news_store is None:
        log.info("negative_overreaction.skip", reason="news_not_configured")
        return
    us_symbols = sorted(set(engine.settings.universe) - set(engine.settings.international_tickers))
    start = now - timedelta(days=10)
    try:
        articles = engine.news_source.fetch(us_symbols, start, now)
    except Exception as error:  # noqa: BLE001
        log.warning("negative_overreaction.news_failed", error=str(error))
        return
    engine.news_store.put_many(articles, seen_at=now)
    bars = engine._refresh_daily(now)
    available = set(bars.index.get_level_values("ticker")) if not bars.empty else set()
    cases: list[dict[str, object]] = []
    accepted: list[tuple[str, str, datetime]] = []
    for article in articles:
        assessment = classify_news(article)
        if assessment.veto:
            continue
        for ticker in article.symbols:
            if ticker not in available:
                continue
            sub = pd.DataFrame(bars.xs(ticker, level="ticker")).sort_index()
            created_et = article.created_at.astimezone(ET)
            effective_day = created_et.date()
            if created_et.hour >= 16:
                effective_day += timedelta(days=1)
            matching = [
                pd.Timestamp(ts)
                for ts in sub.index[-8:]
                if 0 <= (pd.Timestamp(ts).date() - effective_day).days <= 3
            ]
            if not matching:
                continue
            shock = next(
                (
                    candidate
                    for ts in matching
                    if (candidate := detect_shock(ticker, sub, ts)) is not None
                ),
                None,
            )
            if shock is None:
                continue
            shock_dt = shock.shock_at.to_pydatetime().astimezone(timezone.utc)
            if engine.news_store.has_case(article.article_id, ticker, shock_dt):
                continue
            confirmation = confirm_reversal(sub, shock)
            if confirmation is None:
                continue
            cases.append(
                {
                    "ticker": ticker,
                    "event_type": assessment.event_type,
                    "drop": shock.drop,
                    "volume_ratio": shock.volume_ratio,
                    "confirmed_at": confirmation.confirmed_at.date(),
                }
            )
            accepted.append((article.article_id, ticker, shock_dt))
    if not cases:
        log.info("negative_overreaction.done", articles=len(articles), cases=0)
        return
    delivered = engine.notifier.send(negative_overreaction_card(cases))
    if delivered:
        for article_id, ticker, shock_at in accepted:
            engine.news_store.record_case(
                article_id, ticker, shock_at, "CONFIRMED", now
            )
    log.info("negative_overreaction.done", articles=len(articles), cases=len(cases))
