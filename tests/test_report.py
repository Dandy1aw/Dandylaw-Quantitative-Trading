"""收盘复盘卡（纯构建）：持仓表现表、组合小结、信号计数、数据边界脚注。"""

from datetime import date, datetime, timezone
from decimal import Decimal

from quant_signal.notifier.base import CardKind
from quant_signal.report import PositionRecap, SignalTally, build_close_recap

SESSION = date(2026, 7, 10)
OBSERVED_AT = datetime(2026, 7, 10, 13, 15, tzinfo=timezone.utc)


def recap(
    symbol: str = "MU",
    *,
    close: str | None = "105",
    day: float | None = 0.05,
    pnl: float | None = 0.20,
    mv: str | None = "1050",
    international: bool = False,
) -> PositionRecap:
    return PositionRecap(
        symbol=symbol,
        close=Decimal(close) if close is not None else None,
        day_change_pct=day,
        position_pnl_pct=pnl,
        market_value=Decimal(mv) if mv is not None else None,
        international=international,
    )


def tally(total: int = 9, buys: int = 7, sells: int = 2, pushed: int = 1) -> SignalTally:
    return SignalTally(total=total, buys=buys, sells=sells, pushed=pushed)


def test_renders_position_rows_and_weighted_summary() -> None:
    card = build_close_recap(
        [
            recap("MU", close="105", day=0.05, pnl=0.20, mv="1050"),
            recap("SMH", close="200", day=-0.01, pnl=-0.10, mv="2100"),
        ],
        session=SESSION,
        tally=tally(),
        observed_at=OBSERVED_AT,
    )
    assert card.kind is CardKind.REPORT
    assert "收盘复盘" in card.title and "2026-07-10" in card.title
    assert "MU" in card.body_md and "SMH" in card.body_md
    assert "+5.0%" in card.body_md and "-1.0%" in card.body_md
    assert "+20.0%" in card.body_md and "-10.0%" in card.body_md
    # 加权当日: (1050*0.05 + 2100*-0.01) / 3150 = +1.0%
    assert "+1.0%" in card.body_md
    # USD 合计市值
    assert "3,150" in card.body_md
    # 截图时间入脚注
    assert "07-10" in card.body_md


def test_missing_values_render_dash_and_skip_summary() -> None:
    card = build_close_recap(
        [recap("RAM", close=None, day=None, pnl=None, mv=None)],
        session=SESSION,
        tally=tally(),
        observed_at=OBSERVED_AT,
    )
    assert "RAM" in card.body_md
    assert "-" in card.body_md
    # 没有可加权的行时不给组合当日涨跌编数字
    assert "加权" not in card.body_md


def test_international_positions_excluded_from_usd_total() -> None:
    card = build_close_recap(
        [
            recap("MU", mv="1000", day=0.10),
            recap("000660.KS", mv="99999", day=0.10, international=True),
        ],
        session=SESSION,
        tally=tally(),
        observed_at=OBSERVED_AT,
    )
    assert "1,000" in card.body_md
    assert "100,999" not in card.body_md
    assert "非USD" in card.body_md or "国际" in card.body_md


def test_signal_tally_line() -> None:
    card = build_close_recap(
        [recap()], session=SESSION, tally=tally(9, 7, 2, 1), observed_at=OBSERVED_AT
    )
    assert "9" in card.body_md
    assert "BUY 7" in card.body_md
    assert "SELL 2" in card.body_md


def test_no_positions_still_reports_signals() -> None:
    card = build_close_recap(
        [], session=SESSION, tally=tally(3, 3, 0, 0), observed_at=None
    )
    assert "无持仓" in card.body_md
    assert "BUY 3" in card.body_md
