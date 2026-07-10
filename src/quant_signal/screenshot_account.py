from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_signal.account import (
    AccountFetchError,
    AccountSnapshot,
    AccountState,
    BrokerPosition,
    ObservedPosition,
)
from quant_signal.ledger import SignalLedger


def _optional_decimal(value: object) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


class ScreenshotAccountProvider:
    """Read-only account provider backed by the latest validated screenshot import."""

    def __init__(self, ledger: SignalLedger, *, max_age: timedelta) -> None:
        self._ledger = ledger
        self._max_age = max_age

    def snapshot(self, now: datetime) -> AccountState:
        row = self._ledger.latest_observed_account()
        if row is None:
            raise AccountFetchError("no validated screenshot account")
        observed_at = datetime.fromisoformat(str(row["observed_at"]))
        if now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc) > self._max_age:
            raise AccountFetchError("screenshot account is stale")

        partial = not bool(row["positions_complete"])
        risk_rows = self._ledger.active_observed_positions(exact_only=False)
        observed_positions = tuple(
            ObservedPosition(
                symbol=str(position["symbol"]),
                qty=_optional_decimal(position["qty"]),
                avg_entry_price=_optional_decimal(position["avg_entry_price"]),
                current_price=_optional_decimal(position["current_price"]),
                market_value=_optional_decimal(position["market_value"]),
                estimated_market_value=_optional_decimal(position["estimated_market_value"]),
                pnl=_optional_decimal(position["pnl"]),
                pnl_pct=_optional_decimal(position["pnl_pct"]),
                weight_pct=_optional_decimal(position["weight_pct"]),
                precision=str(position["precision"]),
            )
            for position in risk_rows
        )
        exact_rows = self._ledger.active_observed_positions(exact_only=True)
        fresh_exact_rows = [
            position
            for position in exact_rows
            if now.astimezone(timezone.utc)
            - datetime.fromisoformat(str(position["observed_at"])).astimezone(timezone.utc)
            <= self._max_age
        ]
        positions = tuple(
            BrokerPosition(
                symbol=str(position["symbol"]),
                qty=Decimal(str(position["qty"])),
                side="long",
                avg_entry_price=Decimal(str(position["avg_entry_price"])),
                market_value=Decimal(str(position["market_value"])),
            )
            for position in fresh_exact_rows
        )
        snapshot = AccountSnapshot(
            account_id=f"screenshot:{str(row['import_id'])[:12]}",
            equity=Decimal(str(row["equity"])),
            cash=Decimal(str(row["cash"])),
            buying_power=Decimal(str(row["buying_power"])),
            currency=str(row["currency"]),
            retrieved_at=observed_at,
            source="screenshot",
            market_value=Decimal(str(row["market_value"])),
            capital_limit=Decimal(str(row["capital_limit"])),
            max_financing_ratio=Decimal(str(row["max_financing_ratio"])),
        )
        return AccountState(
            snapshot=snapshot,
            positions=positions,
            open_orders=(),
            recent_orders=(),
            observed_positions=observed_positions,
            positions_partial=partial,
            reported_position_count=int(str(row["reported_position_count"])),
        )
