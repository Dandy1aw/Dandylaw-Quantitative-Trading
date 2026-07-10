"""Point-in-time news feed abstraction and Alpaca implementation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

_URL = "https://data.alpaca.markets/v1beta1/news"


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    created_at: datetime
    updated_at: datetime
    symbols: tuple[str, ...]
    headline: str
    summary: str
    content: str
    url: str
    source: str


class NewsSource(Protocol):
    def fetch(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> list[NewsArticle]: ...


def _time(value: object) -> datetime:
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)


class AlpacaNewsSource:
    def __init__(self, key: str, secret: str) -> None:
        if not key or not secret:
            raise ValueError("ALPACA_KEY/ALPACA_SECRET 未配置")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def fetch(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> list[NewsArticle]:
        if not symbols:
            return []
        params: dict[str, str | int] = {
            "symbols": ",".join(sorted(set(symbols))),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "sort": "asc",
            "limit": 50,
        }
        output: dict[str, NewsArticle] = {}
        while True:
            response = httpx.get(
                _URL, params=params, headers=self._headers, timeout=30.0
            )
            response.raise_for_status()
            body = response.json()
            for raw in body.get("news") or []:
                created = _time(raw["created_at"])
                article = NewsArticle(
                    article_id=str(raw["id"]),
                    created_at=created,
                    updated_at=_time(raw.get("updated_at") or raw["created_at"]),
                    symbols=tuple(sorted(set(str(s) for s in raw.get("symbols") or []))),
                    headline=str(raw.get("headline") or ""),
                    summary=str(raw.get("summary") or ""),
                    content=str(raw.get("content") or ""),
                    url=str(raw.get("url") or ""),
                    source=str(raw.get("source") or ""),
                )
                output.setdefault(article.article_id, article)
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = str(token)
        return sorted(output.values(), key=lambda row: row.created_at)
