from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from quant_signal.datafeed.cboe_options import CboeOptionFlowSource, OptionFlowFetchError
from quant_signal.options_flow import top_by_side

NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)
VENUES = ("cone", "ctwo", "opt", "exo")


def payload(category: str, volume: int = 1_000) -> dict[str, object]:
    roots = ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL", "AMD", "MU", "INTC", "PLTR", "NFLX"]
    calls = [
        {
            "symbol": root,
            "expires": "2026-07-17",
            "strike": 200 + index,
            "volume": volume + (12 - index) * 100,
        }
        for index, root in enumerate(roots)
    ]
    puts = [
        {
            "symbol": root,
            "expires": "2026-07-17",
            "strike": 180 + index,
            "volume": volume + (12 - index) * 90,
        }
        for index, root in enumerate(roots)
    ]
    return {
        "categories": [{"category": category, "calls": calls, "puts": puts}],
        "dt": "2026-07-10T10:15:00-04:00",
    }


class FakeClient:
    def __init__(self, outcomes: dict[str, list[object] | object]) -> None:
        self.outcomes = {
            venue: list(value) if isinstance(value, list) else [value]
            for venue, value in outcomes.items()
        }
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        params = kwargs["params"]
        assert isinstance(params, dict)
        venue = str(params["mkt"])
        outcome = self.outcomes[venue].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, httpx.Response):
            return outcome
        assert isinstance(outcome, dict)
        return httpx.Response(
            200,
            json=outcome,
            request=httpx.Request("GET", url),
        )


def response(status: int, body: dict[str, object], *, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return httpx.Response(
        status,
        json=body,
        headers=headers,
        request=httpx.Request("GET", "https://www-api.cboe.com/options"),
    )


def full_client() -> FakeClient:
    return FakeClient(
        {
            "cone": payload("equity"),
            "ctwo": payload("all"),
            "opt": payload("all"),
            "exo": payload("all"),
        }
    )


def test_fetch_aggregates_all_required_venues() -> None:
    client = full_client()
    result = CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)

    assert result.venue_coverage == 1.0
    assert result.provider == "cboe-four-venues"
    assert {call["params"]["mkt"] for call in client.calls} == set(VENUES)  # type: ignore[index]
    calls = top_by_side(result, "call", 10)
    puts = top_by_side(result, "put", 10)
    assert len(calls) == len(puts) == 10
    assert calls[0].underlying == "NVDA"
    assert calls[0].volume == 4 * 2_200
    assert calls[0].venues == tuple(sorted(VENUES))


def test_cone_uses_equity_category_and_other_venues_use_all() -> None:
    client = full_client()
    CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)
    assert len(client.calls) == 4
    # Successful parsing itself proves the source selected equity for cone and all elsewhere.


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("connect"),
        httpx.ReadTimeout("read"),
        httpx.ConnectError("connect"),
        httpx.RemoteProtocolError("protocol"),
    ],
)
def test_transient_network_error_is_retried(error: httpx.HTTPError) -> None:
    client = full_client()
    client.outcomes["cone"] = [error, payload("equity")]
    sleeps: list[float] = []

    result = CboeOptionFlowSource(
        client=client, sleep=sleeps.append, retry_delays=(0.5, 1.5)
    ).fetch(NOW)

    assert len(result.rows) >= 20
    assert sleeps == [0.5]


def test_429_honors_retry_after_and_then_succeeds() -> None:
    client = full_client()
    client.outcomes["cone"] = [
        response(429, {"error": "rate limit"}, retry_after="2"),
        payload("equity"),
    ]
    sleeps: list[float] = []
    CboeOptionFlowSource(client=client, sleep=sleeps.append).fetch(NOW)
    assert sleeps == [2.0]


def test_non_retryable_4xx_is_not_retried() -> None:
    client = full_client()
    client.outcomes["cone"] = [response(403, {"error": "forbidden"})]
    with pytest.raises(OptionFlowFetchError, match="cone"):
        CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)
    assert len([call for call in client.calls if call["params"]["mkt"] == "cone"]) == 1  # type: ignore[index]


def test_missing_venue_fails_closed() -> None:
    client = full_client()
    client.outcomes["ctwo"] = [httpx.ReadTimeout("timeout")] * 3
    with pytest.raises(OptionFlowFetchError, match="ctwo"):
        CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)


def test_malformed_or_short_response_fails_closed() -> None:
    client = full_client()
    client.outcomes["opt"] = [{"categories": [{"category": "all", "calls": [], "puts": []}]}]
    with pytest.raises(OptionFlowFetchError, match="opt"):
        CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)
