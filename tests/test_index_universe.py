from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from quant_signal.index_universe import (
    NASDAQ100_URL,
    SPY_HOLDINGS_URL,
    IndexConstituents,
    IndexUniverseProvider,
    StaleUniverseError,
    UniverseCache,
    UniverseError,
    UniverseValidationError,
    merge_members,
    parse_nasdaq100_payload,
    parse_sp500_workbook,
    to_canonical_symbol,
    to_yfinance_symbol,
    validate_member_counts,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _symbols(prefix: str, count: int) -> set[str]:
    return {f"{prefix}{i:03d}" for i in range(count)}


def _valid_members() -> dict[str, set[str]]:
    sp500 = _symbols("S", 500)
    nasdaq100 = _symbols("N", 100)
    return {"sp500": sp500, "nasdaq100": nasdaq100}


def _snapshot(*, fetched_at: datetime = NOW):  # type: ignore[no-untyped-def]
    return merge_members(
        _valid_members(),
        fetched_at,
        as_of=date(2026, 7, 9),
        sources={"sp500": SPY_HOLDINGS_URL, "nasdaq100": NASDAQ100_URL},
    )


def _workbook(
    symbols: set[str],
    *,
    actual_date_format: bool = False,
    date_value: str | None = "2026-07-09",
    include_asset_class: bool = True,
) -> bytes:
    rows: list[list[object]] = [["Fund Name:", "SPDR S&P 500 ETF Trust", None]]
    if actual_date_format:
        rows.append(["Holdings:", "As of 09-Jul-2026", None])
    elif date_value is not None:
        rows.append(["As of Date:", date_value, None])
    rows.append([None, None, None])
    header = ["Ticker", "Name"]
    if include_asset_class:
        header.append("Asset Class")
    rows.append(header)
    rows.extend(
        [
            [symbol, f"Company {symbol}", "Equity"]
            if include_asset_class
            else [symbol, f"Company {symbol}"]
            for symbol in sorted(symbols)
        ]
    )
    rows.extend(
        [
            ["USD", "US DOLLAR", "Cash"]
            if include_asset_class
            else ["USD", "US DOLLAR"],
            ["ESU6", "S&P 500 FUTURE", "Futures"]
            if include_asset_class
            else ["ESU6", "S&P 500 FUTURE"],
            [None, "Unmapped", "Equity"]
            if include_asset_class
            else [None, "Unmapped"],
        ]
    )
    output = BytesIO()
    pd.DataFrame(rows).to_excel(output, index=False, header=False, engine="openpyxl")
    return output.getvalue()


def test_symbol_aliases_are_canonical_and_provider_specific() -> None:
    assert to_canonical_symbol(" brk-b ") == "BRK.B"
    assert to_canonical_symbol("BF.B") == "BF.B"
    assert to_canonical_symbol("aapl") == "AAPL"
    assert to_yfinance_symbol("BRK.B") == "BRK-B"
    assert to_yfinance_symbol("BF.B") == "BF-B"
    assert to_yfinance_symbol("AAPL") == "AAPL"


def test_merge_preserves_memberships_hash_and_immutability() -> None:
    first = merge_members(
        {"sp500": {"BRK-B", "AAPL"}, "nasdaq100": {"MSFT", "AAPL"}},
        NOW,
    )
    reordered = merge_members(
        {"nasdaq100": {"AAPL", "MSFT"}, "sp500": {"AAPL", "BRK.B"}},
        NOW + timedelta(hours=1),
    )

    assert first.symbols == ("AAPL", "BRK.B", "MSFT")
    assert first.members["AAPL"].memberships == ("nasdaq100", "sp500")
    assert first.content_hash == reordered.content_hash
    with pytest.raises(FrozenInstanceError):
        first.as_of = date(2026, 7, 10)  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.members["NEW"] = first.members["AAPL"]  # type: ignore[index]


def test_official_payload_and_workbook_parsers_filter_non_equities() -> None:
    nasdaq = parse_nasdaq100_payload(
        {
            "data": {
                "asOf": "2026-07-09T16:00:00.000",
                "data": {
                    "rows": [
                        {"symbol": "AAPL"},
                        {"symbol": "BRK-B"},
                        {"symbol": ""},
                    ]
                },
            }
        },
        fallback_as_of=NOW.date(),
    )
    sp500 = parse_sp500_workbook(
        _workbook({"AAPL", "BRK-B", "BF-B"}), fallback_as_of=NOW.date()
    )

    assert nasdaq.members == frozenset({"AAPL", "BRK.B"})
    assert nasdaq.as_of == date(2026, 7, 9)
    assert sp500.members == frozenset({"AAPL", "BRK.B", "BF.B"})
    assert sp500.as_of == date(2026, 7, 9)


def test_sp500_parser_recognizes_state_street_actual_as_of_format() -> None:
    parsed = parse_sp500_workbook(
        _workbook({"AAPL", "MSFT"}, actual_date_format=True),
        fallback_as_of=date(2026, 7, 10),
    )

    assert parsed.as_of == date(2026, 7, 9)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"data": {"rows": [{"symbol": "AAPL"}]}}},
        {
            "data": {
                "date": "not-a-date",
                "data": {"rows": [{"symbol": "AAPL"}]},
            }
        },
    ],
)
def test_nasdaq_parser_rejects_missing_or_unparseable_source_date(
    payload: dict[str, object],
) -> None:
    with pytest.raises(UniverseValidationError, match="date"):
        parse_nasdaq100_payload(payload, fallback_as_of=NOW.date())


