"""S6 数据体检 pipeline：每日对比 Alpaca(IEX) vs yfinance 美股收盘，偏差告警。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from quant_signal.dataqa import divergences, latest_common_closes
from quant_signal.notifier.cards import alert_card

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

THRESHOLD = 0.005   # 0.5%


def run(engine: Engine, now: datetime) -> None:
    if engine.settings.data_source != "alpaca":
        log.info("dataqa.skip", reason="single_source")
        return
    us = [
        t for t in engine.settings.universe
        if t not in engine.settings.international_tickers
    ]
    start, end = (now - timedelta(days=7)).date(), now.date()
    try:
        primary = engine.source.fetch_daily_bars(us, start, end)
        secondary = engine._intl_source.fetch_daily_bars(us, start, end)
    except Exception as error:  # noqa: BLE001
        log.warning("dataqa.fetch_failed", error=str(error))
        return
    pairs = latest_common_closes(primary, secondary)
    hits = divergences(pairs, THRESHOLD)
    if hits:
        lines = ["| 标的 | Alpaca(IEX) | yfinance | 偏差 |", "|---|---|---|---|"]
        for ticker, pa, pb, pct in hits[:10]:
            lines.append(f"| {ticker} | {pa:.2f} | {pb:.2f} | {pct:.2%} |")
        lines.append("")
        lines.append("IEX 免费源只含约2-3%成交量；偏差大的标的动量输入不可靠，建议核对。")
        engine.notifier.send(alert_card("双源收盘价偏差", "\n".join(lines)))
    log.info("dataqa.done", checked=len(pairs), diverged=len(hits))
