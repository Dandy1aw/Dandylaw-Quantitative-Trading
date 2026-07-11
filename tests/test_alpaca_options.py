from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from quant_signal.datafeed.alpaca_options import AlpacaOptionEnricher
from quant_signal.options_flow import OptionContractVolume

NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)


def contract(index: int = 0) -> OptionContractVolume:
    strike = 300 + index
    return OptionContractVolume(
        contract_symbol=f"AAPL260717C{strike * 1000:08d}",
        underlying="AAPL",
        side="call",
        expiration=date(2026, 7, 17),
        strike=Decimal(strike),
        volume=2_000,
        rank=index + 1,
        venues=("cone",),
        captured_at=NOW,
    )


def snapshot(symbol: str) -> dict[str, object]:
    return {
        "latestTrade": {"p": 2.5, "t": "2026-07-10T14:00:00Z"},
        "latestQuote": {"bp": 2.45, "ap": 2.55, "t": "2026-07-10T14:00:01Z"},
        "impliedVolatility": 0.42,
        "greeks": {"delta": 0.35, "gamma": 0.02, "theta": -0.1, "vega": 0.15},
    }


class FakeClient:
    def __init__(
        self,
        *,
        snapshot_status: int = 200,
        snapshot_error: Exception | None = None,
        missing_snapshot: bool = False,
        missing_metadata: bool = False,
    ) -> None:
        self.snapshot_status = snapshot_status
        self.snapshot_error = snapshot_error
        self.missing_snapshot = missing_snapshot
        self.missing_metadata = missing_metadata
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if "/options/snapshots" in url:
            if self.snapshot_error is not None:
                error, self.snapshot_error = self.snapshot_error, None
                raise error
            params = kwargs["params"]
            assert isinstance(params, dict)
            symbols = str(params["symbols"]).split(",")
            body = {
                "snapshots": (
                    {}
                    if self.missing_snapshot
                    else {symbol: snapshot(symbol) for symbol in symbols}
                )
            }
            return httpx.Response(
                self.snapshot_status,
                json=body if self.snapshot_status == 200 else {"message": "OPRA agreement is not signed"},
                request=httpx.Request("GET", url),
            )
        symbol = url.rsplit("/", 1)[-1]
        status = 404 if self.missing_metadata else 200
        return httpx.Response(
            status,
            json=(
                {"message": "not found"}
                if status == 404
                else {
                    "symbol": symbol,
                    "open_interest": "1000",
                    "open_interest_date": "2026-07-09",
                    "size": "100",
                }
            ),
            request=httpx.Request("GET", url),
        )


def snapshot_calls(client: FakeClient) -> list[dict[str, object]]:
    return [call for call in client.calls if "/options/snapshots" in str(call["url"])]


def metadata_calls(client: FakeClient) -> list[dict[str, object]]:
    return [call for call in client.calls if "/options/contracts/" in str(call["url"])]


def test_enrich_joins_snapshot_contract_metadata_and_calculates_context() -> None:
    client = FakeClient()
    row = contract()
    result = AlpacaOptionEnricher("k", "s", client=client).enrich((row,), NOW)

    item = result[row.contract_symbol]
    assert item.latest_trade_price == Decimal("2.5")
    assert item.bid == Decimal("2.45") and item.ask == Decimal("2.55")
    assert item.implied_volatility == 0.42 and item.delta == 0.35
    assert item.open_interest == 1_000
    assert item.open_interest_date == date(2026, 7, 9)
    assert item.multiplier == 100
    assert item.estimated_premium(row.volume) == Decimal("500000.00")
    assert item.volume_oi_ratio(row.volume) == Decimal("2")


def test_snapshots_are_batched_at_one_hundred_symbols() -> None:
    client = FakeClient()
    rows = tuple(contract(index) for index in range(101))
    result = AlpacaOptionEnricher("k", "s", client=client).enrich(rows, NOW)
    assert len(result) == 101
    assert len(snapshot_calls(client)) == 2
    assert all(len(str(call["params"]["symbols"]).split(",")) <= 100 for call in snapshot_calls(client))  # type: ignore[index]


def test_missing_snapshot_and_metadata_remain_nullable() -> None:
    client = FakeClient(missing_snapshot=True, missing_metadata=True)
    row = contract()
    item = AlpacaOptionEnricher("k", "s", client=client).enrich((row,), NOW)[row.contract_symbol]
    assert item.latest_trade_price is None
    assert item.open_interest is None and item.multiplier is None
    assert item.estimated_premium(row.volume) is None


def test_opra_403_is_not_silently_downgraded() -> None:
    client = FakeClient(snapshot_status=403)
    with pytest.raises(httpx.HTTPStatusError):
        AlpacaOptionEnricher("k", "s", feed="opra", client=client).enrich((contract(),), NOW)
    assert snapshot_calls(client)[0]["params"]["feed"] == "opra"  # type: ignore[index]
    assert len(snapshot_calls(client)) == 1


def test_contract_metadata_is_cached_for_the_session_day() -> None:
    client = FakeClient()
    enricher = AlpacaOptionEnricher("k", "s", client=client)
    row = contract()
    enricher.enrich((row,), NOW)
    enricher.enrich((row,), NOW.replace(hour=15))
    assert len(snapshot_calls(client)) == 2
    assert len(metadata_calls(client)) == 1


def test_transient_snapshot_failure_is_retried() -> None:
    client = FakeClient(snapshot_error=httpx.ReadTimeout("timeout"))
    sleeps: list[float] = []
    result = AlpacaOptionEnricher(
        "k", "s", client=client, sleep=sleeps.append, retry_delays=(0.5, 1.5)
    ).enrich((contract(),), NOW)
    assert result
    assert sleeps == [0.5]


def test_missing_credentials_are_rejected() -> None:
    with pytest.raises(ValueError, match="ALPACA"):
        AlpacaOptionEnricher("", "")