@pytest.mark.parametrize("date_value", [None, "not-a-date"])
def test_sp500_parser_rejects_missing_or_unparseable_source_date(
    date_value: str | None,
) -> None:
    with pytest.raises(UniverseValidationError, match="date"):
        parse_sp500_workbook(
            _workbook({"AAPL", "MSFT"}, date_value=date_value),
            fallback_as_of=NOW.date(),
        )


def test_sp500_parser_requires_recognizable_equity_classification() -> None:
    with pytest.raises(UniverseValidationError, match="classification"):
        parse_sp500_workbook(
            _workbook(
                {"AAPL", "MSFT"},
                include_asset_class=False,
            ),
            fallback_as_of=NOW.date(),
        )


def test_sp500_identifier_sector_branch_rejects_non_equity_sectors() -> None:
    rows = [
        ["Fund Name:", "SPDR S&P 500 ETF Trust", None, None],
        ["Holdings:", "As of 09-Jul-2026", None, None],
        [None, None, None, None],
        ["Name", "Ticker", "Identifier", "Sector"],
        ["Apple Inc", "AAPL", "US0378331005", "Information Technology"],
        ["Microsoft Corp", "MSFT", "US5949181045", "Information Technology"],
        ["Settlement Position", "USD", "USD-ID", "Cash"],
        ["Index Contract", "ESU6", "ES-ID", "Derivatives"],
        ["Residual Bucket", "UNK", "UNK-ID", "Unknown"],
    ]
    output = BytesIO()
    pd.DataFrame(rows).to_excel(
        output, index=False, header=False, engine="openpyxl"
    )

    parsed = parse_sp500_workbook(
        output.getvalue(), fallback_as_of=NOW.date()
    )

    assert parsed.members == frozenset({"AAPL", "MSFT"})


@pytest.mark.parametrize(
    "members",
    [
        {"sp500": _symbols("S", 489), "nasdaq100": _symbols("N", 100)},
        {"sp500": _symbols("S", 500), "nasdaq100": _symbols("N", 94)},
    ],
)
def test_member_count_validation_rejects_bad_source_shapes(
    members: dict[str, set[str]],
) -> None:
    with pytest.raises(UniverseValidationError, match="count"):
        validate_member_counts(members)


def test_member_count_validation_rejects_large_single_refresh_change() -> None:
    previous = _valid_members()
    current = _valid_members()
    current["sp500"] = (_symbols("S", 470) | _symbols("X", 30))

    with pytest.raises(UniverseValidationError, match="changed"):
        validate_member_counts(current, previous=previous)


def test_cache_round_trip_and_rejects_tampered_hash(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "nested" / "index.json")
    expected = _snapshot()
    cache.save(expected)
    got = cache.load()

    assert got == expected
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    payload["members"][0]["ticker"] = "ZZZZ"
    cache.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UniverseValidationError, match="hash"):
        cache.load()


def test_cache_hash_protects_time_metadata(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(_snapshot())
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    payload["fetched_at"] = (NOW + timedelta(days=30)).isoformat()
    cache.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UniverseValidationError, match="snapshot hash"):
        cache.load()


