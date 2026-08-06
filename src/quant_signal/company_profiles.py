"""Company-profile facts and deterministic large-cap sector selection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, Mapping, Sequence

from quant_signal.candidate_lanes import Candidate

ProfileStatus = Literal["ok", "unavailable"]

GICS_SECTORS = frozenset(
    {
        "Communication Services",
        "Consumer Discretionary",
        "Consumer Staples",
        "Energy",
        "Financials",
        "Health Care",
        "Industrials",
        "Information Technology",
        "Materials",
        "Real Estate",
        "Utilities",
    }
)


@dataclass(frozen=True)
class CompanyProfile:
    ticker: str
    as_of: date
    market_cap_usd: int | None
    gics_sector: str | None
    industry: str | None
    company_name: str | None
    business_summary: str | None
    total_revenue: int | None
    revenue_growth: float | None
    earnings_growth: float | None
    profit_margin: float | None
    return_on_equity: float | None
    free_cash_flow: int | None
    quote_type: str | None
    source: str
    data_status: ProfileStatus


@dataclass(frozen=True)
class RankedSectorCandidate:
    candidate: Candidate
    profile: CompanyProfile
    candidate_group: str
    sector_strategy_rank: int
    sector_market_cap_rank: int


@dataclass(frozen=True)
class SectorCandidateSelection:
    selected: tuple[RankedSectorCandidate, ...]
    rejected: Mapping[str, str]


def _eligible_profile(profile: CompanyProfile, min_market_cap_usd: int) -> str | None:
    if profile.data_status != "ok":
        return "PROFILE_UNAVAILABLE"
    if (profile.quote_type or "").upper() != "EQUITY":
        return "NON_EQUITY"
    if profile.gics_sector not in GICS_SECTORS:
        return "PROFILE_UNAVAILABLE"
    if profile.market_cap_usd is None:
        return "PROFILE_UNAVAILABLE"
    if profile.market_cap_usd < min_market_cap_usd:
        return "MARKET_CAP_FILTER"
    return None


def recommendation_group(profile: CompanyProfile) -> str | None:
    """Return the stable notification group used for caps and peer ranks."""
    if profile.gics_sector is None:
        return None
    industry = (profile.industry or "").casefold()
    if "semiconductor" in industry:
        return "Semiconductors"
    if profile.gics_sector == "Information Technology":
        return "Technology"
    return profile.gics_sector


def select_sector_candidates(
    candidates: Sequence[Candidate],
    profiles: Mapping[str, CompanyProfile],
    *,
    min_market_cap_usd: int = 100_000_000_000,
    top_n_per_sector: int = 3,
    max_sectors: int = 3,
    top_n_overrides: Mapping[str, int] | None = None,
) -> SectorCandidateSelection:
    if min_market_cap_usd <= 0:
        raise ValueError("min_market_cap_usd must be positive")
    if top_n_per_sector < 1 or max_sectors < 1:
        raise ValueError("sector limits must be positive")
    limits = dict(top_n_overrides or {})
    if any(limit < 1 for limit in limits.values()):
        raise ValueError("sector overrides must be positive")
    rejected: dict[str, str] = {}
    eligible: list[tuple[Candidate, CompanyProfile]] = []
    for candidate in candidates:
        profile = profiles.get(candidate.ticker)
        if profile is None:
            rejected[candidate.ticker] = "PROFILE_UNAVAILABLE"
            continue
        reason = _eligible_profile(profile, min_market_cap_usd)
        if reason is not None:
            rejected[candidate.ticker] = reason
            continue
        eligible.append((candidate, profile))

    by_sector: dict[str, list[tuple[Candidate, CompanyProfile]]] = {}
    for candidate, profile in eligible:
        group = recommendation_group(profile)
        assert group is not None
        by_sector.setdefault(group, []).append((candidate, profile))
    for rows in by_sector.values():
        rows.sort(key=lambda item: (-item[0].score, item[0].ticker))

    selected_sectors = [
        sector
        for sector, _ in sorted(
            by_sector.items(),
            key=lambda item: (-item[1][0][0].score, item[0]),
        )[:max_sectors]
    ]
    for sector, sector_candidates in by_sector.items():
        if sector not in selected_sectors:
            rejected.update(
                {
                    candidate.ticker: "SECTOR_NOT_SELECTED"
                    for candidate, _ in sector_candidates
                }
            )

    market_cap_ranks: dict[tuple[str, str], int] = {}
    sector_profiles: dict[str, list[CompanyProfile]] = {}
    for profile in profiles.values():
        if _eligible_profile(profile, min_market_cap_usd) is None:
            group = recommendation_group(profile)
            assert group is not None
            sector_profiles.setdefault(group, []).append(profile)
    for sector, profile_rows in sector_profiles.items():
        profile_rows.sort(
            key=lambda profile: (
                -(profile.market_cap_usd or 0),
                profile.ticker,
            )
        )
        for rank, profile in enumerate(profile_rows, start=1):
            market_cap_ranks[(sector, profile.ticker)] = rank

    selected: list[RankedSectorCandidate] = []
    for sector in selected_sectors:
        sector_candidates = by_sector[sector]
        group_limit = limits.get(sector, top_n_per_sector)
        for rank, (candidate, profile) in enumerate(sector_candidates, start=1):
            if rank > group_limit:
                rejected[candidate.ticker] = "SECTOR_CAP"
                continue
            selected.append(
                RankedSectorCandidate(
                    candidate=candidate,
                    profile=profile,
                    candidate_group=sector,
                    sector_strategy_rank=rank,
                    sector_market_cap_rank=market_cap_ranks[
                        (sector, candidate.ticker)
                    ],
                )
            )
    return SectorCandidateSelection(tuple(selected), dict(sorted(rejected.items())))
