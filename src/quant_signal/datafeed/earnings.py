"""财报日历（P4 展示层）：BUY 信号临近财报时卡片标注，提示单日跳空风险。

yfinance 的 Ticker.calendar 提供下次财报日期；尽力而为，取不到就当没有——
这是提示层，不能因为外部数据失败拖累主流程。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import structlog
import yfinance as yf

log = structlog.get_logger()


class EarningsSource(Protocol):
    def next_dates(self, tickers: list[str]) -> dict[str, date]: ...


class YFinanceEarnings:
    def next_dates(self, tickers: list[str]) -> dict[str, date]:
        """返回 {ticker: 下次财报日}；查不到/出错的标的直接缺席。"""
        out: dict[str, date] = {}
        for ticker in tickers:
            try:
                calendar = yf.Ticker(ticker).calendar
                dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
                if dates:
                    first = dates[0]
                    out[ticker] = first if isinstance(first, date) else first.date()
            except Exception as error:  # noqa: BLE001
                log.warning("earnings.fetch_failed", ticker=ticker, error=str(error))
        return out
