from __future__ import annotations

from datetime import date
from typing import Union

import httpx
import pandas as pd

_QueryValue = Union[str, int, float, bool, None]

_BASE = "https://data.alpaca.markets/v2/stocks/bars"
_COLMAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}


class AlpacaSource:
    # 免费 IEX feed 只覆盖部分市场成交量，不适合横截面流动性/量比排名。
    partial_market_volume = True

    def __init__(self, key: str, secret: str) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET 未配置，请填写 config/.env")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _fetch(
        self, tickers: list[str], timeframe: str, start: str, end: str | None
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        params: dict[str, _QueryValue] = {
            "symbols": ",".join(tickers),
            "timeframe": timeframe,
            "start": start,
            "adjustment": "all",
            "feed": "iex",
            "limit": 10_000,
        }
        if end:
            params["end"] = end
        while True:
            resp = httpx.get(_BASE, params=params, headers=self._headers, timeout=30.0)
            resp.raise_for_status()
            body = resp.json()
            for ticker, bars in (body.get("bars") or {}).items():
                if not bars:
                    continue
                df = pd.DataFrame(bars).rename(columns=_COLMAP)
                df["ts"] = pd.to_datetime(df["t"], utc=True)
                df["ticker"] = ticker
                frames.append(df.set_index(["ticker", "ts"])[list(_COLMAP.values())])
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        if not frames:
            return pd.DataFrame(
                columns=list(_COLMAP.values()),
                index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "ts"]),
            )
        return pd.concat(frames).sort_index()

    def fetch_daily_bars(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
        return self._fetch(tickers, "1Day", start.isoformat(), end.isoformat())

    def list_active_symbols(self) -> list[str]:
        """全部 active+tradable 美股普通股代码(排除 OTC 与带后缀的权证/单位类)。
        交易 API 与数据 API 分属不同 host；live 拒绝就试 paper。"""
        for host in ("https://api.alpaca.markets", "https://paper-api.alpaca.markets"):
            resp = httpx.get(
                f"{host}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=self._headers,
                timeout=60.0,
            )
            if resp.status_code in (401, 403):
                continue
            resp.raise_for_status()
            return sorted(
                {
                    str(asset["symbol"])
                    for asset in resp.json()
                    if asset.get("tradable")
                    and asset.get("exchange") != "OTC"
                    and str(asset["symbol"]).isalpha()
                }
            )
        return []

    def fetch_intraday_bars(
        self, tickers: list[str], lookback_days: int = 5
    ) -> pd.DataFrame:
        from datetime import datetime, timedelta, timezone

        start = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        return self._fetch(tickers, "5Min", start, None)
