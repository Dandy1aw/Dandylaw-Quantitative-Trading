"""Daily extreme-mover detection and main-board eligibility rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum

import pandas as pd

from quant_signal.company_profiles import CompanyProfile


class MoverDirection(str, Enum):
    UP = "up"
    DOWN = "down"


class Eligibility(str, Enum):
    ELIGIBLE = "eligible"
    NON_EQUITY = "non_equity"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    LOW_PRICE = "low_price"
    LOW_LIQUIDITY = "low_liquidity"


@dataclass(frozen=True)
class ExtremeMoverEvent:
    session: date
    ticker: str
    direction: MoverDirection
    daily_return: Decimal
    close: Decimal
    avg_dollar_volume_20d: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None
    eligibility: Eligibility = Eligibility.PROFILE_UNAVAILABLE
    source: str = "alpaca_sip"
    backfilled: bool = False


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def detect_extreme_movers(
    bars: pd.DataFrame,
    session: date,
    *,
    threshold: Decimal = Decimal("0.10"),
) -> tuple[ExtremeMoverEvent, ...]:
    """Return symbols whose final close moved at least ``threshold`` that session."""
    if threshold <= 0:
        raise ValueError("threshold must be positive")
    if bars.empty:
        return ()
    if not isinstance(bars.index, pd.MultiIndex):
        raise ValueError("bars must use a ticker/ts MultiIndex")

    events: list[ExtremeMoverEvent] = []
    for ticker, frame in bars.groupby(level="ticker", sort=True):
        rows = frame.reset_index(level="ticker", drop=True).sort_index()
        rows = rows[rows.index.map(lambda value: value.date() <= session)]
        rows = rows[~rows.index.duplicated(keep="last")]
        if len(rows) < 2 or rows.index[-1].date() != session:
            continue
        previous_close = _decimal(rows.iloc[-2]["close"])
        current_close = _decimal(rows.iloc[-1]["close"])
        if previous_close <= 0 or current_close <= 0:
            continue
        daily_return = current_close / previous_close - Decimal("1")
        if daily_return >= threshold:
            direction = MoverDirection.UP
        elif daily_return <= -threshold:
            direction = MoverDirection.DOWN
        else:
            continue
        events.append(
            ExtremeMoverEvent(
                session=session,
                ticker=str(ticker).upper(),
                direction=direction,
                daily_return=daily_return,
                close=current_close,
            )
        )
    return tuple(events)


def average_dollar_volume(frame: pd.DataFrame, *, sessions: int = 20) -> Decimal:
    """Calculate mean close-times-volume over the latest complete rows."""
    if sessions < 1:
        raise ValueError("sessions must be positive")
    values = pd.to_numeric(frame["close"], errors="coerce") * pd.to_numeric(
        frame["volume"], errors="coerce"
    )
    values = values.dropna().tail(sessions)
    if values.empty:
        return Decimal("0")
    return _decimal(values.mean())


def qualify_event(
    event: ExtremeMoverEvent,
    profile: CompanyProfile | None,
    *,
    avg_dollar_volume_20d: Decimal,
    min_price: Decimal,
    min_dollar_volume: Decimal,
) -> ExtremeMoverEvent:
    """Attach point-in-time metadata and decide main-board eligibility."""
    if profile is None or profile.data_status != "ok":
        eligibility = Eligibility.PROFILE_UNAVAILABLE
    elif (profile.quote_type or "").upper() != "EQUITY":
        eligibility = Eligibility.NON_EQUITY
    elif event.close < min_price:
        eligibility = Eligibility.LOW_PRICE
    elif avg_dollar_volume_20d < min_dollar_volume:
        eligibility = Eligibility.LOW_LIQUIDITY
    else:
        eligibility = Eligibility.ELIGIBLE

    return replace(
        event,
        avg_dollar_volume_20d=avg_dollar_volume_20d,
        sector=profile.gics_sector if profile else None,
        industry=profile.industry if profile else None,
        quote_type=profile.quote_type if profile else None,
        eligibility=eligibility,
    )
