from __future__ import annotations

import os
import subprocess
from typing import Any

from quant_signal.ai_briefing import (
    AIBriefingContext,
    USBriefingAIContext,
    build_ai_briefing_prompt,
    run_ai_briefing,
    validate_us_briefing_output,
)
from quant_signal.config import AIBriefingSettings
from quant_signal.notifier.base import CardKind
from quant_signal.notifier.cards import build_ai_briefing_card


def _context() -> AIBriefingContext:
    return AIBriefingContext(
        as_of="2026-07-09 08:00 ET",
        signals=[
            {
                "ticker": "AAA",
                "direction": "BUY",
                "price": 101.2,
                "target_buy": 99.8,
                "take_profit": 112.5,
                "stop_loss": 94.2,
                "strategy": "momentum_rotation",
                "reason": "60日动量排名靠前",
            }
        ],
        ranking=[
            {"ticker": "AAA", "momentum": 0.18, "price": 101.2},
            {"ticker": "BBB", "momentum": -0.05, "price": 88.0},
        ],
        holdings=["BBB"],
        notes=[
            "FEISHU_WEBHOOK=https://example.invalid/secret",
            "ALPACA_SECRET=super-secret",
            "正常市场备注",
        ],
        analysis_cards=[
            {
                "title": "📋 盘前早报 · 美股组",
                "body": "| 标的 | 方向 | 目标买入价 | 止盈目标 | 止损价 |\n"
                "| AAA | BUY | 99.80 | 112.50 | 94.20 |",
            }
        ],
    )


def test_prompt_is_bounded_and_redacts_secret_like_notes() -> None:
    prompt = build_ai_briefing_prompt(_context(), max_chars=500)

    assert len(prompt) <= 500
    assert "仅供观察，不构成投资建议" in prompt
    assert "FEISHU_WEBHOOK" not in prompt
    assert "ALPACA_SECRET" not in prompt
    assert "正常市场备注" in prompt


def test_prompt_requires_price_plan_and_includes_existing_analysis_cards() -> None:
    prompt = build_ai_briefing_prompt(_context(), max_chars=3000)

    assert "观察买入价" in prompt
    assert "止盈/卖出价" in prompt
    assert "止损价" in prompt
    assert "优先引用输入中的 target_buy、take_profit、stop_loss" in prompt
    assert "盘前早报" in prompt
    assert "不要运行命令" in prompt
    assert "不要读取文件" in prompt
    assert "112.50" in prompt


def test_prompt_preserves_core_context_when_analysis_cards_are_long() -> None:
    context = _context().model_copy(
        update={
            "analysis_cards": [
                {
                    "title": "📋 盘前早报 · 美股组",
                    "body": "很长的早报正文 " * 500 + " | AAA | BUY | 99.80 | 112.50 | 94.20 |",
                },
                {
                    "title": "📊 动量全池榜单",
                    "body": "很长的榜单正文 " * 500 + " AAA momentum 18%",
                },
            ]
        }
    )

    prompt = build_ai_briefing_prompt(context, max_chars=1500)

    assert len(prompt) <= 1500
    assert '"signals"' in prompt
    assert '"analysis_cards"' in prompt
    assert '"ranking"' in prompt
    assert '"holdings"' in prompt
    assert "AAA" in prompt
    assert "target_buy" in prompt
    assert "take_profit" in prompt
    assert "stop_loss" in prompt
    assert "盘前早报" in prompt
    assert prompt.count("{") == prompt.count("}")


def test_codex_cli_is_default_ai_provider() -> None:
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="观点", stderr="")

    result = run_ai_briefing(
        AIBriefingSettings(enabled=True),
        _context(),
        runner=runner,
    )

    assert result == "观点"
    command = calls[0]["args"][0]
    if os.name == "nt":
        assert command[0].lower().endswith((".cmd", ".bat"))
        command = ["codex", *command[1:]]
    assert command[:2] == ["codex", "exec"]


def test_claude_code_cli_uses_print_mode() -> None:
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="观点", stderr="")

    result = run_ai_briefing(
        AIBriefingSettings(
            enabled=True,
            provider="claude_code_cli",
            command="claude",
            timeout_seconds=12,
        ),
        _context(),
        runner=runner,
    )

    assert result == "观点"
    command = calls[0]["args"][0]
    assert command[:2] == ["claude", "-p"]
    assert calls[0]["kwargs"]["timeout"] == 12
    assert calls[0]["kwargs"]["capture_output"] is True
    assert calls[0]["kwargs"]["text"] is True


def test_codex_cli_uses_exec_read_only_mode() -> None:
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="观点", stderr="")

    result = run_ai_briefing(
        AIBriefingSettings(
            enabled=True,
            provider="codex_cli",
            command="codex",
            timeout_seconds=9,
        ),
        _context(),
        runner=runner,
    )

    assert result == "观点"
    command = calls[0]["args"][0]
    if os.name == "nt":
        assert command[0].lower().endswith((".cmd", ".bat"))
        command = ["codex", *command[1:]]
    assert command[:5] == [
        "codex",
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
    ]
    assert command[-1] != calls[0]["kwargs"]["input"]
    assert "signals" in calls[0]["kwargs"]["input"]
    assert calls[0]["kwargs"]["timeout"] == 9
    assert calls[0]["kwargs"]["encoding"] == "utf-8"
    assert calls[0]["kwargs"]["errors"] == "replace"


