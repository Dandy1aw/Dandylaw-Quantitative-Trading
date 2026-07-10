from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, cast

import httpx

from quant_signal.index_universe import to_canonical_symbol

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
_ORDERS_LIMIT = 200


class AccountFetchError(RuntimeError):
    pass


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> _Response: ...


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    currency: str
    retrieved_at: datetime
    source: str = "alpaca_paper"
    market_value: Decimal | None = None
    capital_limit: Decimal | None = None
    max_financing_ratio: Decimal = Decimal("0")

    @property
    def max_gross_exposure(self) -> Decimal:
        limit = self.capital_limit or self.equity
        return limit * (Decimal("1") + self.max_financing_ratio)


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: Decimal
    side: str
    avg_entry_price: Decimal
    market_value: Decimal


@dataclass(frozen=True)
class ObservedPosition:
    symbol: str
    qty: Decimal | None
    avg_entry_price: Decimal | None
    current_price: Decimal | None
    market_value: Decimal | None
    estimated_market_value: Decimal | None
    pnl: Decimal | None
    pnl_pct: Decimal | None
    weight_pct: Decimal | None
    precision: str

    @property
    def exposure_value(self) -> Decimal:
        return self.market_value or self.estimated_market_value or Decimal("0")


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    symbol: str
    side: str
    status: str
    qty: Decimal | None
    limit_price: Decimal | None
    submitted_at: datetime | None
    filled_qty: Decimal
    filled_avg_price: Decimal | None


@dataclass(frozen=True)
class AccountState:
    snapshot: AccountSnapshot
    positions: tuple[BrokerPosition, ...]
    open_orders: tuple[BrokerOrder, ...]
    recent_orders: tuple[BrokerOrder, ...]
    observed_positions: tuple[ObservedPosition, ...] = ()
    positions_partial: bool = False
    reported_position_count: int | None = None


class AccountProvider(Protocol):
    def snapshot(self, now: datetime) -> AccountState: ...


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AccountFetchError(f"invalid decimal for {field}") from error


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, field)


def _optional_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _parse_order(row: Mapping[str, object]) -> BrokerOrder:
    return BrokerOrder(
        order_id=str(row["id"]),
        symbol=to_canonical_symbol(str(row["symbol"])),
        side=str(row["side"]),
        status=str(row["status"]),
        qty=_optional_decimal(row.get("qty"), "order.qty"),
        limit_price=_optional_decimal(row.get("limit_price"), "order.limit_price"),
        submitted_at=_optional_datetime(row.get("submitted_at")),
        filled_qty=_decimal(row.get("filled_qty", "0"), "order.filled_qty"),
        filled_avg_price=_optional_decimal(
            row.get("filled_avg_price"), "order.filled_avg_price"
        ),
    )


class AlpacaPaperAccountProvider:
    """只读 PAPER 账户适配器: 仅 GET, 不含任何下单/撤单能力, 凭据不进 repr 与异常。"""

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        client: _HTTPClient | None = None,
        base_url: str = PAPER_BASE_URL,
    ) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET 未配置，请填写 config/.env")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._client: _HTTPClient = (
            client if client is not None else cast(_HTTPClient, httpx)
        )
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"AlpacaPaperAccountProvider(base_url={self._base_url!r})"

    def _get(self, path: str, params: dict[str, object] | None = None) -> Any:
        url = f"{self._base_url}{path}"
        try:
            response = self._client.get(
                url, params=params or {}, headers=self._headers, timeout=30.0
            )
            response.raise_for_status()
            return response.json()
        except Exception as error:
            # 底层异常可能带出请求细节, 统一替换为脱敏消息。
            raise AccountFetchError(
                f"alpaca paper account GET {path} failed: {type(error).__name__}"
            ) from None

    def snapshot(self, now: datetime) -> AccountState:
        account = self._get("/v2/account")
        positions = self._get("/v2/positions")
        open_orders = self._get(
            "/v2/orders", {"status": "open", "limit": _ORDERS_LIMIT}
        )
        closed_orders = self._get(
            "/v2/orders", {"status": "closed", "limit": _ORDERS_LIMIT}
        )
        snapshot = AccountSnapshot(
            account_id=str(account.get("id", "")),
            equity=_decimal(account["equity"], "account.equity"),
            cash=_decimal(account["cash"], "account.cash"),
            buying_power=_decimal(account["buying_power"], "account.buying_power"),
            currency=str(account.get("currency", "USD")),
            retrieved_at=now,
        )
        return AccountState(
            snapshot=snapshot,
            positions=tuple(
                BrokerPosition(
                    symbol=to_canonical_symbol(str(row["symbol"])),
                    qty=_decimal(row["qty"], "position.qty"),
                    side=str(row.get("side", "long")),
                    avg_entry_price=_decimal(
                        row["avg_entry_price"], "position.avg_entry_price"
                    ),
                    market_value=_decimal(row["market_value"], "position.market_value"),
                )
                for row in positions
            ),
            open_orders=tuple(_parse_order(row) for row in open_orders),
            recent_orders=tuple(_parse_order(row) for row in closed_orders),
        )
