from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Any, Literal, Protocol, cast

import httpx
import pandas as pd

IndexName = Literal["sp500", "nasdaq100"]

NASDAQ100_URL = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
SPY_HOLDINGS_URL = (
    "https://www.ssga.com/library-content/products/fund-data/etfs/us/"
    "holdings-daily-us-en-spy.xlsx"
)

_COUNT_RANGES: dict[str, tuple[int, int]] = {
    "sp500": (490, 510),
    "nasdaq100": (95, 110),
}
_ALIASES_TO_CANONICAL = {"BRK-B": "BRK.B", "BF-B": "BF.B"}
_ALIASES_TO_YFINANCE = {"BRK.B": "BRK-B", "BF.B": "BF-B"}
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}(?:\.[A-Z])?$")
_HTTP_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "Mozilla/5.0 quant-signal/index-universe",
}


class UniverseError(RuntimeError):
    pass


class UniverseValidationError(UniverseError, ValueError):
    pass


class StaleUniverseError(UniverseError):
    pass


class _Response(Protocol):
    content: bytes

    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> _Response: ...


@dataclass(frozen=True)
class IndexConstituents:
    members: frozenset[str]
    as_of: date
    source: str


@dataclass(frozen=True)
class IndexMember:
    ticker: str
    memberships: tuple[str, ...]

    @property
    def yfinance_symbol(self) -> str:
        return to_yfinance_symbol(self.ticker)


@dataclass(frozen=True)
class IndexUniverseSnapshot:
    as_of: date
    fetched_at: datetime
    source: Mapping[str, str]
    members: Mapping[str, IndexMember]
    content_hash: str

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.members)

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat(),
            "source": dict(self.source),
            "members": [
                {"ticker": member.ticker, "memberships": list(member.memberships)}
                for member in self.members.values()
            ],
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> IndexUniverseSnapshot:
        try:
            raw_members = cast(list[object], payload["members"])
            members_by_index: dict[str, set[str]] = {}
            for item in raw_members:
                row = cast(Mapping[str, object], item)
                ticker = to_canonical_symbol(str(row["ticker"]))
                memberships = cast(list[object], row["memberships"])
                for index in memberships:
                    members_by_index.setdefault(str(index), set()).add(ticker)
            snapshot = merge_members(
                members_by_index,
                datetime.fromisoformat(str(payload["fetched_at"])),
                as_of=date.fromisoformat(str(payload["as_of"])),
                sources=cast(Mapping[str, str], payload.get("source", {})),
            )
            expected_hash = str(payload["content_hash"])
        except (KeyError, TypeError, ValueError) as error:
            raise UniverseValidationError(f"invalid universe cache: {error}") from error
        if snapshot.content_hash != expected_hash:
            raise UniverseValidationError("universe cache content hash mismatch")
        return snapshot


def to_canonical_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    normalized = _ALIASES_TO_CANONICAL.get(normalized, normalized)
    if not _SYMBOL_RE.fullmatch(normalized):
        raise UniverseValidationError(f"invalid index symbol: {symbol!r}")
    return normalized


def to_yfinance_symbol(symbol: str) -> str:
    canonical = to_canonical_symbol(symbol)
    return _ALIASES_TO_YFINANCE.get(canonical, canonical)


