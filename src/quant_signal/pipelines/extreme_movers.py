"""Full-market daily extreme-mover collection and stored ranking delivery."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
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
) -> pd.DataFrame:
    fetch = getattr(source, "fetch_sip_daily_bars", None)
    if fetch is None:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(symbols), chunk_size):
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
    for ticker, frame in bars.groupby(level="ticker"):
        dates = {
            value.date()
            for value in frame.index.get_level_values("ts")
            if value.date() <= session
        }
        if len(dates) >= 2 and session in dates:
            covered.add(str(ticker))
    return covered


def run_close(engine: Any, now: datetime, *, notify: bool = True) -> bool:
    cfg = engine.settings.extreme_movers
    if not cfg.enabled:
        return False
    lister = getattr(engine.source, "list_active_symbols", None)
    if lister is None:
        log.warning("extreme_movers.skip", reason="asset_list_unavailable")
        return False
    symbols = sorted({str(value).strip().upper() for value in lister() if str(value).strip()})
    if not symbols:
        return False
    session = _session_for_close(now)
    short_bars = _fetch_chunks(
        engine.source, symbols, session - timedelta(days=10), session + timedelta(days=1),
        cfg.chunk_size,
    )
    covered = _covered_symbols(short_bars, session)
    if len(covered) / len(symbols) < cfg.min_coverage:
        log.warning(
            "extreme_movers.coverage_failed",
            covered=len(covered), universe=len(symbols), required=cfg.min_coverage,
        )
        return False
    detected = detect_extreme_movers(short_bars, session, threshold=cfg.threshold)
    detected_symbols = [event.ticker for event in detected]
    long_bars = _fetch_chunks(
        engine.source, detected_symbols, session - timedelta(days=400),
        session + timedelta(days=1), cfg.chunk_size,
    ) if detected_symbols else pd.DataFrame()
    profile_source = engine.fundamentals_source
    profiles = (
        profile_source.profiles(detected_symbols)
        if profile_source is not None and detected_symbols else {}
    )
    qualified: list[ExtremeMoverEvent] = []
    for event in detected:
        try:
            frame = cast(pd.DataFrame, long_bars.xs(event.ticker, level="ticker"))
            adv = average_dollar_volume(frame, sessions=20)
        except (KeyError, ValueError):
            adv = Decimal("0")
        qualified.append(
            qualify_event(
                event, profiles.get(event.ticker),
                avg_dollar_volume_20d=adv,
                min_price=cfg.min_price,
                min_dollar_volume=cfg.min_dollar_volume,
            )
        )
    engine.ledger.replace_extreme_mover_run(
        ExtremeMoverRun(session, "COMPLETE", len(symbols), len(covered), now),
        qualified,
    )
    if notify:
        engine.notifier.send(
            extreme_movers_close_card(
                qualified, universe_count=len(symbols), covered_count=len(covered)
            )
        )
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
    events = engine.ledger.extreme_mover_events(session, window_sessions=window)
    card = extreme_movers_premarket_card(
        session=session,
        window_sessions=window,
        movers=rank_movers(events, window_sessions=window),
        sectors=rank_sectors(events, window_sessions=window),
        backfill_warning=any(event.backfilled for event in events),
    )
    if notify:
        engine.notifier.send(card)
    return True
