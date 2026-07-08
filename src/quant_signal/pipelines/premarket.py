from __future__ import annotations

from datetime import datetime
import math
from typing import TYPE_CHECKING

import pandas as pd
import structlog

from quant_signal.concentration import cluster_weight_warning, correlation_clusters
from quant_signal.notifier.cards import momentum_ranking_card, premarket_cards
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.trend_gate import TrendInfo, apply_trend_gate

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def _latest_finite_close(bars: pd.DataFrame, ticker: str) -> float | None:
    series = bars.xs(ticker, level="ticker")["close"].dropna()
    finite = series[series.map(lambda value: math.isfinite(float(value)))]
    return float(finite.iloc[-1]) if not finite.empty else None


def run(engine: Engine, now: datetime) -> None:
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

    if result.to_push:
        live_prices = engine._fetch_live_prices({signal.ticker for signal in result.to_push})
        cards = premarket_cards(
            result.to_push, engine.settings.international_tickers, live_prices
        )
        delivery_results = [engine.notifier.send(card) for card in cards]
        delivered = bool(cards) and all(delivery_results)
        for signal in result.to_push:
            engine.ledger.insert(signal, pushed=delivered, now=now)
    engine.ledger.set_holdings(engine.momentum.strategy_id, target_tickers)
    # P1 展示层：目标持仓的高相关簇合计权重过半时，在榜单卡追加集中度警示
    weights = {
        s.ticker: s.suggested_weight
        for s in targets
        if s.suggested_weight is not None
    }
    close_wide = bars["close"].unstack("ticker").sort_index()
    clusters = correlation_clusters(close_wide, list(weights))
    engine.notifier.send(
        momentum_ranking_card(
            ranking,
            held=set(current),
            trend_flat={info.ticker for info in trend_infos if info.state == "FLAT"},
            insufficient={
                info.ticker for info in trend_infos if info.state == "INSUFFICIENT"
            },
            footer_md=cluster_weight_warning(clusters, weights),
        )
    )
    log.info("premarket.done", signals=len(all_signals), pushed=len(result.to_push))
