"""Pure holdings tactical analysis: weekly mean reversion and option structure."""

from __future__ import annotations

import calendar as month_calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal, Protocol

import numpy as np
import pandas as pd

from quant_signal.calendar import is_trading_day, previous_trading_day

TechnicalState = Literal[
    "OVERSOLD_AT_LOWER_BAND",
    "MEAN_REVERSION_WATCH",
    "NEUTRAL",
    "DOWNTREND_CONTINUATION_RISK",
    "DATA_INSUFFICIENT",
]
WallReliability = Literal["HIGH", "MEDIUM", "LOW"]
OpexState = Literal["NORMAL", "OPEX_WINDOW", "OPEX_DAY", "POST_OPEX_RESET"]
ConfluenceConclusion = Literal[
    "TACTICAL_REBOUND",
    "WAIT_REVERSAL_CONFIRMATION",
    "PUT_WALL_BREAK_RISK",
    "FALLING_KNIFE_RISK",
    "TECHNICAL_ONLY",
    "DATA_INSUFFICIENT",
]


class TacticalOptionContract(Protocol):
    @property
    def side(self) -> str: ...

    @property
    def expiration(self) -> date: ...

    @property
    def strike(self) -> Decimal: ...

    @property
    def open_interest(self) -> int | None: ...

    @property
    def bid(self) -> Decimal | None: ...

    @property
    def ask(self) -> Decimal | None: ...

    @property
    def gamma(self) -> float | None: ...


@dataclass(frozen=True)
class PositionTacticalPolicy:
    wall_high_min_oi_each_side: int = 5_000
    wall_medium_min_oi_each_side: int = 1_000
    wall_high_min_oi_coverage: float = 0.80
    wall_medium_min_oi_coverage: float = 0.50
    wall_high_min_quote_coverage: float = 0.60
    wall_high_min_concentration: float = 0.10
    gamma_near_spot_pct: float = 0.02


@dataclass(frozen=True)
class OptionStructure:
    expiry: date | None
    put_wall: Decimal | None
    call_wall: Decimal | None
    max_pain: Decimal | None
    put_buffer_pct: float | None
    call_upside_pct: float | None
    gamma_pin_score: float | None
    dealer_gamma_state: Literal["UNKNOWN"]
    reliability: WallReliability
    oi_coverage: float
    quote_coverage: float
    truncated: bool


@dataclass(frozen=True)
class OpexContext:
    monthly_expiry: date
    trading_days_to_expiry: int
    state: OpexState


@dataclass(frozen=True)
class PositionTacticalAnalysis:
    symbol: str
    spot: Decimal
    technical: WeeklyTechnical
    options: OptionStructure
    opex: OpexContext
    conclusion: ConfluenceConclusion
    invalidation_price: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    holding_note: str


@dataclass(frozen=True)
class WeeklyTechnical:
    state: TechnicalState
    weekly_rsi: float | None
    bollinger_lower: float | None
    bollinger_mid: float | None
    bollinger_upper: float | None
    middle_slope_4w: float | None
    weekly_atr: float | None
    reversal_confirmed: bool | None
    latest_week_partial: bool
    recent_four_week_low: float | None
    history_weeks: int


def _weekly_frame(daily: pd.DataFrame) -> pd.DataFrame:
    if daily.empty:
        return pd.DataFrame()
    frame = daily.copy()
    index = pd.DatetimeIndex(frame.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    frame.index = index
    numeric = ("open", "high", "low", "close", "volume")
    for column in numeric:
        if column not in frame:
            return pd.DataFrame()
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    weekly = frame.sort_index().resample("W-FRI").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return weekly.dropna(subset=["open", "high", "low", "close"])


def _weekly_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = gain.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    average_loss = loss.ewm(
        alpha=1 / period, adjust=False, min_periods=period
    ).mean()
    strength = average_gain / average_loss.replace(0, np.nan)
    result = 100.0 - 100.0 / (1.0 + strength)
    result = result.mask((average_loss == 0) & (average_gain > 0), 100.0)
    return result.mask((average_loss == 0) & (average_gain == 0), 50.0)


def _weekly_atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ),
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period).mean()


