from datetime import datetime, timezone

import httpx
import pytest

from quant_signal.config import load_settings
from quant_signal.notifier.base import ConsoleNotifier, CardKind
from quant_signal.notifier.cards import alert_card, signal_card
from quant_signal.notifier.feishu import FeishuNotifier, get_notifier
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
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    assert isinstance(get_notifier(load_settings()), ConsoleNotifier)
