"""领域层：持仓期权情报纯函数（spec 2026-07-11-holdings-option-intel）。"""

from datetime import date
from decimal import Decimal
import math

import pytest

from quant_signal.options_intel import (
    OptionChainContract,
    OptionIntelPolicy,
    TopOIStrike,
    compute_intel,
    realized_vol_20d,
)

SESSION = date(2026, 7, 10)
POLICY = OptionIntelPolicy()


def contract(
    *,
    side: str = "call",
    expiration: date = date(2026, 7, 24),  # DTE 14
    strike: str = "100",
    bid: str | None = "2.00",
    ask: str | None = "2.20",
    iv: float | None = 0.50,
    day_volume: int = 100,
    open_interest: int | None = 500,
    underlying: str = "MU",
) -> OptionChainContract:
    marker = "C" if side == "call" else "P"
    encoded = int(Decimal(strike) * 1000)
    return OptionChainContract(
        contract_symbol=f"{underlying}{expiration:%y%m%d}{marker}{encoded:08d}",
        underlying=underlying,
        side=side,  # type: ignore[arg-type]
        expiration=expiration,
        strike=Decimal(strike),
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        implied_volatility=iv,
        day_volume=day_volume,
        open_interest=open_interest,
    )


def straddle_pair(
    expiration: date,
    strike: str = "100",
    *,
    call_bid: str = "2.00",
    call_ask: str = "2.20",
    put_bid: str = "1.80",
    put_ask: str = "2.00",
    call_iv: float | None = 0.52,
    put_iv: float | None = 0.48,
) -> list[OptionChainContract]:
    return [
        contract(side="call", expiration=expiration, strike=strike,
                 bid=call_bid, ask=call_ask, iv=call_iv),
        contract(side="put", expiration=expiration, strike=strike,
                 bid=put_bid, ask=put_ask, iv=put_iv),
    ]


class TestExpectedMove:
    def test_straddle_mid_over_spot(self) -> None:
        rows = straddle_pair(date(2026, 7, 24))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        # (2.10 + 1.90) / 100 = 4.0%
        assert intel.expected_move_pct == pytest.approx(0.04)
        assert intel.expected_move_expiry == date(2026, 7, 24)

    def test_skips_expiry_below_min_dte(self) -> None:
        rows = straddle_pair(date(2026, 7, 11)) + straddle_pair(date(2026, 7, 24))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.expected_move_expiry == date(2026, 7, 24)

    def test_atm_picks_nearest_strike_to_spot(self) -> None:
        far = straddle_pair(date(2026, 7, 24), strike="90",
                            call_bid="9.00", call_ask="9.40",
                            put_bid="0.10", put_ask="0.20")
        near = straddle_pair(date(2026, 7, 24), strike="101",
                             call_bid="1.50", call_ask="1.70",
                             put_bid="2.00", put_ask="2.20")
        intel = compute_intel(
            "MU", far + near, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        # ATM = 101: (1.60 + 2.10) / 100
        assert intel.expected_move_pct == pytest.approx(0.037)

    def test_zero_bid_quote_is_invalid(self) -> None:
        rows = straddle_pair(date(2026, 7, 24), put_bid="0", put_ask="0.50")
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.expected_move_pct is None
        # IV 不依赖报价有效性
        assert intel.atm_iv == pytest.approx(0.50)

    def test_missing_side_yields_none(self) -> None:
        rows = [contract(side="call", expiration=date(2026, 7, 24))]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.expected_move_pct is None


class TestEarningsMove:
    def test_earnings_expiry_after_event(self) -> None:
        rows = straddle_pair(date(2026, 7, 24)) + straddle_pair(
            date(2026, 8, 7), call_bid="4.00", call_ask="4.40",
            put_bid="3.60", put_ask="4.00",
        )
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=date(2026, 8, 1), policy=POLICY,
        )
        assert intel.expected_move_expiry == date(2026, 7, 24)
        assert intel.earnings_move_expiry == date(2026, 8, 7)
        assert intel.earnings_move_pct == pytest.approx(0.08)

    def test_no_earnings_move_when_same_expiry(self) -> None:
        rows = straddle_pair(date(2026, 7, 24))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=date(2026, 7, 20), policy=POLICY,
        )
        assert intel.earnings_move_pct is None
        assert intel.earnings_move_expiry is None

    def test_no_earnings_move_beyond_window(self) -> None:
        rows = straddle_pair(date(2026, 7, 24)) + straddle_pair(date(2026, 9, 18))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=date(2026, 9, 10), policy=POLICY,
        )
        assert intel.earnings_move_pct is None


