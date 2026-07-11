"""持仓期权情报管道：期权链定价 → 持股决策语境卡片（只观察，不交易）。

与全市场期权榜频道不同，这里逐标的 fail-open：单个标的取不到数据就如实
显示缺失，不拖累其余标的；但缺失只能显示缺失，绝不能编造。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import re
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import structlog

from quant_signal.notifier.cards import option_intel_card
from quant_signal.options_intel import OptionIntel, OptionIntelPolicy, compute_intel

if TYPE_CHECKING:
    from quant_signal.engine import Engine

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")
_US_ROOT = re.compile(r"^[A-Z]{1,6}$")


def held_symbols(engine: "Engine") -> set[str]:
    """截图账户持仓 ∪ 全策略虚拟持仓中的美股标的（完整集合，不截断）。"""
    observed = {
        str(row["symbol"]) for row in engine.ledger.active_observed_positions()
    }
    virtual = set(engine.ledger.all_held_tickers())
    international = set(engine.settings.international_tickers)
    return {
        symbol
        for symbol in observed | virtual
        if symbol not in international and _US_ROOT.fullmatch(symbol)
    }


def holdings_universe(engine: "Engine") -> list[str]:
    """情报卡覆盖的标的（有序，受 max_tickers 截断保护）。"""
    limit = engine.settings.option_intel.max_tickers
    return sorted(held_symbols(engine))[:limit]


def _spot_and_closes(
    engine: "Engine", symbol: str, now: datetime
) -> tuple[Decimal | None, list[float]]:
    closes: list[float] = []
    try:
        bars = engine.store.read_daily_bars(
            [symbol], start=now - timedelta(days=90)
        )
        series = bars.xs(symbol, level="ticker")["close"].dropna()
        closes = [float(value) for value in series.sort_index()]
    except Exception:  # noqa: BLE001 - 行情缺失只是降级,不中断
        closes = []
    live = engine._fetch_live_price(symbol)
    if live is not None and live > 0:
        return Decimal(str(round(live, 4))), closes
    if closes and closes[-1] > 0:
        return Decimal(str(round(closes[-1], 4))), closes
    return None, closes


def build_intel(
    engine: "Engine", symbol: str, now: datetime
) -> OptionIntel | None:
    """单标的情报；现价拿不到返回 None（无法给任何比值一个分母）。"""
    if engine.option_chain_source is None:
        return None
    cfg = engine.settings.option_intel
    session = now.astimezone(_ET).date()
    spot, closes = _spot_and_closes(engine, symbol, now)
    if spot is None:
        log.warning("option_intel.no_spot", symbol=symbol)
        return None
    earnings_date = None
    if engine.earnings_source is not None:
        try:
            earnings_date = engine.earnings_source.next_dates([symbol]).get(symbol)
        except Exception as error:  # noqa: BLE001 - 财报标注尽力而为
            log.warning(
                "option_intel.earnings_failed", symbol=symbol, error=str(error)
            )
    result = engine.option_chain_source.fetch_chain(
        symbol, session=session, max_expiry_days=cfg.max_expiry_days
    )
    intel = compute_intel(
        symbol,
        result.contracts,
        spot=spot,
        session=session,
        closes=closes,
        earnings_date=earnings_date,
        policy=OptionIntelPolicy(
            max_expiry_days=cfg.max_expiry_days,
            min_expected_move_dte=cfg.min_expected_move_dte,
            top_oi_strikes=cfg.top_oi_strikes,
        ),
    )
    if result.truncated and intel.data_note is None:
        from dataclasses import replace

        intel = replace(intel, data_note="期权链数据不完整(分页超限)")
    return intel


def _has_data(intel: OptionIntel) -> bool:
    return (
        intel.expected_move_pct is not None
        or intel.atm_iv is not None
        or intel.pc_volume_ratio is not None
        or bool(intel.top_oi_strikes)
    )


def run(engine: "Engine", now: datetime) -> None:
    """逐标的拉链计算 → 落库 → 一张情报卡。"""
    if now.tzinfo is None:
        raise ValueError("option intel run time must be timezone-aware")
    cfg = engine.settings.option_intel
    if not cfg.enabled:
        return
    if engine.option_chain_source is None:
        log.warning("option_intel.no_chain_source")
        return
    symbols = holdings_universe(engine)
    if not symbols:
        log.info("option_intel.no_holdings")
        return
    session = now.astimezone(_ET).date()
    intels: list[OptionIntel] = []
    for symbol in symbols:
        try:
            intel = build_intel(engine, symbol, now)
        except Exception as error:  # noqa: BLE001 - 单标的失败不拖累其余
            log.warning(
                "option_intel.symbol_failed", symbol=symbol, error=str(error)
            )
            continue
        if intel is None:
            continue
        intels.append(intel)
        if _has_data(intel):
            engine.ledger.save_option_intel_daily(intel, now=now)
    if not any(_has_data(intel) for intel in intels):
        log.info("option_intel.no_data", symbols=symbols)
        return
    card = option_intel_card(
        intels, session=session, iv_rv_warn_ratio=cfg.iv_rv_warn_ratio
    )
    sent = engine.notifier.send(card)
    log.info(
        "option_intel.completed",
        session=session.isoformat(),
        symbols=len(symbols),
        with_data=sum(1 for intel in intels if _has_data(intel)),
        sent=sent,
    )
