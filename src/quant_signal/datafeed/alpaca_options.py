"""Optional Alpaca context for contracts discovered by the Cboe flow source."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import time
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import httpx

from quant_signal.options_flow import OptionContractVolume, OptionEnrichment

_SNAPSHOTS = "https://data.alpaca.markets/v1beta1/options/snapshots"
_CONTRACT = "https://paper-api.alpaca.markets/v2/options/contracts"
_ET = ZoneInfo("America/New_York")
_TRANSIENT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> httpx.Response: ...


@dataclass(frozen=True)
class _ContractMetadata:
    open_interest: int | None = None
    open_interest_date: date | None = None
    multiplier: int | None = None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


class AlpacaOptionEnricher:
    def __init__(
        self,
        key: str,
        secret: str,
        *,
        feed: Literal["indicative", "opra"] = "indicative",
        client: _HTTPClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (0.5, 1.5),
    ) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET are required for option enrichment")
        self._headers = {
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        }
        self._feed = feed
        self._client = client or cast(
            _HTTPClient,
            httpx.Client(
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            ),
        )
        self._sleep = sleep
        self._retry_delays = retry_delays
        self._metadata_cache: dict[str, tuple[date, _ContractMetadata]] = {}

    def _get(
        self,
        url: str,
        params: dict[str, str | int],
        *,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers=self._headers,
                )
            except _TRANSIENT_ERRORS:
                if attempt >= attempts - 1:
                    raise
                self._sleep(self._retry_delays[attempt])
                continue
            if allow_not_found and response.status_code == 404:
                return None
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < attempts - 1:
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

    def _metadata(self, symbol: str, session: date) -> _ContractMetadata:
        cached = self._metadata_cache.get(symbol)
        if cached is not None and cached[0] == session:
            return cached[1]
        response = self._get(
            f"{_CONTRACT}/{symbol}",
            {},
            allow_not_found=True,
        )
        if response is None:
            result = _ContractMetadata()
        else:
            payload = response.json()
            if not isinstance(payload, Mapping):
                result = _ContractMetadata()
            else:
                try:
                    open_interest = (
                        int(payload["open_interest"])
                        if payload.get("open_interest") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    open_interest = None
                try:
                    multiplier = (
                        int(payload["size"])
                        if payload.get("size") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    multiplier = None
                result = _ContractMetadata(
                    open_interest=open_interest,
                    open_interest_date=_date(payload.get("open_interest_date")),
                    multiplier=multiplier,
                )
        self._metadata_cache[symbol] = (session, result)
        return result

    def _snapshots(
        self, symbols: Sequence[str]
    ) -> dict[str, Mapping[str, object]]:
        output: dict[str, Mapping[str, object]] = {}
        for start in range(0, len(symbols), 100):
            batch = symbols[start : start + 100]
            response = self._get(
                _SNAPSHOTS,
                {
                    "symbols": ",".join(batch),
                    "feed": self._feed,
                    "limit": 1000,
                },
            )
            assert response is not None
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("Alpaca option snapshots response is not an object")
            raw_snapshots = payload.get("snapshots")
            if not isinstance(raw_snapshots, Mapping):
                raise ValueError("Alpaca option snapshots are missing")
            for symbol, item in raw_snapshots.items():
                if isinstance(item, Mapping):
                    output[str(symbol)] = item
        return output

    def enrich(
        self,
        rows: Sequence[OptionContractVolume],
        now: datetime,
    ) -> dict[str, OptionEnrichment]:
        if now.tzinfo is None:
            raise ValueError("option enrichment time must be timezone-aware")
        symbols = tuple(dict.fromkeys(row.contract_symbol for row in rows))
        if not symbols:
            return {}
        snapshots = self._snapshots(symbols)
        session = now.astimezone(_ET).date()
        output: dict[str, OptionEnrichment] = {}
        for symbol in symbols:
            item = snapshots.get(symbol, {})
            trade = item.get("latestTrade")
            quote = item.get("latestQuote")
            greeks = item.get("greeks")
            trade_map = trade if isinstance(trade, Mapping) else {}
            quote_map = quote if isinstance(quote, Mapping) else {}
            greek_map = greeks if isinstance(greeks, Mapping) else {}
            metadata = self._metadata(symbol, session)
            output[symbol] = OptionEnrichment(
                latest_trade_price=_decimal(trade_map.get("p")),
                latest_trade_at=_datetime(trade_map.get("t")),
                bid=_decimal(quote_map.get("bp")),
                ask=_decimal(quote_map.get("ap")),
                quote_at=_datetime(quote_map.get("t")),
                implied_volatility=_float(item.get("impliedVolatility")),
                delta=_float(greek_map.get("delta")),
                gamma=_float(greek_map.get("gamma")),
                theta=_float(greek_map.get("theta")),
                vega=_float(greek_map.get("vega")),
                open_interest=metadata.open_interest,
                open_interest_date=metadata.open_interest_date,
                multiplier=metadata.multiplier,
            )
        return output
