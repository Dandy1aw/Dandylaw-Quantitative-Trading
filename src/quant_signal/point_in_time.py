"""Certified point-in-time index membership history.

A plain mapping of dates to tickers is not sufficient evidence because it does
not say when the data became knowable or which source version produced it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
from typing import cast

from quant_signal.index_universe import to_canonical_symbol


SCHEMA_VERSION = "pit-membership-v1"


@dataclass(frozen=True)
class MembershipSnapshot:
    effective_at: datetime
    known_at: datetime
    members: frozenset[str]


class CertifiedMembershipMap(Mapping[date, set[str]]):
    """Read-only session mapping backed by certified timestamped snapshots."""

    def __init__(
        self,
        *,
        source: str,
        source_version: str,
        snapshots: tuple[MembershipSnapshot, ...],
    ) -> None:
        if not source.strip() or not source_version.strip():
            raise ValueError("point-in-time source and source_version are required")
        if not snapshots:
            raise ValueError("point-in-time membership history is empty")
        ordered = tuple(sorted(snapshots, key=lambda item: item.effective_at))
        if len({item.effective_at for item in ordered}) != len(ordered):
            raise ValueError("duplicate effective_at in membership history")
        self.source = source.strip()
        self.source_version = source_version.strip()
        self.snapshots = ordered

    def __getitem__(self, session: date) -> set[str]:
        cutoff = datetime.combine(session, time.max, tzinfo=timezone.utc)
        eligible = [
            item
            for item in self.snapshots
            if item.effective_at <= cutoff and item.known_at <= cutoff
        ]
        if not eligible:
            raise KeyError(session)
        return set(eligible[-1].members)

    def __iter__(self) -> Iterator[date]:
        return iter(tuple(item.effective_at.date() for item in self.snapshots))

    def __len__(self) -> int:
        return len(self.snapshots)


def _aware_timestamp(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_membership_history(path: Path) -> CertifiedMembershipMap:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"membership JSON must use schema_version={SCHEMA_VERSION}")
    raw_snapshots = raw.get("snapshots")
    if not isinstance(raw_snapshots, list):
        raise ValueError("membership snapshots must be a list")
    snapshots: list[MembershipSnapshot] = []
    for position, item in enumerate(raw_snapshots):
        if not isinstance(item, dict):
            raise ValueError(f"membership snapshot {position} must be an object")
        row = cast(dict[str, object], item)
        effective_at = _aware_timestamp(row.get("effective_at"), "effective_at")
        known_at = _aware_timestamp(row.get("known_at"), "known_at")
        if known_at > effective_at:
            raise ValueError("known_at must not be later than effective_at")
        raw_members = row.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise ValueError("each membership snapshot requires a non-empty members list")
        members = frozenset(to_canonical_symbol(str(member)) for member in raw_members)
        if len(members) != len(raw_members):
            raise ValueError("membership snapshot contains duplicate symbols")
        snapshots.append(MembershipSnapshot(effective_at, known_at, members))
    return CertifiedMembershipMap(
        source=str(raw.get("source") or ""),
        source_version=str(raw.get("source_version") or ""),
        snapshots=tuple(snapshots),
    )
