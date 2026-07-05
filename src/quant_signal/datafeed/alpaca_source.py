"""Alpaca 数据源。C1 阶段实现真实 REST 拉取，当前为占位。"""

from __future__ import annotations

from datetime import date

import pandas as pd


class AlpacaSource:
    def __init__(self, key: str, secret: str) -> None:
        raise NotImplementedError(
            "AlpacaSource 将在 C1 阶段实现；当前请使用 data_source: yfinance"
        )

    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame:
        raise NotImplementedError