def _reversal_confirmed(complete: pd.DataFrame, rsi: pd.Series) -> bool:
    if len(complete) < 2:
        return False
    current = complete.iloc[-1]
    previous = complete.iloc[-2]
    body = abs(float(current["close"]) - float(current["open"]))
    lower_shadow = min(float(current["open"]), float(current["close"])) - float(
        current["low"]
    )
    hammer = (
        float(current["close"]) > float(current["open"])
        and lower_shadow >= max(body * 1.5, 0.01)
    )
    bullish_engulfing = (
        float(previous["close"]) < float(previous["open"])
        and float(current["close"]) > float(current["open"])
        and float(current["open"]) <= float(previous["close"])
        and float(current["close"]) >= float(previous["open"])
    )
    rsi_current = rsi.loc[complete.index[-1]]
    rsi_previous = rsi.loc[complete.index[-2]]
    momentum_turn = (
        float(current["close"]) >= float(previous["low"])
        and pd.notna(rsi_current)
        and pd.notna(rsi_previous)
        and float(rsi_current) > float(rsi_previous)
    )
    return bool(hammer or bullish_engulfing or momentum_turn)


def compute_weekly_technical(
    daily: pd.DataFrame,
    *,
    as_of: date,
) -> WeeklyTechnical:
    weekly = _weekly_frame(daily)
    history_weeks = len(weekly)
    latest_partial = bool(
        history_weeks and pd.Timestamp(weekly.index[-1]).date() > as_of
    )
    if history_weeks < 21:
        return WeeklyTechnical(
            state="DATA_INSUFFICIENT",
            weekly_rsi=None,
            bollinger_lower=None,
            bollinger_mid=None,
            bollinger_upper=None,
            middle_slope_4w=None,
            weekly_atr=None,
            reversal_confirmed=None,
            latest_week_partial=latest_partial,
            recent_four_week_low=None,
            history_weeks=history_weeks,
        )

    close = weekly["close"]
    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=1)
    lower = middle - 2 * deviation
    upper = middle + 2 * deviation
    rsi = _weekly_rsi(close)
    atr = _weekly_atr(weekly)
    complete_mask = pd.DatetimeIndex(weekly.index).date <= as_of
    complete: pd.DataFrame = weekly.loc[complete_mask].copy()
    current_price = float(close.iloc[-1])
    current_mid = float(middle.iloc[-1])
    current_lower = float(lower.iloc[-1])
    current_upper = float(upper.iloc[-1])
    current_rsi = float(rsi.iloc[-1])
    current_atr = float(atr.iloc[-1])
    slope = (
        float(current_mid / middle.iloc[-5] - 1.0)
        if len(middle.dropna()) >= 5 and float(middle.iloc[-5]) > 0
        else None
    )
    recent_low = (
        float(complete["low"].tail(4).min()) if not complete.empty else None
    )
    new_four_week_low = bool(
        len(complete) >= 4
        and float(complete["close"].iloc[-1])
        <= float(complete["close"].tail(4).min())
    )
    if slope is not None and slope < -0.02 and new_four_week_low:
        state: TechnicalState = "DOWNTREND_CONTINUATION_RISK"
    elif current_rsi <= 35 and current_price <= current_lower * 1.03:
        state = "OVERSOLD_AT_LOWER_BAND"
    elif (
        current_rsi <= 45
        and current_price >= current_lower
        and current_price <= current_mid * 0.90
    ):
        state = "MEAN_REVERSION_WATCH"
    else:
        state = "NEUTRAL"
    return WeeklyTechnical(
        state=state,
        weekly_rsi=round(current_rsi, 4),
        bollinger_lower=round(current_lower, 4),
        bollinger_mid=round(current_mid, 4),
        bollinger_upper=round(current_upper, 4),
        middle_slope_4w=round(slope, 6) if slope is not None else None,
        weekly_atr=round(current_atr, 4),
        reversal_confirmed=_reversal_confirmed(complete, rsi),
        latest_week_partial=latest_partial,
        recent_four_week_low=(
            round(recent_low, 4) if recent_low is not None else None
        ),
        history_weeks=history_weeks,
    )


def _largest_oi(
    contracts: Sequence[TacticalOptionContract],
    *,
    side: str,
    spot: Decimal,
) -> TacticalOptionContract | None:
    eligible = [
        item
        for item in contracts
        if item.side == side
        and item.open_interest is not None
        and item.open_interest > 0
        and (
            item.strike <= spot
            if side == "put"
            else item.strike >= spot
        )
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (
            item.open_interest or 0,
            -abs(item.strike - spot),
        ),
    )


def _max_pain(
    contracts: Sequence[TacticalOptionContract],
) -> Decimal | None:
    with_oi = [
        item
        for item in contracts
        if item.open_interest is not None and item.open_interest > 0
    ]
    strikes = sorted({item.strike for item in with_oi})
    if not strikes:
        return None

    def payout(settlement: Decimal) -> Decimal:
        total = Decimal("0")
        for item in with_oi:
            intrinsic = (
                max(settlement - item.strike, Decimal("0"))
                if item.side == "call"
                else max(item.strike - settlement, Decimal("0"))
            )
            total += intrinsic * Decimal(item.open_interest or 0)
        return total

    return min(strikes, key=lambda strike: (payout(strike), strike))


