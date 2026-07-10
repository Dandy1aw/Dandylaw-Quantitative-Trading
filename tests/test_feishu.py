from datetime import datetime, timezone

import httpx
import pytest

from conftest import make_test_settings
from quant_signal.notifier.base import Card, CardKind, CardSection, ConsoleNotifier
from quant_signal.notifier.cards import alert_card, signal_card
from quant_signal.notifier.feishu import FeishuNotifier, _to_feishu_payload, get_notifier
from quant_signal.strategies.base import Direction, Signal


def make_signal() -> Signal:
    return Signal(
        ticker="NVDA",
        direction=Direction.BUY,
        price=102.0,
        reason="突破20日高点",
        strategy_id="breakout_20d",
        ts=datetime(2026, 7, 6, 14, 30, tzinfo=timezone.utc),
        suggested_weight=0.33,
    )


def test_signal_card_contains_fields_and_sgt_time() -> None:
    card = signal_card(make_signal())
    assert card.kind is CardKind.SIGNAL
    assert "NVDA" in card.title and "BUY" in card.title.upper()
    assert "102.0" in card.body_md and "突破20日高点" in card.body_md
    assert "22:30" in card.body_md          # 14:30 UTC = 22:30 SGT
    assert card.url == "https://www.tradingview.com/chart/?symbol=NVDA"


def test_signal_card_delayed_flag() -> None:
    assert "延迟" in signal_card(make_signal(), delayed=True).body_md


def test_feishu_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        sent["url"] = url
        sent["payload"] = json
        return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ok = FeishuNotifier("https://open.feishu.cn/hook/xxx").send(alert_card("t", "b"))
    assert ok is True
    assert sent["url"] == "https://open.feishu.cn/hook/xxx"
    assert sent["payload"]["msg_type"] == "interactive"  # type: ignore[index]


def test_feishu_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_post(url: str, json: dict, timeout: float) -> httpx.Response:  # type: ignore[type-arg]
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("quant_signal.notifier.feishu._BACKOFF", [0, 0, 0])
    ok = FeishuNotifier("https://open.feishu.cn/hook/xxx").send(alert_card("t", "b"))
    assert ok is False and calls["n"] == 3


def test_get_notifier_falls_back_to_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # 显式置空：load_dotenv 不覆盖已存在的环境变量，避免受本机 .env 已填 webhook 影响。
    monkeypatch.setenv("FEISHU_WEBHOOK", "")
    assert isinstance(get_notifier(make_test_settings(feishu_webhook="")), ConsoleNotifier)


def test_feishu_renders_structured_sections_as_separate_divs() -> None:
    card = Card(
        kind=CardKind.REPORT,
        title="行动卡",
        body_md="账户\n计划",
        sections=(CardSection("账户"), CardSection("计划")),
    )
    payload = _to_feishu_payload(card)
    elements = payload["card"]["elements"]  # type: ignore[index]
    divs = [element for element in elements if element["tag"] == "div"]  # type: ignore[index]
    assert [div["text"]["content"] for div in divs] == ["账户", "计划"]  # type: ignore[index]
