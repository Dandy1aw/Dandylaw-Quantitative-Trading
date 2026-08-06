from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import math
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.concentration import cluster_weight_warning, correlation_clusters
from quant_signal.ai_briefing import AIBriefingContext, run_ai_briefing
from quant_signal.notifier.cards import (
    build_ai_briefing_card,
    momentum_ranking_card,
    premarket_cards,
)
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.trend_gate import TrendInfo, apply_trend_gate

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def _annotate_earnings(
    engine: Engine, signals: list[Signal], now: datetime
) -> list[Signal]:
    """P4：BUY 信号 7 天内有财报 → extra 标注 earnings_in_days(提示单日跳空风险)。
    引擎未注入 earnings_source(如测试)时原样返回，零网络依赖。"""
    if (engine.earnings_source is None and engine.fundamentals_source is None) or not signals:
        return signals
    intl = set(engine.settings.international_tickers)
    buy_tickers = sorted(
        {s.ticker for s in signals if s.direction == Direction.BUY and s.ticker not in intl}
    )
    if not buy_tickers:
        return signals
    dates = (
        engine.earnings_source.next_dates(buy_tickers)
        if engine.earnings_source is not None
        else {}
    )
    surprises = (
        engine.earnings_source.recent_surprise(buy_tickers, now.date())
        if engine.earnings_source is not None
        else {}
    )
    quality = (
        engine.fundamentals_source.quality_flags(buy_tickers)
        if engine.fundamentals_source is not None
        else {}
    )
    out: list[Signal] = []
    for s in signals:
        extra = dict(s.extra or {})
        if s.direction == Direction.BUY:
            earnings = dates.get(s.ticker)
            days = (earnings - now.date()).days if earnings else None
            if days is not None and 0 <= days <= 7:
                extra["earnings_in_days"] = days
            flag = quality.get(s.ticker)
            if flag:
                extra["quality_flag"] = flag
            surprise = surprises.get(s.ticker)
            if surprise is not None:
                extra["earnings_surprise"] = surprise
        out.append(replace(s, extra=extra) if extra != (s.extra or {}) else s)
    return out


def _latest_finite_close(bars: pd.DataFrame, ticker: str) -> float | None:
    series = bars.xs(ticker, level="ticker")["close"].dropna()
    finite = series[series.map(lambda value: math.isfinite(float(value)))]
    return float(finite.iloc[-1]) if not finite.empty else None


def _signal_context(
    signals: list[Signal], ticker_currency: dict[str, str] | None = None
) -> list[dict[str, object]]:
    ticker_currency = ticker_currency or {}
    rows: list[dict[str, object]] = []
    for signal in signals:
        extra = signal.extra or {}
        row: dict[str, object] = {
            "ticker": signal.ticker,
            "direction": signal.direction.value.upper(),
            "price": round(signal.price, 4),
            "currency": ticker_currency.get(signal.ticker, "USD"),
            "price_source": "structured_signal",
            "strategy": signal.strategy_id,
            "reason": signal.reason,
        }
        if signal.suggested_weight is not None:
            row["suggested_weight"] = round(signal.suggested_weight, 4)
        for key in (
            "target_buy",
            "take_profit",
            "stop_loss",
            "entry_low",
            "entry_high",
            "momentum_60d",
            "holding_return",
            "earnings_in_days",
            "quality_flag",
            "earnings_surprise",
        ):
            if key in extra:
                row[key] = extra[key]
        rows.append(row)
    return rows


def _ranking_context(
    ranking: list[tuple[str, float, float]]
) -> list[dict[str, object]]:
    return [
        {
            "ticker": ticker,
            "momentum": round(momentum, 4),
            "price": round(price, 4),
        }
        for ticker, momentum, price in ranking[:8]
    ]


def _maybe_send_ai_briefing(
    engine: Engine,
    now: datetime,
    signals: list[Signal],
    ranking: list[tuple[str, float, float]],
    holdings: list[str],
    notes: list[str],
    analysis_cards: list[dict[str, str]],
) -> None:
    cfg = engine.settings.ai_briefing
    if not cfg.enabled:
        return
    context = AIBriefingContext(
        as_of=now.isoformat(),
        signals=_signal_context(signals, engine.settings.international_tickers),
        ranking=_ranking_context(ranking),
        holdings=sorted(holdings),
        notes=[note for note in notes if note],
        analysis_cards=analysis_cards,
    )
    try:
        body = run_ai_briefing(cfg, context)
    except Exception as error:  # noqa: BLE001
        log.warning("ai_briefing.unexpected_error", error=str(error))
        return
    if body:
        engine.notifier.send(build_ai_briefing_card(body))


