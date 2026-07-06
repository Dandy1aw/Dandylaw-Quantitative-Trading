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
            # yf.download([单票], group_by='ticker') 可能返回带 ticker 层的 MultiIndex 列，
            # 剥掉外层，只留 Open/High/Low/Close/Volume，否则下面列名匹配不上
            if isinstance(sub.columns, pd.MultiIndex):
                lvl0 = sub.columns.get_level_values(0)
                sub = pd.DataFrame(sub[t]) if t in lvl0 else sub.droplevel(0, axis=1)
        else:
            if t not in raw.columns.get_level_values(0):
                continue
            sub = pd.DataFrame(raw[t]).copy()
        sub = sub.rename(columns=str.lower)
        if not {"open", "high", "low", "close", "volume"} <= set(sub.columns):
            continue  # yfinance 对该标的/时段没有返回任何数据
        sub = sub[["open", "high", "low", "close", "volume"]]
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

    def fetch_live_price(self, ticker: str) -> float | None:
        """取含盘前/盘后的最新价作为卡片'现价'展示：yfinance prepost=True。
        美股盘前(4:00-9:30)/盘后(16:00-20:00)都能刷新——Alpaca 免费 IEX 源没有
        盘前盘后数据，故现价改由 yf 取；港股/韩股无美式盘前盘后，prepost 对其无副作用。
        尽力而为，取不到返回 None。"""
        raw = yf.download(
            [ticker],
            period="1d",
            interval="5m",
            prepost=True,
            auto_adjust=True,
            group_by="ticker",
            progress=False,
            threads=False,  # Windows 下多线程会触发 yfinance 缓存库锁
        )
        df = _normalize(raw, [ticker])
        if df.empty or ticker not in df.index.get_level_values("ticker"):
            return None
        sub = df.xs(ticker, level="ticker").sort_index()
        return float(sub["close"].iloc[-1]) if not sub.empty else None
