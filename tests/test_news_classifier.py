from datetime import datetime, timezone

from quant_signal.datafeed.news import NewsArticle
from quant_signal.news_classifier import classify_news


def article(text: str) -> NewsArticle:
    now = datetime(2026, 7, 9, tzinfo=timezone.utc)
    return NewsArticle("1", now, now, ("MU",), text, text, text, "", "benzinga")


def test_classifier_vetoes_structural_damage() -> None:
    for text in (
        "Company discloses accounting fraud and auditor resignation",
        "Issuer warns of going concern and liquidity risk",
        "Company announces dilutive public offering",
    ):
        result = classify_news(article(text))
        assert result.structural_damage is True
        assert result.veto


def test_classifier_accepts_repairable_negative_event() -> None:
    miss = classify_news(article("Quarterly revenue misses estimates on one-time charge"))
    downgrade = classify_news(article("Analyst downgrades shares after short-term margin pressure"))
    assert miss.structural_damage is False and miss.confidence >= 0.75
    assert downgrade.structural_damage is False and downgrade.event_type == "analyst_downgrade"


def test_classifier_covers_real_alpaca_headline_forms() -> None:
    assert classify_news(article("Bank of America Downgrades Intel to Neutral")).event_type == "analyst_downgrade"
    assert classify_news(article("Micron Shares Fall On Q4 Print")).event_type == "earnings_miss"
    assert classify_news(article("Street Debates Stock After Q4 Disappoints")).event_type == "earnings_miss"
    assert classify_news(article("Chip timeline delayed into next year")).event_type == "execution_delay"


def test_classifier_keeps_unknown_news_observation_only() -> None:
    result = classify_news(article("Company presents at technology conference"))
    assert result.structural_damage is None
    assert result.veto
