from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.calendar import previous_trading_day
from quant_signal.company_profiles import CompanyProfile
from quant_signal.config import ExtremeMoverSettings, load_settings
from quant_signal.datafeed.alpaca_source import AlpacaSource
from quant_signal.datafeed.fundamentals import YFinanceFundamentals
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.extreme_movers import (
    ExtremeMoverEvent,
    ExtremeMoverRun,
    MoverDirection,
    average_dollar_volume,
    detect_extreme_movers,
    qualify_event,
)
from quant_signal.ledger import SignalLedger
from quant_signal.pipelines.extreme_movers import _coverage_is_acceptable


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
    source_label: str | None = None,
    detected_events: Mapping[date, tuple[ExtremeMoverEvent, ...]] | None = None,
    average_dollar_volumes: Mapping[tuple[str, date], Decimal] | None = None,
) -> dict[date, tuple[ExtremeMoverEvent, ...]]:
    output: dict[date, tuple[ExtremeMoverEvent, ...]] = {}
    for session in sessions:
        detected = (
            detected_events[session]
            if detected_events is not None
            else detect_extreme_movers(bars, session, threshold=settings.threshold)
        )
        qualified: list[ExtremeMoverEvent] = []
        for event in detected:
            event = replace(
                event,
                source=source_label or (
                    "alpaca_iex_screen+yfinance_adjusted_confirm"
                    if settings.feed == "hybrid"
                    else "alpaca_sip_adjustment_all"
                ),
            )
            if average_dollar_volumes is None:
                frame = bars.xs(event.ticker, level="ticker").sort_index()
                history = frame[
                    [value.date() <= session for value in frame.index]
                ]
                adv = average_dollar_volume(history, sessions=20)
            else:
                adv = average_dollar_volumes.get((event.ticker, session), Decimal(0))
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


def _prepared_rows(bars: pd.DataFrame) -> pd.DataFrame:
    if bars.empty:
        return pd.DataFrame(
            columns=["ticker", "ts", "session", "close", "volume"]
        )
    rows = bars.reset_index().copy()
    rows["ticker"] = rows["ticker"].astype(str).str.upper()
    rows["ts"] = pd.to_datetime(rows["ts"], utc=True)
    rows["session"] = rows["ts"].dt.date
    rows = rows.sort_values(["ticker", "ts"]).drop_duplicates(
        ["ticker", "session"], keep="last"
    )
    rows["previous_session"] = rows.groupby("ticker", sort=False)[
        "session"
    ].shift()
    rows["previous_close"] = rows.groupby("ticker", sort=False)["close"].shift()
    unique_sessions = set(rows["session"])
    expected_previous = {
        session: previous_trading_day(session) for session in unique_sessions
    }
    rows["expected_previous_session"] = rows["session"].map(expected_previous)
    return rows


def _coverage_by_session(
    bars: pd.DataFrame,
    *,
    sessions: tuple[date, ...],
) -> dict[date, set[str]]:
    rows = _prepared_rows(bars)
    output = {session: set() for session in sessions}
    if rows.empty:
        return output
    valid = rows[
        rows["session"].isin(sessions)
        & (rows["previous_session"] == rows["expected_previous_session"])
    ]
    for session, frame in valid.groupby("session", sort=False):
        output[session] = set(frame["ticker"])
    return output


