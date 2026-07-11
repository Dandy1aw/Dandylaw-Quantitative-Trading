"""Cboe website-backed discovery for visible active option contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
import time
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

import httpx

from quant_signal.options_flow import (
    OptionFlowSnapshot,
    VenueOptionVolume,
    aggregate_and_rank,
    scan_slot,
    top_by_side,
)

_ENDPOINT = "https://www-api.cboe.com/us/options/market_statistics/most_active/data/"
_ET = ZoneInfo("America/New_York")
_TRANSIENT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)
_DEFAULT_INDEX_ROOTS = frozenset(
    {"SPX", "SPXW", "VIX", "RUT", "RUTW", "NDX", "XSP", "OEX"}
)


class OptionFlowFetchError(RuntimeError):
    """A complete trustworthy venue snapshot could not be assembled."""


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> httpx.Response: ...


class CboeOptionFlowSource:
    """Aggregate the visible top contracts from C1, C2, BZX, and EDGX."""

    def __init__(
        self,
        *,
        client: _HTTPClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (0.5, 1.5),
        venues: tuple[str, ...] = ("cone", "ctwo", "opt", "exo"),
        discovery_limit: int = 50,
        top_n: int = 10,
        excluded_index_roots: frozenset[str] = _DEFAULT_INDEX_ROOTS,
    ) -> None:
        if not venues or len(set(venues)) != len(venues):
            raise ValueError("Cboe venues must be unique and non-empty")
        if discovery_limit < top_n or top_n < 1:
            raise ValueError("discovery_limit must be at least top_n")
        self._client = client or cast(
            _HTTPClient,
            httpx.Client(
                timeout=httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0),
                limits=httpx.Limits(max_keepalive_connections=8, max_connections=8),
            ),
        )
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._venues = venues
        self._discovery_limit = discovery_limit
        self._top_n = top_n
        self._excluded_index_roots = frozenset(
            root.strip().upper() for root in excluded_index_roots
        )

    def _get(self, venue: str) -> httpx.Response:
        params: dict[str, str | int] = {
            "mkt": venue,
            "limit": self._discovery_limit,
        }
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                response = self._client.get(_ENDPOINT, params=params)
            except _TRANSIENT_ERRORS:
                if attempt >= attempts - 1:
                    raise
                self._sleep(self._retry_delays[attempt])
                continue

            retryable_status = response.status_code == 429 or response.status_code >= 500
            if retryable_status and attempt < attempts - 1:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else self._retry_delays[attempt]
                except ValueError:
                    delay = self._retry_delays[attempt]
                self._sleep(max(delay, 0.0))
                continue
            response.raise_for_status()
            return response
        raise AssertionError("unreachable")

    def _parse_side(
        self,
        raw: object,
        *,
        venue: str,
        side: str,
        captured_at: datetime,
    ) -> list[VenueOptionVolume]:
        if not isinstance(raw, list):
            raise ValueError(f"{side} rows are not a list")
        parsed: list[VenueOptionVolume] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError(f"invalid {side} row")
            volume = int(item["volume"])
            strike = Decimal(str(item["strike"]))
            expiration = date.fromisoformat(str(item["expires"]))
            if volume < 0 or strike <= 0:
                raise ValueError(f"invalid {side} volume or strike")
            parsed.append(
                VenueOptionVolume(
                    venue=venue,
                    underlying=str(item["symbol"]),
                    side="call" if side == "calls" else "put",
                    expiration=expiration,
                    strike=strike,
                    volume=volume,
                    captured_at=captured_at,
                )
            )
        if len(parsed) < self._top_n:
            raise ValueError(f"{side} has fewer than {self._top_n} rows")
        return parsed

    def _parse_venue(
        self, venue: str, payload: object, captured_at: datetime
    ) -> list[VenueOptionVolume]:
        if not isinstance(payload, Mapping):
            raise ValueError("response is not an object")
        categories = payload.get("categories")
        if not isinstance(categories, list):
            raise ValueError("categories are missing")
        desired = "equity" if venue == "cone" else "all"
        category = next(
            (
                item
                for item in categories
                if isinstance(item, Mapping) and item.get("category") == desired
            ),
            None,
        )
        if not isinstance(category, Mapping):
            raise ValueError(f"category {desired} is missing")
        return self._parse_side(
            category.get("calls"),
            venue=venue,
            side="calls",
            captured_at=captured_at,
        ) + self._parse_side(
            category.get("puts"),
            venue=venue,
            side="puts",
            captured_at=captured_at,
        )

    def fetch(self, now: datetime) -> OptionFlowSnapshot:
        if now.tzinfo is None:
            raise ValueError("option flow scan time must be timezone-aware")
        venue_rows: list[VenueOptionVolume] = []
        for venue in self._venues:
            try:
                response = self._get(venue)
                payload: Any = response.json()
                venue_rows.extend(self._parse_venue(venue, payload, now))
            except Exception as error:
                raise OptionFlowFetchError(f"Cboe venue {venue} failed: {error}") from error

        coverage = len(self._venues) / len(self._venues)
        ranked = aggregate_and_rank(
            venue_rows,
            session=now.astimezone(_ET).date(),
            discovery_limit=self._discovery_limit,
            excluded_roots=self._excluded_index_roots,
        )
        if (
            len(top_by_side(ranked, "call", self._top_n)) < self._top_n
            or len(top_by_side(ranked, "put", self._top_n)) < self._top_n
        ):
            raise OptionFlowFetchError("Cboe aggregate has fewer than ten valid calls or puts")
        return OptionFlowSnapshot(
            slot=scan_slot(now),
            captured_at=now,
            provider="cboe-four-venues",
            venue_coverage=coverage,
            rows=ranked,
        )
