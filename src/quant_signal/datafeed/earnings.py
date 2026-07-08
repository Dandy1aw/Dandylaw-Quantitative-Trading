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

    def recent_surprise(self, tickers: list[str], now: date) -> dict[str, float]: ...


class YFinanceEarnings:
    SURPRISE_WINDOW_DAYS = 30

    def recent_surprise(self, tickers: list[str], now: date) -> dict[str, float]:
        """S5 PEAD：近30天内最近一次已公布财报的 EPS 超预期幅度(%)。
        财报后价格漂移(PEAD)是文献充分记录的效应——超预期为正向语境，
        不及预期提示拖累。取不到数据的标的缺席，仅提示不决策。"""
        out: dict[str, float] = {}
        for ticker in tickers:
            try:
                df = yf.Ticker(ticker).earnings_dates
                if df is None or df.empty or "Surprise(%)" not in df.columns:
                    continue
                reported = df["Surprise(%)"].dropna()
                if reported.empty:
                    continue
                latest_ts = max(reported.index)
                latest_day = latest_ts.date() if hasattr(latest_ts, "date") else latest_ts
                if 0 <= (now - latest_day).days <= self.SURPRISE_WINDOW_DAYS:
                    out[ticker] = float(reported.loc[latest_ts])
            except Exception as error:  # noqa: BLE001
                log.warning("earnings.surprise_failed", ticker=ticker, error=str(error))
        return out

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
