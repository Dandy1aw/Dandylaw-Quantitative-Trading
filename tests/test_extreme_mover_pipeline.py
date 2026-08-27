from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from quant_signal.company_profiles import CompanyProfile
from quant_signal.config import ExtremeMoverSettings
from quant_signal.extreme_movers import ExtremeMoverRun
from quant_signal.ledger import SignalLedger
from quant_signal.pipelines import extreme_movers as extreme_movers_module
from quant_signal.pipelines.extreme_movers import run_close, run_premarket

NOW = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)


def _bars(symbols: list[str], start: date, end: date) -> pd.DataFrame:
    del start, end
    closes = {"UP": (10.0, 11.2), "DOWN": (10.0, 8.8), "FLAT": (10.0, 10.5)}
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for ts, close in zip(
            pd.to_datetime(["2026-08-06", "2026-08-07"], utc=True),
            closes[symbol],
            strict=True,
        ):
            rows.append({
                "ticker": symbol, "ts": ts, "open": close, "high": close,
                "low": close, "close": close, "volume": 3_000_000,
            })
    return pd.DataFrame(rows).set_index(["ticker", "ts"]).sort_index()


class Source:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.asset_list_calls = 0

    def list_active_symbols(self) -> list[str]:
        self.asset_list_calls += 1
        return ["UP", "DOWN", "FLAT"]

    def fetch_sip_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pd.DataFrame:
        self.calls.append(tuple(symbols))
        return _bars(symbols, start, end)

    def fetch_daily_bars(
        self, symbols: list[str], start: date, end: date
    ) -> pd.DataFrame:
        self.calls.append(tuple(symbols))
        return _bars(symbols, start, end)


class Profiles:
    def __init__(self) -> None:
        self.requested: set[str] = set()

    def profiles(self, tickers: list[str]) -> dict[str, CompanyProfile]:
        self.requested.update(tickers)
        return {
            ticker: CompanyProfile(
                ticker=ticker, as_of=date(2026, 8, 7),
                market_cap_usd=1_000_000_000,
                gics_sector="Information Technology", industry="Software",
                company_name=ticker, business_summary=None, total_revenue=None,
                revenue_growth=None, earnings_growth=None, profit_margin=None,
                return_on_equity=None, free_cash_flow=None, quote_type="EQUITY",
                source="test", data_status="ok",
            )
            for ticker in tickers
        }


class Notifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def _engine(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(extreme_movers=ExtremeMoverSettings(enabled=True)),
        source=Source(),
        confirmation_source=Source(),
        fundamentals_source=Profiles(),
        ledger=SignalLedger(tmp_path / "signals.db"),
        notifier=Notifier(),
    )


def test_close_pipeline_enriches_only_detected_movers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    assert run_close(engine, NOW) is True

    assert engine.source.asset_list_calls == 1
    assert engine.fundamentals_source.requested == {"UP", "DOWN"}
    assert engine.ledger.latest_complete_extreme_mover_session() == date(2026, 8, 7)
    assert len(engine.ledger.extreme_mover_events(date(2026, 8, 7))) == 2
    assert all(
        event.source == "alpaca_iex_screen+yfinance_adjusted_confirm"
        for event in engine.ledger.extreme_mover_events(date(2026, 8, 7))
    )
    assert "美股单日极端异动" in engine.notifier.cards[-1].title


