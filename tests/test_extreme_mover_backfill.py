from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from quant_signal.company_profiles import CompanyProfile
from quant_signal.config import ExtremeMoverSettings
from quant_signal.extreme_movers import Eligibility
from research.backfill_extreme_movers import build_backfill_events


def test_backfill_is_session_bounded_and_marks_survivorship() -> None:
    dates = pd.to_datetime(
        ["2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"], utc=True
    )
    frame = pd.DataFrame(
        {
            "open": [100.0, 111.0, 90.0, 100.0],
            "high": [100.0, 111.0, 90.0, 100.0],
            "low": [100.0, 111.0, 90.0, 100.0],
            "close": [100.0, 111.0, 90.0, 100.0],
            "volume": [3_000_000] * 4,
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
        Decimal("-0.1891891891891891891891891892")
    ]
