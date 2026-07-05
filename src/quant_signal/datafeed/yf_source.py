from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf


def _normalize(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """把 yf.download(group_by='ticker') 的宽表转为约定的 MultiIndex 长表。"""
    frames: list[pd.DataFrame] = []
    for t in tickers:
        if len(tickers) == 1:
            sub = raw.copy()
        else:
            if t not in raw.columns.get_level_values(0):
                continue
            sub = raw[t].copy()
        sub = sub.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        sub = sub.dropna(how="all")
        idx = pd.to_datetime(sub.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        sub.index = pd.MultiIndex.from_arrays(
            [[t] * len(sub), idx], names=["ticker", "ts"]
        )
        frames.append(sub)
    if not frames:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
            index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "ts"]),
        )
    return pd.concat(frames).sort_index()


class YFinanceSource:
    def fetch_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        raw = yf.download(
            tickers,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            group_by="ticker",
            progress=False,
            threads=False,  # Windows 下多线程会触发 yfinance 缓存库锁
        )
        return _normalize(raw, tickers)

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame:
        raw = yf.download(
            tickers,
            period=f"{lookback_days}d",
            interval="5m",
            auto_adjust=True,
            group_by="ticker",
            progress=False,
            threads=False,  # Windows 下多线程会触发 yfinance 缓存库锁
        )
        return _normalize(raw, tickers)
