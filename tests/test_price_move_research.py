from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any

from quant_signal.config import PriceMoveCauseSearchSettings
from quant_signal.price_move_research import research_price_move_causes
from quant_signal.strategies.base import Direction, Signal


NOW = datetime(2026, 8, 5, 16, 2, tzinfo=UTC)


def _signal(ticker: str = "GOOGL") -> Signal:
    return Signal(
        ticker=ticker,
        direction=Direction.SELL,
        price=364.54,
        reason="1分钟下跌 1.60%",
        strategy_id="holding_price_alert",
        ts=NOW,
        extra={
            "window": "1分钟",
            "move_pct": -0.016,
            "session_pct": -0.0397,
            "volume_ratio": 32.1,
            "data_feed": "alpaca_iex_1m_realtime_partial",
        },
    )


def test_codex_search_command_and_structured_result() -> None:
    calls: list[dict[str, Any]] = []

    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        calls.append({"command": command, "kwargs": kwargs})
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "causes": [
                        {
                            "ticker": "GOOGL",
                            "category": "company_news",
                            "confidence": "high",
                            "summary": "公司披露管理层重大调整，与跳水时间接近。",
                            "evidence": [
                                {
                                    "title": "Alphabet announces management change",
                                    "source": "Reuters",
                                    "url": "https://www.reuters.com/example",
                                    "published_at": "2026-08-05T15:58:00Z",
                                }
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = research_price_move_causes(
        [_signal()],
        PriceMoveCauseSearchSettings(enabled=True),
        now=NOW,
        runner=runner,
    )

    assert result["GOOGL"].category == "company_news"
    assert result["GOOGL"].confidence == "high"
    assert result["GOOGL"].evidence[0].url == "https://www.reuters.com/example"
    command = calls[0]["command"]
    assert "--search" in command
    assert "gpt-5.6-terra" in command
    assert 'model_reasoning_effort="low"' in command
    assert "exec" in command
    assert "--sandbox" in command and "read-only" in command
    assert "--output-schema" in command
    prompt = calls[0]["kwargs"]["input"]
    assert "GOOGL" in prompt
    assert "364.54" in prompt
    assert "1分钟下跌" in prompt
    assert "必须联网搜索" in prompt


def test_timeout_returns_explicit_unconfirmed_result() -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args[0], timeout=1)

    result = research_price_move_causes(
        [_signal()],
        PriceMoveCauseSearchSettings(enabled=True, timeout_seconds=5),
        now=NOW,
        runner=runner,
    )

    cause = result["GOOGL"]
    assert cause.category == "unconfirmed"
    assert cause.confidence == "low"
    assert cause.evidence == ()
    assert "未确认" in cause.summary
    assert cause.search_status == "timeout"


def test_disabled_search_does_not_spawn_codex() -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("disabled search must not spawn Codex")

    result = research_price_move_causes(
        [_signal()],
        PriceMoveCauseSearchSettings(enabled=False),
        now=NOW,
        runner=runner,
    )

    assert result["GOOGL"].search_status == "disabled"


def test_unrequested_ticker_and_unsafe_url_are_rejected() -> None:
    def runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = args[0]
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "causes": [
                        {
                            "ticker": "MSFT",
                            "category": "company_news",
                            "confidence": "high",
                            "summary": "无关结果",
                            "evidence": [],
                        },
                        {
                            "ticker": "GOOGL",
                            "category": "company_news",
                            "confidence": "medium",
                            "summary": "只有不安全链接。",
                            "evidence": [
                                {
                                    "title": "bad",
                                    "source": "bad",
                                    "url": "file:///secret",
                                    "published_at": None,
                                }
                            ],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = research_price_move_causes(
        [_signal()],
        PriceMoveCauseSearchSettings(enabled=True),
        now=NOW,
        runner=runner,
    )

    assert set(result) == {"GOOGL"}
    assert result["GOOGL"].evidence == ()
