from __future__ import annotations

import json
import os
from pathlib import Path
from collections import Counter
from decimal import Decimal, InvalidOperation
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, Field

from quant_signal.config import AIBriefingSettings

log = structlog.get_logger()

Runner = Callable[..., subprocess.CompletedProcess[str]]

_SECRET_MARKERS = (
    "SECRET",
    "TOKEN",
    "WEBHOOK",
    "PASSWORD",
    "API_KEY",
    "ALPACA_KEY",
    "ALPACA_SECRET",
    "FEISHU_WEBHOOK",
)


class AIBriefingContext(BaseModel):
    as_of: str
    signals: list[dict[str, object]] = Field(default_factory=list)
    ranking: list[dict[str, object]] = Field(default_factory=list)
    holdings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    analysis_cards: list[dict[str, str]] = Field(default_factory=list)
    execution_plans: list[dict[str, object]] = Field(default_factory=list)
    output_mode: Literal["full", "action_card"] = "full"


class USBriefingAIContext(BaseModel):
    schema_version: Literal["us-briefing-v1"] = "us-briefing-v1"
    report_kind: Literal["US_CLOSE", "ASIA_CONFIRM"]
    as_of: str
    regime: dict[str, object]
    candidates: list[dict[str, object]] = Field(default_factory=list)
    discipline: list[dict[str, object]] = Field(default_factory=list)
    portfolio_risk: dict[str, object] = Field(default_factory=dict)
    observations: list[dict[str, object]] = Field(default_factory=list)
    data_quality: list[str] = Field(default_factory=list)


