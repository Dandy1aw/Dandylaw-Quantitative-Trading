"""收盘复盘管道：截图持仓逐标的取收盘价 → 复盘卡（16:30 ET）。

收盘价优先当日日线；当日 bar 未入库（凌晨 maintenance 才补）时用
收盘后实时价兜底；两者都没有就退回最近收盘价但不冒充当日涨跌。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import pandas as pd
import structlog

from quant_signal.report import PositionRecap, SignalTally, build_close_recap

if TYPE_CHECKING:
    from datetime import date

    from quant_signal.engine import Engine

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")


def _positive_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed > 0 else None


def _recap(
    engine: "Engine", row: dict[str, object], session: "date", now: datetime
) -> PositionRecap:
    symbol = str(row["symbol"])
    international = symbol in engine.settings.international_tickers
    closes: list[tuple["date", Decimal]] = []
    try:
        bars = engine.store.read_daily_bars([symbol], start=now - timedelta(days=30))
        series = bars.xs(symbol, level="ticker")["close"].dropna().sort_index()
        closes = [
            (cast(pd.Timestamp, ts).date(), Decimal(str(round(float(value), 4))))
            for ts, value in series.items()
        ]
    except Exception:  # noqa: BLE001 - 行情缺失只是降级,不中断
        closes = []

    close: Decimal | None = None
    prev: Decimal | None = None
    if closes and closes[-1][0] == session:
        close = closes[-1][1]
        if len(closes) >= 2:
            prev = closes[-2][1]
    else:
        live = engine._fetch_live_price(symbol)
        if live is not None and live > 0:
            close = Decimal(str(round(live, 4)))
            if closes:
                prev = closes[-1][1]
        elif closes:
            close = closes[-1][1]  # 最近收盘(非当日)，当日涨跌保持缺失

    day = float(close / prev) - 1.0 if close and prev else None
    avg = _positive_decimal(row.get("avg_entry_price"))
    pnl: float | None = None
    pnl_as_of_screenshot = False
    if close is not None and avg is not None:
        pnl = float(close / avg) - 1.0
    else:
        raw_pct = row.get("pnl_pct")  # 截图里的盈亏比例(如 "20.23")
        if raw_pct is not None:
            try:
                pnl = float(str(raw_pct)) / 100.0
                pnl_as_of_screenshot = True
            except ValueError:
                pnl = None
    qty = _positive_decimal(row.get("qty"))
    market_value = (
        qty * close
        if qty is not None and close is not None
        else _positive_decimal(row.get("market_value"))
        or _positive_decimal(row.get("estimated_market_value"))
    )
    return PositionRecap(
        symbol=symbol,
        close=close,
        day_change_pct=day,
        position_pnl_pct=pnl,
        market_value=market_value,
        international=international,
        pnl_as_of_screenshot=pnl_as_of_screenshot,
    )


def run(engine: "Engine", now: datetime) -> None:
    """截图持仓收盘表现 + 当日信号计数 → 一张复盘卡（无持仓也发计数）。"""
    if now.tzinfo is None:
        raise ValueError("postmarket run time must be timezone-aware")
    session = now.astimezone(_ET).date()
    positions = engine.ledger.active_observed_positions()
    recaps = [_recap(engine, row, session, now) for row in positions]
    observed_at: datetime | None = None
    if positions:
        raw = positions[0].get("observed_at")
        try:
            observed_at = datetime.fromisoformat(str(raw)) if raw else None
        except ValueError:
            observed_at = None
    rows = engine.ledger.signals_on(session)
    # 盘中同一信号每个周期都会入账(去重记录 pushed=False)，计数按
    # (标的, 策略, 方向) 去重，否则一天能虚报到几百个
    distinct = {
        (str(r["ticker"]), str(r["strategy_id"]), str(r["direction"])) for r in rows
    }
    pushed_keys = {
        (str(r["ticker"]), str(r["strategy_id"]), str(r["direction"]))
        for r in rows
        if bool(r["pushed"])
    }
    tally = SignalTally(
        total=len(distinct),
        buys=sum(1 for key in distinct if key[2] == "buy"),
        sells=sum(1 for key in distinct if key[2] == "sell"),
        pushed=len(pushed_keys),
    )
    card = build_close_recap(
        recaps, session=session, tally=tally, observed_at=observed_at
    )
    sent = engine.notifier.send(card)
    log.info(
        "postmarket.completed",
        session=session.isoformat(),
        positions=len(recaps),
        signals=tally.total,
        sent=sent,
    )
