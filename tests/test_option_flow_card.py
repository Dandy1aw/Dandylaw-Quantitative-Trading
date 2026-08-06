from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from quant_signal.notifier.base import CardKind
from quant_signal.notifier.cards import option_flow_card
from quant_signal.options_flow import (
    HoldingOptionFlow,
    HoldingOptionFlowSnapshot,
    OptionContractVolume,
    OptionEnrichment,
    OptionFlowChange,
    OptionFlowSnapshot,
    scan_slot,
)

NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)


def row(side: str, rank: int) -> OptionContractVolume:
    root = ("AAPL", "NVDA", "MSFT", "TSLA", "AMZN", "META", "GOOGL", "AMD", "MU", "INTC")[rank - 1]
    strike = Decimal(200 + rank)
    marker = "C" if side == "call" else "P"
    enrichment = (
        OptionEnrichment(
            latest_trade_price=Decimal("2.50"),
            open_interest=1_000,
            open_interest_date=date(2026, 7, 9),
            multiplier=100,
        )
        if rank == 1
        else None
    )
    return OptionContractVolume(
        contract_symbol=f"{root}260717{marker}{int(strike * 1000):08d}",
        underlying=root,
        side=side,  # type: ignore[arg-type]
        expiration=date(2026, 7, 17),
        strike=strike,
        volume=20_000 - rank * 500,
        rank=rank,
        venues=("cone", "ctwo", "exo", "opt"),
        captured_at=NOW,
        enrichment=enrichment,
    )


def snapshot() -> OptionFlowSnapshot:
    rows = tuple(row(side, rank) for side in ("call", "put") for rank in range(1, 11))
    return OptionFlowSnapshot(
        slot=scan_slot(NOW),
        captured_at=NOW,
        provider="cboe-four-venues",
        venue_coverage=1.0,
        rows=rows,
    )


def test_option_flow_card_has_one_underlying_section_with_full_side_names() -> None:
    original = snapshot()
    spy_call = replace(
        original.rows[0],
        underlying="SPY",
        contract_symbol="SPY260717C00750000",
        strike=Decimal("750"),
    )
    spy_put = replace(
        original.rows[10],
        underlying="SPY",
        contract_symbol="SPY260717P00740000",
        strike=Decimal("740"),
    )
    current = replace(
        original,
        rows=(spy_call, *original.rows[1:10], spy_put, *original.rows[11:]),
    )
    card = option_flow_card(
        current, (), "baseline", etf_underlyings=frozenset({"SPY"})
    )

    assert card.kind is CardKind.REPORT
    assert "Cboe四市场" in card.title
    assert len(card.sections) == 4
    assert "标的热度 Top10" in card.body_md
    assert "CALL Top10" not in card.body_md and "PUT Top10" not in card.body_md
    assert "#1 SPY · ETF" in card.body_md
    assert "Call 19,500 / Put 19,500 · 总量 39,000" in card.body_md
    assert "可见量 Call/Put 比 1.00 · Call/Put 相对均衡 0%" in card.body_md
    assert "首次可见，无 15 分钟可比增量" in card.body_md
    assert "|---" not in card.body_md
    assert "INDICATIVE" in card.body_md and "约15分钟" in card.body_md
    assert "Call成交不等于看涨" in card.body_md
    assert "不是全 OPRA 总量" in card.body_md
    assert len(card.body_md) <= 3_500


def test_change_card_renders_rank_delta_turnover_and_premium() -> None:
    current = snapshot()
    first = current.rows[0]
    change = OptionFlowChange(
        contract=first,
        previous_rank=12,
        volume_delta=11_000,
        rank_jump=11,
        flags=("NEW_TOP10", "RANK_JUMP", "VOLUME_SURGE", "HIGH_TURNOVER"),
        score=100,
    )
    previous = replace(
        current,
        rows=tuple(replace(item, volume=item.volume - 1_000) for item in current.rows),
    )

    card = option_flow_card(current, (change,), "change", previous=previous)

    assert card.kind is CardKind.SIGNAL
    assert "新进Top10" in card.body_md
    assert "#12→#1" in card.body_md
    assert "+11,000/15m" in card.body_md
    assert "V/OI" in card.body_md
    assert "估算权利金" in card.body_md
    assert "15 分钟增量 Call +1,000 / Put +1,000" in card.body_md


def test_first_seen_contract_renders_indeterminate_delta() -> None:
    current = snapshot()
    first = current.rows[0]
    change = OptionFlowChange(
        contract=first,
        previous_rank=None,
        volume_delta=None,
        rank_jump=0,
        flags=("NEW_TOP10",),
        score=55,
    )
    previous = replace(current, rows=current.rows[1:])

    card = option_flow_card(current, (change,), "change", previous=previous)

    assert "首次可见" in card.body_md
    assert f"+{first.volume:,}/15m" not in card.body_md
    assert "基线" not in card.body_md


