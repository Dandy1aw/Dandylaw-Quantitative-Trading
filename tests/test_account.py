from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from quant_signal.account import (
    AccountFetchError,
    AlpacaPaperAccountProvider,
)

NOW = datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc)

ACCOUNT_JSON = {
    "id": "acct-1",
    "equity": "100000.25",
    "cash": "40000.10",
    "buying_power": "80000.20",
    "currency": "USD",
}
POSITIONS_JSON = [
    {
        "symbol": "BRK-B",
        "qty": "12",
        "side": "long",
        "avg_entry_price": "401.5",
        "market_value": "4830.00",
    },
    {
        "symbol": "AAPL",
        "qty": "3.5",
        "side": "long",
        "avg_entry_price": "210.1",
        "market_value": "735.35",
    },
]
OPEN_ORDERS_JSON = [
    {
        "id": "order-1",
        "symbol": "MSFT",
        "side": "buy",
        "qty": "5",
        "limit_price": "430.25",
        "status": "new",
        "submitted_at": "2026-07-10T13:00:00Z",
        "filled_qty": "0",
        "filled_avg_price": None,
    }
]
CLOSED_ORDERS_JSON = [
    {
        "id": "order-0",
        "symbol": "AAPL",
        "side": "buy",
        "qty": "3.5",
        "limit_price": None,
        "status": "filled",
        "submitted_at": "2026-07-09T14:00:00Z",
        "filled_qty": "3.5",
        "filled_avg_price": "210.1",
    }
]


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.content = b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append((url, dict(kwargs)))
        if url.endswith("/v2/account"):
            return _FakeResponse(ACCOUNT_JSON)
        if url.endswith("/v2/positions"):
            return _FakeResponse(POSITIONS_JSON)
        if url.endswith("/v2/orders"):
            params = kwargs.get("params")
            assert isinstance(params, dict)
            if params.get("status") == "open":
                return _FakeResponse(OPEN_ORDERS_JSON)
            if params.get("status") == "closed":
                return _FakeResponse(CLOSED_ORDERS_JSON)
        raise AssertionError(f"unexpected url {url}")


class _EmptyClient(_FakeClient):
    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append((url, dict(kwargs)))
        if url.endswith("/v2/account"):
            return _FakeResponse(ACCOUNT_JSON)
        return _FakeResponse([])


def make_provider(client: _FakeClient) -> AlpacaPaperAccountProvider:
    return AlpacaPaperAccountProvider("test-key", "test-secret", client=client)


def test_snapshot_parses_decimals_and_canonical_symbols() -> None:
    client = _FakeClient()
    state = make_provider(client).snapshot(NOW)

    assert state.snapshot.equity == Decimal("100000.25")
    assert state.snapshot.cash == Decimal("40000.10")
    assert state.snapshot.buying_power == Decimal("80000.20")
    assert state.snapshot.currency == "USD"
    assert state.snapshot.retrieved_at == NOW

    symbols = [position.symbol for position in state.positions]
    assert symbols == ["BRK.B", "AAPL"]
    assert state.positions[1].qty == Decimal("3.5")
    assert state.positions[0].avg_entry_price == Decimal("401.5")

    assert len(state.open_orders) == 1
    order = state.open_orders[0]
    assert order.order_id == "order-1"
    assert order.limit_price == Decimal("430.25")
    assert order.submitted_at == datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)

    assert len(state.recent_orders) == 1
    assert state.recent_orders[0].filled_avg_price == Decimal("210.1")


def test_snapshot_hits_only_paper_get_endpoints() -> None:
    client = _FakeClient()
    make_provider(client).snapshot(NOW)

    urls = [url for url, _ in client.requests]
    assert all(url.startswith("https://paper-api.alpaca.markets") for url in urls)
    assert any(url.endswith("/v2/account") for url in urls)
    assert any(url.endswith("/v2/positions") for url in urls)
    assert sum(url.endswith("/v2/orders") for url in urls) == 2


def test_empty_account_returns_empty_tuples() -> None:
    state = make_provider(_EmptyClient()).snapshot(NOW)
    assert state.positions == ()
    assert state.open_orders == ()
    assert state.recent_orders == ()


def test_provider_exposes_no_mutating_methods() -> None:
    provider = make_provider(_FakeClient())
    mutating = [
        name
        for name in dir(provider)
        if any(
            verb in name.lower()
            for verb in ("submit", "cancel", "post", "delete", "replace_order")
        )
    ]
    assert mutating == []


def test_errors_and_repr_redact_credentials() -> None:
    class _FailingClient(_FakeClient):
        def get(self, url: str, **kwargs: object) -> _FakeResponse:
            raise RuntimeError("401 unauthorized for key test-key secret test-secret")

    provider = AlpacaPaperAccountProvider(
        "test-key", "test-secret", client=_FailingClient()
    )
    with pytest.raises(AccountFetchError) as excinfo:
        provider.snapshot(NOW)

    message = str(excinfo.value)
    assert "test-secret" not in message
    assert "test-key" not in message
    assert "test-secret" not in repr(provider)
    assert "test-key" not in repr(provider)


def test_missing_credentials_raise() -> None:
    with pytest.raises(ValueError):
        AlpacaPaperAccountProvider("", "", client=_FakeClient())