class TestAtmIV:
    def test_average_of_both_sides(self) -> None:
        rows = straddle_pair(date(2026, 7, 24), call_iv=0.60, put_iv=0.40)
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.atm_iv == pytest.approx(0.50)

    def test_one_side_missing_uses_other(self) -> None:
        rows = straddle_pair(date(2026, 7, 24), call_iv=None, put_iv=0.44)
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.atm_iv == pytest.approx(0.44)


class TestRealizedVol:
    def test_constant_prices_zero_vol(self) -> None:
        assert realized_vol_20d([100.0] * 21) == pytest.approx(0.0)

    def test_insufficient_history_is_none(self) -> None:
        assert realized_vol_20d([100.0] * 20) is None

    def test_annualized_scale(self) -> None:
        closes = [100.0 * (1.01 if i % 2 else 0.99) ** 1 for i in range(21)]
        # 交替 ±1% 的对数收益,std 已知为非零
        value = realized_vol_20d(closes)
        assert value is not None and value > 0
        # 年化系数 sqrt(252)
        daily = value / math.sqrt(252)
        assert 0.005 < daily < 0.05


class TestPutCallRatios:
    def test_volume_and_oi_ratio(self) -> None:
        rows = [
            contract(side="call", day_volume=200, open_interest=1000),
            contract(side="put", strike="95", day_volume=100, open_interest=500),
        ]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.pc_volume_ratio == pytest.approx(0.5)
        assert intel.pc_oi_ratio == pytest.approx(0.5)

    def test_zero_call_denominator_is_none(self) -> None:
        rows = [
            contract(side="call", day_volume=0, open_interest=None),
            contract(side="put", strike="95", day_volume=100, open_interest=500),
        ]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.pc_volume_ratio is None
        assert intel.pc_oi_ratio is None

    def test_excludes_contracts_beyond_max_expiry(self) -> None:
        rows = [
            contract(side="call", day_volume=200),
            contract(side="put", strike="95", day_volume=100),
            contract(side="put", strike="95", expiration=date(2027, 1, 15),
                     day_volume=9_999),
        ]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.pc_volume_ratio == pytest.approx(0.5)


class TestTopOIStrikes:
    def test_top_n_desc_with_tiebreak(self) -> None:
        rows = [
            contract(side="call", strike="110", open_interest=3000),
            contract(side="put", strike="90", open_interest=3000),
            contract(side="call", strike="100", open_interest=5000),
            contract(side="put", strike="95", open_interest=100),
        ]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert [item.open_interest for item in intel.top_oi_strikes] == [5000, 3000, 3000]
        assert intel.top_oi_strikes[0] == TopOIStrike(
            strike=Decimal("100"), side="call",
            expiration=date(2026, 7, 24), open_interest=5000,
        )
        # OI 相同按 OCC symbol 字典序
        assert intel.top_oi_strikes[1].side == "call"

    def test_ignores_missing_or_zero_oi(self) -> None:
        rows = [
            contract(side="call", strike="100", open_interest=None),
            contract(side="put", strike="95", open_interest=0),
        ]
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.top_oi_strikes == ()


class TestDegradation:
    def test_empty_chain_sets_data_note(self) -> None:
        intel = compute_intel(
            "DRAM", [], spot=Decimal("20"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.tactical is None
        assert intel.data_note == "无可用期权数据"
        assert intel.expected_move_pct is None
        assert intel.atm_iv is None
        assert intel.pc_volume_ratio is None
        assert intel.top_oi_strikes == ()

    def test_expired_contracts_are_excluded(self) -> None:
        rows = straddle_pair(date(2026, 7, 9))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=None, policy=POLICY,
        )
        assert intel.data_note == "无可用期权数据"

    def test_invalid_spot_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_intel(
                "MU", [], spot=Decimal("0"), session=SESSION,
                closes=[], earnings_date=None, policy=POLICY,
            )

    def test_earnings_date_is_carried(self) -> None:
        rows = straddle_pair(date(2026, 7, 24))
        intel = compute_intel(
            "MU", rows, spot=Decimal("100"), session=SESSION,
            closes=[], earnings_date=date(2026, 7, 28), policy=POLICY,
        )
        assert intel.earnings_date == date(2026, 7, 28)