def test_card_marks_degraded_enrichment_without_guessing() -> None:
    card = option_flow_card(
        snapshot(), (), "close", enrichment_status="failed"
    )
    assert "Alpaca补全失败" in card.body_md
    assert "V/OI 0" not in card.body_md


def test_card_distinguishes_unconfigured_enrichment_from_failure() -> None:
    card = option_flow_card(
        snapshot(), (), "close", enrichment_status="off"
    )
    assert "未配置Alpaca补全" in card.body_md
    assert "失败" not in card.body_md


def test_underlying_section_sums_multiple_contracts_once() -> None:
    current = snapshot()
    spy_near = replace(
        current.rows[0],
        underlying="SPY",
        contract_symbol="SPY260713C00750000",
        strike=Decimal("750"),
        expiration=date(2026, 7, 13),
        volume=40_000,
        rank=1,
    )
    spy_far = replace(
        current.rows[1],
        underlying="SPY",
        contract_symbol="SPY260717C00755000",
        strike=Decimal("755"),
        volume=39_000,
        rank=2,
    )
    spy_put = replace(
        current.rows[10],
        underlying="SPY",
        contract_symbol="SPY260717P00740000",
        strike=Decimal("740"),
        volume=20_000,
        rank=1,
    )
    rows = (spy_near, spy_far, *current.rows[2:10], spy_put, *current.rows[11:])
    card = option_flow_card(
        replace(current, rows=rows),
        (),
        "baseline",
        etf_underlyings=frozenset({"SPY"}),
    )
    body = card.body_md

    assert body.count("SPY") == 1
    assert "#1 SPY · ETF" in body
    assert "Call 79,000 / Put 20,000 · 总量 99,000" in body
    assert "可见量 Call/Put 比 3.95 · Call 显著占优 60%" in body


def test_underlying_section_marks_partial_comparable_delta() -> None:
    current = snapshot()
    existing = current.rows[0]
    new_call = replace(
        current.rows[1],
        underlying=existing.underlying,
        contract_symbol="AAPL260717C00310000",
        strike=Decimal("310"),
        volume=40_000,
    )
    prior = replace(current, rows=(replace(existing, volume=10_000), *current.rows[2:]))
    present = replace(
        current,
        rows=(replace(existing, volume=19_500), new_call, *current.rows[2:]),
    )
    card = option_flow_card(
        present,
        (),
        "change",
        previous=prior,
    )
    assert "15 分钟已知增量 Call +9,500（部分可比）" in card.body_md


def test_strike_display_strips_trailing_zeros() -> None:
    current = snapshot()
    padded = replace(current.rows[0], strike=Decimal("201.000"))
    fractional = replace(
        current.rows[1],
        strike=Decimal("61.020"),
        contract_symbol="NVDA260717C00061020",
    )
    changes = (
        OptionFlowChange(padded, None, None, 0, ("NEW_TOP10",), 55),
        OptionFlowChange(fractional, None, None, 0, ("NEW_TOP10",), 55),
    )
    card = option_flow_card(
        replace(current, rows=(padded, fractional) + current.rows[2:]),
        changes,
        "change",
    )
    assert "201 Call" in card.body_md and "201.000" not in card.body_md
    assert "61.02 Call" in card.body_md and "61.020" not in card.body_md


def test_card_lists_all_observed_holdings_before_market_top10() -> None:
    holdings = HoldingOptionFlowSnapshot(
        slot=scan_slot(NOW),
        captured_at=NOW,
        provider="alpaca-option-snapshots",
        rows=(
            HoldingOptionFlow("NVDA", 82_140, 61_900, 8_240, 3_110, "ok"),
            HoldingOptionFlow("SKHY", 0, 0, None, None, "no_chain"),
            HoldingOptionFlow("MRVL", 0, 0, None, None, "unavailable"),
        ),
    )

    card = option_flow_card(snapshot(), (), "baseline", holding_snapshot=holdings)

    assert "📌 我的持仓期权" in card.body_md
    assert card.body_md.index("📌 我的持仓期权") < card.body_md.index("标的热度 Top10")
    assert "Call 82,140 / Put 61,900 · 总量 144,040" in card.body_md
    assert "Call/Put 1.33 · Call占优 14%" in card.body_md
    assert "15分钟增量 Call +8,240 / Put +3,110" in card.body_md
    assert "SKHY · 无可用期权链" in card.body_md
    assert "MRVL · 期权数据暂不可用" in card.body_md
