"""财报日历（P4 展示层）：BUY 信号临近财报时卡片标注，提示单日跳空风险。

yfinance 的 Ticker.calendar 提供下次财报日期；尽力而为，取不到就当没有——
这是提示层，不能因为外部数据失败拖累主流程。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Protocol

import structlog

from quant_signal.datafeed.yfinance_compat import yf

log = structlog.get_logger()


class EarningsSource(Protocol):
    def next_dates(self, tickers: list[str]) -> dict[str, date]: ...

    def recent_surprise(self, tickers: list[str], now: date) -> dict[str, float]: ...


class YFinanceEarnings:
    SURPRISE_WINDOW_DAYS = 30

    def __init__(
        self,
        *,
        failure_ttl_seconds: float = 6 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_ttl_seconds = max(0.0, failure_ttl_seconds)
        self._clock = clock
        self._failed_until: dict[tuple[str, str], float] = {}

    def _is_suppressed(self, operation: str, ticker: str) -> bool:
        key = (operation, ticker.upper())
        failed_until = self._failed_until.get(key, 0.0)
        if self._clock() < failed_until:
            return True
        self._failed_until.pop(key, None)
        return False

    def _mark_failure(self, operation: str, ticker: str) -> None:
        self._failed_until[(operation, ticker.upper())] = (
            self._clock() + self._failure_ttl_seconds
        )

    def recent_surprise(self, tickers: list[str], now: date) -> dict[str, float]:
        """S5 PEAD：近30天内最近一次已公布财报的 EPS 超预期幅度(%)。
        财报后价格漂移(PEAD)是文献充分记录的效应——超预期为正向语境，
        不及预期提示拖累。取不到数据的标的缺席，仅提示不决策。"""
        out: dict[str, float] = {}
        failures: list[tuple[str, str]] = []
        for ticker in dict.fromkeys(tickers):
            if self._is_suppressed("recent_surprise", ticker):
                continue
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
                self._mark_failure("recent_surprise", ticker)
                failures.append((ticker, str(error)))
        if failures:
            log.warning(
                "earnings.surprise_batch_failed",
                failed_count=len(failures),
                sample=failures[:5],
            )
        return out

    def next_dates(self, tickers: list[str]) -> dict[str, date]:
        """返回 {ticker: 下次财报日}；查不到/出错的标的直接缺席。"""
        out: dict[str, date] = {}
        failures: list[tuple[str, str]] = []
        for ticker in dict.fromkeys(tickers):
            if self._is_suppressed("next_dates", ticker):
                continue
            try:
                calendar = yf.Ticker(ticker).calendar
                dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
                if dates:
                    first = dates[0]
                    out[ticker] = first if isinstance(first, date) else first.date()
            except Exception as error:  # noqa: BLE001
                self._mark_failure("next_dates", ticker)
                failures.append((ticker, str(error)))
        if failures:
            log.warning(
                "earnings.fetch_batch_failed",
                failed_count=len(failures),
                sample=failures[:5],
            )
        return out