def run(engine: Engine, now: datetime) -> bool:
    action_card_only = engine.settings.notify.action_card_only
    delivery_ok = True
    bars = engine._refresh_daily(now)
    engine._refresh_fx_rates()
    ranking = engine.momentum.rank(bars)
    targets = engine.momentum.generate(bars)
    current = engine.ledger.get_holdings(engine.momentum.strategy_id)
    trend_infos: list[TrendInfo] = []
    if engine.trend_gate_cfg is not None and targets:
        targets, trend_infos = apply_trend_gate(
            targets,
            bars,
            engine.settings.asset_type,
            engine.settings.international_tickers,
            engine.trend_gate_cfg,
            use_mom=engine.trend_gate_use_mom,
        )
        held_diagnostics = [
            Signal(
                ticker=ticker,
                direction=Direction.BUY,
                price=price,
                reason="持仓趋势复核",
                strategy_id=engine.momentum.strategy_id,
                ts=now,
                suggested_weight=0.0,
            )
            for ticker in current
            if ticker in bars.index.get_level_values("ticker")
            and (price := _latest_finite_close(bars, ticker)) is not None
        ]
        if held_diagnostics:
            _, held_infos = apply_trend_gate(
                held_diagnostics,
                bars,
                engine.settings.asset_type,
                engine.settings.international_tickers,
                engine.trend_gate_cfg,
                use_mom=engine.trend_gate_use_mom,
            )
            info_by_ticker = {info.ticker: info for info in trend_infos}
            info_by_ticker.update({info.ticker: info for info in held_infos})
            trend_infos = list(info_by_ticker.values())
    target_tickers = [signal.ticker for signal in targets]
    as_of = targets[0].ts if targets else now
    sells = []
    for ticker in current:
        if ticker in target_tickers or ticker not in bars.index.get_level_values("ticker"):
            continue
        price = _latest_finite_close(bars, ticker)
        if price is None:
            continue
        # P3: 卖出信号带持有期收益(相对最近一次 BUY 信号价)，给盈亏语境
        buy_price = engine.ledger.latest_price_for(
            engine.momentum.strategy_id, ticker, Direction.BUY.value
        )
        extra: dict[str, object] | None = (
            {"holding_return": price / buy_price - 1.0} if buy_price else None
        )
        sells.append(
            Signal(
                ticker=ticker,
                direction=Direction.SELL,
                price=price,
                reason="动量排名跌出前列，轮动调出",
                strategy_id=engine.momentum.strategy_id,
                ts=as_of,
                extra=extra,
            )
        )
    extra_signals = (
        engine.rsi.generate(bars)
        + engine.macd.generate(bars)
        + engine.bollinger.generate(bars)
    )
    all_signals = engine._attach_exit_prices(targets + sells + extra_signals, bars)
    result = engine._dedup(all_signals, now, channel="premarket")
    for signal in result.suppressed + result.overflow:
        engine.ledger.insert(signal, pushed=False, now=now)

    to_push = _annotate_earnings(engine, result.to_push, now)
    analysis_cards: list[dict[str, str]] = []
    if to_push:
        live_prices = engine._fetch_live_prices({signal.ticker for signal in to_push})
        cards = premarket_cards(
            to_push, engine.settings.international_tickers, live_prices
        )
        analysis_cards.extend(
            {"title": card.title, "body": card.body_md} for card in cards
        )
        delivery_results = (
            [engine.notifier.send(card) for card in cards]
            if not action_card_only
            else []
        )
        delivered = bool(cards) and bool(delivery_results) and all(delivery_results)
        if not action_card_only:
            delivery_ok = delivery_ok and delivered
        for signal in to_push:
            engine.ledger.insert(signal, pushed=delivered, now=now)
    engine.ledger.set_holdings(engine.momentum.strategy_id, target_tickers)
    # 目标组合与券商实际持仓分离: strategy_targets 只是策略意图, 不代表成交
    engine.ledger.set_strategy_targets(
        engine.momentum.strategy_id,
        {
            signal.ticker: float(signal.suggested_weight)
            for signal in targets
            if signal.suggested_weight
        },
        as_of=now,
    )
    # P1 展示层：目标持仓的高相关簇合计权重过半时，在榜单卡追加集中度警示
    weights = {
        s.ticker: s.suggested_weight
        for s in targets
        if s.suggested_weight is not None
    }
    close_wide = bars["close"].unstack("ticker").sort_index()
    clusters = correlation_clusters(close_wide, list(weights))
    concentration_note = cluster_weight_warning(clusters, weights)
    ranking_card = momentum_ranking_card(
        ranking,
        held=set(current),
        trend_flat={info.ticker for info in trend_infos if info.state == "FLAT"},
        insufficient={
            info.ticker for info in trend_infos if info.state == "INSUFFICIENT"
        },
        footer_md=concentration_note,
    )
    if not action_card_only:
        ranking_delivered = engine.notifier.send(ranking_card)
        delivery_ok = ranking_delivered and delivery_ok
    analysis_cards.append({"title": ranking_card.title, "body": ranking_card.body_md})
    # AI 只在统一每日行动简报中调用一次，不再发送独立早报观点卡。
    log.info("premarket.done", signals=len(all_signals), pushed=len(result.to_push))
    return delivery_ok
