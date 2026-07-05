from __future__ import annotations

from datetime import date
from typing import Protocol

import pandas as pd

from quant_signal.config import Settings


class DataSource(Protocol):
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame: ...

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame: ...


def get_source(settings: Settings) -> DataSource:
    if settings.data_source == "yfinance":
        from quant_signal.datafeed.yf_source import YFinanceSource

        return YFinanceSource()
    if settings.data_source == "alpaca":
        from quant_signal.datafeed.alpaca_source import AlpacaSource

        return AlpacaSource(settings.alpaca_key, settings.alpaca_secret)
    raise ValueError(f"未知数据源: {settings.data_source}")
