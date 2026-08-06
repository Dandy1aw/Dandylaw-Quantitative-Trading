from __future__ import annotations

from datetime import date
import time
from typing import Callable, Literal, Protocol, Union, cast

import httpx
import pandas as pd

_QueryValue = Union[str, int, float, bool, None]

_BASE = "https://data.alpaca.markets/v2/stocks/bars"
_COLMAP = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
_TRANSIENT_ERRORS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> httpx.Response: ...


class AlpacaSource:
    # 免费 IEX feed 只覆盖部分市场成交量，不适合横截面流动性/量比排名。
    partial_market_volume = True

    def __init__(
        self,
        key: str,
        secret: str,
        *,
        client: _HTTPClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (0.5, 1.5),
    ) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET 未配置，请填写 config/.env")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self._client = client or cast(
            _HTTPClient,
            httpx.Client(
                timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            ),
        )
        self._sleep = sleep
        self._retry_delays = retry_delays

    def _get(self, url: str, params: dict[str, _QueryValue]) -> httpx.Response:
        for attempt in range(len(self._retry_delays) + 1):
            try:
                response = self._client.get(
                    url,
                    params=params,
                    headers=self._headers,
                )
                response.raise_for_status()
                return response
            except _TRANSIENT_ERRORS:
                if attempt >= len(self._retry_delays):
                    raise
                self._sleep(self._retry_delays[attempt])
        raise AssertionError("unreachable")

    def _fetch(
        self,
        tickers: list[str],
        timeframe: str,
        start: str,
        end: str | None,
        *,
        feed: Literal["iex", "sip"] = "iex",
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        params: dict[str, _QueryValue] = {
            "symbols": ",".join(tickers),
            "timeframe": timeframe,
            "start": start,
            "adjustment": "all",
            "feed": feed,
            "limit": 10_000,
        }
        if end:
            params["end"] = end
        while True:
            resp = self._get(_BASE, params)
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

    def fetch_sip_daily_bars(
        self, tickers: list[str], start: date, end: date
    ) -> pd.DataFrame:
        return self._fetch(
            tickers,
            "1Day",
            start.isoformat(),
            end.isoformat(),
            feed="sip",
        )

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

    def fetch_minute_bars(
        self, tickers: list[str], lookback_minutes: int = 45
    ) -> pd.DataFrame:
        """获取持仓异动所需的 1 分钟线。

        Alpaca 免费账户的 SIP 端点会成功返回但延迟约 15 分钟，不能用于
        实时告警。IEX 虽是部分市场，但可提供新鲜价格，因此实时链路优先 IEX。
        """
        from datetime import datetime, timedelta, timezone

        # 跨午休/隔夜也要能取到当日开盘价，因此至少回看 24 小时。
        minutes = max(lookback_minutes, 24 * 60)
        start = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        try:
            frame = self._fetch(tickers, "1Min", start, None, feed="iex")
            frame.attrs["feed"] = "alpaca_iex_1m_realtime_partial"
            return frame
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {401, 403}:
                raise
        frame = self._fetch(tickers, "1Min", start, None, feed="sip")
        frame.attrs["feed"] = "alpaca_sip_1m_delayed"
        return frame
