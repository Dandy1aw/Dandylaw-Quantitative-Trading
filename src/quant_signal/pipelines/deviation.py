from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from quant_signal.notifier.cards import signal_card
from quant_signal.watch_monitor import (
    TARGET_HIT_STRATEGY_ID,
    check_deviations,
    check_target_hits,
)

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()

TARGET_WINDOW_DAYS = 3
# 到价提醒不追踪告警型信号自身产生的目标
_TARGET_EXCLUDE = {TARGET_HIT_STRATEGY_ID, "price_deviation"}


def _pending_buy_targets(engine: Engine, now: datetime) -> dict[str, float]:
    """近 TARGET_WINDOW_DAYS 天推送过的 BUY 信号目标买入价(每标的取最新)。"""
    targets: dict[str, float] = {}
    for row in engine.ledger.pushed_signals(now - timedelta(days=TARGET_WINDOW_DAYS)):
        if str(row["direction"]) != "buy" or str(row["strategy_id"]) in _TARGET_EXCLUDE:
            continue
        raw = row.get("extra_json")
        if not raw:
            continue
        try:
            extra = json.loads(str(raw))
        except json.JSONDecodeError:
            continue
        target = extra.get("target_buy")
        if isinstance(target, (int, float)) and target > 0:
            targets[str(row["ticker"])] = float(target)   # 时间升序, 后者覆盖=最新
    return targets


def run(engine: Engine, now: datetime) -> None:
    watch_set = engine.ledger.get_holdings(engine.momentum.strategy_id)
    targets = _pending_buy_targets(engine, now)
    if not watch_set and not targets:
        log.info("watch_deviation.skip", reason="no_holdings")
        return
    since = now - timedelta(hours=24)
    ref_prices = {
        ticker: price
        for ticker in watch_set
        if (
            price := engine.ledger.latest_signal_price(
                engine.momentum.strategy_id, ticker, since
            )
        )
        is not None
    }
    if not ref_prices and not targets:
        log.info("watch_deviation.skip", reason="no_reference_price")
        return
    live_prices = {
        ticker: price
        for ticker, price in engine._fetch_live_prices(
            set(ref_prices) | set(targets)
        ).items()
        if price is not None
    }
    threshold = float(
        engine.settings.strategies.get("price_deviation", {}).get("threshold", 0.02)
    )
    signals = check_deviations(
        ref_prices,
        live_prices,
        now=now,
        bands=[threshold * multiplier for multiplier in (1.0, 2.5, 5.0, 10.0)],
    )
    # 到价提醒：近3日 BUY 信号的目标买入价被触及即提示(持仓中的不再提示买入)
    held = set(watch_set)
    signals += check_target_hits(
        {t: p for t, p in targets.items() if t not in held}, live_prices, now=now
    )
    result = engine._dedup(signals, now, channel="deviation")
    for signal in result.to_push:
        delivered = engine.notifier.send(signal_card(signal))
        engine.ledger.insert(signal, pushed=delivered, now=now)
    for signal in result.suppressed + result.overflow:
        engine.ledger.insert(signal, pushed=False, now=now)
    log.info(
        "watch_deviation.done", checked=len(ref_prices), pushed=len(result.to_push)
    )
