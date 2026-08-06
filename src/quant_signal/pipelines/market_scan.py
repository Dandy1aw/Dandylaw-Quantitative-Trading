"""T2 全市场扫描 pipeline：每交易日从全美股找 Top1 候选推送并入台账受检验。

两段式控制成本：全量代码→近5日日线(分块批量)→流动性前500→130日历史→三因子打分。
诚实定位：候选发现器，非"必涨"承诺；Top1 写入台账(strategy_id=market_scan)，
绩效周报会持续汇报它的真实胜率。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import TYPE_CHECKING, Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.notifier.cards import report_card
from quant_signal.calendar import previous_trading_day
from quant_signal.scanner import (
    MIN_HISTORY,
    ScanResult,
    liquidity_filter,
    scan_scores,
    valid_ohlcv_bars,
    validate_scan_bars,
)
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.indicators import (
    chandelier_stop,
    entry_hint,
    expected_move_target,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

STRATEGY_ID = "market_scan"
CHUNK = 200
TOP_LIQUID = 500
# 指数扫描总 deadline: 超时在 chunk 之间中止并 fail closed, 不得挤占 08:00 早报
SCAN_DEADLINE_SECONDS = 600.0


def _standalone_card_enabled(engine: Engine) -> bool:
    briefing = engine.settings.us_briefing
    return not engine.settings.notify.action_card_only and not (
        briefing.enabled and briefing.delivery_mode == "live"
    )


class _DailySource(Protocol):
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame: ...


class _SipDailySource(Protocol):
    def fetch_sip_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame: ...


def _scan_daily_source(engine: Engine) -> tuple[_DailySource, str]:
    """部分市场成交量源只负责列举代码；扫描日线改用完整成交量源。"""
    if bool(getattr(engine.source, "partial_market_volume", False)):
        return cast(_DailySource, engine._intl_source), "yfinance"
    return cast(_DailySource, engine.source), engine.settings.data_source


def _chunked_daily(
    source: _DailySource, symbols: list[str], start: date, end: date
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        try:
            frames.append(source.fetch_daily_bars(chunk, start, end))
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
        "**观察榜 Top5**",
    ]
    for i, r in enumerate(top[:5], start=1):
        lines.append(
            f"{i}. **{r.ticker}** · 分数 {r.score:+.2f} · 动量 {r.momentum_60d:+.1%} ·"
            f" 近高 {r.high20_proximity:.0%} · 量比 {r.volume_ratio:.1f}x · {r.price:.2f}"
        )
    lines += [
        "",
        "> 候选发现器：截尾百分位打分(动量40%/近高30%/量比30%)，非'必涨'预测；"
        "Top1 按次日开盘入场、持有20个交易日的固定口径接受周报检验。",
    ]
    return "\n".join(lines)


def _fetch_sip_chunks(
    engine: Engine,
    source: _SipDailySource,
    symbols: list[str],
    start: date,
    end: date,
    deadline_monotonic: float | None = None,
) -> bool:
    """按 chunk 拉取 SIP 日线; 到达总 deadline 时在 chunk 之间中止, 返回是否完整完成。"""
    import time as _time

    for offset in range(0, len(symbols), CHUNK):
        if deadline_monotonic is not None and _time.monotonic() >= deadline_monotonic:
            log.warning(
                "market_scan.deadline_exceeded",
                offset=offset,
                total=len(symbols),
            )
            return False
        chunk = symbols[offset : offset + CHUNK]
        try:
            fetched = source.fetch_sip_daily_bars(chunk, start, end)
        except Exception as error:  # noqa: BLE001
            log.warning("market_scan.sip_chunk_failed", offset=offset, error=str(error))
            continue
        valid = valid_ohlcv_bars(fetched)
        if not valid.empty:
            engine.store.write_daily_bars(valid, source="alpaca_sip")
    return True


def _cached_scan_window(engine: Engine, symbols: list[str], as_of: date) -> pd.DataFrame:
    end = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    bars = engine.store.read_daily_bars(
        symbols, start=end - timedelta(days=400), end=end
    )
    if bars.empty:
        return bars
    return bars.groupby(level="ticker", group_keys=False, sort=False).tail(210).sort_index()


def _execution_levels(bars: pd.DataFrame) -> dict[str, object]:
    hint = entry_hint(bars["high"], bars["low"], bars["close"])
    stop = chandelier_stop(bars["high"], bars["low"], bars["close"])
    take_profit = expected_move_target(bars["close"])
    if hint is None or stop is None or take_profit is None:
        return {"block_reason": "MISSING_EXECUTION_LEVELS"}
    entry_low, entry_high, overheated = hint
    levels = {
        "entry_low": round(entry_low, 2),
        "entry_high": round(entry_high, 2),
        "target_buy": round(entry_low, 2),
        "stop_loss": round(stop, 2),
        "take_profit": round(take_profit, 2),
    }
    ordered = (
        all(math.isfinite(float(value)) and float(value) > 0 for value in levels.values())
        and float(levels["stop_loss"])
        < float(levels["entry_low"])
        <= float(levels["entry_high"])
        < float(levels["take_profit"])
    )
    output: dict[str, object] = dict(levels)
    if overheated:
        output["overheat"] = True
    if not ordered:
        output["block_reason"] = "INVALID_PRICE_ORDER"
    return output


def _run_index_scan(engine: Engine, now: datetime) -> bool:
    market_as_of = previous_trading_day(
        now.astimezone(ZoneInfo("America/New_York")).date()
    )
    provider = engine.index_universe_provider
    sip_fetcher = getattr(engine.source, "fetch_sip_daily_bars", None)
    if provider is None or sip_fetcher is None:
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.warning("market_scan.index_skip", reason="missing_index_or_sip_provider")
        return False
    try:
        snapshot = provider.load(now)
    except Exception as error:  # noqa: BLE001
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.warning("market_scan.index_skip", reason="universe_unavailable", error=str(error))
        return False
    symbols = list(snapshot.symbols)
    if not symbols:
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.warning("market_scan.index_skip", reason="empty_index_universe")
        return False
    cached_before = _cached_scan_window(engine, symbols, market_as_of)
    counts = (
        cached_before.groupby(level="ticker").size().to_dict()
        if not cached_before.empty
        else {}
    )
    bootstrap = [symbol for symbol in symbols if int(counts.get(symbol, 0)) < MIN_HISTORY]
    incremental = [symbol for symbol in symbols if symbol not in set(bootstrap)]
    source = cast(_SipDailySource, engine.source)
    import time as _time

    deadline = _time.monotonic() + SCAN_DEADLINE_SECONDS
    completed = True
    if bootstrap:
        completed = _fetch_sip_chunks(
            engine,
            source,
            bootstrap,
            market_as_of - timedelta(days=320),
            market_as_of,
            deadline_monotonic=deadline,
        )
    if completed and incremental:
        completed = _fetch_sip_chunks(
            engine,
            source,
            incremental,
            market_as_of - timedelta(days=14),
            market_as_of,
            deadline_monotonic=deadline,
        )
    if not completed:
        # 超时 fail closed: 已写入的 bar 保留, 但当日不产生候选
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.warning("market_scan.index_skip", reason="deadline_exceeded")
        return False
    bars = _cached_scan_window(engine, symbols, market_as_of)
    validation = validate_scan_bars(bars, symbols, market_as_of)
    config = engine.settings.index_universe
    if validation.coverage < config.min_coverage:
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.warning(
            "market_scan.index_coverage_failed",
            coverage=validation.coverage,
            required=config.min_coverage,
            rejected=dict(validation.rejected),
        )
        return False
    liquid = liquidity_filter(
        validation.bars,
        min_dollar_volume=config.min_dollar_volume,
        top_k=len(validation.valid_symbols),
    )
    if not liquid:
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.info("market_scan.index_skip", reason="no_liquid_candidates")
        return True
    liquid_bars = validation.bars.loc[
        validation.bars.index.get_level_values("ticker").isin(liquid)
    ]
    results = scan_scores(liquid_bars)
    top = results[: config.scan_top_n]
    if not top:
        engine.ledger.replace_scan_candidates(now.date(), [], as_of=market_as_of)
        log.info("market_scan.index_skip", reason="no_scores")
        return True
    candidate_rows: list[Mapping[str, object]] = []
    extras: list[dict[str, object]] = []
    for rank, result in enumerate(top, start=1):
        extra: dict[str, object] = {
            "memberships": list(snapshot.members[result.ticker].memberships),
            "market_as_of": market_as_of.isoformat(),
            "momentum_60d": result.momentum_60d,
            "high20_proximity": result.high20_proximity,
            "volume_ratio": result.volume_ratio,
        }
        if rank <= config.execution_top_n:
            sub = cast(
                pd.DataFrame,
                validation.bars.xs(result.ticker, level="ticker").sort_index(),
            )
            extra.update(_execution_levels(sub))
        extras.append(extra)
        candidate_rows.append(
            {
                "ticker": result.ticker,
                "rank": rank,
                "score": result.score,
                "price": result.price,
                "extra": extra,
            }
        )
    engine.ledger.replace_scan_candidates(
        now.date(), candidate_rows, as_of=market_as_of
    )
    delivered = False
    if _standalone_card_enabled(engine):
        delivered = engine.notifier.send(
            report_card(
                "指数发现池 · 今日候选",
                _card_body(
                    top,
                    f"- 行情日 {market_as_of.isoformat()} · 覆盖率 {validation.coverage:.1%}",
                ),
            )
        )
    first = top[0]
    engine.ledger.insert(
        Signal(
            ticker=first.ticker,
            direction=Direction.BUY,
            price=first.price,
            reason=f"指数发现池Top1，60日动量{first.momentum_60d:+.1%}",
            strategy_id=STRATEGY_ID,
            ts=now,
            extra=extras[0],
        ),
        pushed=delivered,
        now=now,
    )
    log.info(
        "market_scan.index_done",
        universe=len(symbols),
        coverage=validation.coverage,
        scored=len(results),
        persisted=len(top),
        top1=first.ticker,
    )
    return delivered if _standalone_card_enabled(engine) else True


def _run_legacy_scan(engine: Engine, now: datetime) -> bool:
    lister = getattr(engine.source, "list_active_symbols", None)
    if lister is None:
        log.info("market_scan.skip", reason="source_without_asset_list")
        return False
    symbols = [s for s in lister() if s not in engine.settings.international_tickers]
    if not symbols:
        log.info("market_scan.skip", reason="no_symbols")
        return True

    daily_source, source_name = _scan_daily_source(engine)
    bars5 = _chunked_daily(
        daily_source, symbols, (now - timedelta(days=9)).date(), now.date()
    )
    liquid = liquidity_filter(bars5, top_k=TOP_LIQUID)
    if not liquid:
        log.info("market_scan.skip", reason="no_liquid_candidates")
        return True
    bars = _chunked_daily(
        daily_source, liquid, (now - timedelta(days=210)).date(), now.date()
    )
    results = scan_scores(bars)
    if not results:
        log.info("market_scan.skip", reason="no_scores")
        return True

    top = results[:5]
    first = top[0]
    sub = bars.xs(first.ticker, level="ticker").sort_index()
    engine.store.write_daily_bars(bars.loc[[first.ticker]], source=source_name)
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

    delivered = False
    if _standalone_card_enabled(engine):
        delivered = engine.notifier.send(
            report_card("🔎 全市场扫描 · 今日Top1", _card_body(top, note))
        )
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
    return delivered if _standalone_card_enabled(engine) else True


def run(engine: Engine, now: datetime) -> bool:
    if engine.settings.index_universe.enabled:
        return _run_index_scan(engine, now)
    return _run_legacy_scan(engine, now)