def test_close_pipeline_sip_fails_closed_below_coverage(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.settings.extreme_movers.feed = "sip"
    engine.settings.extreme_movers.min_coverage = 1.0
    engine.source.list_active_symbols = lambda: ["UP", "DOWN", "FLAT", "MISSING"]
    engine.source.fetch_sip_daily_bars = (
        lambda symbols, start, end: _bars(
            [symbol for symbol in symbols if symbol != "MISSING"], start, end
        )
    )

    assert run_close(engine, NOW) is False
    assert engine.ledger.latest_complete_extreme_mover_session() is None
    assert engine.ledger.extreme_mover_run(date(2026, 8, 7))["status"] == "FAILED"
    assert engine.notifier.cards == []


def test_premarket_reads_latest_complete_session_without_refetch(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert run_close(engine, NOW) is True
    latest = engine.ledger.latest_complete_extreme_mover_session()
    assert latest is not None
    for offset in range(1, 30):
        session = latest - timedelta(days=offset)
        engine.ledger.replace_extreme_mover_run(
            ExtremeMoverRun(session, "COMPLETE", 1, 1, NOW),
            [],
        )
    engine.source.calls.clear()
    engine.notifier.cards.clear()

    assert run_premarket(engine, datetime(2026, 8, 10, 12, 0, tzinfo=UTC)) is True

    assert engine.source.calls == []
    assert engine.notifier.cards[-1].title.startswith("30日涨超10%次数榜")


def test_premarket_discloses_iex_only_backfill_source(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert run_close(engine, NOW, notify=False) is True
    latest = engine.ledger.latest_complete_extreme_mover_session()
    assert latest is not None
    events = engine.ledger.extreme_mover_events(latest)
    engine.ledger.replace_extreme_mover_run(
        ExtremeMoverRun(latest, "COMPLETE", 3, 3, NOW, feed="hybrid_iex_backfill"),
        [
            replace(
                event,
                source="alpaca_iex_adjustment_all_backfill_unconfirmed",
                backfilled=True,
            )
            for event in events
        ],
    )
    for offset in range(1, 30):
        session = latest - timedelta(days=offset)
        engine.ledger.replace_extreme_mover_run(
            ExtremeMoverRun(session, "COMPLETE", 1, 1, NOW),
            [],
        )

    assert run_premarket(engine, NOW) is True

    body = engine.notifier.cards[-1].body_md
    assert "Alpaca IEX adjusted 回填（无 Yahoo 二次确认）" in body


def test_premarket_refuses_incomplete_thirty_session_history(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert run_close(engine, NOW) is True
    engine.notifier.cards.clear()

    assert run_premarket(engine, datetime(2026, 8, 10, 12, 0, tzinfo=UTC)) is False

    assert engine.notifier.cards == []


def test_close_pipeline_hybrid_accepts_nonzero_partial_iex_coverage(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    engine.settings.extreme_movers.min_coverage = 1.0
    engine.source.list_active_symbols = lambda: ["UP", "DOWN", "FLAT", "MISSING"]
    engine.source.fetch_daily_bars = (
        lambda symbols, start, end: _bars(
            [symbol for symbol in symbols if symbol != "MISSING"], start, end
        )
    )

    assert run_close(engine, NOW, notify=False) is True

    run = engine.ledger.extreme_mover_run(date(2026, 8, 7))
    assert run is not None
    assert run["covered_count"] == 3


def test_coverage_predicate_keeps_hybrid_partial_and_sip_strict() -> None:
    predicate = getattr(extreme_movers_module, "_coverage_is_acceptable", None)
    assert callable(predicate)
    assert predicate(feed="hybrid", covered=3, universe=4, required=1.0) is True
    assert predicate(feed="hybrid", covered=0, universe=4, required=0.1) is False
    assert predicate(feed="sip", covered=3, universe=4, required=1.0) is False
    assert predicate(feed="sip", covered=4, universe=4, required=1.0) is True


def test_delivery_failure_is_reported_after_complete_snapshot(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.notifier.send = lambda card: False

    assert run_close(engine, NOW) is False
    assert engine.ledger.latest_complete_extreme_mover_session() == date(2026, 8, 7)


def test_confirmation_coverage_requires_exact_previous_session(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.settings.extreme_movers.min_confirmation_coverage = 0.5

    def confirmation_bars(
        symbols: list[str], start: date, end: date
    ) -> pd.DataFrame:
        del start, end
        rows: list[dict[str, object]] = []
        for symbol in symbols:
            dates = ["2026-08-06", "2026-08-07"]
            if symbol == "DOWN":
                dates[0] = "2026-08-05"
            closes = (10.0, 11.2) if symbol == "UP" else (10.0, 8.8)
            for ts, close in zip(pd.to_datetime(dates, utc=True), closes, strict=True):
                rows.append(
                    {
                        "ticker": symbol,
                        "ts": ts,
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "volume": 3_000_000,
                    }
                )
        return pd.DataFrame(rows).set_index(["ticker", "ts"]).sort_index()

    engine.confirmation_source.fetch_daily_bars = confirmation_bars

    assert run_close(engine, NOW, notify=False) is True

    run = engine.ledger.extreme_mover_run(date(2026, 8, 7))
    events = {
        event.ticker: event
        for event in engine.ledger.extreme_mover_events(date(2026, 8, 7))
    }
    assert run["confirmed_count"] == 1
    assert events["DOWN"].eligibility.value == "profile_unavailable"


def test_deadline_starts_before_asset_listing_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_signal.pipelines import extreme_movers as module

    engine = _engine(tmp_path)
    engine.settings.extreme_movers.deadline_seconds = 60
    clock = {"value": 0.0}

    def slow_list() -> list[str]:
        clock["value"] = 61.0
        return ["UP", "DOWN", "FLAT"]

    engine.source.list_active_symbols = slow_list
    monkeypatch.setattr(module.monotonic_time, "monotonic", lambda: clock["value"])

    assert run_close(engine, NOW, notify=False) is False
    assert engine.ledger.extreme_mover_run(date(2026, 8, 7))["error"] == "DEADLINE_EXCEEDED"
    assert engine.notifier.cards == []


def test_deadline_after_profile_fetch_prevents_commit_and_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quant_signal.pipelines import extreme_movers as module

    engine = _engine(tmp_path)
    engine.settings.extreme_movers.deadline_seconds = 60
    clock = {"value": 0.0}
    original_profiles = engine.fundamentals_source.profiles

    def slow_profiles(tickers: list[str]) -> dict[str, CompanyProfile]:
        result = original_profiles(tickers)
        clock["value"] = 61.0
        return result

    engine.fundamentals_source.profiles = slow_profiles
    monkeypatch.setattr(module.monotonic_time, "monotonic", lambda: clock["value"])

    assert run_close(engine, NOW, notify=True) is False
    assert engine.ledger.latest_complete_extreme_mover_session() is None
    assert engine.ledger.extreme_mover_run(date(2026, 8, 7))["error"] == "DEADLINE_EXCEEDED"
    assert engine.notifier.cards == []