def _detect_by_session(
    bars: pd.DataFrame,
    *,
    sessions: tuple[date, ...],
    threshold: Decimal,
) -> dict[date, tuple[ExtremeMoverEvent, ...]]:
    rows = _prepared_rows(bars)
    output = {session: () for session in sessions}
    if rows.empty:
        return output
    rows["close"] = pd.to_numeric(rows["close"], errors="coerce")
    rows["previous_close"] = pd.to_numeric(rows["previous_close"], errors="coerce")
    rows["daily_return"] = rows["close"] / rows["previous_close"] - 1.0
    finite = rows[["close", "previous_close", "daily_return"]].apply(
        lambda column: column.map(lambda value: pd.notna(value) and float(value) not in (float("inf"), float("-inf")))
    ).all(axis=1)
    valid = rows[
        rows["session"].isin(sessions)
        & (rows["previous_session"] == rows["expected_previous_session"])
        & finite
        & (rows["close"] > 0)
        & (rows["previous_close"] > 0)
        & (rows["daily_return"].abs() >= float(threshold))
    ]
    for session, frame in valid.groupby("session", sort=False):
        events = [
            ExtremeMoverEvent(
                session=session,
                ticker=str(row.ticker),
                direction=(
                    MoverDirection.UP
                    if float(row.daily_return) >= float(threshold)
                    else MoverDirection.DOWN
                ),
                daily_return=Decimal(str(row.daily_return)),
                close=Decimal(str(row.close)),
            )
            for row in frame.itertuples(index=False)
        ]
        output[session] = tuple(sorted(events, key=lambda event: event.ticker))
    return output


def _average_dollar_volumes(
    bars: pd.DataFrame,
    *,
    sessions: tuple[date, ...],
) -> dict[tuple[str, date], Decimal]:
    rows = _prepared_rows(bars)
    if rows.empty:
        return {}
    rows["dollar_volume"] = (
        pd.to_numeric(rows["close"], errors="coerce")
        * pd.to_numeric(rows["volume"], errors="coerce")
    )
    rows["average_dollar_volume_20d"] = rows.groupby(
        "ticker", sort=False
    )["dollar_volume"].transform(
        lambda values: values.rolling(20, min_periods=20).mean()
    )
    selected = rows[
        rows["session"].isin(sessions)
        & rows["average_dollar_volume_20d"].notna()
    ]
    return {
        (str(row.ticker), row.session): Decimal(
            str(row.average_dollar_volume_20d)
        )
        for row in selected.itertuples(index=False)
    }


_FUND_NAME_MARKERS = (
    " ETF",
    " ETN",
    " EXCHANGE TRADED",
    " FUND",
    " TRUST",
    " PORTFOLIO",
    " DIREXION ",
    " ISHARES ",
    " PROSHARES ",
    " SPDR ",
    " VANGUARD ",
)


def build_alpaca_asset_profiles(
    assets: list[Mapping[str, object]],
    *,
    as_of: date,
) -> dict[str, CompanyProfile]:
    """Build auditable minimal profiles for an explicit IEX-only backfill."""
    profiles: dict[str, CompanyProfile] = {}
    for asset in assets:
        ticker = str(asset.get("symbol") or "").strip().upper()
        if not ticker:
            continue
        name = str(asset.get("name") or "").strip()
        normalized_name = f" {name.upper()} "
        if " PREFERRED " in normalized_name:
            quote_type = "PREFERRED"
        elif any(marker in normalized_name for marker in _FUND_NAME_MARKERS):
            quote_type = "FUND"
        else:
            quote_type = "EQUITY"
        profiles[ticker] = CompanyProfile(
            ticker=ticker,
            as_of=as_of,
            market_cap_usd=None,
            gics_sector=None,
            industry=None,
            company_name=name or None,
            business_summary=None,
            total_revenue=None,
            revenue_growth=None,
            earnings_growth=None,
            profit_margin=None,
            return_on_equity=None,
            free_cash_flow=None,
            quote_type=quote_type,
            source="alpaca_asset_name_heuristic",
            data_status="ok",
        )
    return profiles