def compute_option_structure(
    contracts: Sequence[TacticalOptionContract],
    *,
    spot: Decimal,
    session: date,
    truncated: bool,
    policy: PositionTacticalPolicy,
) -> OptionStructure:
    expiries = sorted(
        {item.expiration for item in contracts if item.expiration >= session}
    )
    if not expiries:
        return OptionStructure(
            expiry=None,
            put_wall=None,
            call_wall=None,
            max_pain=None,
            put_buffer_pct=None,
            call_upside_pct=None,
            gamma_pin_score=None,
            dealer_gamma_state="UNKNOWN",
            reliability="LOW",
            oi_coverage=0.0,
            quote_coverage=0.0,
            truncated=truncated,
        )
    expiry = expiries[0]
    selected = [item for item in contracts if item.expiration == expiry]
    count = len(selected)
    oi_count = sum(item.open_interest is not None for item in selected)
    quote_count = sum(
        item.bid is not None
        and item.ask is not None
        and item.bid > 0
        and item.ask >= item.bid
        for item in selected
    )
    oi_coverage = oi_count / count if count else 0.0
    quote_coverage = quote_count / count if count else 0.0
    put_wall_contract = _largest_oi(selected, side="put", spot=spot)
    call_wall_contract = _largest_oi(selected, side="call", spot=spot)
    put_wall = put_wall_contract.strike if put_wall_contract is not None else None
    call_wall = call_wall_contract.strike if call_wall_contract is not None else None
    put_total = sum(
        item.open_interest or 0 for item in selected if item.side == "put"
    )
    call_total = sum(
        item.open_interest or 0 for item in selected if item.side == "call"
    )
    put_concentration = (
        (put_wall_contract.open_interest or 0) / put_total
        if put_wall_contract is not None and put_total > 0
        else 0.0
    )
    call_concentration = (
        (call_wall_contract.open_interest or 0) / call_total
        if call_wall_contract is not None and call_total > 0
        else 0.0
    )
    walls_present = put_wall is not None and call_wall is not None
    if (
        not truncated
        and walls_present
        and oi_coverage >= policy.wall_high_min_oi_coverage
        and quote_coverage >= policy.wall_high_min_quote_coverage
        and put_total >= policy.wall_high_min_oi_each_side
        and call_total >= policy.wall_high_min_oi_each_side
        and put_concentration >= policy.wall_high_min_concentration
        and call_concentration >= policy.wall_high_min_concentration
    ):
        reliability: WallReliability = "HIGH"
    elif (
        not truncated
        and walls_present
        and oi_coverage >= policy.wall_medium_min_oi_coverage
        and put_total >= policy.wall_medium_min_oi_each_side
        and call_total >= policy.wall_medium_min_oi_each_side
    ):
        reliability = "MEDIUM"
    else:
        reliability = "LOW"

    gamma_exposure: list[tuple[TacticalOptionContract, float]] = []
    for item in selected:
        if (
            item.gamma is None
            or item.gamma <= 0
            or item.open_interest is None
            or item.open_interest <= 0
        ):
            continue
        exposure = (
            item.gamma
            * item.open_interest
            * 100
            * float(spot) ** 2
            * 0.01
        )
        gamma_exposure.append((item, exposure))
    total_gamma = sum(value for _, value in gamma_exposure)
    near_gamma = sum(
        value
        for item, value in gamma_exposure
        if abs(float(item.strike / spot - 1)) <= policy.gamma_near_spot_pct
    )
    gamma_pin_score = near_gamma / total_gamma if total_gamma > 0 else None
    return OptionStructure(
        expiry=expiry,
        put_wall=put_wall,
        call_wall=call_wall,
        max_pain=_max_pain(selected),
        put_buffer_pct=(
            float((spot - put_wall) / spot) if put_wall is not None else None
        ),
        call_upside_pct=(
            float((call_wall - spot) / spot) if call_wall is not None else None
        ),
        gamma_pin_score=(
            round(gamma_pin_score, 6) if gamma_pin_score is not None else None
        ),
        dealer_gamma_state="UNKNOWN",
        reliability=reliability,
        oi_coverage=round(oi_coverage, 6),
        quote_coverage=round(quote_coverage, 6),
        truncated=truncated,
    )


def _monthly_expiry(year: int, month: int) -> date:
    weeks = month_calendar.monthcalendar(year, month)
    fridays = [week[month_calendar.FRIDAY] for week in weeks if week[month_calendar.FRIDAY]]
    third_friday = date(year, month, fridays[2])
    if is_trading_day(third_friday):
        return third_friday
    return previous_trading_day(third_friday + timedelta(days=1))


