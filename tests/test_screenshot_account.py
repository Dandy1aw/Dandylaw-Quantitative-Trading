from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_signal.account import AccountFetchError
from quant_signal.config import ExecutionPlanSettings
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.ledger import SignalLedger
from quant_signal.portfolio_import import (
    ExtractedAccount,
    ExtractedPosition,
    PortfolioExtraction,
    validate_extraction,
)
from quant_signal.screenshot_account import ScreenshotAccountProvider
from conftest import make_test_settings

NOW = datetime(2026, 7, 11, 2, 0, tzinfo=timezone(timedelta(hours=8)))


def seed_partial(ledger: SignalLedger, uploaded_at: datetime = NOW) -> None:
    extraction = PortfolioExtraction(
        account=ExtractedAccount(
            equity="5995.52",
            market_value="4244.15",
            cash="1751.13",
            buying_power="3474.15",
            currency="USD",
            reported_position_count=6,
            observed_at=uploaded_at,
        ),
        positions=(
            ExtractedPosition(symbol="DRAM", weight_pct="14.80", pnl="-511.13", pnl_pct="-36.54"),
            ExtractedPosition(symbol="MU", weight_pct="16.53", pnl="166.82", pnl_pct="20.23"),
            ExtractedPosition(symbol="RAM", weight_pct="6.07", pnl="45.13", pnl_pct="14.16"),
            ExtractedPosition(symbol="SMH", weight_pct="20.46", pnl="11.52", pnl_pct="0.95"),
            ExtractedPosition(symbol="SNXX", weight_pct="12.92", pnl="158.73", pnl_pct="25.77"),
        ),
    )
    ledger.save_portfolio_import(
        validate_extraction(
            extraction,
            image_sha256="e" * 64,
            uploaded_at=uploaded_at,
            capital_limit=Decimal("6000"),
            max_financing_ratio=Decimal("0.20"),
        )
    )


def test_provider_returns_real_summary_and_risk_exposures(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    seed_partial(ledger)

    state = ScreenshotAccountProvider(
        ledger, max_age=timedelta(hours=72)
    ).snapshot(NOW)

    assert state.snapshot.source == "screenshot"
    assert state.snapshot.equity == Decimal("5995.52")
    assert state.snapshot.cash == Decimal("1751.13")
    assert state.snapshot.market_value == Decimal("4244.15")
    assert state.snapshot.capital_limit == Decimal("6000")
    assert state.snapshot.max_financing_ratio == Decimal("0.20")
    assert state.snapshot.max_gross_exposure == Decimal("7200")
    assert state.positions == ()
    assert state.positions_partial is True
    assert {row.symbol for row in state.observed_positions} == {"DRAM", "MU", "RAM", "SMH", "SNXX"}
    assert "AAPL" not in {row.symbol for row in state.observed_positions}


def test_provider_rejects_missing_or_stale_snapshot(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    provider = ScreenshotAccountProvider(ledger, max_age=timedelta(hours=24))
    with pytest.raises(AccountFetchError, match="no validated screenshot"):
        provider.snapshot(NOW)

    seed_partial(ledger, uploaded_at=NOW - timedelta(hours=25))
    with pytest.raises(AccountFetchError, match="stale"):
        provider.snapshot(NOW)


def test_engine_wires_screenshot_provider_without_alpaca_credentials(tmp_path: Path) -> None:
    class EmptySource:
        def fetch_daily_bars(self, tickers, start, end):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

        def fetch_intraday_bars(self, tickers, lookback_days=5):  # type: ignore[no-untyped-def]
            raise AssertionError("not used")

    class NullNotifier:
        def send(self, card: object) -> bool:
            return True

    settings = make_test_settings(
        execution_plan=ExecutionPlanSettings(
            enabled=True,
            account_provider="screenshot",
            screenshot_max_age_hours=72,
        )
    )
    ledger = SignalLedger(tmp_path / "signals.db")
    engine = Engine(
        settings,
        BarStore(tmp_path / "bars.duckdb"),
        EmptySource(),  # type: ignore[arg-type]
        ledger,
        NullNotifier(),  # type: ignore[arg-type]
    )

    assert isinstance(engine.account_provider, ScreenshotAccountProvider)


def test_partial_snapshot_keeps_last_exact_position_quantities(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    exact = PortfolioExtraction(
        account=ExtractedAccount(
            equity="5995.52",
            market_value="991.06",
            cash="5004.46",
            buying_power="5004.46",
            currency="USD",
            reported_position_count=2,
            observed_at=NOW - timedelta(hours=1),
        ),
        positions=(
            ExtractedPosition(
                symbol="MU",
                qty="1",
                avg_entry_price="824.24",
                current_price="991.06",
                market_value="991.06",
                weight_pct="16.53",
                pnl="166.82",
                pnl_pct="20.23",
            ),
            ExtractedPosition(
                symbol="MUU",
                qty="0",
                avg_entry_price="0",
                current_price="29.848",
                market_value="0",
            ),
        ),
    )
    ledger.save_portfolio_import(
        validate_extraction(
            exact,
            image_sha256="1" * 64,
            uploaded_at=NOW - timedelta(hours=1),
            capital_limit=Decimal("6000"),
            max_financing_ratio=Decimal("0.20"),
        )
    )
    seed_partial(ledger, uploaded_at=NOW)

    state = ScreenshotAccountProvider(
        ledger, max_age=timedelta(hours=72)
    ).snapshot(NOW)

    assert state.positions_partial is True
    assert len(state.positions) == 1
    assert state.positions[0].symbol == "MU"
    assert state.positions[0].qty == Decimal("1")
    assert {row.symbol for row in state.observed_positions} == {"DRAM", "MU", "RAM", "SMH", "SNXX"}
