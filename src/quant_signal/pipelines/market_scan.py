"""T2 全市场扫描 pipeline：每交易日从全美股找 Top1 候选推送并入台账受检验。

两段式控制成本：全量代码→近5日日线(分块批量)→流动性前500→130日历史→三因子打分。
诚实定位：候选发现器，非"必涨"承诺；Top1 写入台账(strategy_id=market_scan)，
绩效周报会持续汇报它的真实胜率。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.notifier.cards import report_card
from quant_signal.scanner import ScanResult, liquidity_filter, scan_scores
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.indicators import chandelier_stop, entry_hint

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

STRATEGY_ID = "market_scan"
CHUNK = 200
TOP_LIQUID = 500


def _chunked_daily(engine: Engine, symbols: list[str], start: date, end: date) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        try:
            frames.append(engine.source.fetch_daily_bars(chunk, start, end))
        except Exception as error:  # noqa: BLE001
            log.warning("market_scan.chunk_failed", offset=i, error=str(error))
    valid = [f for f in frames if not f.empty]
    return pd.concat(valid).sort_index() if valid else pd.DataFrame()


def _card_body(top: list[ScanResult], extra_note: str) -> str:
    first = top[0]
    lines = [
        f"**今日 Top1：{first.ticker}**  现价 {first.price:.2f}",
        f"- 60日动量 {first.momentum_60d:+.1%} · 距20日高 {first.high20_proximity:.0%} · "
        f"5日量比 {first.volume_ratio:.1f}x",
        extra_note,
        "",
        "| 排名 | 标的 | 综合分 | 60日动量 | 距20日高 | 量比 | 现价 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, start=1):
        lines.append(
            f"| {i} | {r.ticker} | {r.score:+.2f} | {r.momentum_60d:+.1%} |"
            f" {r.high20_proximity:.0%} | {r.volume_ratio:.1f}x | {r.price:.2f} |"
        )
    lines += [
        "",
        "> 候选发现器：可解释规则打分(动量40%/近高30%/量比30%)，非'必涨'预测；"
        "Top1 已入虚拟盘，胜率见每周绩效周报。",
    ]
    return "\n".join(lines)


def run(engine: Engine, now: datetime) -> None:
    lister = getattr(engine.source, "list_active_symbols", None)
    if lister is None:
        log.info("market_scan.skip", reason="source_without_asset_list")
        return
    symbols = [s for s in lister() if s not in engine.settings.international_tickers]
    if not symbols:
        log.info("market_scan.skip", reason="no_symbols")
        return

    bars5 = _chunked_daily(engine, symbols, (now - timedelta(days=9)).date(), now.date())
    liquid = liquidity_filter(bars5, top_k=TOP_LIQUID)
    if not liquid:
        log.info("market_scan.skip", reason="no_liquid_candidates")
        return
    bars = _chunked_daily(engine, liquid, (now - timedelta(days=210)).date(), now.date())
    results = scan_scores(bars)
    if not results:
        log.info("market_scan.skip", reason="no_scores")
        return

    top = results[:5]
    first = top[0]
    sub = bars.xs(first.ticker, level="ticker").sort_index()
    hint = entry_hint(sub["high"], sub["low"], sub["close"])
    stop = chandelier_stop(sub["high"], sub["low"], sub["close"])
    extra: dict[str, object] = {
        "momentum_60d": first.momentum_60d,
        "high20_proximity": first.high20_proximity,
        "volume_ratio": first.volume_ratio,
    }
    notes: list[str] = []
    if hint is not None:
        target_buy = round(hint[0], 2)
        extra["target_buy"] = target_buy
        notes.append(f"目标买入价 {target_buy:.2f}" + ("（⚠短线过热,建议等回踩）" if hint[2] else ""))
        if hint[2]:
            extra["overheat"] = True
    if stop is not None:
        extra["stop_loss"] = round(stop, 2)
        notes.append(f"止损参考 {stop:.2f}")
    note = "- " + " · ".join(notes) if notes else ""

    delivered = engine.notifier.send(report_card("🔎 全市场扫描 · 今日Top1", _card_body(top, note)))
    engine.ledger.insert(
        Signal(
            ticker=first.ticker,
            direction=Direction.BUY,
            price=first.price,
            reason=f"全市场扫描Top1：60日动量{first.momentum_60d:+.1%}，距20日高{first.high20_proximity:.0%}",
            strategy_id=STRATEGY_ID,
            ts=now,
            extra=extra,
        ),
        pushed=delivered,
        now=now,
    )
    log.info(
        "market_scan.done",
        universe=len(symbols),
        liquid=len(liquid),
        scored=len(results),
        top1=first.ticker,
    )