def _next_trading_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _trading_days_between(start: date, end: date) -> int:
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if is_trading_day(current):
            count += 1
        current += timedelta(days=1)
    return count


def compute_opex_context(session: date) -> OpexContext:
    current_expiry = _monthly_expiry(session.year, session.month)
    if session == current_expiry:
        return OpexContext(current_expiry, 0, "OPEX_DAY")
    if session == _next_trading_day(current_expiry):
        return OpexContext(current_expiry, -1, "POST_OPEX_RESET")
    expiry = current_expiry
    if session > current_expiry:
        next_month = 1 if session.month == 12 else session.month + 1
        next_year = session.year + 1 if session.month == 12 else session.year
        expiry = _monthly_expiry(next_year, next_month)
    distance = _trading_days_between(session, expiry)
    state: OpexState = "OPEX_WINDOW" if 1 <= distance <= 3 else "NORMAL"
    return OpexContext(expiry, distance, state)


def _price(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


def classify_confluence(
    symbol: str,
    spot: Decimal,
    technical: WeeklyTechnical,
    options: OptionStructure,
    opex: OpexContext,
) -> PositionTacticalAnalysis:
    if technical.state == "DATA_INSUFFICIENT":
        conclusion: ConfluenceConclusion = "DATA_INSUFFICIENT"
    elif technical.state == "DOWNTREND_CONTINUATION_RISK":
        conclusion = "FALLING_KNIFE_RISK"
    elif (
        options.put_buffer_pct is not None
        and options.put_buffer_pct < 0.01
    ):
        conclusion = "PUT_WALL_BREAK_RISK"
    elif options.reliability == "LOW":
        conclusion = "TECHNICAL_ONLY"
    elif technical.state in (
        "OVERSOLD_AT_LOWER_BAND",
        "MEAN_REVERSION_WATCH",
    ):
        if not technical.reversal_confirmed:
            conclusion = "WAIT_REVERSAL_CONFIRMATION"
        elif (
            options.put_buffer_pct is not None
            and 0.02 <= options.put_buffer_pct <= 0.10
        ):
            conclusion = "TACTICAL_REBOUND"
        else:
            conclusion = "TECHNICAL_ONLY"
    else:
        conclusion = "TECHNICAL_ONLY"

    invalidation: Decimal | None = None
    if technical.weekly_atr is not None:
        if (
            options.reliability in ("HIGH", "MEDIUM")
            and options.put_wall is not None
        ):
            buffer = max(
                0.5 * technical.weekly_atr,
                0.01 * float(spot),
            )
            invalidation = _price(float(options.put_wall) - buffer)
        elif technical.recent_four_week_low is not None:
            invalidation = _price(
                technical.recent_four_week_low - 0.5 * technical.weekly_atr
            )
        if invalidation is not None and invalidation <= 0:
            invalidation = None

    raw_targets = [
        value
        for value in (
            technical.bollinger_mid,
            float(options.call_wall) if options.call_wall is not None else None,
        )
        if value is not None and value > float(spot)
    ]
    targets = sorted({_price(value) for value in raw_targets})
    target_1 = targets[0] if targets else None
    target_2 = targets[1] if len(targets) > 1 else None
    holding_note = {
        "NORMAL": "按周线与墙位观察，墙位变化时重新评估。",
        "OPEX_WINDOW": "临近月度OPEX，控制仓位；到期后重新计算墙位。",
        "OPEX_DAY": "月度OPEX当日不追单，避免依据旧墙位隔夜加仓。",
        "POST_OPEX_RESET": "OPEX后首个交易日，旧墙位可能失效，等待结构重建。",
    }[opex.state]
    return PositionTacticalAnalysis(
        symbol=symbol.upper(),
        spot=spot,
        technical=technical,
        options=options,
        opex=opex,
        conclusion=conclusion,
        invalidation_price=invalidation,
        target_1=target_1,
        target_2=target_2,
        holding_note=holding_note,
    )


def analyze_position_tactical(
    symbol: str,
    daily: pd.DataFrame,
    contracts: Sequence[TacticalOptionContract],
    *,
    spot: Decimal,
    session: date,
    chain_truncated: bool,
    policy: PositionTacticalPolicy,
) -> PositionTacticalAnalysis:
    technical = compute_weekly_technical(daily, as_of=session)
    options = compute_option_structure(
        contracts,
        spot=spot,
        session=session,
        truncated=chain_truncated,
        policy=policy,
    )
    opex = compute_opex_context(session)
    return classify_confluence(symbol, spot, technical, options, opex)
