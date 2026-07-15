from decimal import Decimal

from quant_signal.config import PositionDisciplineSettings
from quant_signal.market_regime import Regime
from quant_signal.position_discipline import (
    DisciplineState,
    DisciplineStatus,
    PositionInput,
    advise_position,
    summarize_portfolio_risk,
)


CFG = PositionDisciplineSettings()


def _position(
    *,
    ticker: str = "MU",
    price: str = "116",
    cost: str | None = "100",
    qty: str | None = "100",
    pnl_pct: str | None = None,
    market_value: str | None = None,
    equity: str = "100000",
    complete: bool = True,
    atr: str = "3",
) -> PositionInput:
    return PositionInput(
        ticker=ticker,
        current_price=Decimal(price),
        quantity=Decimal(qty) if qty is not None else None,
        avg_entry_price=Decimal(cost) if cost is not None else None,
        pnl_pct=Decimal(pnl_pct) if pnl_pct is not None else None,
        market_value=Decimal(market_value) if market_value is not None else None,
        account_equity=Decimal(equity),
        atr=Decimal(atr),
        positions_complete=complete,
    )


def test_first_profit_stage_is_cumulative_and_idempotent() -> None:
    advice = advise_position(_position(), leverage=1, prior=None, settings=CFG)

    assert advice.status == DisciplineStatus.TAKE_PROFIT_DUE
    assert advice.cumulative_sell_fraction == Decimal("0.25")
    assert advice.incremental_sell_fraction == Decimal("0.25")
    assert advice.incremental_sell_qty == Decimal("25")

    repeated = advise_position(
        _position(), leverage=1, prior=advice.next_state, settings=CFG
    )
    assert repeated.incremental_sell_qty == Decimal("0")
    assert repeated.status == DisciplineStatus.HOLD


def test_second_stage_only_sells_increment_from_original_position() -> None:
    first = advise_position(_position(), leverage=1, prior=None, settings=CFG)
    second = advise_position(
        _position(price="121", qty="75"),
        leverage=1,
        prior=first.next_state,
        settings=CFG,
    )

    assert second.cumulative_sell_fraction == Decimal("0.50")
    assert second.incremental_sell_fraction == Decimal("0.25")
    assert second.incremental_sell_qty == Decimal("25")
    assert second.protection_price == Decimal("108.00")


def test_two_x_product_has_tighter_hard_stop_and_no_financing() -> None:
    advice = advise_position(
        _position(ticker="RAM", price="100"), leverage=2, prior=None, settings=CFG
    )

    assert advice.hard_stop_price == Decimal("95.00")
    assert advice.financing_allowed is False


def test_three_x_product_uses_three_x_rules() -> None:
    advice = advise_position(
        _position(ticker="TQQQ", price="108"), leverage=3, prior=None, settings=CFG
    )
    assert advice.hard_stop_price == Decimal("96.50")
    assert advice.cumulative_sell_fraction == Decimal("0.25")


def test_missing_qty_never_invents_share_count() -> None:
    advice = advise_position(
        _position(qty=None, complete=False), leverage=1, prior=None, settings=CFG
    )

    assert advice.incremental_sell_qty is None
    assert advice.incremental_sell_fraction == Decimal("0.25")
    assert advice.quantity_quality == "MISSING"
    assert "POSITIONS_PARTIAL" in advice.warnings


def test_cost_can_be_estimated_from_current_price_and_pnl() -> None:
    advice = advise_position(
        _position(price="120", cost=None, pnl_pct="0.20"),
        leverage=1,
        prior=None,
        settings=CFG,
    )

    assert advice.cost_basis == Decimal("100.00")
    assert advice.cost_quality == "ESTIMATED"
    assert advice.hard_stop_price == Decimal("92.00")


def test_missing_cost_suppresses_cost_anchored_levels() -> None:
    advice = advise_position(
        _position(cost=None, pnl_pct=None), leverage=1, prior=None, settings=CFG
    )

    assert advice.cost_basis is None
    assert advice.hard_stop_price is None
    assert advice.incremental_sell_qty is None
    assert "COST_BASIS_MISSING" in advice.warnings


def test_cost_basis_change_resets_notified_stage() -> None:
    first = advise_position(_position(), leverage=1, prior=None, settings=CFG)
    changed = advise_position(
        _position(price="127.2", cost="106", qty="120"),
        leverage=1,
        prior=first.next_state,
        settings=CFG,
    )

    assert changed.next_state.basis_version != first.next_state.basis_version
    assert changed.incremental_sell_qty == Decimal("60")


def test_stop_breach_is_exit_due() -> None:
    advice = advise_position(
        _position(price="91"), leverage=1, prior=None, settings=CFG
    )
    assert advice.status == DisciplineStatus.EXIT_DUE
    assert advice.hard_stop_price == Decimal("92.00")


def test_final_stage_uses_peak_atr_trailing_protection() -> None:
    prior = DisciplineState(
        ticker="MU",
        basis_version="MU:100.0000",
        notified_stage=2,
        peak_price=Decimal("135"),
        basis_quantity=Decimal("100"),
    )
    advice = advise_position(
        _position(price="132", atr="3"), leverage=1, prior=prior, settings=CFG
    )
    assert advice.cumulative_sell_fraction == Decimal("0.75")
    assert advice.protection_price == Decimal("126.00")


def test_effective_exposure_and_cluster_risk_are_leverage_adjusted() -> None:
    ram = advise_position(
        _position(
            ticker="RAM", price="50", cost="50", qty="12", market_value="600", equity="6000"
        ),
        leverage=2,
        prior=None,
        settings=CFG,
    )
    mu = advise_position(
        _position(ticker="MU", price="120", qty="5", market_value="600", equity="6000"),
        leverage=1,
        prior=None,
        settings=CFG,
    )
    summary = summarize_portfolio_risk(
        [ram, mu], equity=Decimal("6000"), clusters={"memory": ["RAM", "MU"]}
    )

    assert ram.effective_exposure == Decimal("1200.00")
    assert ram.effective_weight == Decimal("0.2000")
    assert summary.total_effective_exposure == Decimal("1800.00")
    assert summary.cluster_effective_weights["memory"] == Decimal("0.3000")


def test_risk_off_disables_all_financing() -> None:
    advice = advise_position(
        _position(), leverage=1, prior=None, settings=CFG, regime=Regime.RISK_OFF
    )
    assert advice.financing_allowed is False
