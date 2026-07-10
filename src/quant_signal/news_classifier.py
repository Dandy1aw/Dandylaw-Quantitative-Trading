"""Conservative deterministic news classification for overreaction candidates."""

from __future__ import annotations

from dataclasses import dataclass

from quant_signal.datafeed.news import NewsArticle


@dataclass(frozen=True)
class NewsAssessment:
    event_type: str
    severity: int
    structural_damage: bool | None
    confidence: float
    summary: str
    evidence: str

    @property
    def veto(self) -> bool:
        return self.structural_damage is not False or self.confidence < 0.75


_STRUCTURAL: tuple[tuple[str, str], ...] = (
    ("accounting fraud", "accounting_integrity"),
    ("accounting irregular", "accounting_integrity"),
    ("auditor resignation", "accounting_integrity"),
    ("going concern", "solvency"),
    ("liquidity risk", "solvency"),
    ("bankruptcy", "solvency"),
    ("dilutive public offering", "dilution"),
    ("registered direct offering", "dilution"),
    ("fda rejects", "regulatory_failure"),
    ("regulatory ban", "regulatory_failure"),
    ("loses key customer", "customer_loss"),
    ("cuts full-year guidance", "guidance_cut"),
    ("withdraws guidance", "guidance_cut"),
)

_REPAIRABLE: tuple[tuple[str, str], ...] = (
    ("misses estimates", "earnings_miss"),
    ("misses revenue", "earnings_miss"),
    ("misses earnings", "earnings_miss"),
    ("one-time charge", "one_time_charge"),
    ("analyst downgrade", "analyst_downgrade"),
    ("downgrades shares", "analyst_downgrade"),
    ("downgrades ", "analyst_downgrade"),
    ("downgraded ", "analyst_downgrade"),
    ("lowers price target", "analyst_downgrade"),
    ("shares fall on", "earnings_miss"),
    ("after q4 disappoints", "earnings_miss"),
    ("after earnings disappoint", "earnings_miss"),
    ("weak results", "earnings_miss"),
    ("timeline delayed", "execution_delay"),
    ("production delay", "execution_delay"),
    ("rollout delay", "execution_delay"),
    ("margin pressure", "temporary_margin"),
    ("supply disruption", "supply_disruption"),
    ("lawsuit", "litigation"),
    ("investigation", "investigation"),
)


def classify_news(article: NewsArticle) -> NewsAssessment:
    text = " ".join((article.headline, article.summary, article.content)).lower()
    for keyword, event_type in _STRUCTURAL:
        if keyword in text:
            return NewsAssessment(event_type, 5, True, 0.95, article.headline, keyword)
    for keyword, event_type in _REPAIRABLE:
        if keyword in text:
            severity = 3 if event_type in {"earnings_miss", "litigation", "investigation"} else 2
            return NewsAssessment(event_type, severity, False, 0.85, article.headline, keyword)
    return NewsAssessment("unknown", 1, None, 0.30, article.headline, "")
