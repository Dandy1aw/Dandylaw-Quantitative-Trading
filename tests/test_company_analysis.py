from __future__ import annotations

from datetime import date

from quant_signal.company_analysis import (
    clear_company_rationale_cache,
    run_company_rationales,
)
from quant_signal.config import AIBriefingSettings, CandidateLaneSettings


def _candidates() -> list[dict[str, object]]:
    return [
        {
            "ticker": ticker,
            "candidate_group": "Technology",
            "sector_strategy_rank": rank,
            "sector_market_cap_rank": rank,
            "revenue_growth": 0.2,
            "free_cash_flow": 10_000_000_000,
        }
        for rank, ticker in enumerate(("AAA", "BBB", "CCC"), start=1)
    ]


def test_total_deadline_stops_retries_and_fills_deterministic_fallbacks() -> None:
    clear_company_rationale_cache()
    clock = [0.0]
    calls: list[list[str]] = []
    settings = CandidateLaneSettings(
        ai_company_batch_size=2,
        ai_company_total_timeout_seconds=60,
    )

    def run_ai(ai_settings, context):  # type: ignore[no-untyped-def]
        calls.append([str(row["ticker"]) for row in context.candidates])
        assert ai_settings.timeout_seconds == 60
        clock[0] += 61
        return "partial"

    def parse(raw, context):  # type: ignore[no-untyped-def]
        ticker = str(context.candidates[0]["ticker"])
        return {ticker: f"{ticker}-AI"}

    result = run_company_rationales(
        _candidates(),
        news={},
        as_of=date(2026, 7, 29),
        candidate_settings=settings,
        ai_settings=AIBriefingSettings(enabled=True),
        run_ai=run_ai,
        parse=parse,
        clock=lambda: clock[0],
    )

    assert calls == [["AAA", "BBB"]]
    assert result["AAA"] == "AAA-AI"
    assert "上涨逻辑：" in result["BBB"]
    assert "反证：" in result["CCC"]


def test_accepted_ai_results_are_reused_from_semantic_cache() -> None:
    clear_company_rationale_cache()
    calls = 0

    def run_ai(ai_settings, context):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return "complete"

    def parse(raw, context):  # type: ignore[no-untyped-def]
        return {
            str(row["ticker"]): f"{row['ticker']}-AI"
            for row in context.candidates
        }

    kwargs = {
        "news": {},
        "as_of": date(2026, 7, 29),
        "candidate_settings": CandidateLaneSettings(),
        "ai_settings": AIBriefingSettings(enabled=True),
        "run_ai": run_ai,
        "parse": parse,
    }
    first = run_company_rationales(_candidates(), **kwargs)
    second = run_company_rationales(_candidates(), **kwargs)

    assert first == second
    assert calls == 1