def _content_hash(members: Mapping[str, IndexMember]) -> str:
    content = [
        [member.ticker, list(member.memberships)] for member in members.values()
    ]
    encoded = json.dumps(content, ensure_ascii=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def merge_members(
    members_by_index: Mapping[str, Iterable[str]],
    fetched_at: datetime,
    *,
    as_of: date | None = None,
    sources: Mapping[str, str] | None = None,
) -> IndexUniverseSnapshot:
    memberships: dict[str, set[str]] = {}
    for index, symbols in members_by_index.items():
        if index not in _COUNT_RANGES:
            raise UniverseValidationError(f"unsupported index: {index}")
        for raw_symbol in symbols:
            ticker = to_canonical_symbol(raw_symbol)
            memberships.setdefault(ticker, set()).add(index)
    members = {
        ticker: IndexMember(ticker, tuple(sorted(indexes)))
        for ticker, indexes in sorted(memberships.items())
    }
    immutable_members = MappingProxyType(members)
    normalized_time = _utc(fetched_at)
    return IndexUniverseSnapshot(
        as_of=as_of or normalized_time.date(),
        fetched_at=normalized_time,
        source=MappingProxyType(dict(sorted((sources or {}).items()))),
        members=immutable_members,
        content_hash=_content_hash(immutable_members),
    )


def _canonical_sets(
    members_by_index: Mapping[str, Iterable[str]],
) -> dict[str, set[str]]:
    return {
        index: {to_canonical_symbol(symbol) for symbol in symbols}
        for index, symbols in members_by_index.items()
    }


def validate_member_counts(
    members_by_index: Mapping[str, Iterable[str]],
    *,
    previous: Mapping[str, Iterable[str]] | None = None,
    max_changes: int = 25,
) -> None:
    normalized = _canonical_sets(members_by_index)
    if not normalized:
        raise UniverseValidationError("index universe has no members")
    for index, members in normalized.items():
        if index not in _COUNT_RANGES:
            raise UniverseValidationError(f"unsupported index: {index}")
        low, high = _COUNT_RANGES[index]
        if not low <= len(members) <= high:
            raise UniverseValidationError(
                f"{index} count {len(members)} outside [{low}, {high}]"
            )
    if {"sp500", "nasdaq100"}.issubset(normalized):
        union_count = len(normalized["sp500"] | normalized["nasdaq100"])
        if not 450 <= union_count <= 650:
            raise UniverseValidationError(
                f"combined index count {union_count} outside [450, 650]"
            )
    if previous is not None:
        old = _canonical_sets(previous)
        changed = sum(
            len(normalized[index] ^ old.get(index, set())) for index in normalized
        )
        if changed > max_changes:
            raise UniverseValidationError(
                f"index memberships changed by {changed}, limit is {max_changes}"
            )


def _parse_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def parse_nasdaq100_payload(
    payload: Mapping[str, object], *, fallback_as_of: date
) -> IndexConstituents:
    try:
        outer = cast(Mapping[str, object], payload["data"])
        inner = cast(Mapping[str, object], outer["data"])
        rows = cast(list[object], inner["rows"])
    except (KeyError, TypeError) as error:
        raise UniverseValidationError("Nasdaq payload missing data.data.rows") from error
    members: set[str] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        raw_symbol = item.get("symbol")
        if not raw_symbol:
            continue
        try:
            members.add(to_canonical_symbol(str(raw_symbol)))
        except UniverseValidationError:
            continue
    if not members:
        raise UniverseValidationError("Nasdaq payload contained no valid symbols")
    as_of = _parse_date(outer.get("asOf") or outer.get("date")) or fallback_as_of
    return IndexConstituents(frozenset(members), as_of, NASDAQ100_URL)


def parse_sp500_workbook(
    content: bytes, *, fallback_as_of: date
) -> IndexConstituents:
    try:
        table = pd.read_excel(BytesIO(content), header=None, engine="openpyxl")
    except Exception as error:  # noqa: BLE001
        raise UniverseValidationError(f"invalid SPY holdings workbook: {error}") from error
    header_row: int | None = None
    ticker_column: int | None = None
    asset_column: int | None = None
    name_column: int | None = None
    for row_number, (_, row) in enumerate(table.iterrows()):
        names = {
            str(value).strip().lower(): column
            for column, value in enumerate(row.tolist())
        }
        if "ticker" in names:
            header_row = row_number
            ticker_column = names["ticker"]
            asset_column = names.get("asset class")
            name_column = names.get("name")
            break
    if header_row is None or ticker_column is None:
        raise UniverseValidationError("SPY holdings workbook missing Ticker header")
    as_of = fallback_as_of
    for _, row in table.iloc[:header_row].iterrows():
        values = [value for value in row.tolist() if not pd.isna(value)]
        if values and str(values[0]).strip().lower() in {"as of date:", "as of date"}:
            as_of = _parse_date(values[1] if len(values) > 1 else None) or fallback_as_of
            break
    members: set[str] = set()
    for _, row in table.iloc[header_row + 1 :].iterrows():
        if asset_column is not None:
            asset = str(row.iloc[asset_column]).strip().lower()
            if asset not in {"equity", "common stock"}:
                continue
        if name_column is not None:
            name = str(row.iloc[name_column]).lower()
            if "cash" in name or "future" in name:
                continue
        raw_symbol = row.iloc[ticker_column]
        if pd.isna(raw_symbol):
            continue
        try:
            members.add(to_canonical_symbol(str(raw_symbol)))
        except UniverseValidationError:
            continue
    if not members:
        raise UniverseValidationError("SPY holdings workbook contained no valid equities")
    return IndexConstituents(frozenset(members), as_of, SPY_HOLDINGS_URL)


class UniverseCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> IndexUniverseSnapshot:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise UniverseValidationError(f"unable to read universe cache: {error}") from error
        if not isinstance(payload, Mapping):
            raise UniverseValidationError("universe cache root must be an object")
        return IndexUniverseSnapshot.from_dict(cast(Mapping[str, object], payload))

    def save(self, snapshot: IndexUniverseSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(snapshot.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


Fetcher = Callable[[], IndexConstituents | Iterable[str]]


class IndexUniverseProvider:
    def __init__(
        self,
        *,
        cache: UniverseCache,
        indices: Iterable[IndexName] | None = None,
        refresh_days: int = 7,
        max_stale_days: int = 14,
        client: _HTTPClient | None = None,
        fetchers: Mapping[IndexName, Fetcher] | None = None,
    ) -> None:
        self.cache = cache
        selected = indices or (
            tuple(fetchers) if fetchers is not None else ("sp500", "nasdaq100")
        )
        self.indices: tuple[IndexName, ...] = tuple(selected)
        self.refresh_days = refresh_days
        self.max_stale_days = max_stale_days
        self._client = client or cast(_HTTPClient, httpx.Client())
        self._fetchers = dict(fetchers or {})

    @staticmethod
    def _members_by_index(snapshot: IndexUniverseSnapshot) -> dict[str, set[str]]:
        output: dict[str, set[str]] = {}
        for member in snapshot.members.values():
            for index in member.memberships:
                output.setdefault(index, set()).add(member.ticker)
        return output

    def _fetch(self, index: IndexName, now: datetime) -> IndexConstituents:
        custom = self._fetchers.get(index)
        if custom is not None:
            result = custom()
            if isinstance(result, IndexConstituents):
                return result
            return IndexConstituents(
                frozenset(to_canonical_symbol(symbol) for symbol in result),
                now.date(),
                f"injected:{index}",
            )
        if index == "sp500":
            response = self._client.get(
                SPY_HOLDINGS_URL, headers=_HTTP_HEADERS, timeout=30.0
            )
            response.raise_for_status()
            return parse_sp500_workbook(response.content, fallback_as_of=now.date())
        response = self._client.get(NASDAQ100_URL, headers=_HTTP_HEADERS, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise UniverseValidationError("Nasdaq response root must be an object")
        return parse_nasdaq100_payload(
            cast(Mapping[str, object], payload), fallback_as_of=now.date()
        )

    def _refresh(
        self, now: datetime, previous: IndexUniverseSnapshot | None
    ) -> IndexUniverseSnapshot:
        fetched = {index: self._fetch(index, now) for index in self.indices}
        members: dict[str, Iterable[str]] = {
            index: result.members for index, result in fetched.items()
        }
        prior_members = self._members_by_index(previous) if previous is not None else None
        validate_member_counts(members, previous=prior_members)
        snapshot = merge_members(
            members,
            now,
            as_of=min(result.as_of for result in fetched.values()),
            sources={index: result.source for index, result in fetched.items()},
        )
        self.cache.save(snapshot)
        return snapshot

    def load(self, now: datetime) -> IndexUniverseSnapshot:
        now = _utc(now)
        cached: IndexUniverseSnapshot | None = None
        if self.cache.path.exists():
            try:
                cached = self.cache.load()
            except UniverseValidationError:
                cached = None
        if cached is not None:
            age = now - cached.fetched_at
            if age.days < self.refresh_days:
                return cached
        try:
            return self._refresh(now, cached)
        except Exception as error:  # noqa: BLE001
            if cached is None:
                raise UniverseError(f"index universe refresh failed: {error}") from error
            age_days = (now - cached.fetched_at).days
            if age_days > self.max_stale_days:
                raise StaleUniverseError(
                    f"last-known-good index universe is {age_days} days old"
                ) from error
            return cached
