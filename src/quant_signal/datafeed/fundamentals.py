"""质量因子否决器（P5 提示层）：BUY 信号的标的基本面质量差时卡片标注。

v1 只做两条硬规则(可解释、不拟合)：ROE 为负；负债/权益 > 300%。
仅提示不决策——先观察与绩效闭环的关系，有数据后再定是否进选股。
yfinance Ticker.info 尽力而为，取不到就当没有。
"""

from __future__ import annotations

from typing import Protocol

import structlog
import yfinance as yf

log = structlog.get_logger()


class FundamentalsSource(Protocol):
    def quality_flags(self, tickers: list[str]) -> dict[str, str]: ...


class YFinanceFundamentals:
    def quality_flags(self, tickers: list[str]) -> dict[str, str]:
        """返回 {ticker: 质量问题描述}；无问题/取不到数据的标的缺席。"""
        out: dict[str, str] = {}
        for ticker in tickers:
            try:
                info = yf.Ticker(ticker).info or {}
            except Exception as error:  # noqa: BLE001
                log.warning("fundamentals.fetch_failed", ticker=ticker, error=str(error))
                continue
            problems: list[str] = []
            roe = info.get("returnOnEquity")
            if isinstance(roe, (int, float)) and roe < 0:
                problems.append("ROE为负")
            debt = info.get("debtToEquity")
            if isinstance(debt, (int, float)) and debt > 300:
                problems.append("高负债")
            if problems:
                out[ticker] = "/".join(problems)
        return out
