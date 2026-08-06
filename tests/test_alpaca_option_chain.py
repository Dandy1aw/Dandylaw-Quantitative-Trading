"""数据层：Alpaca 期权链快照 + 合约 OI 拉取（AlpacaOptionChainSource）。"""

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from quant_signal.datafeed.alpaca_options import AlpacaOptionChainSource

SESSION = date(2026, 7, 10)


def snapshot_item(
    volume: int = 150,
    iv: float = 0.5,
    gamma: float = 0.0123,
) -> dict[str, Any]:
    return {
        "latestQuote": {"bp": 2.0, "ap": 2.2, "t": "2026-07-10T14:00:01Z"},
        "impliedVolatility": iv,
        "greeks": {"gamma": gamma},
        "dailyBar": {"v": volume},
    }


def contract_item(symbol: str, oi: int | None = 800) -> dict[str, Any]:
    return {"symbol": symbol, "open_interest": str(oi) if oi is not None else None}


class FakeChainClient:
    """snapshot_pages / contract_pages: 每页 (payload_dict, next_token|None)。"""

    def __init__(
        self,
        snapshot_pages: list[tuple[dict[str, Any], str | None]],
        contract_pages: list[tuple[list[dict[str, Any]], str | None]] | None = None,
        *,
        snapshot_status: int = 200,
    ) -> None:
        self.snapshot_pages = snapshot_pages
        self.contract_pages = contract_pages if contract_pages is not None else [([], None)]
        self.snapshot_status = snapshot_status
        self.calls: list[dict[str, object]] = []
        self._snapshot_index = 0
        self._contract_index = 0

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        request = httpx.Request("GET", url)
        if "/options/snapshots/" in url:
            if self.snapshot_status != 200:
                return httpx.Response(
                    self.snapshot_status, json={"message": "nope"}, request=request
                )
            payload, token = self.snapshot_pages[self._snapshot_index]
            self._snapshot_index += 1
            return httpx.Response(
                200,
                json={"snapshots": payload, "next_page_token": token},
                request=request,
            )
        assert "/options/contracts" in url
        items, token = self.contract_pages[self._contract_index]
        self._contract_index += 1
        return httpx.Response(
            200,
            json={"option_contracts": items, "next_page_token": token},
            request=request,
        )


def fetch(client: FakeChainClient, **kwargs: Any) -> Any:
    source = AlpacaOptionChainSource("k", "s", client=client)
    return source.fetch_chain("MU", session=SESSION, max_expiry_days=60, **kwargs)


def test_merges_snapshot_quotes_with_contract_oi() -> None:
    call_symbol = "MU260724C00100000"
    put_symbol = "MU260724P00095000"
    client = FakeChainClient(
        snapshot_pages=[
            ({call_symbol: snapshot_item(volume=300, iv=0.55),
              put_symbol: snapshot_item(volume=120, iv=0.45)}, None)
        ],
        contract_pages=[([contract_item(call_symbol, oi=900)], None)],
    )
    result = fetch(client)

    assert result.truncated is False
    by_symbol = {item.contract_symbol: item for item in result.contracts}
    call = by_symbol[call_symbol]
    assert call.underlying == "MU"
    assert call.side == "call"
    assert call.expiration == date(2026, 7, 24)
    assert call.strike == Decimal("100")
    assert call.bid == Decimal("2.0") and call.ask == Decimal("2.2")
    assert call.implied_volatility == 0.55
    assert call.day_volume == 300
    assert call.open_interest == 900
    assert call.gamma == 0.0123
    put = by_symbol[put_symbol]
    assert put.side == "put" and put.strike == Decimal("95")
    assert put.open_interest is None  # contracts 端点没给,不猜


def test_sends_expiry_window_and_feed_params() -> None:
    client = FakeChainClient(snapshot_pages=[({}, None)])
    fetch(client)
    snapshot_call = next(
        call for call in client.calls if "/options/snapshots/MU" in str(call["url"])
    )
    params = snapshot_call["params"]
    assert isinstance(params, dict)
    assert params["feed"] == "indicative"
    assert params["expiration_date_gte"] == "2026-07-10"
    assert params["expiration_date_lte"] == "2026-09-08"


def test_paginates_both_endpoints() -> None:
    first = "MU260724C00100000"
    second = "MU260724C00105000"
    client = FakeChainClient(
        snapshot_pages=[
            ({first: snapshot_item()}, "token-1"),
            ({second: snapshot_item()}, None),
        ],
        contract_pages=[
            ([contract_item(first, oi=10)], "token-2"),
            ([contract_item(second, oi=20)], None),
        ],
    )
    result = fetch(client)
    assert {item.contract_symbol for item in result.contracts} == {first, second}
    assert {item.open_interest for item in result.contracts} == {10, 20}
    snapshot_calls = [c for c in client.calls if "/options/snapshots/" in str(c["url"])]
    params = snapshot_calls[1]["params"]
    assert isinstance(params, dict) and params["page_token"] == "token-1"


def test_page_cap_marks_truncated() -> None:
    pages = [
        ({f"MU260724C0010{i}000": snapshot_item()}, f"token-{i}") for i in range(3)
    ]
    client = FakeChainClient(snapshot_pages=pages)
    source = AlpacaOptionChainSource("k", "s", client=client, max_pages=2)
    result = source.fetch_chain("MU", session=SESSION, max_expiry_days=60)
    assert result.truncated is True
    assert len(result.contracts) == 2


def test_unknown_underlying_404_returns_empty() -> None:
    client = FakeChainClient(snapshot_pages=[({}, None)], snapshot_status=404)
    result = fetch(client)
    assert result.contracts == ()
    assert result.truncated is False


def test_skips_unparseable_occ_symbols() -> None:
    client = FakeChainClient(
        snapshot_pages=[
            ({"BROKEN": snapshot_item(),
              "MU260724C00100000": snapshot_item()}, None)
        ],
    )
    result = fetch(client)
    assert [item.contract_symbol for item in result.contracts] == ["MU260724C00100000"]


def test_missing_quote_fields_become_none() -> None:
    client = FakeChainClient(
        snapshot_pages=[({"MU260724C00100000": {}}, None)],
    )
    result = fetch(client)
    item = result.contracts[0]
    assert item.bid is None and item.ask is None
    assert item.implied_volatility is None
    assert item.day_volume == 0


def test_lightweight_holding_chain_skips_open_interest_endpoint() -> None:
    symbol = "MU260724C00100000"
    client = FakeChainClient(
        snapshot_pages=[({symbol: snapshot_item(volume=321)}, None)],
        contract_pages=[([contract_item(symbol, oi=999)], None)],
    )

    result = fetch(client, include_open_interest=False)

    assert result.contracts[0].day_volume == 321
    assert result.contracts[0].open_interest is None
    assert all("/options/contracts" not in str(call["url"]) for call in client.calls)
