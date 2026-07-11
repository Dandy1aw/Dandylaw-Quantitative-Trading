from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quant_signal.options_flow import (
    OptionContractVolume,
    OptionEnrichment,
    OptionFlowPolicy,
    OptionFlowSnapshot,
    VenueOptionVolume,
    aggregate_and_rank,
    detect_material_changes,
    display_top_by_side,
    occ_symbol,
    scan_slot,
    snapshot_from_dict,
    snapshot_to_dict,
    top_by_side,
)

NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)  # 10:15 ET
SESSION = date(2026, 7, 10)


def venue(
    name: str,
    underlying: str = "NVDA",
    side: str = "call",
    expiration: date = date(2026, 7, 17),
    strike: str = "210",
    volume: int = 1_000,
) -> VenueOptionVolume:
    return VenueOptionVolume(
        venue=name,
        underlying=underlying,
        side=side,  # type: ignore[arg-type]
        expiration=expiration,
        strike=Decimal(strike),
        volume=volume,
        captured_at=NOW,
    )


def contract(
    symbol: str = "NVDA260717C00210000",
    *,
    underlying: str = "NVDA",
    side: str = "call",
    expiration: date = date(2026, 7, 17),
    strike: str = "210",
    volume: int = 10_000,
    rank: int = 1,
    enrichment: OptionEnrichment | None = None,
) -> OptionContractVolume:
    return OptionContractVolume(
        contract_symbol=symbol,
        underlying=underlying,
        side=side,  # type: ignore[arg-type]
        expiration=expiration,
        strike=Decimal(strike),
        volume=volume,
        rank=rank,
        venues=("cone", "opt"),
        captured_at=NOW,
        enrichment=enrichment,
    )


def snapshot(*rows: OptionContractVolume, at: datetime = NOW) -> OptionFlowSnapshot:
    return OptionFlowSnapshot(
        slot=scan_slot(at),
        captured_at=at,
        provider="cboe-four-venues",
        venue_coverage=1.0,
        rows=rows,
    )


def test_models_are_immutable_and_occ_symbol_is_standard() -> None:
    row = venue("cone")
    assert occ_symbol("NVDA", date(2026, 7, 17), "call", Decimal("210")) == (
        "NVDA260717C00210000"
    )
    with pytest.raises(FrozenInstanceError):
        row.volume = 2_000  # type: ignore[misc]
    with pytest.raises(ValueError):
        occ_symbol("AAPL1", date(2026, 7, 17), "call", Decimal("300"))


def test_aggregate_visible_venue_volume_and_rank_each_side() -> None:
    rows = (
        venue("cone", volume=7_000),
        venue("opt", volume=3_000),
        venue("cone", "AAPL", "call", strike="300", volume=10_000),
        venue("cone", "MSFT", "put", strike="500", volume=12_000),
        venue("exo", "TSLA", "put", strike="400", volume=8_000),
    )

    ranked = aggregate_and_rank(rows, session=SESSION, discovery_limit=50)
    calls = top_by_side(ranked, "call", 10)
    puts = top_by_side(ranked, "put", 10)

    assert calls[0].contract_symbol == "AAPL260717C00300000"  # tie by symbol
    assert calls[1].contract_symbol == "NVDA260717C00210000"
    assert calls[1].volume == 10_000
    assert calls[1].venues == ("cone", "opt")
    assert [item.rank for item in calls] == [1, 2]
    assert [item.rank for item in puts] == [1, 2]


def test_aggregation_filters_adjusted_expired_and_invalid_rows() -> None:
    rows = (
        venue("cone", "2BMY", "put", volume=50_000),
        venue("cone", "AAPL1", "call", volume=40_000),
        venue("cone", "AAPL", "call", expiration=date(2026, 7, 9), volume=30_000),
        venue("cone", "MSFT", "put", strike="0", volume=20_000),
        venue("cone", "NVDA", "call", volume=-1),
    )
    assert aggregate_and_rank(rows, session=SESSION) == ()


