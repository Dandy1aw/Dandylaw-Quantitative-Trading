from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd

from quant_signal.company_profiles import CompanyProfile
from quant_signal.extreme_movers import (
    Eligibility,
    ExtremeMoverEvent,
    MoverDirection,
    average_dollar_volume,
    detect_extreme_movers,
    qualify_event,
    rank_movers,
    rank_sectors,
    window_total_return,
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


def test_detection_rejects_gap_and_non_finite_close() -> None:
    rows = pd.DataFrame(
        [
            {"ticker": "GAP", "ts": pd.Timestamp("2026-08-05", tz="UTC"), "close": 10.0},
            {"ticker": "GAP", "ts": pd.Timestamp("2026-08-07", tz="UTC"), "close": 12.0},
            {"ticker": "NAN", "ts": pd.Timestamp("2026-08-06", tz="UTC"), "close": 10.0},
            {"ticker": "NAN", "ts": pd.Timestamp("2026-08-07", tz="UTC"), "close": float("nan")},
        ]
    ).set_index(["ticker", "ts"])

    assert detect_extreme_movers(rows, SESSION) == ()


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
        avg_dollar_volume_20d=Decimal(20000000),
        min_price=Decimal(5),
        min_dollar_volume=Decimal(20000000),
    ).eligibility is Eligibility.ELIGIBLE
    assert qualify_event(
        source_events["ETF"],
        _profile("ETF", quote_type="ETF"),
        avg_dollar_volume_20d=Decimal(30000000),
        min_price=Decimal(5),
        min_dollar_volume=Decimal(20000000),
    ).eligibility is Eligibility.NON_EQUITY
    assert qualify_event(
        source_events["LOW"],
        _profile("LOW"),
        avg_dollar_volume_20d=Decimal(30000000),
        min_price=Decimal(5),
        min_dollar_volume=Decimal(20000000),
    ).eligibility is Eligibility.LOW_PRICE
    assert qualify_event(
        source_events["THIN"],
        _profile("THIN"),
        avg_dollar_volume_20d=Decimal(19999999),
        min_price=Decimal(5),
        min_dollar_volume=Decimal(20000000),
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

    assert average_dollar_volume(frame, sessions=20) == Decimal(20000000)


def test_average_dollar_volume_requires_full_window() -> None:
    frame = pd.DataFrame(
        {"close": [10.0] * 19, "volume": [3_000_000] * 19},
        index=pd.date_range("2026-07-01", periods=19, freq="B", tz="UTC"),
    )

    assert average_dollar_volume(frame, sessions=20) == Decimal(0)


def test_average_dollar_volume_rejects_non_finite_rows() -> None:
    frame = pd.DataFrame(
        {
            "close": [10.0] * 19 + [float("inf")],
            "volume": [3_000_000] * 20,
        },
        index=pd.date_range("2026-07-01", periods=20, freq="B", tz="UTC"),
    )

    assert average_dollar_volume(frame, sessions=20) == Decimal(0)


def _eligible(
    ticker: str,
    daily_return: str,
    *,
    day: int,
    sector: str = "Information Technology",
) -> ExtremeMoverEvent:
    return ExtremeMoverEvent(
        session=date(2026, 8, day),
        ticker=ticker,
        direction=(
            MoverDirection.UP
            if Decimal(daily_return) > 0
            else MoverDirection.DOWN
        ),
        daily_return=Decimal(daily_return),
        close=Decimal(20),
        avg_dollar_volume_20d=Decimal(30000000),
        sector=sector,
        industry="Software",
        quote_type="EQUITY",
        eligibility=Eligibility.ELIGIBLE,
    )


def test_rank_movers_counts_days_and_compounds_event_returns() -> None:
    rows = [
        _eligible("A", "0.10", day=1),
        _eligible("A", "0.20", day=2),
        _eligible("B", "0.15", day=2),
    ]

    ranked = rank_movers(rows, window_sessions=60)

    assert ranked[0].ticker == "A"
    assert ranked[0].event_days == 2
    assert ranked[0].event_compound_return == Decimal("0.32")
    assert ranked[0].most_recent_event == date(2026, 8, 2)


def test_rank_movers_keeps_up_and_down_boards_separate() -> None:
    rows = [
        _eligible("A", "0.11", day=1),
        _eligible("A", "-0.12", day=2),
        _eligible("B", "-0.15", day=3),
    ]

    ranked = rank_movers(rows, window_sessions=20)

    assert [(row.ticker, row.direction, row.event_days) for row in ranked] == [
        ("B", MoverDirection.DOWN, 1),
        ("A", MoverDirection.DOWN, 1),
        ("A", MoverDirection.UP, 1),
    ]


def test_rank_sectors_uses_event_days_then_repeat_intensity() -> None:
    rows = [
        _eligible("A", "0.10", day=1),
        _eligible("A", "0.11", day=2),
        _eligible("B", "0.12", day=2),
        _eligible("B", "0.13", day=3),
        _eligible("C", "0.20", day=3, sector="Energy"),
    ]

    ranked = rank_sectors(rows, window_sessions=60)

    assert ranked[0].sector == "Information Technology"
    assert ranked[0].event_days == 4
    assert ranked[0].unique_movers == 2
    assert ranked[0].repeat_intensity == Decimal(4) / Decimal(2 * 60)


def test_rank_sectors_keeps_eligible_unknown_sector_as_uncategorized() -> None:
    event = _eligible("A", "0.12", day=2)

    ranked = rank_sectors([event.__class__(**{**event.__dict__, "sector": None})], window_sessions=20)

    assert ranked[0].sector == "未分类"


def test_window_total_return_uses_first_and_last_close() -> None:
    assert window_total_return([Decimal(100), Decimal(80), Decimal(120)]) == Decimal("0.2")
    assert window_total_return([]) is None
