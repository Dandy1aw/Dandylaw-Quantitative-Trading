from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any, Literal

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
    context: AIBriefingContext, max_chars: int = 6000
) -> str:
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
    if Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        return [resolved, *command[1:]]
    return command


def run_ai_briefing(
    settings: AIBriefingSettings,
    context: AIBriefingContext,
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
