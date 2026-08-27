from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from quant_signal.company_profiles import CompanyProfile
from quant_signal.config import ExtremeMoverSettings
from quant_signal.extreme_movers import Eligibility
from research import backfill_extreme_movers as backfill_module
from research.backfill_extreme_movers import (
    _coverage_by_session,
    _detect_by_session,
    build_alpaca_asset_profiles,
    build_backfill_events,
)


def test_backfill_uses_hybrid_aware_coverage_predicate() -> None:
    predicate = getattr(backfill_module, "_coverage_is_acceptable", None)
    assert callable(predicate)
    assert predicate(feed="hybrid", covered=8, universe=10, required=0.9) is True
    assert predicate(feed="sip", covered=8, universe=10, required=0.9) is False


def test_backfill_is_session_bounded_and_marks_survivorship() -> None:
    dates = pd.date_range(end="2026-08-07", periods=24, freq="B", tz="UTC")
    closes = [100.0] * 22 + [80.0, 90.0]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [3_000_000] * len(closes),
        },
        index=pd.MultiIndex.from_product([["AAA"], dates], names=["ticker", "ts"]),
    )
    profile = CompanyProfile(
        ticker="AAA", as_of=date(2026, 8, 7), market_cap_usd=1_000_000_000,
        gics_sector="Information Technology", industry="Software", company_name="AAA",
        business_summary=None, total_revenue=None, revenue_growth=None,
        earnings_growth=None, profit_margin=None, return_on_equity=None,
        free_cash_flow=None, quote_type="EQUITY", source="test", data_status="ok",
    )

    result = build_backfill_events(
        frame,
        sessions=(date(2026, 8, 6), date(2026, 8, 7)),
        profiles={"AAA": profile},
        settings=ExtremeMoverSettings(enabled=True),
    )

    assert list(result) == [date(2026, 8, 6), date(2026, 8, 7)]
    assert all(event.backfilled for rows in result.values() for event in rows)
    assert all(
        event.eligibility is Eligibility.ELIGIBLE
        for rows in result.values() for event in rows
    )
    assert [event.daily_return for event in result[date(2026, 8, 6)]] == [
        Decimal("-0.2")
    ]


def test_alpaca_asset_profiles_exclude_funds_and_preferred_shares() -> None:
    profiles = build_alpaca_asset_profiles(
        [
            {"symbol": "AAOI", "name": "Applied Optoelectronics, Inc. Common Stock"},
            {"symbol": "SPY", "name": "State Street SPDR S&P 500 ETF Trust"},
            {"symbol": "PREF", "name": "Example Corp. 7% Preferred Stock"},
        ],
        as_of=date(2026, 8, 26),
    )

    assert profiles["AAOI"].quote_type == "EQUITY"
    assert profiles["AAOI"].data_status == "ok"
    assert profiles["SPY"].quote_type == "FUND"
    assert profiles["PREF"].quote_type == "PREFERRED"


def test_backfill_can_record_explicit_iex_only_source() -> None:
    dates = pd.date_range(end="2026-08-26", periods=21, freq="B", tz="UTC")
    closes = [100.0] * 20 + [112.0]
    frame = pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [3_000_000] * len(closes),
        },
        index=pd.MultiIndex.from_product([["AAOI"], dates], names=["ticker", "ts"]),
    )
    profile = build_alpaca_asset_profiles(
        [{"symbol": "AAOI", "name": "Applied Optoelectronics, Inc. Common Stock"}],
        as_of=date(2026, 8, 26),
    )["AAOI"]

    result = build_backfill_events(
        frame,
        sessions=(date(2026, 8, 26),),
        profiles={"AAOI": profile},
        settings=ExtremeMoverSettings(enabled=True),
        source_label="alpaca_iex_adjustment_all_backfill_unconfirmed",
    )

    assert result[date(2026, 8, 26)][0].source == (
        "alpaca_iex_adjustment_all_backfill_unconfirmed"
    )


def test_vectorized_backfill_detection_and_coverage_match_session_rules() -> None:
    dates = pd.to_datetime(
        ["2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26"],
        utc=True,
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 112.0, 100.0, 111.0],
            "high": [100.0, 112.0, 100.0, 111.0],
            "low": [100.0, 112.0, 100.0, 111.0],
            "close": [100.0, 112.0, 100.0, 111.0],
            "volume": [3_000_000] * 4,
        },
        index=pd.MultiIndex.from_product([["AAOI"], dates], names=["ticker", "ts"]),
    )
    sessions = (date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26))

    detected = _detect_by_session(
        frame,
        sessions=sessions,
        threshold=Decimal("0.10"),
    )
    covered = _coverage_by_session(frame, sessions=sessions)

    assert [event.ticker for event in detected[date(2026, 8, 24)]] == ["AAOI"]
    assert detected[date(2026, 8, 25)][0].direction.value == "down"
    assert [event.ticker for event in detected[date(2026, 8, 26)]] == ["AAOI"]
    assert covered == {session: {"AAOI"} for session in sessions}