def test_snapshot_json_round_trip_keeps_decimal_dates_and_enrichment() -> None:
    enrichment = OptionEnrichment(
        latest_trade_price=Decimal("2.50"),
        latest_trade_at=NOW,
        bid=Decimal("2.45"),
        ask=Decimal("2.55"),
        quote_at=NOW,
        implied_volatility=0.42,
        delta=0.35,
        gamma=0.02,
        theta=-0.10,
        vega=0.15,
        open_interest=1_000,
        open_interest_date=date(2026, 7, 9),
        multiplier=100,
    )
    original = snapshot(contract(enrichment=enrichment))
    restored = snapshot_from_dict(snapshot_to_dict(original))
    assert restored == original
    assert enrichment.estimated_premium(2_000) == Decimal("500000.00")
    assert enrichment.volume_oi_ratio(2_000) == Decimal("2")
    # 设计 §5：OI > 0 且 OI 日期有效即可展示 V/OI；OI ≥ 100 只是 HIGH_TURNOVER 的门槛
    assert replace(enrichment, open_interest=99).volume_oi_ratio(99) == Decimal("1")
    assert replace(enrichment, open_interest=0).volume_oi_ratio(2_000) is None
    assert replace(enrichment, open_interest_date=None).volume_oi_ratio(2_000) is None


def test_scan_slot_is_stable_et_fifteen_minute_floor() -> None:
    first = datetime(2026, 7, 10, 14, 16, 1, tzinfo=UTC)
    second = datetime(2026, 7, 10, 14, 29, 59, tzinfo=UTC)
    assert scan_slot(first) == scan_slot(second) == "2026-07-10T10:15:00-04:00"
    assert scan_slot(datetime(2026, 7, 10, 14, 30, tzinfo=UTC)).endswith("10:30:00-04:00")
    with pytest.raises(ValueError, match="timezone"):
        scan_slot(datetime(2026, 7, 10, 14, 30))


def test_material_change_detects_new_top10_rank_jump_and_surge() -> None:
    prior = contract(volume=8_000, rank=12)
    current = contract(volume=19_000, rank=4)
    changes = detect_material_changes(snapshot(prior), snapshot(current), OptionFlowPolicy())
    assert len(changes) == 1
    assert changes[0].flags == ("NEW_TOP10", "RANK_JUMP", "VOLUME_SURGE")
    assert changes[0].volume_delta == 11_000
    assert changes[0].rank_jump == 8
    assert changes[0].score >= 50


def test_zero_dte_and_etf_use_higher_surge_threshold() -> None:
    prior = contract(
        "SPY260710C00754000",
        underlying="SPY",
        expiration=SESSION,
        strike="754",
        volume=50_000,
        rank=5,
    )
    current = replace(prior, volume=65_000, rank=1)
    assert detect_material_changes(snapshot(prior), snapshot(current), OptionFlowPolicy()) == ()
    surged = replace(prior, volume=71_000, rank=1)
    changes = detect_material_changes(snapshot(prior), snapshot(surged), OptionFlowPolicy())
    assert changes[0].flags == ("RANK_JUMP", "VOLUME_SURGE")


def test_unseen_contract_delta_is_indeterminate_not_a_surge() -> None:
    # 上一轮 50 名候选里不存在的合约，其全天累计量不可当作 15 分钟增量：
    # 不得触发 VOLUME_SURGE，NEW_TOP10 单独(30分)不足以出卡。
    prior = contract(
        "AAPL260717C00300000", underlying="AAPL", strike="300", volume=9_000, rank=1
    )
    unseen = contract(volume=250_000, rank=2)
    changes = detect_material_changes(
        snapshot(prior),
        snapshot(replace(prior, volume=9_100), unseen),
        OptionFlowPolicy(),
    )
    assert changes == ()


def test_high_turnover_cannot_trigger_without_rank_or_volume_change() -> None:
    enrichment = OptionEnrichment(open_interest=100, open_interest_date=SESSION)
    prior = contract(volume=10_000, rank=1, enrichment=enrichment)
    current = replace(prior, volume=10_100)
    assert detect_material_changes(snapshot(prior), snapshot(current), OptionFlowPolicy()) == ()


def test_high_turnover_flag_requires_open_interest_at_least_100() -> None:
    thin = OptionEnrichment(open_interest=99, open_interest_date=SESSION)
    prior = contract(volume=8_000, rank=12, enrichment=thin)
    current = contract(volume=19_000, rank=4, enrichment=thin)
    changes = detect_material_changes(snapshot(prior), snapshot(current), OptionFlowPolicy())
    assert "HIGH_TURNOVER" not in changes[0].flags

    deep = OptionEnrichment(open_interest=100, open_interest_date=SESSION)
    changes = detect_material_changes(
        snapshot(replace(prior, enrichment=deep)),
        snapshot(replace(current, enrichment=deep)),
        OptionFlowPolicy(),
    )
    assert "HIGH_TURNOVER" in changes[0].flags


