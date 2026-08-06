"""Bounded, cached company-rationale orchestration with deterministic fallback."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from hashlib import sha256
import json
import math
import time

import structlog

from quant_signal.ai_briefing import CompanyRationaleAIContext
from quant_signal.config import AIBriefingSettings, CandidateLaneSettings

log = structlog.get_logger()

RunAI = Callable[[AIBriefingSettings, CompanyRationaleAIContext], str | None]
ParseAI = Callable[[str, CompanyRationaleAIContext], dict[str, str]]
_CACHE_LIMIT = 512
_RATIONALE_CACHE: OrderedDict[str, str] = OrderedDict()


def clear_company_rationale_cache() -> None:
    _RATIONALE_CACHE.clear()


def _cache_key(
    candidate: Mapping[str, object],
    news: Sequence[Mapping[str, object]],
    as_of: date,
) -> str:
    payload = {
        "as_of": as_of.isoformat(),
        "candidate": dict(candidate),
        "news": [dict(item) for item in news],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _remember(key: str, rationale: str) -> None:
    _RATIONALE_CACHE[key] = rationale
    _RATIONALE_CACHE.move_to_end(key)
    while len(_RATIONALE_CACHE) > _CACHE_LIMIT:
        _RATIONALE_CACHE.popitem(last=False)


def _display_number(value: object) -> str:
    if not isinstance(value, (int, float, str)):
        return "暂无可靠数据"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "暂无可靠数据"
    if not math.isfinite(number):
        return "暂无可靠数据"
    return f"{number:.1%}" if abs(number) <= 2 else f"{number:,.0f}"


def _fallback(candidate: Mapping[str, object]) -> str:
    group = str(candidate.get("candidate_group") or "所属行业")
    strategy_rank = candidate.get("sector_strategy_rank") or "暂无"
    market_cap_rank = candidate.get("sector_market_cap_rank") or "暂无"
    growth = _display_number(candidate.get("revenue_growth"))
    cash_flow = _display_number(candidate.get("free_cash_flow"))
    return "\n".join(
        (
            f"上涨逻辑：策略趋势通过筛选；营收增长 {growth}、自由现金流 {cash_flow} 仅作基本面验证。",
            f"行业地位：{group}策略第 {strategy_rank}，合格同行市值第 {market_cap_rank}。",
            "壁垒：大市值、现金流与行业排名形成规模优势，仍需后续数据持续确认。",
            "反证：跌破策略失效位，或营收增长、自由现金流持续转弱时，该逻辑失效。",
        )
    )


def run_company_rationales(
    candidates: Sequence[dict[str, object]],
    *,
    news: Mapping[str, list[dict[str, object]]],
    as_of: date,
    candidate_settings: CandidateLaneSettings,
    ai_settings: AIBriefingSettings,
    run_ai: RunAI,
    parse: ParseAI,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    output: dict[str, str] = {}
    key_by_ticker: dict[str, str] = {}
    candidate_by_ticker: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        ticker = str(candidate.get("ticker", "")).upper()
        if not ticker:
            continue
        candidate_by_ticker[ticker] = candidate
        key = _cache_key(candidate, news.get(ticker, []), as_of)
        key_by_ticker[ticker] = key
        cached = _RATIONALE_CACHE.get(key)
        if cached is not None:
            output[ticker] = cached
            _RATIONALE_CACHE.move_to_end(key)

    deadline = clock() + candidate_settings.ai_company_total_timeout_seconds

    def call(batch: list[dict[str, object]]) -> None:
        remaining = deadline - clock()
        if not batch or remaining <= 0:
            return
        tickers = {
            str(row.get("ticker", "")).upper()
            for row in batch
            if row.get("ticker")
        }
        context = CompanyRationaleAIContext(
            as_of=as_of.isoformat(),
            candidates=batch,
            news={ticker: news.get(ticker, []) for ticker in tickers},
            max_chars_per_company=candidate_settings.ai_company_max_chars,
        )
        bounded_settings = ai_settings.model_copy(
            deep=True,
            update={
                "timeout_seconds": max(
                    1,
                    min(ai_settings.timeout_seconds, math.ceil(remaining)),
                )
            },
        )
        try:
            raw = run_ai(bounded_settings, context)
            parsed = parse(raw, context) if raw else {}
        except Exception as error:  # noqa: BLE001
            log.warning(
                "company_analysis.ai_failed",
                tickers=sorted(tickers),
                error=str(error),
            )
            return
        for ticker, rationale in parsed.items():
            normalized = ticker.upper()
            if normalized not in candidate_by_ticker:
                continue
            output[normalized] = rationale
            _remember(key_by_ticker[normalized], rationale)

    uncached = [
        candidate
        for ticker, candidate in candidate_by_ticker.items()
        if ticker not in output
    ]
    batch_size = candidate_settings.ai_company_batch_size
    for offset in range(0, len(uncached), batch_size):
        if clock() >= deadline:
            break
        call(uncached[offset : offset + batch_size])

    missing = [
        candidate
        for ticker, candidate in candidate_by_ticker.items()
        if ticker not in output
    ]
    for candidate in missing:
        if clock() >= deadline:
            break
        call([candidate])

    for ticker, candidate in candidate_by_ticker.items():
        output.setdefault(ticker, _fallback(candidate))
    return output
