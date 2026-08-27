"""Full-market daily extreme-mover collection and stored ranking delivery."""

from __future__ import annotations

import json
import time as monotonic_time
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.calendar import is_trading_day, previous_trading_day
from quant_signal.extreme_movers import (
    ExtremeMoverEvent,
    ExtremeMoverRun,
    average_dollar_volume,
    detect_extreme_movers,
    qualify_event,
    rank_movers,
    rank_sectors,
)
from quant_signal.notifier.cards import (
    extreme_movers_close_card,
    extreme_movers_premarket_card,
)

log = structlog.get_logger()
ET = ZoneInfo("America/New_York")


def _session_for_close(now: datetime) -> date:
    now_et = now.astimezone(ET)
    if is_trading_day(now_et.date()) and now_et.time() >= time(16, 0):
        return now_et.date()
    return previous_trading_day(now_et.date())


def _fetch_chunks(
    source: object,
    symbols: list[str],
    start: date,
    end: date,
    chunk_size: int,
    feed: str,
    deadline: float | None = None,
) -> pd.DataFrame:
    method = "fetch_sip_daily_bars" if feed == "sip" else "fetch_daily_bars"
    fetch = getattr(source, method, None)
    if fetch is None:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(symbols), chunk_size):
        if deadline is not None and monotonic_time.monotonic() >= deadline:
            log.warning("extreme_movers.deadline_exceeded", offset=offset)
            break
        chunk = symbols[offset : offset + chunk_size]
        try:
            frame = fetch(chunk, start, end)
        except Exception as error:  # noqa: BLE001
            log.warning("extreme_movers.chunk_failed", offset=offset, error=str(error))
            continue
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            frames.append(frame)
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


def _covered_symbols(bars: pd.DataFrame, session: date) -> set[str]:
    if bars.empty or not isinstance(bars.index, pd.MultiIndex):
        return set()
    covered: set[str] = set()
    required = {previous_trading_day(session), session}
    for ticker, frame in bars.groupby(level="ticker"):
        dates = {
            value.date()
            for value in frame.index.get_level_values("ts")
            if value.date() <= session
        }
        if required.issubset(dates):
            covered.add(str(ticker))
    return covered


def _coverage_is_acceptable(
    *,
    feed: str,
    covered: int,
    universe: int,
    required: float,
) -> bool:
    if universe <= 0 or covered <= 0:
        return False
    if feed == "hybrid":
        return True
    return covered / universe >= required


