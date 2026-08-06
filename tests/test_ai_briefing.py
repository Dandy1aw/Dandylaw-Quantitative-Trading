from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
from typing import Any

from quant_signal.ai_briefing import (
    AIBriefingContext,
    CompanyRationaleAIContext,
    USBriefingAIContext,
    build_ai_briefing_prompt,
    _resolve_windows_script_command,
    run_ai_briefing,
    parse_company_rationales,
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
        assert command[0].lower().endswith(".exe")
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
        assert command[0].lower().endswith(".exe")
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


def test_large_us_prompt_keeps_complete_compact_json() -> None:
    base = _us_context()
    context = base.model_copy(
        update={
            "candidates": [
                {**base.candidates[0], "ticker": f"N{index:02d}"}
                for index in range(9)
            ],
            "discipline": [
                {
                    **base.discipline[0],
                    "ticker": f"H{index:02d}",
                    "next_state": {
                        "basis_version": "internal-state-that-ai-does-not-need" * 5,
                        "notified_stage": 2,
                    },
                }
                for index in range(5)
            ],
            "observations": [
                {
                    "ticker": f"X{index:02d}",
                    "reason": "INSUFFICIENT_HISTORY",
                    "history_days": index,
                }
                for index in range(80)
            ],
        }
    )

    prompt = build_ai_briefing_prompt(context, max_chars=4000)
    payload_text = prompt.split("输入数据：\n", 1)[1].split("\n\n必须保留：", 1)[0]
    payload = json.loads(payload_text)

    assert len(prompt) <= 4000
    assert payload["schema_version"] == "us-briefing-v1"
    assert payload["candidates"] and payload["discipline"]
    assert payload["observation_counts"] == {"INSUFFICIENT_HISTORY": 80}
    assert all("next_state" not in row for row in payload["discipline"])


def test_us_ai_output_rejects_changed_discipline_price() -> None:
    assert validate_us_briefing_output("MU 保护价 91.00", _us_context()) is None


def test_us_ai_output_accepts_structured_numbers_and_tickers() -> None:
    output = "市场处于回调。MU 保护价 92.0；AAPL 观察 205.0-208.0。仅供观察，不构成投资建议。"
    assert validate_us_briefing_output(output, _us_context()) == output


def test_us_ai_output_accepts_derived_observation_count() -> None:
    context = _us_context().model_copy(
        update={
            "observations": [
                {"ticker": f"X{index:02d}", "reason": "OVERHEATED"}
                for index in range(4)
            ]
        }
    )
    output = "有 4 个标的因过热被排除。仅供观察，不构成投资建议。"

    assert validate_us_briefing_output(output, context) == output


def test_us_ai_output_accepts_numbers_from_payload_keys() -> None:
    context = _us_context()
    output = "50日线上宽度为 58%。仅供观察，不构成投资建议。"

    assert validate_us_briefing_output(output, context) == output


def test_us_ai_output_accepts_uppercase_terms_present_in_payload() -> None:
    context = _us_context().model_copy(
        update={"regime": {"regime": "TREND", "breadth_above_50d": 0.58}}
    )
    output = "市场状态为 TREND。仅供观察，不构成投资建议。"

    assert validate_us_briefing_output(output, context) == output


def _company_context() -> CompanyRationaleAIContext:
    return CompanyRationaleAIContext(
        as_of="2026-07-14",
        max_chars_per_company=220,
        candidates=[
            {
                "ticker": "MSFT",
                "company_name": "Microsoft",
                "gics_sector": "Information Technology",
                "candidate_group": "Technology",
                "industry": "Software - Infrastructure",
                "market_cap_usd": 3_500_000_000_000,
                "sector_strategy_rank": 1,
                "sector_market_cap_rank": 1,
                "revenue_growth": 0.15,
                "earnings_growth": 0.18,
                "profit_margin": 0.36,
                "free_cash_flow": 74_000_000_000,
                "reasons": ["趋势延续"],
            }
        ],
        news={
            "MSFT": [
                {
                    "headline": "Cloud demand remains strong",
                    "created_at": "2026-07-13T12:00:00+00:00",
                    "source": "test",
                }
            ]
        },
    )


def test_company_prompt_requires_evidence_rank_moat_and_counter_thesis() -> None:
    prompt = build_ai_briefing_prompt(_company_context(), max_chars=5000)

    assert "上涨逻辑：" in prompt
    assert "行业地位：" in prompt
    assert "壁垒：" in prompt
    assert "反证：" in prompt
    assert "sector_strategy_rank" in prompt
    assert '"candidate_group": "Technology"' in prompt
    assert "Cloud demand remains strong" in prompt
    assert "不得新增标的、排名或数字" in prompt


def test_company_prompt_requires_chinese_and_human_readable_financial_units() -> None:
    prompt = build_ai_briefing_prompt(_company_context(), max_chars=5000)

    assert "除股票代码、公司名称和行业专用名词外，全部使用中文" in prompt
    assert "百分比" in prompt
    assert "亿美元或万亿美元" in prompt
    assert "不得输出未经格式化的长整数" in prompt
    assert "三级止盈和买盘资金强度只能解释" in prompt


def test_company_rationale_parser_accepts_schema_and_rejects_unknown_or_absolute() -> None:
    valid = (
        "[MSFT]\n"
        "上涨逻辑：云业务需求与盈利增长支持趋势延续。\n"
        "行业地位：行业策略第1，合格同行市值第1。\n"
        "壁垒：企业客户迁移成本、云与办公软件协同形成难复制能力。\n"
        "反证：云增速或利润率显著低于当前输入时，逻辑失效。"
    )
    parsed = parse_company_rationales(valid, _company_context())
    assert parsed == {"MSFT": "\n".join(valid.splitlines()[1:])}

    assert parse_company_rationales(valid.replace("[MSFT]", "[NVDA]"), _company_context()) == {}
    assert parse_company_rationales(valid.replace("难复制", "不可替代"), _company_context()) == {}
    acronym_output = valid.replace(
        "行业策略第1，合格同行市值第1",
        "行业内策略排名1；合格同行市值排名1",
    ).replace("企业客户迁移成本", "AI与CXL企业客户迁移成本")
    assert set(parse_company_rationales(acronym_output, _company_context())) == {"MSFT"}


def test_company_rationale_parser_accepts_human_readable_financial_units() -> None:
    output = (
        "[MSFT]\n"
        "上涨逻辑：营收增长15%，自由现金流740.00亿美元支持趋势。\n"
        "行业地位：行业策略第1，合格同行市值第1。\n"
        "壁垒：企业客户迁移成本与产品协同难复制。\n"
        "反证：营收增长转弱时逻辑失效。"
    )

    assert set(parse_company_rationales(output, _company_context())) == {"MSFT"}


def test_company_rationale_parser_rejects_unformatted_long_financial_integer() -> None:
    output = (
        "[MSFT]\n"
        "上涨逻辑：自由现金流74000000000美元支持趋势。\n"
        "行业地位：行业策略第1，合格同行市值第1。\n"
        "壁垒：企业客户迁移成本与产品协同难复制。\n"
        "反证：现金流转弱时逻辑失效。"
    )

    assert parse_company_rationales(output, _company_context()) == {}


def test_company_rationale_parser_rejects_changed_rank_and_length() -> None:
    valid = (
        "[MSFT]\n"
        "上涨逻辑：云业务需求支持趋势。\n"
        "行业地位：行业策略第1，合格同行市值第1。\n"
        "壁垒：企业客户迁移成本较高。\n"
        "反证：云增速下降会令逻辑失效。"
    )
    assert parse_company_rationales(valid.replace("策略第1", "策略第2"), _company_context()) == {}
    short_limit = _company_context().model_copy(update={"max_chars_per_company": 80})
    too_long = valid.replace("企业客户迁移成本较高", "企业客户迁移成本较高" * 20)
    assert parse_company_rationales(too_long, short_limit) == {}


def test_windows_codex_wrapper_resolves_to_packaged_native_binary(
    tmp_path: Path, monkeypatch: object
) -> None:
    if os.name != "nt":
        return
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text("@echo off", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"test")
    monkeypatch.setattr(  # type: ignore[attr-defined]
        "quant_signal.ai_briefing.shutil.which", lambda _: str(wrapper)
    )

    resolved = _resolve_windows_script_command(["codex", "exec"])

    assert resolved == [str(native), "exec"]


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
    assert "最多180个字符" in prompt
    assert "主线：" in prompt and "持仓：" in prompt and "动作：" in prompt


def test_action_card_output_requires_exact_three_lines_and_hard_cap() -> None:
    context = AIBriefingContext(
        as_of="2026-07-10T12:15:00+00:00",
        output_mode="action_card",
        holdings=["NVDA"],
    )

    def valid_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="主线：风险偏好稳定\n持仓：NVDA继续观察\n动作：不追价",
            stderr="",
        )

    settings = AIBriefingSettings(
        enabled=True,
        provider="claude_code_cli",
        output_max_chars=180,
    )
    output = run_ai_briefing(settings, context, runner=valid_runner)
    assert output == "主线：风险偏好稳定\n持仓：NVDA继续观察\n动作：不追价"
    assert len(output) <= 180

    def invalid_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="一段没有固定结构的长观点", stderr=""
        )

    assert run_ai_briefing(settings, context, runner=invalid_runner) is None


def test_action_card_output_rejects_over_limit() -> None:
    context = AIBriefingContext(
        as_of="2026-07-10T12:15:00+00:00", output_mode="action_card"
    )

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="主线：" + "长" * 100 + "\n持仓：" + "长" * 100 + "\n动作：不追价",
            stderr="",
        )

    assert run_ai_briefing(
        AIBriefingSettings(
            enabled=True, provider="claude_code_cli", output_max_chars=180
        ),
        context,
        runner=runner,
    ) is None
