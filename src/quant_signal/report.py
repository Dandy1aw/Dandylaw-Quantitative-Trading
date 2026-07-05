from __future__ import annotations

from datetime import date

from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.notifier.cards import report_card


def build_daily_report(ledger: SignalLedger, store: BarStore, day: date) -> Card:
    rows = ledger.signals_on(day)
    title = f"📊 信号日报 {day.isoformat()}"
    if not rows:
        return report_card(title, "今日无信号。")

    pushed = sum(1 for r in rows if r["pushed"])
    lines = [
        f"**信号总数**: {len(rows)}　**已推送**: {pushed}",
        "",
        "| 时间(UTC) | 标的 | 方向 | 信号价 | 理论收益 | 策略 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        ret = "-"
        if r["direction"] == "buy":
            bars = store.read_daily_bars([str(r["ticker"])])
            if not bars.empty:
                last_close = float(bars["close"].iloc[-1])
                ret = f"{(last_close - float(r['price'])) / float(r['price']):+.1%}"
        hhmm = str(r["ts"])[11:16]
        lines.append(
            f"| {hhmm} | {r['ticker']} | {str(r['direction']).upper()} |"
            f" {float(r['price']):.2f} | {ret} | {r['strategy_id']} |"
        )
    return report_card(title, "\n".join(lines))
