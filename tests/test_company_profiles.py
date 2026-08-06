from datetime import date

from quant_signal.candidate_lanes import Candidate, CandidateLane
from quant_signal.company_profiles import CompanyProfile, select_sector_candidates


AS_OF = date(2026, 7, 21)


def candidate(ticker: str, score: float) -> Candidate:
    return Candidate(
        ticker=ticker,
        lane=CandidateLane.TREND_CONTINUATION,
        score=score,
        price=100.0,
        entry_low=98.0,
        entry_high=100.0,
        invalidation_price=90.0,
        target_price=120.0,
        atr=3.0,
        history_days=252,
        reasons=("LONG_TREND",),
    )


def profile(
    ticker: str,
    sector: str,
    market_cap: int | None,
    *,
    quote_type: str = "EQUITY",
    status: str = "ok",
    industry: str | None = None,
) -> CompanyProfile:
    return CompanyProfile(
        ticker=ticker,
        as_of=AS_OF,
        market_cap_usd=market_cap,
        gics_sector=sector,
        industry=industry or f"{sector} industry",
        company_name=ticker,
        business_summary=f"{ticker} summary",
        total_revenue=None,
        revenue_growth=None,
        earnings_growth=None,
        profit_margin=None,
        return_on_equity=None,
        free_cash_flow=None,
        quote_type=quote_type,
        source="test",
        data_status=status,
    )


def test_market_cap_boundary_and_missing_profile_fail_closed() -> None:
    candidates = (
        candidate("PASS", 3.0),
        candidate("LOW", 2.0),
        candidate("MISS", 1.0),
        candidate("ETF", 0.5),
    )
    profiles = {
        "PASS": profile("PASS", "Information Technology", 100_000_000_000),
        "LOW": profile("LOW", "Information Technology", 99_999_999_999),
        "ETF": profile(
            "ETF", "Information Technology", 500_000_000_000, quote_type="ETF"
        ),
    }

    result = select_sector_candidates(candidates, profiles)

    assert [row.candidate.ticker for row in result.selected] == ["PASS"]
    assert result.rejected == {
        "ETF": "NON_EQUITY",
        "LOW": "MARKET_CAP_FILTER",
        "MISS": "PROFILE_UNAVAILABLE",
    }


def test_sector_selection_caps_three_per_sector_and_three_sectors() -> None:
    rows = tuple(
        candidate(ticker, score)
        for ticker, score in (
            ("T1", 10.0), ("T2", 9.0), ("T3", 8.0), ("T4", 7.0),
            ("H1", 6.0), ("H2", 5.0), ("H3", 4.0),
            ("F1", 3.0), ("F2", 2.0),
            ("C1", 1.0),
        )
    )
    sectors = {
        **{ticker: "Information Technology" for ticker in ("T1", "T2", "T3", "T4")},
        **{ticker: "Health Care" for ticker in ("H1", "H2", "H3")},
        **{ticker: "Financials" for ticker in ("F1", "F2")},
        "C1": "Consumer Staples",
    }
    profiles = {
        ticker: profile(ticker, sector, 100_000_000_000 + index)
        for index, (ticker, sector) in enumerate(sectors.items())
    }

    result = select_sector_candidates(rows, profiles, top_n_per_sector=3, max_sectors=3)

    selected = [row.candidate.ticker for row in result.selected]
    assert selected == ["T1", "T2", "T3", "H1", "H2", "H3", "F1", "F2"]
    assert result.rejected["T4"] == "SECTOR_CAP"
    assert result.rejected["C1"] == "SECTOR_NOT_SELECTED"


def test_sector_and_market_cap_ranks_are_deterministic() -> None:
    candidates = (candidate("B", 2.0), candidate("A", 2.0), candidate("C", 1.0))
    profiles = {
        "A": profile("A", "Health Care", 200_000_000_000),
        "B": profile("B", "Health Care", 300_000_000_000),
        "C": profile("C", "Health Care", 150_000_000_000),
        "PEER": profile("PEER", "Health Care", 400_000_000_000),
    }

    result = select_sector_candidates(candidates, profiles)
    by_ticker = {row.candidate.ticker: row for row in result.selected}

    assert [row.candidate.ticker for row in result.selected] == ["A", "B", "C"]
    assert by_ticker["A"].sector_strategy_rank == 1
    assert by_ticker["B"].sector_strategy_rank == 2
    assert by_ticker["A"].sector_market_cap_rank == 3
    assert by_ticker["B"].sector_market_cap_rank == 2
    assert by_ticker["C"].sector_market_cap_rank == 4


def test_semiconductor_and_technology_groups_expand_to_ten() -> None:
    candidates = tuple(
        candidate(ticker, 100.0 - index)
        for index, ticker in enumerate(
            [
                *(f"S{number:02d}" for number in range(12)),
                *(f"T{number:02d}" for number in range(12)),
                *(f"C{number:02d}" for number in range(5)),
            ]
        )
    )
    profiles = {
        **{
            f"S{number:02d}": profile(
                f"S{number:02d}",
                "Information Technology",
                300_000_000_000 - number,
                industry=(
                    "Semiconductors"
                    if number < 8
                    else "Semiconductor Equipment & Materials"
                ),
            )
            for number in range(12)
        },
        **{
            f"T{number:02d}": profile(
                f"T{number:02d}",
                "Information Technology",
                250_000_000_000 - number,
                industry="Software - Infrastructure",
            )
            for number in range(12)
        },
        **{
            f"C{number:02d}": profile(
                f"C{number:02d}",
                "Consumer Discretionary",
                200_000_000_000 - number,
                industry="Internet Retail",
            )
            for number in range(5)
        },
    }

    result = select_sector_candidates(
        candidates,
        profiles,
        top_n_per_sector=3,
        max_sectors=5,
        top_n_overrides={"Semiconductors": 10, "Technology": 10},
    )

    by_group: dict[str, list[str]] = {}
    for row in result.selected:
        by_group.setdefault(row.candidate_group, []).append(row.candidate.ticker)
    assert len(by_group["Semiconductors"]) == 10
    assert len(by_group["Technology"]) == 10
    assert len(by_group["Consumer Discretionary"]) == 3
    assert result.rejected["S10"] == "SECTOR_CAP"
    assert result.rejected["T10"] == "SECTOR_CAP"
    assert result.rejected["C03"] == "SECTOR_CAP"
