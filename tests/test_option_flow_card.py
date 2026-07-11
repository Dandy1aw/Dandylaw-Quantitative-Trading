from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

from quant_signal.notifier.base import CardKind
from quant_signal.notifier.cards import option_flow_card
from quant_signal.options_flow import (
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


def test_option_flow_card_has_two_top10_sections_without_tables() -> None:
    current = snapshot()
    card = option_flow_card(current, (), "baseline", NOW)

    assert card.kind is CardKind.REPORT
    assert "Cboe四市场" in card.title
    assert len(card.sections) == 5
    assert "CALL Top10" in card.body_md and "PUT Top10" in card.body_md
    assert "|---" not in card.body_md
    assert "INDICATIVE" in card.body_md and "约15分钟" in card.body_md
    assert "Call成交不等于看涨" in card.body_md
    assert "可见榜单量下限近似" in card.body_md
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

    card = option_flow_card(current, (change,), "change", NOW, previous=previous)

    assert card.kind is CardKind.SIGNAL
    assert "新进Top10" in card.body_md
    assert "#12→#1" in card.body_md
    assert "+11,000/15m" in card.body_md
    assert "V/OI" in card.body_md
    assert "估算权利金" in card.body_md
    assert "+1,000/15m" in card.body_md


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

    card = option_flow_card(current, (change,), "change", NOW, previous=previous)

    assert "首次可见" in card.body_md
    assert f"+{first.volume:,}/15m" not in card.body_md
    assert "基线" not in card.body_md


def test_card_marks_degraded_enrichment_without_guessing() -> None:
    card = option_flow_card(
        snapshot(), (), "close", NOW, enrichment_available=False
    )
    assert "Alpaca补全失败" in card.body_md
    assert "V/OI 0" not in card.body_md
