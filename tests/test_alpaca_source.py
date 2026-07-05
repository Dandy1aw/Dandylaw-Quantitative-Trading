from datetime import date

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


def test_fetch_daily_bars_parses_and_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, params: dict, headers: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        captured["params"] = params
        captured["headers"] = headers
        return httpx.Response(200, json=FAKE_PAGE, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    out = AlpacaSource("k", "s").fetch_daily_bars(["SPY"], date(2026, 7, 1), date(2026, 7, 3))
    assert len(out) == 2
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.names == ["ticker", "ts"]
    assert captured["params"]["adjustment"] == "all"  # type: ignore[index]
    assert captured["headers"]["APCA-API-KEY-ID"] == "k"  # type: ignore[index]


def test_missing_credentials_raises() -> None:
    with pytest.raises(ValueError):
        AlpacaSource("", "")