def run_close(engine: Any, now: datetime, *, notify: bool = True) -> bool:
    cfg = engine.settings.extreme_movers
    if not cfg.enabled:
        return False
    deadline = monotonic_time.monotonic() + cfg.deadline_seconds
    lister = getattr(engine.source, "list_active_symbols", None)
    if lister is None:
        log.warning("extreme_movers.skip", reason="asset_list_unavailable")
        return False
    symbols = sorted({str(value).strip().upper() for value in lister() if str(value).strip()})
    if not symbols:
        return False
    session = _session_for_close(now)
    universe_hash = sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    config_hash = sha256(
        json.dumps(cfg.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()

    def fail(
        reason: str,
        *,
        covered_count: int = 0,
        screened_count: int = 0,
        confirmed_count: int = 0,
    ) -> bool:
        engine.ledger.record_extreme_mover_run(
            ExtremeMoverRun(
                session, "FAILED", len(symbols), covered_count, now,
                screened_count=screened_count,
                confirmed_count=confirmed_count,
                feed=cfg.feed,
                error=reason,
                universe_hash=universe_hash,
                config_hash=config_hash,
            )
        )
        return False

    def deadline_failed(
        *,
        covered_count: int = 0,
        screened_count: int = 0,
        confirmed_count: int = 0,
    ) -> bool:
        if monotonic_time.monotonic() < deadline:
            return False
        log.warning("extreme_movers.deadline_exceeded", phase="run_close")
        fail(
            "DEADLINE_EXCEEDED",
            covered_count=covered_count,
            screened_count=screened_count,
            confirmed_count=confirmed_count,
        )
        return True

    if deadline_failed():
        return False

    short_bars = _fetch_chunks(
        engine.source, symbols, session - timedelta(days=10), session + timedelta(days=1),
        cfg.chunk_size, cfg.feed, deadline,
    )
    if deadline_failed():
        return False
    covered = _covered_symbols(short_bars, session)
    if not _coverage_is_acceptable(
        feed=cfg.feed,
        covered=len(covered),
        universe=len(symbols),
        required=cfg.min_coverage,
    ):
        log.warning(
            "extreme_movers.coverage_failed",
            covered=len(covered), universe=len(symbols), required=cfg.min_coverage,
        )
        return fail("UNIVERSE_COVERAGE_FAILED", covered_count=len(covered))
    screen_threshold = cfg.threshold if cfg.feed == "sip" else cfg.screen_threshold
    screened = detect_extreme_movers(
        short_bars, session, threshold=screen_threshold
    )
    screened_symbols = [event.ticker for event in screened]
    if deadline_failed(
        covered_count=len(covered), screened_count=len(screened_symbols)
    ):
        return False
    confirmation_source = engine.source
    confirmation_feed = "sip"
    confirmation_chunk_size = cfg.chunk_size
    if cfg.feed == "hybrid":
        confirmation_source = getattr(engine, "confirmation_source", None) or getattr(
            engine, "_intl_source", None
        )
        confirmation_feed = "daily"
        confirmation_chunk_size = cfg.confirmation_chunk_size
    if confirmation_source is None and screened_symbols:
        log.warning("extreme_movers.skip", reason="confirmation_source_unavailable")
        return fail(
            "CONFIRMATION_SOURCE_UNAVAILABLE",
            covered_count=len(covered),
            screened_count=len(screened_symbols),
        )
    long_bars = _fetch_chunks(
        confirmation_source, screened_symbols, session - timedelta(days=400),
        session + timedelta(days=1), confirmation_chunk_size, confirmation_feed,
        deadline,
    ) if screened_symbols else pd.DataFrame()
    if deadline_failed(
        covered_count=len(covered), screened_count=len(screened_symbols)
    ):
        return False
    confirmed_coverage = _covered_symbols(long_bars, session)
    if screened_symbols and (
        len(confirmed_coverage) / len(screened_symbols)
        < cfg.min_confirmation_coverage
    ):
        log.warning(
            "extreme_movers.confirmation_coverage_failed",
            covered=len(confirmed_coverage), candidates=len(screened_symbols),
            required=cfg.min_confirmation_coverage,
        )
        return fail(
            "CONFIRMATION_COVERAGE_FAILED",
            covered_count=len(covered),
            screened_count=len(screened_symbols),
            confirmed_count=len(confirmed_coverage),
        )
    detected = detect_extreme_movers(
        long_bars, session, threshold=cfg.threshold
    ) if screened_symbols else ()
    detected_symbols = [event.ticker for event in detected]
    if deadline_failed(
        covered_count=len(covered),
        screened_count=len(screened_symbols),
        confirmed_count=len(confirmed_coverage),
    ):
        return False
    profile_source = engine.fundamentals_source
    profiles = (
        profile_source.profiles(detected_symbols)
        if profile_source is not None and detected_symbols else {}
    )
    if deadline_failed(
        covered_count=len(covered),
        screened_count=len(screened_symbols),
        confirmed_count=len(confirmed_coverage),
    ):
        return False
    qualified: list[ExtremeMoverEvent] = []
    for event in detected:
        source_label = (
            "alpaca_sip_adjustment_all"
            if cfg.feed == "sip"
            else "alpaca_iex_screen+yfinance_adjusted_confirm"
        )
        event = replace(event, source=source_label)
        try:
            frame = cast(pd.DataFrame, long_bars.xs(event.ticker, level="ticker"))
            adv = average_dollar_volume(frame, sessions=20)
        except (KeyError, ValueError):
            adv = Decimal(0)
        qualified.append(
            qualify_event(
                event, profiles.get(event.ticker),
                avg_dollar_volume_20d=adv,
                min_price=cfg.min_price,
                min_dollar_volume=cfg.min_dollar_volume,
            )
        )
    screened_by_ticker = {event.ticker: event for event in screened}
    for ticker in sorted(set(screened_symbols) - confirmed_coverage):
        qualified.append(
            qualify_event(
                replace(
                    screened_by_ticker[ticker],
                    source="alpaca_iex_screen+confirmation_unavailable",
                ),
                None,
                avg_dollar_volume_20d=Decimal(0),
                min_price=cfg.min_price,
                min_dollar_volume=cfg.min_dollar_volume,
            )
        )
    if deadline_failed(
        covered_count=len(covered),
        screened_count=len(screened_symbols),
        confirmed_count=len(confirmed_coverage),
    ):
        return False
    engine.ledger.replace_extreme_mover_run(
        ExtremeMoverRun(
            session, "COMPLETE", len(symbols), len(covered), now,
            screened_count=len(screened_symbols),
            confirmed_count=len(confirmed_coverage),
            feed=cfg.feed,
            universe_hash=universe_hash,
            config_hash=config_hash,
        ),
        qualified,
    )
    if notify:
        return bool(engine.notifier.send(
            extreme_movers_close_card(
                qualified,
                universe_count=len(symbols),
                covered_count=len(covered),
                top_n=cfg.top_stocks,
                source_label=(
                    "best-effort IEX 8% 初筛 + Yahoo adjusted 确认"
                    if cfg.feed == "hybrid"
                    else "Alpaca SIP adjusted 严格模式"
                ),
                session=session,
            )
        ))
    return True


def run_premarket(
    engine: Any,
    now: datetime,
    *,
    window_sessions: int | None = None,
    notify: bool = True,
) -> bool:
    del now
    cfg = engine.settings.extreme_movers
    if not cfg.enabled:
        return False
    session = engine.ledger.latest_complete_extreme_mover_session()
    if session is None:
        return False
    window = window_sessions or cfg.default_window
    if window not in cfg.windows:
        raise ValueError("unsupported extreme mover window")
    complete_sessions = engine.ledger.complete_extreme_mover_session_count(session)
    if complete_sessions < window:
        log.warning(
            "extreme_movers.premarket_skip",
            reason="incomplete_window",
            required_sessions=window,
            complete_sessions=complete_sessions,
            through=session.isoformat(),
        )
        return False
    events = engine.ledger.extreme_mover_events(session, window_sessions=window)
    has_iex_only_backfill = any(
        event.source == "alpaca_iex_adjustment_all_backfill_unconfirmed"
        for event in events
    )
    window_summaries: dict[int, tuple[int, int]] = {}
    from quant_signal.extreme_movers import Eligibility, MoverDirection

    for summary_window in cfg.windows:
        summary_events = engine.ledger.extreme_mover_events(
            session, window_sessions=summary_window
        )
        window_summaries[summary_window] = (
            sum(
                event.eligibility is Eligibility.ELIGIBLE
                and event.direction is MoverDirection.UP
                for event in summary_events
            ),
            sum(
                event.eligibility is Eligibility.ELIGIBLE
                and event.direction is MoverDirection.DOWN
                for event in summary_events
            ),
        )
    card = extreme_movers_premarket_card(
        session=session,
        window_sessions=window,
        movers=rank_movers(events, window_sessions=window),
        sectors=rank_sectors(events, window_sessions=window),
        backfill_warning=any(event.backfilled for event in events),
        top_stocks=cfg.top_stocks,
        top_sectors=cfg.top_sectors,
        source_label=(
            "best-effort Alpaca IEX adjusted 回填（无 Yahoo 二次确认）"
            if has_iex_only_backfill
            else "best-effort IEX 初筛 + Yahoo adjusted 确认"
            if cfg.feed == "hybrid"
            else "Alpaca SIP adjusted 严格模式"
        ),
        window_summaries=window_summaries,
    )
    if notify:
        return bool(engine.notifier.send(card))
    return True
