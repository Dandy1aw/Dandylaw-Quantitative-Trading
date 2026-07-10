"""Bounded concurrent loading for sparse historical event windows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from quant_signal.datafeed.news import NewsArticle, NewsSource


@dataclass(frozen=True)
class NewsWindow:
    ticker: str
    start: datetime
    end: datetime

    @property
    def key(self) -> tuple[str, datetime, datetime]:
        return (self.ticker, self.start, self.end)


def fetch_news_windows(
    source: NewsSource,
    windows: list[NewsWindow],
    *,
    max_workers: int = 6,
) -> tuple[
    dict[tuple[str, datetime, datetime], list[NewsArticle]],
    dict[tuple[str, datetime, datetime], str],
]:
    rows: dict[tuple[str, datetime, datetime], list[NewsArticle]] = {}
    failures: dict[tuple[str, datetime, datetime], str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(source.fetch, [window.ticker], window.start, window.end): window
            for window in windows
        }
        for future in as_completed(futures):
            window = futures[future]
            try:
                rows[window.key] = future.result()
            except Exception as error:  # noqa: BLE001
                failures[window.key] = str(error)
    return rows, failures
