from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from quant_signal.company_profiles import CompanyProfile
from quant_signal.extreme_movers import (
    Eligibility,
    MoverDirection,
    average_dollar_volume,
    detect_extreme_movers,
    qualify_event,
)


SESSION = date(2026, 8, 7)


def _bars(closes: dict[str, list[float]], *, volumes: dict[str, list[int]] | None = None) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    dates = pd.to_datetime(["2026-08-06", "2026-08-07"], utc=True)
    for ticker, values in closes.items():
        ticker_volumes = (volumes or {}).get(ticker, [1_000_000] * len(values))
        for ts, close, volume in zip(dates, values, ticker_volumes, strict=True):
            rows.append(
                {
                    "ticker": ticker,
                    "ts": ts,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": volume,
                }
            )
    return pd.DataFrame(rows).set_index(["ticker", "ts"]).sort_index()


def _profile(ticker: str, quote_type: str = "EQUITY") -> CompanyProfile:
    return CompanyProfile(
        ticker=ticker,
        as_of=SESSION,
        market_cap_usd=10_000_000_000,
        gics_sector="Information Technology",
        industry="Software",
        company_name=ticker,
        business_summary=None,
        total_revenue=None,
        revenue_growth=None,
        earnings_growth=None,
        profit_margin=None,
        return_on_equity=None,
        free_cash_flow=None,
        quote_type=quote_type,
        source="test",
        data_status="ok",
    )


def test_detects_exact_positive_and_negative_thresholds() -> None:
    events = detect_extreme_movers(
        _bars(
            {
                "UP": [100.0, 110.0],
                "DOWN": [100.0, 90.0],
                "FLAT": [100.0, 109.99],
            }
        ),
        SESSION,
        threshold=Decimal("0.10"),
    )

    assert [
        (row.ticker, row.direction, row.daily_return)
        for row in events
    ] == [
        ("DOWN", MoverDirection.DOWN, Decimal("-0.1")),
        ("UP", MoverDirection.UP, Decimal("0.1")),
    ]


def test_detection_requires_two_distinct_sessions_and_current_final_bar() -> None:
    bars = _bars({"OK": [10.0, 11.0], "STALE": [10.0, 11.0]})
    stale = bars.drop(index=("STALE", pd.Timestamp("2026-08-07", tz="UTC")))
    one_row = stale.drop(index=("STALE", pd.Timestamp("2026-08-06", tz="UTC")))
    combined = pd.concat([stale.loc[["OK"]], one_row]).sort_index()

    events = detect_extreme_movers(
        combined,
        SESSION,
        threshold=Decimal("0.10"),
    )

    assert [row.ticker for row in events] == ["OK"]


def test_qualification_requires_equity_price_and_liquidity() -> None:
    source_events = {
        event.ticker: event
        for event in detect_extreme_movers(
            _bars(
                {
                    "GOOD": [10.0, 11.0],
                    "ETF": [10.0, 11.0],
                    "LOW": [4.0, 4.5],
                    "THIN": [10.0, 11.0],
                }
            ),
            SESSION,
            threshold=Decimal("0.10"),
        )
    }

    assert qualify_event(
        source_events["GOOD"],
        _profile("GOOD"),
        avg_dollar_volume_20d=Decimal("20000000"),
        min_price=Decimal("5"),
        min_dollar_volume=Decimal("20000000"),
    ).eligibility is Eligibility.ELIGIBLE
    assert qualify_event(
        source_events["ETF"],
        _profile("ETF", quote_type="ETF"),
        avg_dollar_volume_20d=Decimal("30000000"),
        min_price=Decimal("5"),
        min_dollar_volume=Decimal("20000000"),
    ).eligibility is Eligibility.NON_EQUITY
    assert qualify_event(
        source_events["LOW"],
        _profile("LOW"),
        avg_dollar_volume_20d=Decimal("30000000"),
        min_price=Decimal("5"),
        min_dollar_volume=Decimal("20000000"),
    ).eligibility is Eligibility.LOW_PRICE
    assert qualify_event(
        source_events["THIN"],
        _profile("THIN"),
        avg_dollar_volume_20d=Decimal("19999999"),
        min_price=Decimal("5"),
        min_dollar_volume=Decimal("20000000"),
    ).eligibility is Eligibility.LOW_LIQUIDITY


def test_average_dollar_volume_uses_last_twenty_complete_rows() -> None:
    dates = pd.date_range("2026-07-01", periods=25, freq="B", tz="UTC")
    frame = pd.DataFrame(
        {
            "close": [10.0] * 25,
            "volume": [1_000_000] * 5 + [2_000_000] * 20,
        },
        index=dates,
    )

    assert average_dollar_volume(frame, sessions=20) == Decimal("20000000")

