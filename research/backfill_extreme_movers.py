from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.calendar import previous_trading_day  # noqa: E402
from quant_signal.config import ExtremeMoverSettings, load_settings  # noqa: E402
from quant_signal.datafeed.alpaca_source import AlpacaSource  # noqa: E402
from quant_signal.datafeed.fundamentals import YFinanceFundamentals  # noqa: E402
from quant_signal.datafeed.yf_source import YFinanceSource  # noqa: E402
from quant_signal.extreme_movers import (  # noqa: E402
    ExtremeMoverEvent,
    ExtremeMoverRun,
    average_dollar_volume,
    detect_extreme_movers,
    qualify_event,
)
from quant_signal.ledger import SignalLedger  # noqa: E402
from quant_signal.pipelines.extreme_movers import _coverage_is_acceptable  # noqa: E402


def _sessions(through: date, count: int) -> tuple[date, ...]:
    if count < 1 or count > 252:
        raise ValueError("sessions must be between 1 and 252")
    rows: list[date] = []
    cursor = through + timedelta(days=1)
    while len(rows) < count:
        cursor = previous_trading_day(cursor)
        rows.append(cursor)
    return tuple(reversed(rows))


def build_backfill_events(
    bars: pd.DataFrame,
    *,
    sessions: tuple[date, ...],
    profiles: dict[str, object],
    settings: ExtremeMoverSettings,
) -> dict[date, tuple[ExtremeMoverEvent, ...]]:
    output: dict[date, tuple[ExtremeMoverEvent, ...]] = {}
    for session in sessions:
        detected = detect_extreme_movers(bars, session, threshold=settings.threshold)
        qualified: list[ExtremeMoverEvent] = []
        for event in detected:
            event = replace(
                event,
                source=(
                    "alpaca_iex_screen+yfinance_adjusted_confirm"
                    if settings.feed == "hybrid"
                    else "alpaca_sip_adjustment_all"
                ),
            )
            frame = bars.xs(event.ticker, level="ticker").sort_index()
            history = frame[frame.index.map(lambda value: value.date() <= session)]
            adv = average_dollar_volume(history, sessions=20)
            qualified.append(
                replace(
                    qualify_event(
                        event,
                        profiles.get(event.ticker),  # type: ignore[arg-type]
                        avg_dollar_volume_20d=adv,
                        min_price=settings.min_price,
                        min_dollar_volume=settings.min_dollar_volume,
                    ),
                    backfilled=True,
                )
            )
        output[session] = tuple(qualified)
    return output


