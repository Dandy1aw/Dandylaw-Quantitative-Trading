"""持仓期权情报纯函数：把期权链定价转成持股决策语境（只观察，不交易）。

设计：docs/superpowers/specs/2026-07-11-holdings-option-intel-design.md
缺数据只能显示缺失（None），绝不能编造。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import math
import statistics
from typing import Protocol

from quant_signal.options_flow import OptionSide

_ANNUALIZE = math.sqrt(252.0)
_RV_RETURNS = 20


@dataclass(frozen=True)
class OptionChainContract:
    """数据层产出的单张合约（indicative feed，约 15 分钟延迟）。"""

    contract_symbol: str
    underlying: str
    side: OptionSide
    expiration: date
    strike: Decimal
    bid: Decimal | None
    ask: Decimal | None
    implied_volatility: float | None
    day_volume: int
    open_interest: int | None


@dataclass(frozen=True)
class OptionChainFetchResult:
    contracts: tuple[OptionChainContract, ...]
    # True = 分页超出上限,链不完整——展示层须如实标注,不得当完整数据用
    truncated: bool


class OptionChainProvider(Protocol):
    """Provider boundary for one underlying's option chain."""

    def fetch_chain(
        self, underlying: str, *, session: date, max_expiry_days: int
    ) -> OptionChainFetchResult: ...


@dataclass(frozen=True)
class TopOIStrike:
    strike: Decimal
    side: OptionSide
    expiration: date
    open_interest: int


@dataclass(frozen=True)
class OptionIntelPolicy:
    max_expiry_days: int = 60
    min_expected_move_dte: int = 7
    top_oi_strikes: int = 3
    earnings_window_days: int = 30


@dataclass(frozen=True)
class OptionIntel:
    """单标的情报结果；None 一律表示"算不出"，卡片渲染为 '-'。"""

    symbol: str
    spot: Decimal
    session: date
    expected_move_pct: float | None
    expected_move_expiry: date | None
    earnings_move_pct: float | None
    earnings_move_expiry: date | None
    atm_iv: float | None
    realized_vol_20d: float | None
    pc_volume_ratio: float | None
    pc_oi_ratio: float | None
    top_oi_strikes: tuple[TopOIStrike, ...]
    earnings_date: date | None
    data_note: str | None


def realized_vol_20d(closes: Sequence[float]) -> float | None:
    """近 20 个日对数收益的样本 std × √252；不足 21 根收盘价返回 None。"""
    if len(closes) < _RV_RETURNS + 1:
        return None
    window = list(closes)[-(_RV_RETURNS + 1) :]
    if any(price <= 0 for price in window):
        return None
    returns = [
        math.log(window[i] / window[i - 1]) for i in range(1, len(window))
    ]
    return statistics.stdev(returns) * _ANNUALIZE


def _mid(contract: OptionChainContract) -> Decimal | None:
    # bid=0 的报价视为无效（无人接盘的僵尸盘口，mid 不可信）
    if contract.bid is None or contract.ask is None:
        return None
    if contract.bid <= 0 or contract.ask < contract.bid:
        return None
    return (contract.bid + contract.ask) / 2


def _atm_pair(
    contracts: Sequence[OptionChainContract],
    expiry: date,
    spot: Decimal,
) -> tuple[OptionChainContract | None, OptionChainContract | None]:
    def pick(side: OptionSide) -> OptionChainContract | None:
        candidates = [
            item
            for item in contracts
            if item.expiration == expiry and item.side == side
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda item: (abs(item.strike - spot), item.strike, item.contract_symbol),
        )

    return pick("call"), pick("put")


def _straddle_move(
    contracts: Sequence[OptionChainContract],
    expiry: date,
    spot: Decimal,
) -> float | None:
    call, put = _atm_pair(contracts, expiry, spot)
    if call is None or put is None:
        return None
    call_mid = _mid(call)
    put_mid = _mid(put)
    if call_mid is None or put_mid is None:
        return None
    return float((call_mid + put_mid) / spot)


def _atm_iv(
    contracts: Sequence[OptionChainContract],
    expiry: date,
    spot: Decimal,
) -> float | None:
    call, put = _atm_pair(contracts, expiry, spot)
    values = [
        item.implied_volatility
        for item in (call, put)
        if item is not None and item.implied_volatility is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def compute_intel(
    symbol: str,
    contracts: Sequence[OptionChainContract],
    *,
    spot: Decimal,
    session: date,
    closes: Sequence[float],
    earnings_date: date | None,
    policy: OptionIntelPolicy,
) -> OptionIntel:
    if spot <= 0:
        raise ValueError("spot price must be positive")

    rv = realized_vol_20d(closes)
    eligible = [
        item
        for item in contracts
        if session <= item.expiration
        and (item.expiration - session).days <= policy.max_expiry_days
    ]
    if not eligible:
        return OptionIntel(
            symbol=symbol,
            spot=spot,
            session=session,
            expected_move_pct=None,
            expected_move_expiry=None,
            earnings_move_pct=None,
            earnings_move_expiry=None,
            atm_iv=None,
            realized_vol_20d=rv,
            pc_volume_ratio=None,
            pc_oi_ratio=None,
            top_oi_strikes=(),
            earnings_date=earnings_date,
            data_note="无可用期权数据",
        )

    expiries = sorted({item.expiration for item in eligible})
    move_expiry = next(
        (
            expiry
            for expiry in expiries
            if (expiry - session).days >= policy.min_expected_move_dte
        ),
        None,
    )
    expected_move = (
        _straddle_move(eligible, move_expiry, spot) if move_expiry is not None else None
    )
    atm_iv = _atm_iv(eligible, move_expiry, spot) if move_expiry is not None else None

    earnings_move: float | None = None
    earnings_expiry: date | None = None
    if (
        earnings_date is not None
        and 0 <= (earnings_date - session).days <= policy.earnings_window_days
    ):
        candidate = next(
            (expiry for expiry in expiries if expiry >= earnings_date), None
        )
        if candidate is not None and candidate != move_expiry:
            move = _straddle_move(eligible, candidate, spot)
            if move is not None:
                earnings_move = move
                earnings_expiry = candidate

    call_volume = sum(item.day_volume for item in eligible if item.side == "call")
    put_volume = sum(item.day_volume for item in eligible if item.side == "put")
    pc_volume = put_volume / call_volume if call_volume > 0 else None

    call_oi = sum(
        item.open_interest or 0 for item in eligible if item.side == "call"
    )
    put_oi = sum(item.open_interest or 0 for item in eligible if item.side == "put")
    pc_oi = put_oi / call_oi if call_oi > 0 else None

    with_oi = [
        item
        for item in eligible
        if item.open_interest is not None and item.open_interest > 0
    ]
    with_oi.sort(key=lambda item: (-(item.open_interest or 0), item.contract_symbol))
    top_oi = tuple(
        TopOIStrike(
            strike=item.strike,
            side=item.side,
            expiration=item.expiration,
            open_interest=item.open_interest or 0,
        )
        for item in with_oi[: policy.top_oi_strikes]
    )

    return OptionIntel(
        symbol=symbol,
        spot=spot,
        session=session,
        expected_move_pct=expected_move,
        expected_move_expiry=move_expiry,
        earnings_move_pct=earnings_move,
        earnings_move_expiry=earnings_expiry,
        atm_iv=atm_iv,
        realized_vol_20d=rv,
        pc_volume_ratio=pc_volume,
        pc_oi_ratio=pc_oi,
        top_oi_strikes=top_oi,
        earnings_date=earnings_date,
        data_note=None,
    )
