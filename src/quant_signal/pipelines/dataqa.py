"""S6 数据体检 pipeline：每日对比 Alpaca(IEX) vs yfinance 美股收盘，偏差告警。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.dataqa import divergences, latest_common_closes
from quant_signal.notifier.cards import alert_card

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

def _concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


def _symbols(engine: Engine, now: datetime) -> list[str]:
    output = {
        ticker
        for ticker in engine.settings.universe
        if ticker not in engine.settings.international_tickers
    }
    provider = engine.index_universe_provider
    if provider is not None:
        try:
            output.update(provider.load(now).symbols)
        except Exception as error:  # noqa: BLE001 - static universe remains usable
            log.warning("dataqa.universe_failed", error=str(error))
    return sorted(
        ticker
        for ticker in output
        if ticker not in engine.settings.international_tickers
    )


def run(engine: Engine, now: datetime) -> bool:
    if engine.settings.data_source != "alpaca":
        log.info("dataqa.skip", reason="single_source")
        return True
    us = _symbols(engine, now)
    if not us:
        log.warning("dataqa.skip", reason="empty_universe")
        return False
    cfg = engine.settings.data_qa
    start, end = (now - timedelta(days=cfg.lookback_days)).date(), now.date()
    primary_frames: list[pd.DataFrame] = []
    secondary_frames: list[pd.DataFrame] = []
    failed: set[str] = set()
    for offset in range(0, len(us), cfg.batch_size):
        batch = us[offset : offset + cfg.batch_size]
        primary_batch: pd.DataFrame | None = None
        secondary_batch: pd.DataFrame | None = None
        try:
            primary_batch = engine.source.fetch_daily_bars(batch, start, end)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "dataqa.batch_failed",
                source="primary",
                offset=offset,
                size=len(batch),
                error=str(error),
            )
        try:
            secondary_batch = engine._intl_source.fetch_daily_bars(batch, start, end)
        except Exception as error:  # noqa: BLE001
            log.warning(
                "dataqa.batch_failed",
                source="secondary",
                offset=offset,
                size=len(batch),
                error=str(error),
            )
        if primary_batch is None or secondary_batch is None:
            failed.update(batch)
            continue
        primary_frames.append(primary_batch)
        secondary_frames.append(secondary_batch)
    primary_all = _concat(primary_frames)
    secondary_all = _concat(secondary_frames)
    pairs = latest_common_closes(primary_all, secondary_all)
    hits = divergences(pairs, cfg.divergence_threshold)
    delivered = True
    if hits:
        lines = ["| 标的 | Alpaca(IEX) | yfinance | 偏差 |", "|---|---|---|---|"]
        for ticker, pa, pb, pct in hits[:10]:
            lines.append(f"| {ticker} | {pa:.2f} | {pb:.2f} | {pct:.2%} |")
        lines.append("")
        lines.append("IEX 免费源只含约2-3%成交量；偏差大的标的动量输入不可靠，建议核对。")
        delivered = engine.notifier.send(
            alert_card("双源收盘价偏差", "\n".join(lines))
        )
    coverage = len(pairs) / len(us)
    log.info(
        "dataqa.done",
        universe=len(us),
        checked=len(pairs),
        missing=len(us) - len(pairs),
        failed_batches_symbols=len(failed),
        coverage=coverage,
        required_coverage=cfg.min_coverage,
        diverged=len(hits),
        healthy=delivered and coverage >= cfg.min_coverage,
    )
    return delivered and coverage >= cfg.min_coverage
