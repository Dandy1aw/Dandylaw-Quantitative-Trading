from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_signal.datafeed.news import NewsArticle
from quant_signal.news_store import NewsStore


NOW = datetime(2026, 7, 9, 1, 0, tzinfo=timezone.utc)


def article(headline: str = "first") -> NewsArticle:
    return NewsArticle(
        article_id="n1", created_at=NOW, updated_at=NOW,
        symbols=("MU",), headline=headline, summary="summary", content="content",
        url="https://example/n1", source="benzinga",
    )


def test_news_store_preserves_first_snapshot(tmp_path: Path) -> None:
    store = NewsStore(tmp_path / "news.db")
    store.put_many([article("first")], seen_at=NOW)
    store.put_many([article("edited")], seen_at=NOW + timedelta(hours=1))

    rows = store.between(["MU"], NOW - timedelta(days=1), NOW + timedelta(days=1))

    assert len(rows) == 1
    assert rows[0].headline == "first"
    assert rows[0].first_seen_at == NOW


def test_news_store_filters_by_symbol_and_time(tmp_path: Path) -> None:
    store = NewsStore(tmp_path / "news.db")
    store.put_many([article()], seen_at=NOW)
    assert store.between(["AMD"], NOW - timedelta(days=1), NOW + timedelta(days=1)) == []
    assert len(store.between(["MU"], NOW - timedelta(minutes=1), NOW + timedelta(1))) == 1