def _fetch_alpaca_assets(key: str, secret: str) -> list[Mapping[str, object]]:
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {"status": "active", "asset_class": "us_equity"}
    for host in ("https://api.alpaca.markets", "https://paper-api.alpaca.markets"):
        response = httpx.get(
            f"{host}/v2/assets",
            params=params,
            headers=headers,
            timeout=60.0,
        )
        if response.status_code in (401, 403):
            continue
        response.raise_for_status()
        return [row for row in response.json() if isinstance(row, Mapping)]
    raise RuntimeError("unable to load Alpaca asset metadata")


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
    parser.add_argument(
        "--confirmation-source",
        choices=("yfinance", "iex-only"),
        default="yfinance",
    )
    parser.add_argument("--no-notify", action="store_true", default=True)
    args = parser.parse_args()
    del args.no_notify

    app_settings = load_settings()
    cfg = app_settings.extreme_movers
    through = args.through or previous_trading_day(
        datetime.now(UTC).date()
    )
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
    screen_coverage = _coverage_by_session(screen_bars, sessions=sessions)
    screen_events = _detect_by_session(
        screen_bars,
        sessions=sessions,
        threshold=(cfg.threshold if cfg.feed == "sip" else cfg.screen_threshold),
    )
    screened_symbols = sorted(
        {event.ticker for rows in screen_events.values() for event in rows}
    )
    confirmation_bars = screen_bars
    if (
        cfg.feed == "hybrid"
        and screened_symbols
        and args.confirmation_source == "yfinance"
    ):
        confirmation_bars = _fetch_all(
            YFinanceSource(),
            screened_symbols,
            sessions[0] - timedelta(days=40),
            sessions[-1] + timedelta(days=1),
            cfg.confirmation_chunk_size,
            "daily",
        )
    confirmation_coverage = _coverage_by_session(
        confirmation_bars, sessions=sessions
    )
    for session in sessions:
        covered_count = len(screen_coverage[session])
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
        confirmed = confirmation_coverage[session] & expected
        if expected and len(confirmed) / len(expected) < cfg.min_confirmation_coverage:
            raise RuntimeError(
                f"confirmation coverage failed for {session}: "
                f"{len(confirmed)}/{len(expected)}"
            )
    confirmation_events = _detect_by_session(
        confirmation_bars,
        sessions=sessions,
        threshold=cfg.threshold,
    )
    detected_symbols = sorted({
        event.ticker for rows in confirmation_events.values() for event in rows
    })
    source_label = None
    run_feed = cfg.feed
    if args.confirmation_source == "iex-only":
        assets = _fetch_alpaca_assets(
            app_settings.alpaca_key,
            app_settings.alpaca_secret,
        )
        profiles = build_alpaca_asset_profiles(assets, as_of=sessions[-1])
        profiles = {
            ticker: profiles[ticker]
            for ticker in detected_symbols
            if ticker in profiles
        }
        source_label = "alpaca_iex_adjustment_all_backfill_unconfirmed"
        run_feed = "hybrid_iex_backfill"
    else:
        profiles = YFinanceFundamentals().profiles(detected_symbols)
    events = build_backfill_events(
        confirmation_bars,
        sessions=sessions,
        profiles=profiles,
        settings=cfg,
        source_label=source_label,
        detected_events=confirmation_events,
        average_dollar_volumes=_average_dollar_volumes(
            confirmation_bars, sessions=sessions
        ),
    )
    for session in sessions:
        missing = {
            event.ticker: event for event in screen_events[session]
            if event.ticker not in confirmation_coverage[session]
        }
        audit_rows = tuple(
            replace(
                qualify_event(
                    replace(
                        event,
                        source="alpaca_iex_screen+confirmation_unavailable",
                    ),
                    None,
                    avg_dollar_volume_20d=Decimal(0),
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
        covered = len(screen_coverage[session])
        screened = len(screen_events[session])
        confirmed = len(confirmation_coverage[session] & {
            event.ticker for event in screen_events[session]
        })
        ledger.replace_extreme_mover_run(
            ExtremeMoverRun(
                session, "COMPLETE", len(symbols), covered,
                datetime.now(UTC),
                screened_count=screened,
                confirmed_count=confirmed,
                feed=run_feed,
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
        "confirmation_source": args.confirmation_source,
        "survivorship": "current_active_symbols",
    }, indent=2))


if __name__ == "__main__":
    main()
