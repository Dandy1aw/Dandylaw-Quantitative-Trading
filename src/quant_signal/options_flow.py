"""Pure option-flow domain models and ranking helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Literal, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

OptionSide = Literal["call", "put"]
_ET = ZoneInfo("America/New_York")
_STANDARD_ROOT = re.compile(r"^[A-Z]{1,6}$")


@dataclass(frozen=True)
class VenueOptionVolume:
    """Visible cumulative contract volume reported by one options venue."""

    venue: str
    underlying: str
    side: OptionSide
    expiration: date
    strike: Decimal
    volume: int
    captured_at: datetime


@dataclass(frozen=True)
class OptionEnrichment:
    """Nullable contract context; unavailable values must never be guessed."""

    latest_trade_price: Decimal | None = None
    latest_trade_at: datetime | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    quote_at: datetime | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: int | None = None
    open_interest_date: date | None = None
    multiplier: int | None = None

    def estimated_premium(self, volume: int) -> Decimal | None:
        if (
            self.latest_trade_price is None
            or self.multiplier is None
            or self.latest_trade_price < 0
            or self.multiplier <= 0
            or volume < 0
        ):
            return None
        return (
            self.latest_trade_price * Decimal(volume) * Decimal(self.multiplier)
        ).quantize(Decimal("0.01"))

    def volume_oi_ratio(self, volume: int) -> Decimal | None:
        if self.open_interest is None or self.open_interest < 100 or volume < 0:
            return None
        return Decimal(volume) / Decimal(self.open_interest)


@dataclass(frozen=True)
class OptionContractVolume:
    contract_symbol: str
    underlying: str
    side: OptionSide
    expiration: date
    strike: Decimal
    volume: int
    rank: int
    venues: tuple[str, ...]
    captured_at: datetime
    enrichment: OptionEnrichment | None = None


@dataclass(frozen=True)
class OptionFlowSnapshot:
    slot: str
    captured_at: datetime
    provider: str
    venue_coverage: float
    rows: tuple[OptionContractVolume, ...]

    @property
    def session_date(self) -> date:
        return self.captured_at.astimezone(_ET).date()


@dataclass(frozen=True)
class OptionFlowPolicy:
    top_n: int = 10
    rank_jump: int = 3
    surge_volume: int = 10_000
    zero_dte_surge_volume: int = 20_000
    etf_roots: frozenset[str] = field(
        default_factory=lambda: frozenset({"SPY", "QQQ", "IWM"})
    )


@dataclass(frozen=True)
class OptionFlowChange:
    contract: OptionContractVolume
    previous_rank: int | None
    volume_delta: int
    rank_jump: int
    flags: tuple[str, ...]
    score: int


class OptionFlowSource(Protocol):
    """Provider boundary for one trustworthy option-volume snapshot."""

    def fetch(self, now: datetime) -> OptionFlowSnapshot: ...


class OptionFlowEnricher(Protocol):
    """Optional delayed context keyed by OCC contract symbol."""

    def enrich(
        self,
        rows: Sequence[OptionContractVolume],
        now: datetime,
    ) -> dict[str, OptionEnrichment]: ...


def _standard_root(root: str) -> str | None:
    normalized = root.strip().upper()
    return normalized if _STANDARD_ROOT.fullmatch(normalized) else None


def occ_symbol(
    underlying: str,
    expiration: date,
    side: OptionSide,
    strike: Decimal,
) -> str:
    root = _standard_root(underlying)
    if root is None:
        raise ValueError("option root must contain 1-6 A-Z characters")
    if side not in ("call", "put"):
        raise ValueError("option side must be call or put")
    if strike <= 0:
        raise ValueError("option strike must be positive")
    scaled = strike * Decimal("1000")
    if scaled != scaled.to_integral_value():
        raise ValueError("option strike supports at most three decimal places")
    encoded = int(scaled)
    if encoded > 99_999_999:
        raise ValueError("option strike exceeds OCC symbol capacity")
    marker = "C" if side == "call" else "P"
    return f"{root}{expiration:%y%m%d}{marker}{encoded:08d}"


def aggregate_and_rank(
    rows: Sequence[VenueOptionVolume],
    *,
    session: date,
    discovery_limit: int = 50,
    excluded_roots: frozenset[str] = frozenset(),
) -> tuple[OptionContractVolume, ...]:
    if discovery_limit < 1:
        raise ValueError("discovery_limit must be positive")
    excluded = {root.upper() for root in excluded_roots}
    grouped: dict[
        tuple[str, OptionSide, date, Decimal],
        tuple[int, set[str], datetime],
    ] = {}
    for row in rows:
        root = _standard_root(row.underlying)
        if (
            root is None
            or root in excluded
            or row.side not in ("call", "put")
            or row.expiration < session
            or row.strike <= 0
            or row.volume < 0
            or row.captured_at.tzinfo is None
        ):
            continue
        try:
            occ_symbol(root, row.expiration, row.side, row.strike)
        except ValueError:
            continue
        key = (root, row.side, row.expiration, row.strike)
        total, venues, captured_at = grouped.get(
            key, (0, set(), row.captured_at)
        )
        grouped[key] = (
            total + row.volume,
            venues | {row.venue},
            max(captured_at, row.captured_at),
        )

    output: list[OptionContractVolume] = []
    for side in ("call", "put"):
        candidates: list[OptionContractVolume] = []
        for (root, item_side, expiration, strike), (
            volume,
            venues,
            captured_at,
        ) in grouped.items():
            if item_side != side:
                continue
            candidates.append(
                OptionContractVolume(
                    contract_symbol=occ_symbol(root, expiration, side, strike),
                    underlying=root,
                    side=side,
                    expiration=expiration,
                    strike=strike,
                    volume=volume,
                    rank=0,
                    venues=tuple(sorted(venues)),
                    captured_at=captured_at,
                )
            )
        candidates.sort(key=lambda item: (-item.volume, item.contract_symbol))
        output.extend(
            replace(item, rank=rank)
            for rank, item in enumerate(candidates[:discovery_limit], start=1)
        )
    return tuple(output)


def top_by_side(
    rows: Sequence[OptionContractVolume] | OptionFlowSnapshot,
    side: OptionSide,
    top_n: int,
) -> tuple[OptionContractVolume, ...]:
    source = rows.rows if isinstance(rows, OptionFlowSnapshot) else rows
    return tuple(
        sorted(
            (item for item in source if item.side == side),
            key=lambda item: (item.rank, item.contract_symbol),
        )[:top_n]
    )


def scan_slot(at: datetime, minutes: int = 15) -> str:
    if at.tzinfo is None:
        raise ValueError("scan time must be timezone-aware")
    if minutes < 1 or 60 % minutes:
        raise ValueError("slot minutes must divide one hour")
    local = at.astimezone(_ET)
    floored = local.replace(
        minute=local.minute - local.minute % minutes,
        second=0,
        microsecond=0,
    )
    return floored.isoformat()


def detect_material_changes(
    previous: OptionFlowSnapshot,
    current: OptionFlowSnapshot,
    policy: OptionFlowPolicy,
) -> tuple[OptionFlowChange, ...]:
    prior = {item.contract_symbol: item for item in previous.rows}
    changes: list[OptionFlowChange] = []
    session = current.session_date
    for item in current.rows:
        if item.rank > policy.top_n:
            continue
        old = prior.get(item.contract_symbol)
        old_rank = old.rank if old is not None else None
        new_top = old is None or old.rank > policy.top_n
        jump = max((old.rank - item.rank) if old is not None else 0, 0)
        delta = max(item.volume - (old.volume if old is not None else 0), 0)
        threshold = (
            policy.zero_dte_surge_volume
            if item.expiration == session or item.underlying in policy.etf_roots
            else policy.surge_volume
        )
        rank_jump = jump >= policy.rank_jump
        surged = delta >= threshold
        ratio = (
            item.enrichment.volume_oi_ratio(item.volume)
            if item.enrichment is not None
            else None
        )
        turnover = ratio is not None and ratio >= Decimal("2")
        if not (new_top or rank_jump or surged):
            continue
        flags: list[str] = []
        if new_top:
            flags.append("NEW_TOP10")
        if rank_jump:
            flags.append("RANK_JUMP")
        if surged:
            flags.append("VOLUME_SURGE")
        if turnover:
            flags.append("HIGH_TURNOVER")
        score = round(
            30 * int(new_top)
            + min(25, 5 * jump)
            + min(35.0, 35.0 * delta / threshold)
            + 10 * int(turnover)
        )
        if score < 50:
            continue
        changes.append(
            OptionFlowChange(
                contract=item,
                previous_rank=old_rank,
                volume_delta=delta,
                rank_jump=jump,
                flags=tuple(flags),
                score=score,
            )
        )

    changes.sort(
        key=lambda change: (
            -change.score,
            -change.volume_delta,
            change.contract.contract_symbol,
        )
    )
    selected: list[OptionFlowChange] = []
    counts: dict[str, int] = {}
    for change in changes:
        root = change.contract.underlying
        if counts.get(root, 0) >= 2:
            continue
        counts[root] = counts.get(root, 0) + 1
        selected.append(change)
    return tuple(selected)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _enrichment_to_dict(item: OptionEnrichment | None) -> dict[str, object] | None:
    if item is None:
        return None
    return {
        "latest_trade_price": str(item.latest_trade_price) if item.latest_trade_price is not None else None,
        "latest_trade_at": _dt(item.latest_trade_at),
        "bid": str(item.bid) if item.bid is not None else None,
        "ask": str(item.ask) if item.ask is not None else None,
        "quote_at": _dt(item.quote_at),
        "implied_volatility": item.implied_volatility,
        "delta": item.delta,
        "gamma": item.gamma,
        "theta": item.theta,
        "vega": item.vega,
        "open_interest": item.open_interest,
        "open_interest_date": _date(item.open_interest_date),
        "multiplier": item.multiplier,
    }


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _optional_date(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value is not None else None


def _enrichment_from_dict(value: object) -> OptionEnrichment | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("invalid option enrichment")
    return OptionEnrichment(
        latest_trade_price=_optional_decimal(value.get("latest_trade_price")),
        latest_trade_at=_optional_datetime(value.get("latest_trade_at")),
        bid=_optional_decimal(value.get("bid")),
        ask=_optional_decimal(value.get("ask")),
        quote_at=_optional_datetime(value.get("quote_at")),
        implied_volatility=float(value["implied_volatility"]) if value.get("implied_volatility") is not None else None,
        delta=float(value["delta"]) if value.get("delta") is not None else None,
        gamma=float(value["gamma"]) if value.get("gamma") is not None else None,
        theta=float(value["theta"]) if value.get("theta") is not None else None,
        vega=float(value["vega"]) if value.get("vega") is not None else None,
        open_interest=int(value["open_interest"]) if value.get("open_interest") is not None else None,
        open_interest_date=_optional_date(value.get("open_interest_date")),
        multiplier=int(value["multiplier"]) if value.get("multiplier") is not None else None,
    )


def snapshot_to_dict(snapshot: OptionFlowSnapshot) -> dict[str, object]:
    return {
        "slot": snapshot.slot,
        "captured_at": snapshot.captured_at.isoformat(),
        "provider": snapshot.provider,
        "venue_coverage": snapshot.venue_coverage,
        "rows": [
            {
                "contract_symbol": item.contract_symbol,
                "underlying": item.underlying,
                "side": item.side,
                "expiration": item.expiration.isoformat(),
                "strike": str(item.strike),
                "volume": item.volume,
                "rank": item.rank,
                "venues": list(item.venues),
                "captured_at": item.captured_at.isoformat(),
                "enrichment": _enrichment_to_dict(item.enrichment),
            }
            for item in snapshot.rows
        ],
    }


def snapshot_from_dict(payload: Mapping[str, Any]) -> OptionFlowSnapshot:
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError("option snapshot rows must be a list")
    rows: list[OptionContractVolume] = []
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("invalid option snapshot row")
        side = str(raw["side"])
        if side not in ("call", "put"):
            raise ValueError("invalid option side")
        side_value: OptionSide = "call" if side == "call" else "put"
        rows.append(
            OptionContractVolume(
                contract_symbol=str(raw["contract_symbol"]),
                underlying=str(raw["underlying"]),
                side=side_value,
                expiration=date.fromisoformat(str(raw["expiration"])),
                strike=Decimal(str(raw["strike"])),
                volume=int(raw["volume"]),
                rank=int(raw["rank"]),
                venues=tuple(str(item) for item in raw["venues"]),
                captured_at=datetime.fromisoformat(str(raw["captured_at"])),
                enrichment=_enrichment_from_dict(raw.get("enrichment")),
            )
        )
    return OptionFlowSnapshot(
        slot=str(payload["slot"]),
        captured_at=datetime.fromisoformat(str(payload["captured_at"])),
        provider=str(payload["provider"]),
        venue_coverage=float(payload["venue_coverage"]),
        rows=tuple(rows),
    )
