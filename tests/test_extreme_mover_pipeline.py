from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from quant_signal.company_profiles import CompanyProfile
from quant_signal.config import ExtremeMoverSettings
from quant_signal.ledger import SignalLedger
from quant_signal.pipelines.extreme_movers import run_close, run_premarket


NOW = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)


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
    assert "美股单日极端异动" in engine.notifier.cards[-1].title


def test_close_pipeline_fails_closed_below_coverage(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.settings.extreme_movers.min_coverage = 1.0
    engine.source.list_active_symbols = lambda: ["UP", "DOWN", "FLAT", "MISSING"]

    assert run_close(engine, NOW) is False
    assert engine.ledger.latest_complete_extreme_mover_session() is None
    assert engine.notifier.cards == []


def test_premarket_reads_latest_complete_session_without_refetch(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    assert run_close(engine, NOW) is True
    engine.source.calls.clear()
    engine.notifier.cards.clear()

    assert run_premarket(engine, datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)) is True

    assert engine.source.calls == []
    assert engine.notifier.cards[-1].title.startswith("盘前极端异动累计榜")
