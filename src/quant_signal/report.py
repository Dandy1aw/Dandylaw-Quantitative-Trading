"""收盘复盘卡：截图持仓的当日表现 + 信号计数。

替代旧"信号日报"（同日信号价 vs 收盘的"理论收益"只是当天涨跌噪音，
真正的信号质量评估在 performance 周报）。缺数据一律显示 '-'，不编造。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import report_card


@dataclass(frozen=True)
class PositionRecap:
    """单个截图持仓在收盘时点的表现；None 字段展示为 '-'。"""

    symbol: str
    close: Decimal | None
    day_change_pct: float | None
    position_pnl_pct: float | None
    market_value: Decimal | None
    international: bool = False
    pnl_as_of_screenshot: bool = False  # 缺股数/成本时退回截图时点盈亏


@dataclass(frozen=True)
class SignalTally:
    total: int
    buys: int
    sells: int
    pushed: int


def _pct(value: float | None) -> str:
    return f"{value:+.1%}" if value is not None else "-"


def _money(value: Decimal | None) -> str:
    return f"{value:,.2f}" if value is not None else "-"


def build_close_recap(
    recaps: Sequence[PositionRecap],
    *,
    session: date,
    tally: SignalTally,
    observed_at: datetime | None,
) -> Card:
    title = f"📊 收盘复盘 · {session.isoformat()}"
    lines: list[str] = []
    if recaps:
        lines += [
            "**持仓表现**",
            "",
            "| 标的 | 收盘 | 当日 | 持仓盈亏 | 市值 |",
            "|---|---|---|---|---|",
        ]
        for r in recaps:
            symbol = f"{r.symbol}†" if r.international else r.symbol
            pnl = _pct(r.position_pnl_pct)
            if r.position_pnl_pct is not None and r.pnl_as_of_screenshot:
                pnl += "*"
            lines.append(
                f"| {symbol} | {_money(r.close)} | {_pct(r.day_change_pct)} |"
                f" {pnl} | {_money(r.market_value)} |"
            )
        usd = [
            r for r in recaps if not r.international and r.market_value is not None
        ]
        summary: list[str] = []
        if usd:
            total_mv = sum(
                (r.market_value or Decimal("0") for r in usd), Decimal("0")
            )
            summary.append(f"市值合计 ${total_mv:,.0f}")
        weighted = [r for r in usd if r.day_change_pct is not None]
        if weighted:
            moved = sum(
                float(r.market_value or 0) * float(r.day_change_pct or 0.0)
                for r in weighted
            )
            base = sum(float(r.market_value or 0) for r in weighted)
            summary.append(f"当日 {moved / base:+.1%}（按市值加权）")
        if summary:
            lines += ["", "**组合(USD)** " + " · ".join(summary)]
    else:
        lines.append("台账无持仓截图记录。")
    lines += [
        "",
        f"**今日信号** {tally.total} 个 · BUY {tally.buys} / SELL {tally.sells}"
        f" · 已推送 {tally.pushed}",
    ]
    notes = ["持仓盈亏=收盘价 vs 截图成本"]
    if observed_at is not None:
        notes.insert(0, f"持仓快照 {observed_at:%m-%d %H:%M}")
    if any(r.pnl_as_of_screenshot for r in recaps):
        notes.append("* 截图时点盈亏（截图缺股数/成本明细）")
    if any(r.international for r in recaps):
        notes.append("† 非USD标的，不计入市值合计")
    lines += ["", "> " + " · ".join(notes)]
    return report_card(title, "\n".join(lines))