def _fetch_all(
    source: object,
    symbols: list[str],
    start: date,
    end: date,
    chunk_size: int,
    feed: str,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for offset in range(0, len(symbols), chunk_size):
        fetch = getattr(
            source,
            "fetch_sip_daily_bars" if feed == "sip" else "fetch_daily_bars",
        )
        frame = fetch(symbols[offset : offset + chunk_size], start, end)
        if not frame.empty:
            frames.append(frame)
        print(f"bars {min(offset + chunk_size, len(symbols))}/{len(symbols)}")
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


def _covered(bars: pd.DataFrame, session: date) -> set[str]:
    if bars.empty:
        return set()
    output: set[str] = set()
    for ticker, frame in bars.groupby(level="ticker"):
        dates = {value.date() for value in frame.index.get_level_values("ts")}
        if session in dates and previous_trading_day(session) in dates:
            output.add(str(ticker))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill current-active US symbols into the ±10% event ledger."
    )
    parser.add_argument("--sessions", type=int, default=252)
    parser.add_argument("--through", type=date.fromisoformat)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--no-notify", action="store_true", default=True)
    args = parser.parse_args()
    del args.no_notify

    app_settings = load_settings()
    cfg = app_settings.extreme_movers
    through = args.through or previous_trading_day(date.today())
    sessions = _sessions(through, args.sessions)
    if args.checkpoint and args.checkpoint.exists():
        checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
        completed_through = date.fromisoformat(str(checkpoint["through"]))
        sessions = tuple(session for session in sessions if session > completed_through)
        if not sessions:
            print(json.dumps({"status": "already_complete", "sessions": 0}))
            return
    source = AlpacaSource(app_settings.alpaca_key, app_settings.alpaca_secret)
    symbols = source.list_active_symbols()
    screen_bars = _fetch_all(
        source,
        symbols,
        sessions[0] - timedelta(days=40),
        sessions[-1] + timedelta(days=1),
        cfg.chunk_size,
        cfg.feed,
    )
    screen_events = {
        session: detect_extreme_movers(
            screen_bars,
            session,
            threshold=(cfg.threshold if cfg.feed == "sip" else cfg.screen_threshold),
        )
        for session in sessions
    }
    screened_symbols = sorted(
        {event.ticker for rows in screen_events.values() for event in rows}
    )
    confirmation_bars = screen_bars
    if cfg.feed == "hybrid" and screened_symbols:
        confirmation_bars = _fetch_all(
            YFinanceSource(),
            screened_symbols,
            sessions[0] - timedelta(days=40),
            sessions[-1] + timedelta(days=1),
            cfg.confirmation_chunk_size,
            "daily",
        )
    for session in sessions:
        covered_count = len(_covered(screen_bars, session))
        if not _coverage_is_acceptable(
            feed=cfg.feed,
            covered=covered_count,
            universe=len(symbols),
            required=cfg.min_coverage,
        ):
            universe_coverage = covered_count / len(symbols)
            raise RuntimeError(
                f"universe coverage failed for {session}: {universe_coverage:.3%}"
            )
        expected = {event.ticker for event in screen_events[session]}
        confirmed = _covered(confirmation_bars, session) & expected
        if expected and len(confirmed) / len(expected) < cfg.min_confirmation_coverage:
            raise RuntimeError(
                f"confirmation coverage failed for {session}: "
                f"{len(confirmed)}/{len(expected)}"
            )
    detected_symbols = sorted({
        event.ticker
        for session in sessions
        for event in detect_extreme_movers(
            confirmation_bars, session, threshold=cfg.threshold
        )
    })
    profiles = YFinanceFundamentals().profiles(detected_symbols)
    events = build_backfill_events(
        confirmation_bars, sessions=sessions, profiles=profiles, settings=cfg
    )
    for session in sessions:
        missing = {
            event.ticker: event for event in screen_events[session]
            if event.ticker not in _covered(confirmation_bars, session)
        }
        audit_rows = tuple(
            replace(
                qualify_event(
                    replace(
                        event,
                        source="alpaca_iex_screen+confirmation_unavailable",
                    ),
                    None,
                    avg_dollar_volume_20d=Decimal("0"),
                    min_price=cfg.min_price,
                    min_dollar_volume=cfg.min_dollar_volume,
                ),
                backfilled=True,
            )
            for event in missing.values()
        )
        events[session] = tuple(sorted(
            (*events[session], *audit_rows), key=lambda event: event.ticker
        ))
    ledger = SignalLedger((args.db or (app_settings.db_path / "signals.db")).resolve())
    for index, session in enumerate(sessions, start=1):
        covered = len(_covered(screen_bars, session))
        screened = len(screen_events[session])
        confirmed = len(_covered(confirmation_bars, session) & {
            event.ticker for event in screen_events[session]
        })
        ledger.replace_extreme_mover_run(
            ExtremeMoverRun(
                session, "COMPLETE", len(symbols), covered,
                datetime.now(timezone.utc),
                screened_count=screened,
                confirmed_count=confirmed,
                feed=cfg.feed,
            ),
            events[session],
        )
        if args.checkpoint:
            args.checkpoint.parent.mkdir(parents=True, exist_ok=True)
            args.checkpoint.write_text(
                json.dumps(
                    {
                        "through": session.isoformat(),
                        "completed": index,
                        "requested": len(sessions),
                        "survivorship": "current_active_symbols",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
    print(json.dumps({
        "status": "complete",
        "sessions": len(sessions),
        "events": sum(len(rows) for rows in events.values()),
        "symbols": len(symbols),
        "profiled_movers": len(detected_symbols),
        "survivorship": "current_active_symbols",
    }, indent=2))


if __name__ == "__main__":
    main()