def test_card_gate_keeps_secondary_material_changes() -> None:
    # 设计 §6：分数 >=50 是“出不出卡”的门槛；一旦出卡，
    # 通过硬门槛的次级变化(分数 <50)也应留在聚焦列表里。
    big_prior = contract(volume=8_000, rank=12)
    big_now = contract(volume=19_000, rank=4)  # 30+25+35 ≥ 50
    small_prior = contract(
        "MSFT260717P00500000", underlying="MSFT", side="put", strike="500",
        volume=9_000, rank=7,
    )
    small_now = replace(small_prior, rank=4)  # RANK_JUMP=3 → 15 分
    changes = detect_material_changes(
        snapshot(big_prior, small_prior),
        snapshot(big_now, small_now),
        OptionFlowPolicy(),
    )
    assert len(changes) == 2
    assert changes[0].contract.underlying == "NVDA"
    assert changes[1].contract.underlying == "MSFT"

    alone = detect_material_changes(
        snapshot(small_prior), snapshot(small_now), OptionFlowPolicy()
    )
    assert alone == ()


def test_display_dedupes_underlying_and_sorts_by_expiry() -> None:
    rows = (
        contract(
            "SPY260713C00750000", underlying="SPY", strike="750",
            expiration=date(2026, 7, 13), volume=30_000, rank=1,
        ),
        contract(
            "SPY260717C00755000", underlying="SPY", strike="755",
            expiration=date(2026, 7, 17), volume=25_000, rank=2,
        ),
        contract("NVDA260717C00210000", volume=20_000, rank=3),
        contract(
            "MSFT260714C00500000", underlying="MSFT", strike="500",
            expiration=date(2026, 7, 14), volume=15_000, rank=4,
        ),
    )
    shown = display_top_by_side(rows, "call", 3)
    assert [item.underlying for item in shown] == ["SPY", "MSFT", "NVDA"]
    assert shown[0].contract_symbol == "SPY260713C00750000"  # SPY 只留最高量那张


def test_display_selection_is_still_by_volume_not_expiry() -> None:
    # 近月只影响顺序不影响入选：量大的远月不能被量小的近月挤出榜。
    rows = (
        contract("NVDA260717C00210000", volume=50_000, rank=1),
        contract(
            "MSFT260713C00500000", underlying="MSFT", strike="500",
            expiration=date(2026, 7, 13), volume=1_000, rank=2,
        ),
    )
    shown = display_top_by_side(rows, "call", 1)
    assert [item.underlying for item in shown] == ["NVDA"]


def test_display_switches_can_be_disabled_independently() -> None:
    rows = (
        contract(
            "SPY260717C00755000", underlying="SPY", strike="755",
            volume=25_000, rank=1,
        ),
        contract(
            "SPY260713C00750000", underlying="SPY", strike="750",
            expiration=date(2026, 7, 13), volume=30_000, rank=2,
        ),
    )
    raw = display_top_by_side(rows, "call", 2, dedupe=False, sort_by_expiry=False)
    assert [item.rank for item in raw] == [1, 2]  # 完全回退旧展示
    deduped = display_top_by_side(rows, "call", 2, sort_by_expiry=False)
    assert len(deduped) == 1 and deduped[0].volume == 30_000


def test_display_dedupe_tie_breaks_by_contract_symbol() -> None:
    rows = (
        contract("SPY260717C00755000", underlying="SPY", strike="755", volume=10_000, rank=1),
        contract("SPY260717C00750000", underlying="SPY", strike="750", volume=10_000, rank=2),
    )
    shown = display_top_by_side(rows, "call", 2, sort_by_expiry=False)
    assert [item.contract_symbol for item in shown] == ["SPY260717C00750000"]


def test_focus_limits_each_underlying_to_two_contracts() -> None:
    previous_rows = []
    current_rows = []
    for index, strike in enumerate(("750", "751", "752"), start=1):
        symbol = f"SPY260717C{int(Decimal(strike) * 1000):08d}"
        previous_rows.append(
            contract(symbol, underlying="SPY", strike=strike, volume=1_000, rank=20 + index)
        )
        current_rows.append(
            contract(symbol, underlying="SPY", strike=strike, volume=30_000, rank=index)
        )
    changes = detect_material_changes(
        snapshot(*previous_rows), snapshot(*current_rows), OptionFlowPolicy()
    )
    assert len(changes) == 2
