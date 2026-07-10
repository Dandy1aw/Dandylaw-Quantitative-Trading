"""Immutable first-seen snapshots for point-in-time news replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from quant_signal.datafeed.news import NewsArticle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    article_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    content TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_created ON news(created_at);
CREATE TABLE IF NOT EXISTS overreaction_cases (
    article_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    shock_at TEXT NOT NULL,
    state TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (article_id, ticker, shock_at)
);
"""


@dataclass(frozen=True)
class StoredNews:
    article_id: str
    created_at: datetime
    updated_at: datetime
    first_seen_at: datetime
    symbols: tuple[str, ...]
    headline: str
    summary: str
    content: str
    url: str
    source: str


class NewsStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.executescript(_SCHEMA)

    def put_many(
        self, articles: list[NewsArticle], seen_at: datetime | None = None
    ) -> int:
        first_seen = (seen_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        before = self._con.total_changes
        self._con.executemany(
            "INSERT OR IGNORE INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.article_id,
                    row.created_at.astimezone(timezone.utc).isoformat(),
                    row.updated_at.astimezone(timezone.utc).isoformat(),
                    first_seen.isoformat(),
                    json.dumps(row.symbols),
                    row.headline,
                    row.summary,
                    row.content,
                    row.url,
                    row.source,
                )
                for row in articles
            ],
        )
        self._con.commit()
        return self._con.total_changes - before

    def between(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> list[StoredNews]:
        if not symbols:
            return []
        rows = self._con.execute(
            "SELECT * FROM news WHERE created_at >= ? AND created_at <= ? ORDER BY created_at",
            (
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            ),
        ).fetchall()
        wanted = set(symbols)
        output: list[StoredNews] = []
        for row in rows:
            article_symbols = tuple(json.loads(row["symbols_json"]))
            if not wanted.intersection(article_symbols):
                continue
            output.append(
                StoredNews(
                    article_id=row["article_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                    first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
                    symbols=article_symbols,
                    headline=row["headline"],
                    summary=row["summary"],
                    content=row["content"],
                    url=row["url"],
                    source=row["source"],
                )
            )
        return output

    def has_case(self, article_id: str, ticker: str, shock_at: datetime) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM overreaction_cases WHERE article_id=? AND ticker=? AND shock_at=?",
            (article_id, ticker, shock_at.astimezone(timezone.utc).isoformat()),
        ).fetchone()
        return row is not None

    def record_case(
        self,
        article_id: str,
        ticker: str,
        shock_at: datetime,
        state: str,
        recorded_at: datetime,
    ) -> bool:
        before = self._con.total_changes
        self._con.execute(
            "INSERT OR IGNORE INTO overreaction_cases VALUES (?, ?, ?, ?, ?)",
            (
                article_id,
                ticker,
                shock_at.astimezone(timezone.utc).isoformat(),
                state,
                recorded_at.astimezone(timezone.utc).isoformat(),
            ),
        )
        self._con.commit()
        return self._con.total_changes > before
