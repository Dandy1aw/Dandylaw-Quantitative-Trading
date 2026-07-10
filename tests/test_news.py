from datetime import datetime, timezone

import quant_signal.datafeed.news as news_module
from quant_signal.datafeed.news import AlpacaNewsSource


class Response:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.body


def test_alpaca_news_fetch_paginates_and_normalizes(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[dict[str, object]] = []
    pages = [
        {"news": [{"id": 2, "headline": "Downgrade", "summary": "s", "content": "c",
                   "created_at": "2026-07-08T14:00:00Z", "updated_at": "2026-07-08T14:01:00Z",
                   "symbols": ["MU", "MU"], "url": "https://x/2", "source": "benzinga"}],
         "next_page_token": "next"},
        {"news": [{"id": 1, "headline": "Miss", "summary": "s1", "content": "c1",
                   "created_at": "2026-07-08T13:00:00Z", "symbols": ["AMD"], "url": "https://x/1"}]},
    ]

    def fake_get(url, *, params, headers, timeout):  # type: ignore[no-untyped-def]
        calls.append(dict(params))
        return Response(pages[len(calls) - 1])

    monkeypatch.setattr(news_module.httpx, "get", fake_get)
    source = AlpacaNewsSource("key", "secret")
    rows = source.fetch(
        ["MU", "AMD"],
        datetime(2026, 7, 8, tzinfo=timezone.utc),
        datetime(2026, 7, 9, tzinfo=timezone.utc),
    )

    assert [row.article_id for row in rows] == ["1", "2"]
    assert rows[1].symbols == ("MU",)
    assert rows[1].created_at.tzinfo is not None
    assert calls[1]["page_token"] == "next"


def test_alpaca_news_requires_credentials() -> None:
    try:
        AlpacaNewsSource("", "")
    except ValueError as error:
        assert "ALPACA" in str(error)
    else:
        raise AssertionError("expected missing credential error")
