from datetime import date
from typing import Any

import httpx
import pytest

from quant_signal.datafeed.alpaca_source import AlpacaSource

FAKE_PAGE = {
    "bars": {
        "SPY": [
            {
                "t": "2026-07-01T04:00:00Z",
                "o": 500.0,
                "h": 505.0,
                "l": 499.0,
                "c": 503.0,
                "v": 50_000_000,
            },
            {
                "t": "2026-07-02T04:00:00Z",
                "o": 503.0,
                "h": 506.0,
                "l": 501.0,
                "c": 505.0,
                "v": 48_000_000,
            },
        ]
    },
    "next_page_token": None,
}


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, dict)
        status = int(outcome.get("status", 200))
        payload: Any = outcome.get("json", outcome)
        return httpx.Response(
            status,
            json=payload,
            request=httpx.Request("GET", url),
        )


def test_fetch_daily_bars_parses_and_normalizes() -> None:
    client = FakeClient([FAKE_PAGE])
    out = AlpacaSource("k", "s", client=client).fetch_daily_bars(
        ["SPY"], date(2026, 7, 1), date(2026, 7, 3)
    )
    assert len(out) == 2
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.names == ["ticker", "ts"]
    assert client.calls[0]["params"]["adjustment"] == "all"  # type: ignore[index]
    assert client.calls[0]["params"]["feed"] == "iex"  # type: ignore[index]
    assert client.calls[0]["headers"]["APCA-API-KEY-ID"] == "k"  # type: ignore[index]


def test_fetch_sip_daily_bars_requests_full_market_feed() -> None:
    client = FakeClient([FAKE_PAGE])
    out = AlpacaSource("k", "s", client=client).fetch_sip_daily_bars(
        ["SPY"], date(2026, 7, 1), date(2026, 7, 3)
    )

    assert len(out) == 2
    params = client.calls[0]["params"]
    assert params["feed"] == "sip"  # type: ignore[index]
    assert params["timeframe"] == "1Day"  # type: ignore[index]
    assert params["start"] == "2026-07-01"  # type: ignore[index]
    assert params["end"] == "2026-07-03"  # type: ignore[index]


def test_fetch_minute_bars_prefers_realtime_iex_and_labels_partial_volume() -> None:
    client = FakeClient([FAKE_PAGE])
    out = AlpacaSource("k", "s", client=client).fetch_minute_bars(["SPY"])

    params = client.calls[0]["params"]
    assert params["feed"] == "iex"  # type: ignore[index]
    assert params["timeframe"] == "1Min"  # type: ignore[index]
    assert out.attrs["feed"] == "alpaca_iex_1m_realtime_partial"


def test_fetch_minute_bars_labels_sip_fallback_as_delayed() -> None:
    client = FakeClient(
        [
            {"status": 403, "json": {"message": "IEX unavailable"}},
            FAKE_PAGE,
        ]
    )
    out = AlpacaSource("k", "s", client=client).fetch_minute_bars(["SPY"])

    assert client.calls[0]["params"]["feed"] == "iex"  # type: ignore[index]
    assert client.calls[1]["params"]["feed"] == "sip"  # type: ignore[index]
    assert out.attrs["feed"] == "alpaca_sip_1m_delayed"


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("connect"),
        httpx.ReadTimeout("read"),
        httpx.ConnectError("connect"),
        httpx.RemoteProtocolError("protocol"),
    ],
)
def test_transient_error_is_retried_then_succeeds(error: httpx.HTTPError) -> None:
    client = FakeClient([error, FAKE_PAGE])
    sleeps: list[float] = []

    out = AlpacaSource(
        "k", "s", client=client, sleep=sleeps.append, retry_delays=(0.5, 1.5)
    ).fetch_daily_bars(["SPY"], date(2026, 7, 1), date(2026, 7, 3))

    assert len(out) == 2
    assert len(client.calls) == 2
    assert sleeps == [0.5]


def test_transient_error_is_raised_after_three_attempts() -> None:
    errors = [httpx.ConnectTimeout(f"connect-{i}") for i in range(3)]
    client = FakeClient(errors)
    sleeps: list[float] = []

    with pytest.raises(httpx.ConnectTimeout, match="connect-2"):
        AlpacaSource(
            "k", "s", client=client, sleep=sleeps.append, retry_delays=(0.5, 1.5)
        ).fetch_daily_bars(["SPY"], date(2026, 7, 1), date(2026, 7, 3))

    assert len(client.calls) == 3
    assert sleeps == [0.5, 1.5]


def test_http_status_error_is_not_retried() -> None:
    client = FakeClient([{"status": 401, "json": {"message": "unauthorized"}}])

    with pytest.raises(httpx.HTTPStatusError):
        AlpacaSource(
            "k", "s", client=client, sleep=lambda _: None
        ).fetch_daily_bars(["SPY"], date(2026, 7, 1), date(2026, 7, 3))

    assert len(client.calls) == 1


def test_missing_credentials_raises() -> None:
    with pytest.raises(ValueError):
        AlpacaSource("", "")
