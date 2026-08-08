from __future__ import annotations

import structlog

from quant_signal.datafeed.yfinance_compat import yf

log = structlog.get_logger()


def fetch_usd_rates(currencies: set[str]) -> dict[str, float]:
    """返回 {货币代码: 1美元兑换该货币的数量}，USD 恒为 1.0。"""
    rates: dict[str, float] = {}
    for ccy in currencies:
        if ccy == "USD":
            rates[ccy] = 1.0
            continue
        hist = yf.Ticker(f"{ccy}=X").history(period="5d")
        if hist.empty:
            log.warning("fx.fetch_failed", currency=ccy)
            continue
        rates[ccy] = float(hist["Close"].iloc[-1])
    return rates
