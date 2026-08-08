"""质量因子否决器（P5 提示层）：BUY 信号的标的基本面质量差时卡片标注。

v1 只做两条硬规则(可解释、不拟合)：ROE 为负；负债/权益 > 300%。
仅提示不决策——先观察与绩效闭环的关系，有数据后再定是否进选股。
yfinance Ticker.info 尽力而为，取不到就当没有。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Protocol

import structlog
import yfinance as yf

from quant_signal.company_profiles import CompanyProfile

log = structlog.get_logger()


class FundamentalsSource(Protocol):
    def quality_flags(self, tickers: list[str]) -> dict[str, str]: ...

    def profiles(self, tickers: list[str]) -> dict[str, CompanyProfile]: ...


_SECTOR_TO_GICS = {
    "Technology": "Information Technology",
    "Healthcare": "Health Care",
    "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}


def _int(value: object) -> int | None:
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


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

    def profiles(self, tickers: list[str]) -> dict[str, CompanyProfile]:
        as_of = datetime.now(UTC).date()
        unique = [ticker.strip().upper() for ticker in dict.fromkeys(tickers) if ticker.strip()]

        def fetch(raw_ticker: str) -> CompanyProfile:
            ticker = raw_ticker.strip().upper()
            try:
                info = yf.Ticker(ticker).info or {}
            except Exception as error:  # noqa: BLE001 - cached unavailable profile
                log.warning("fundamentals.profile_failed", ticker=ticker, error=str(error))
                return CompanyProfile(
                    ticker, as_of, None, None, None, None, None,
                    None, None, None, None, None, None, None,
                    "yfinance", "unavailable",
                )
            sector = info.get("sector")
            market_cap = _int(info.get("marketCap"))
            gics = _SECTOR_TO_GICS.get(str(sector)) if sector is not None else None
            quote_type = str(info.get("quoteType") or "").upper() or None
            # ``data_status`` describes whether the instrument type was verified.
            # Sector and market cap are optional enrichment fields; downstream
            # strategies apply their own completeness requirements.
            status = "ok" if quote_type is not None else "unavailable"
            return CompanyProfile(
                ticker=ticker,
                as_of=as_of,
                market_cap_usd=market_cap,
                gics_sector=gics,
                industry=str(info.get("industry")) if info.get("industry") else None,
                company_name=str(info.get("shortName")) if info.get("shortName") else None,
                business_summary=(
                    str(info.get("longBusinessSummary"))[:2_000]
                    if info.get("longBusinessSummary")
                    else None
                ),
                total_revenue=_int(info.get("totalRevenue")),
                revenue_growth=_float(info.get("revenueGrowth")),
                earnings_growth=_float(info.get("earningsGrowth")),
                profit_margin=_float(info.get("profitMargins")),
                return_on_equity=_float(info.get("returnOnEquity")),
                free_cash_flow=_int(info.get("freeCashflow")),
                quote_type=quote_type,
                source="yfinance",
                data_status=status,  # type: ignore[arg-type]
            )
        if not unique:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(unique))) as executor:
            profiles = list(executor.map(fetch, unique))
        return {profile.ticker: profile for profile in profiles}