def _is_secret_like(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return None if _is_secret_like(value) else value
    if isinstance(value, list):
        return [clean for item in value if (clean := _sanitize(item)) is not None]
    if isinstance(value, dict):
        clean_dict: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_like(str(key)):
                continue
            clean_item = _sanitize(item)
            if clean_item is not None:
                clean_dict[str(key)] = clean_item
        return clean_dict
    return value


def build_ai_briefing_prompt(
    context: AIBriefingContext | USBriefingAIContext, max_chars: int = 6000
) -> str:
    if isinstance(context, USBriefingAIContext):
        instructions = (
            "你是量化交易系统的简报解释员。只解释本 prompt 中的结构化数据，"
            "不运行命令、不读文件、不联网。价格、区间、股数、比例、市场状态均由"
            "确定性规则产生；不得新增或修改任何数字，不得新增标的，不得重新计算。"
            "只解释候选与持仓纪律，不重新计算。输出不超过 400 个中文字符，按“市场、"
            "候选、持仓、风险”四个短段落组织，不使用表格，不声称已经成交。\n\n"
            "输入数据：\n"
        )
        suffix = "\n\n必须保留：仅供观察，不构成投资建议。"
        budget = max(0, max_chars - len(instructions) - len(suffix))
        payload = _bounded_us_context_payload(context, budget)
        return f"{instructions}{payload}{suffix}"[:max_chars]
    if context.output_mode == "action_card":
        instructions = (
            "你是量化交易系统的行动卡分析员。只分析本 prompt 输入，不运行命令、不读文件、不联网。"
            "结构化执行计划中的价格、数量、止损和止盈不可改写或补算。"
            "只输出主线、最大风险、今日倾向三点，总计最多300个中文字符；不要表格、不要复述全部标的。"
            "必须保留：仅供观察，不构成投资建议。\n\n输入数据：\n"
        )
    else:
        instructions = (
        "你是量化交易系统的盘前早报交易计划分析员。请先阅读 analysis_cards 里的"
        "盘前早报和动量榜单正文，再结合 signals/ranking/holdings 输出中文观点。"
        "不要运行命令，不要读取文件，不要联网检索；只分析本 prompt 输入的数据。"
        "不要新增输入里没有出现的交易标的，不要覆盖量化策略，不要下单。\n"
        "价格口径硬约束：signals 是交易价位的唯一权威来源；逐标的交易计划里的观察买入价、止盈/卖出价、止损价"
        "只能引用 signals 中的 price、target_buy、take_profit、stop_loss、entry_low、entry_high。"
        "不要从 analysis_cards 正文推导、放大、缩小或换算价格；analysis_cards 只用于理解叙事和风险背景。"
        "必须保留并标注 currency；若 signals 缺少某个价位，只能写“缺少结构化价位/仅观察”，不要自行编造。\n"
        "输出 Markdown，必须包含：今日主线、信号解读、逐标的交易计划、风险提醒、今日倾向、置信度。\n"
        "逐标的交易计划必须给出：观察买入价、止盈/卖出价、止损价、理由。"
        "优先引用输入中的 target_buy、take_profit、stop_loss；若缺少价位，只能基于参考价给观察区间，"
        "并明确标注“按参考价估算/数据不足”。SELL 信号应重点给出卖出/减仓价位和回补观察条件。\n"
        "必须保留这句话：仅供观察，不构成投资建议。\n\n"
        "输入数据：\n"
        )
    execution_rules = (
        "执行计划硬约束：execution_plans 中的 limit_price、suggested_qty、"
        "suggested_notional、stop_loss、take_profit 是确定性规则计算的结构化数据，"
        "禁止改写、放大、缩小、推导或补算任何价格与数量；字段缺失或为 null 时只能写"
        "“不可用”，不得估算；PAPER 是模拟账户，禁止把它描述为实盘账户，"
        "也不得把计划描述为已成交或确定交易指令。\n"
    )
    if context.execution_plans:
        instructions = instructions.replace(
            "输入数据：\n", f"{execution_rules}输入数据：\n"
        )
    compact_instructions = (
        "你是量化交易系统的盘前早报交易计划分析员。只分析本 prompt 输入，不运行命令、不读文件、不联网。"
        "signals 是交易价位的唯一权威来源，analysis_cards 只作叙事背景；必须保留币种和免责声明。"
        "输出中文 Markdown。\n\n输入数据：\n"
    )
    suffix = "\n\n再次强调：仅供观察，不构成投资建议。"
    if max_chars - len(instructions) - len(suffix) < 120:
        instructions = compact_instructions
    budget = max(0, max_chars - len(instructions) - len(suffix))
    payload = _bounded_context_payload(context, budget)
    prompt = f"{instructions}{payload}{suffix}"
    if len(prompt) <= max_chars:
        return prompt
    keep = max(0, max_chars - len(instructions) - len(suffix))
    return f"{instructions}{payload[:keep]}{suffix}"[:max_chars]


def _bounded_context_payload(context: AIBriefingContext, budget: int) -> str:
    raw_base: dict[str, object] = {
        "as_of": context.as_of,
        "notes": context.notes,
        "holdings": context.holdings,
        "signals": context.signals,
        "ranking": context.ranking,
        "analysis_cards": [],
    }
    if context.execution_plans:
        raw_base["execution_plans"] = context.execution_plans
    base = _sanitize(raw_base)
    cards = [
        {
            "title": card.get("title", ""),
            "body": card.get("body", ""),
        }
        for card in context.analysis_cards
    ]
    for body_limit in (1200, 800, 500, 300, 160, 80, 0):
        candidate = dict(base)
        candidate["analysis_cards"] = [
            {
                "title": card["title"],
                "body": _truncate_text(card["body"], body_limit),
            }
            for card in cards
        ]
        payload = json.dumps(_sanitize(candidate), ensure_ascii=False, indent=2)
        if len(payload) <= budget or body_limit == 0:
            return payload
    return json.dumps(base, ensure_ascii=False, indent=2)


def _selected_fields(
    row: dict[str, object], fields: tuple[str, ...]
) -> dict[str, object]:
    return {field: row[field] for field in fields if field in row}


def _bounded_us_context_payload(
    context: USBriefingAIContext, budget: int
) -> str:
    """Serialize a compact, always-valid briefing payload within the prompt budget."""
    regime_fields = (
        "regime",
        "coverage",
        "benchmark_price",
        "benchmark_ma20",
        "benchmark_ma50",
        "benchmark_ma200",
        "breadth_above_20d",
        "breadth_above_50d",
        "breadth_above_200d",
        "participation_5d",
        "realized_volatility",
        "atr_pct",
        "reasons",
        "data_quality",
        "asia_context",
    )
    candidate_fields = (
        "ticker",
        "lane",
        "entry_low",
        "entry_high",
        "invalidation_price",
        "target_price",
        "suggested_qty",
        "suggested_notional",
        "plan_state",
        "block_reason",
        "valid_session",
        "reasons",
    )
    discipline_fields = (
        "ticker",
        "status",
        "leverage",
        "current_price",
        "cost_basis",
        "cost_quality",
        "quantity",
        "quantity_quality",
        "pnl_pct",
        "hard_stop_price",
        "protection_price",
        "next_profit_price",
        "cumulative_sell_fraction",
        "incremental_sell_fraction",
        "pending_sell_fraction",
        "incremental_sell_qty",
        "financing_allowed",
        "effective_weight",
        "warnings",
    )
    risk_fields = (
        "total_effective_exposure",
        "total_effective_weight",
        "leveraged_effective_exposure",
        "leveraged_effective_weight",
        "cluster_effective_weights",
        "warnings",
    )
    observation_counts = Counter(
        str(row.get("reason", "UNKNOWN")) for row in context.observations
    )
    notable_observations = [
        _selected_fields(row, ("ticker", "reason", "history_days", "price"))
        for row in context.observations
        if row.get("ticker") == "SKHY" or row.get("reason") == "EARNINGS_WINDOW"
    ][:8]
    payload: dict[str, object] = {
        "schema_version": context.schema_version,
        "report_kind": context.report_kind,
        "as_of": context.as_of,
        "regime": _selected_fields(context.regime, regime_fields),
        "candidates": [
            _selected_fields(row, candidate_fields) for row in context.candidates
        ],
        "discipline": [
            _selected_fields(row, discipline_fields) for row in context.discipline
        ],
        "portfolio_risk": _selected_fields(context.portfolio_risk, risk_fields),
        "observation_counts": dict(sorted(observation_counts.items())),
        "notable_observations": notable_observations,
        "data_quality": context.data_quality,
    }

    def render() -> str:
        clean = _sanitize(payload)
        return json.dumps(clean, ensure_ascii=False, separators=(",", ": "))

    serialized = render()
    if len(serialized) <= budget:
        return serialized

    payload.pop("notable_observations", None)
    for row in cast(list[dict[str, object]], payload["candidates"]):
        row.pop("reasons", None)
    for row in cast(list[dict[str, object]], payload["discipline"]):
        row.pop("warnings", None)
    cast(dict[str, object], payload["regime"]).pop("reasons", None)
    cast(dict[str, object], payload["portfolio_risk"]).pop("warnings", None)
    serialized = render()
    if len(serialized) <= budget:
        return serialized

    candidates = cast(list[dict[str, object]], payload["candidates"])
    while len(serialized) > budget and len(candidates) > 1:
        candidates.pop()
        payload["candidate_rows_limited"] = True
        serialized = render()
    discipline = cast(list[dict[str, object]], payload["discipline"])
    while len(serialized) > budget and len(discipline) > 1:
        discipline.pop()
        payload["discipline_rows_limited"] = True
        serialized = render()
    if len(serialized) <= budget:
        return serialized

    minimal = {
        "schema_version": context.schema_version,
        "report_kind": context.report_kind,
        "as_of": context.as_of,
        "data_quality": context.data_quality,
        "payload_limited": True,
    }
    serialized = json.dumps(
        _sanitize(minimal), ensure_ascii=False, separators=(",", ": ")
    )
    return serialized if len(serialized) <= budget else "{}"


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def _command(
    settings: AIBriefingSettings, prompt: str, output_path: str | None = None
) -> list[str]:
    if settings.provider == "claude_code_cli":
        return [settings.command or "claude", "-p", prompt]
    if settings.provider == "codex_cli":
        command = [
            settings.command or "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        ]
        if output_path is not None:
            command += ["--output-last-message", output_path]
        return _resolve_windows_script_command(command)
    raise ValueError(f"unsupported ai briefing provider: {settings.provider}")


def _resolve_windows_script_command(command: list[str]) -> list[str]:
    if os.name != "nt":
        return command
    executable = command[0]
    resolved = shutil.which(executable) or executable
    resolved_path = Path(resolved)
    if resolved_path.suffix.lower() in {".cmd", ".bat"}:
        package_root = (
            resolved_path.parent
            / "node_modules"
            / "@openai"
            / "codex"
            / "node_modules"
            / "@openai"
        )
        native = sorted(
            package_root.glob("codex-win32-*/vendor/*/bin/codex.exe")
        )
        if native:
            return [str(native[0]), *command[1:]]
        return [resolved, *command[1:]]
    return command


def run_ai_briefing(
    settings: AIBriefingSettings,
    context: AIBriefingContext | USBriefingAIContext,
    runner: Runner = subprocess.run,
) -> str | None:
    if not settings.enabled:
        return None
    prompt = build_ai_briefing_prompt(context, settings.max_chars)
    output_path: str | None = None
    if settings.provider == "codex_cli":
        with tempfile.NamedTemporaryFile(
            prefix="quant-ai-briefing-", suffix=".md", delete=False
        ) as output_file:
            output_path = output_file.name
    command = _command(settings, prompt, output_path)
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=prompt if settings.provider == "codex_cli" else None,
            stdin=(
                subprocess.DEVNULL
                if settings.provider != "codex_cli"
                else None
            ),
            timeout=settings.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("ai_briefing.timeout", provider=settings.provider)
        if output_path is not None:
            Path(output_path).unlink(missing_ok=True)
        return None
    except OSError as error:
        log.warning(
            "ai_briefing.spawn_failed",
            provider=settings.provider,
            error=str(error),
        )
        if output_path is not None:
            Path(output_path).unlink(missing_ok=True)
        return None
    if completed.returncode != 0:
        log.warning(
            "ai_briefing.failed",
            provider=settings.provider,
            returncode=completed.returncode,
            stderr=completed.stderr[-500:],
        )
        if output_path is not None:
            Path(output_path).unlink(missing_ok=True)
        return None
    if output_path is not None:
        output_path_obj = Path(output_path)
        output = (
            output_path_obj.read_text(encoding="utf-8").strip()
            if output_path_obj.exists()
            else ""
        )
        output_path_obj.unlink(missing_ok=True)
        if output:
            return output
    output = completed.stdout.strip()
    return output or None


_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_TICKER_TOKEN = re.compile(r"\b[A-Z][A-Z0-9.-]{0,6}\b")
_TICKER_EXEMPT = {
    "AI",
    "ATR",
    "ETF",
    "HOLD",
    "QQQ",
    "REDUCE",
    "RSI",
    "USD",
    "WATCH",
}


def _walk_values(value: object) -> list[object]:
    if isinstance(value, dict):
        output: list[object] = []
        for item in value.values():
            output.extend(_walk_values(item))
        return output
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(_walk_values(item))
        return output
    return [value]


def _walk_keys(value: object) -> list[str]:
    if isinstance(value, dict):
        key_output = [str(key) for key in value]
        for item in value.values():
            key_output.extend(_walk_keys(item))
        return key_output
    if isinstance(value, list):
        list_output: list[str] = []
        for item in value:
            list_output.extend(_walk_keys(item))
        return list_output
    return []


def _canonical_number(token: str) -> Decimal | None:
    try:
        return Decimal(token).normalize()
    except InvalidOperation:
        return None


def validate_us_briefing_output(
    output: str, context: USBriefingAIContext
) -> str | None:
    """Reject AI prose that introduces tickers or numeric facts outside the payload."""
    payload = context.model_dump(mode="json")
    allowed_numbers: set[Decimal] = set()
    allowed_tickers: set[str] = set()
    for value in _walk_values(payload):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = _canonical_number(str(value))
            if number is not None:
                allowed_numbers.add(number)
                if abs(number) <= 1:
                    allowed_numbers.add((number * 100).normalize())
        elif isinstance(value, str):
            allowed_numbers.update(
                number
                for token in _NUMBER_TOKEN.findall(value)
                if (number := _canonical_number(token)) is not None
            )
            allowed_tickers.update(_TICKER_TOKEN.findall(value.upper()))
    for key in _walk_keys(payload):
        allowed_numbers.update(
            number
            for token in _NUMBER_TOKEN.findall(key)
            if (number := _canonical_number(token)) is not None
        )
    for rows in (context.candidates, context.discipline, context.observations):
        allowed_numbers.add(Decimal(len(rows)))
    allowed_numbers.update(
        Decimal(count)
        for count in Counter(
            str(row.get("reason", "UNKNOWN")) for row in context.observations
        ).values()
    )
    for row in (*context.candidates, *context.discipline, *context.observations):
        ticker = row.get("ticker")
        if ticker:
            allowed_tickers.add(str(ticker).upper())
    for token in _NUMBER_TOKEN.findall(output):
        number = _canonical_number(token)
        if number is not None and number not in allowed_numbers:
            log.warning("ai_briefing.numeric_guard_rejected", token=token)
            return None
    for token in _TICKER_TOKEN.findall(output):
        if token not in allowed_tickers and token not in _TICKER_EXEMPT:
            log.warning("ai_briefing.ticker_guard_rejected", token=token)
            return None
    return output.strip() or None
