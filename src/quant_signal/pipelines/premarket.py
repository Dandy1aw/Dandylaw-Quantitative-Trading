from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from quant_signal.notifier.cards import momentum_ranking_card, premarket_cards
from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.trend_gate import TrendInfo, apply_trend_gate

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()


def run(engine: Engine, now: datetime) -> None:
    bars = engine._refresh_daily(now)
    engine._refresh_fx_rates()
    ranking = engine.momentum.rank(bars)
    targets = engine.momentum.generate(bars)
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
    target_tickers = [signal.ticker for signal in targets]
    current = engine.ledger.get_holdings(engine.momentum.strategy_id)
    as_of = targets[0].ts if targets else now
    sells = [
        Signal(
            ticker=ticker,
            direction=Direction.SELL,
            price=float(bars.xs(ticker, level="ticker")["close"].iloc[-1]),
            reason="动量排名跌出前列，轮动调出",
            strategy_id=engine.momentum.strategy_id,
            ts=as_of,
        )
        for ticker in current
        if ticker not in target_tickers and ticker in bars.index.get_level_values("ticker")
    ]
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
    engine.notifier.send(
        momentum_ranking_card(
            ranking,
            held=set(current),
            trend_flat={info.ticker for info in trend_infos if info.state == "FLAT"},
            insufficient={
                info.ticker for info in trend_infos if info.state == "INSUFFICIENT"
            },
        )
    )
    log.info("premarket.done", signals=len(all_signals), pushed=len(result.to_push))