def test_failed_refresh_keeps_last_known_good(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    expected = _snapshot(fetched_at=NOW - timedelta(days=8))
    cache.save(expected)

    def fail() -> set[str]:
        raise RuntimeError("source unavailable")

    got = IndexUniverseProvider(
        cache=cache,
        fetchers={"sp500": fail},
        refresh_days=7,
        max_stale_days=14,
    ).load(NOW)

    assert got.content_hash == expected.content_hash
    assert cache.load().content_hash == expected.content_hash


def test_failed_refresh_rejects_stale_last_known_good(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(_snapshot(fetched_at=NOW - timedelta(days=15)))

    def fail() -> set[str]:
        raise RuntimeError("source unavailable")

    provider = IndexUniverseProvider(
        cache=cache,
        fetchers={"sp500": fail},
        refresh_days=7,
        max_stale_days=14,
    )

    with pytest.raises(StaleUniverseError, match="15"):
        provider.load(NOW)


def test_stale_limit_uses_exact_elapsed_time(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(_snapshot(fetched_at=NOW - timedelta(days=14, seconds=1)))

    def fail() -> set[str]:
        raise RuntimeError("source unavailable")

    with pytest.raises(StaleUniverseError, match="14"):
        IndexUniverseProvider(cache=cache, fetchers={"sp500": fail}).load(NOW)


def test_provider_rejects_stale_source_date_even_after_successful_fetch(
    tmp_path: Path,
) -> None:
    old_as_of = NOW.date() - timedelta(days=15)

    def fetch_sp500() -> IndexConstituents:
        return IndexConstituents(frozenset(_symbols("S", 500)), old_as_of, "sp")

    def fetch_nasdaq100() -> IndexConstituents:
        return IndexConstituents(frozenset(_symbols("N", 100)), old_as_of, "ndx")

    provider = IndexUniverseProvider(
        cache=UniverseCache(tmp_path / "index.json"),
        fetchers={"sp500": fetch_sp500, "nasdaq100": fetch_nasdaq100},
    )

    with pytest.raises(StaleUniverseError, match="source"):
        provider.load(NOW)


def test_provider_rejects_future_date_from_any_individual_source(
    tmp_path: Path,
) -> None:
    def fetch_sp500() -> IndexConstituents:
        return IndexConstituents(
            frozenset(_symbols("S", 500)), NOW.date() - timedelta(days=1), "sp"
        )

    def fetch_nasdaq100() -> IndexConstituents:
        return IndexConstituents(
            frozenset(_symbols("N", 100)), NOW.date() + timedelta(days=1), "ndx"
        )

    provider = IndexUniverseProvider(
        cache=UniverseCache(tmp_path / "index.json"),
        fetchers={"sp500": fetch_sp500, "nasdaq100": fetch_nasdaq100},
    )

    with pytest.raises(UniverseError, match="nasdaq100.*future"):
        provider.load(NOW)


def test_provider_does_not_trust_future_fetched_at_cache(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(_snapshot(fetched_at=NOW + timedelta(days=1)))

    def fail() -> set[str]:
        raise RuntimeError("source unavailable")

    with pytest.raises(UniverseError, match="refresh failed"):
        IndexUniverseProvider(cache=cache, fetchers={"sp500": fail}).load(NOW)


def test_fresh_cache_with_different_configured_indices_is_refreshed(
    tmp_path: Path,
) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(merge_members({"sp500": _symbols("S", 500)}, NOW))
    calls: list[str] = []

    def fetch_sp500() -> set[str]:
        calls.append("sp500")
        return _symbols("S", 500)

    def fetch_nasdaq100() -> set[str]:
        calls.append("nasdaq100")
        return _symbols("N", 100)

    provider = IndexUniverseProvider(
        cache=cache,
        indices=["sp500", "nasdaq100"],
        fetchers={"sp500": fetch_sp500, "nasdaq100": fetch_nasdaq100},
    )

    snapshot = provider.load(NOW + timedelta(hours=1))

    assert calls == ["sp500", "nasdaq100"]
    assert len(snapshot.members) == 600


class _Response:
    def __init__(self, *, payload: dict[str, Any] | None = None, content: bytes = b"") -> None:
        self._payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        assert self._payload is not None
        return self._payload


class _Client:
    def __init__(self, workbook: bytes) -> None:
        self.workbook = workbook
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.urls.append(url)
        if url == NASDAQ100_URL:
            return _Response(
                payload={
                    "data": {
                        "asOf": "2026-07-09",
                        "data": {
                            "rows": [
                                {"symbol": symbol}
                                for symbol in sorted(_symbols("N", 100))
                            ]
                        },
                    }
                }
            )
        assert url == SPY_HOLDINGS_URL
        return _Response(content=self.workbook)


def test_provider_uses_injected_http_client_and_writes_valid_snapshot(
    tmp_path: Path,
) -> None:
    client = _Client(_workbook(_symbols("S", 500)))
    cache = UniverseCache(tmp_path / "index.json")
    provider = IndexUniverseProvider(cache=cache, client=client)

    snapshot = provider.load(NOW)

    assert client.urls == [SPY_HOLDINGS_URL, NASDAQ100_URL]
    assert len(snapshot.members) == 600
    assert snapshot.as_of == date(2026, 7, 9)
    assert cache.load().content_hash == snapshot.content_hash