def test_codex_cli_prefers_output_last_message_file() -> None:
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        calls.append({"args": args, "kwargs": kwargs})
        output_path = command[command.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("最终观点")
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="codex progress noise", stderr=""
        )

    result = run_ai_briefing(
        AIBriefingSettings(enabled=True, provider="codex_cli"),
        _context(),
        runner=runner,
    )

    assert result == "最终观点"
    assert "--output-last-message" in calls[0]["args"][0]


def test_cli_returns_none_on_failure_timeout_or_empty_output() -> None:
    def failed_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=1, stdout="", stderr="boom")

    def empty_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="  ", stderr="")

    def timeout_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    settings = AIBriefingSettings(enabled=True, provider="claude_code_cli")

    assert run_ai_briefing(settings, _context(), runner=failed_runner) is None
    assert run_ai_briefing(settings, _context(), runner=empty_runner) is None
    assert run_ai_briefing(settings, _context(), runner=timeout_runner) is None


def test_disabled_ai_briefing_does_not_call_cli() -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("disabled provider should not call CLI")

    assert (
        run_ai_briefing(
            AIBriefingSettings(enabled=False, provider="claude_code_cli"),
            _context(),
            runner=runner,
        )
        is None
    )


def test_ai_briefing_card_is_report() -> None:
    card = build_ai_briefing_card("今日偏观察，等待回踩。")

    assert card.kind is CardKind.REPORT
    assert "AI" in card.title
    assert "今日偏观察" in card.body_md
    assert "仅供观察，不构成投资建议" in card.body_md


def test_prompt_includes_execution_plan_guardrails() -> None:
    context = AIBriefingContext(
        as_of="2026-07-10T12:15:00+00:00",
        execution_plans=[
            {
                "ticker": "AAPL",
                "state": "CANDIDATE",
                "limit_price": 102.0,
                "suggested_qty": 71,
                "suggested_notional": 7242.0,
                "stop_loss": 95.0,
                "take_profit": 115.0,
                "account_label": "PAPER",
            }
        ],
    )
    prompt = build_ai_briefing_prompt(context)

    # 结构化计划数据必须原样进入 prompt
    assert "execution_plans" in prompt
    assert "7242" in prompt
    # 硬约束: 结构化价格/数量不可改写, 缺失只能写不可用, PAPER 不得称实盘
    assert "limit_price" in prompt and "suggested_qty" in prompt
    assert "禁止改写" in prompt or "不可改写" in prompt
    assert "不可用" in prompt
    assert "PAPER" in prompt
    assert "实盘" in prompt


def _us_context() -> USBriefingAIContext:
    return USBriefingAIContext(
        report_kind="US_CLOSE",
        as_of="2026-07-14",
        regime={"regime": "PULLBACK", "breadth_above_50d": 0.58},
        candidates=[
            {
                "ticker": "AAPL",
                "lane": "TREND_PULLBACK",
                "entry_low": 205.0,
                "entry_high": 208.0,
                "invalidation_price": 198.0,
                "target_price": 228.0,
            }
        ],
        discipline=[
            {
                "ticker": "MU",
                "incremental_sell_qty": 25,
                "protection_price": 92.0,
            }
        ],
        portfolio_risk={"total_effective_weight": 0.82},
        data_quality=["COMPLETE"],
    )


def test_us_prompt_is_versioned_and_forbids_numeric_rewrites() -> None:
    prompt = build_ai_briefing_prompt(_us_context(), max_chars=4000)

    assert '"schema_version": "us-briefing-v1"' in prompt
    assert '"report_kind": "US_CLOSE"' in prompt
    assert "不得新增或修改任何数字" in prompt
    assert "只解释候选与持仓纪律，不重新计算" in prompt


def test_us_ai_output_rejects_changed_discipline_price() -> None:
    assert validate_us_briefing_output("MU 保护价 91.00", _us_context()) is None


def test_us_ai_output_accepts_structured_numbers_and_tickers() -> None:
    output = "市场处于回调。MU 保护价 92.0；AAPL 观察 205.0-208.0。仅供观察，不构成投资建议。"
    assert validate_us_briefing_output(output, _us_context()) == output


def test_prompt_without_execution_plans_omits_execution_rules() -> None:
    context = AIBriefingContext(as_of="2026-07-10T12:15:00+00:00")
    prompt = build_ai_briefing_prompt(context)
    assert "execution_plans" not in prompt


def test_action_card_ai_prompt_requires_three_short_points() -> None:
    context = AIBriefingContext(
        as_of="2026-07-10T12:15:00+00:00",
        output_mode="action_card",
        execution_plans=[{"ticker": "AAPL", "suggested_qty": 1}],
    )
    prompt = build_ai_briefing_prompt(context)
    assert "最多300个中文字符" in prompt
    assert "主线、最大风险、今日倾向" in prompt
