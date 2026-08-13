from datetime import UTC, datetime

import httpx
import pytest
from conftest import make_test_settings

from quant_signal.notifier.base import Card, CardKind, CardSection, ConsoleNotifier
from quant_signal.notifier.cards import alert_card, signal_card
from quant_signal.notifier.feishu import (
    FeishuNotifier,
    _to_feishu_payload,
    get_notifier,
)
from quant_signal.strategies.base import Direction, Signal


def make_signal() -> Signal:
    return Signal(
        ticker="NVDA",
        direction=Direction.BUY,
        price=102.0,
        reason="突破20日高点",
        strategy_id="breakout_20d",
        ts=datetime(2026, 7, 6, 14, 30, tzinfo=UTC),
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


class FakeCardSender:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.sent: list[tuple[str, str, Card]] = []
        self.results = list(results or [])

    def send_card_to(self, receive_id: str, receive_id_type: str, card: Card) -> bool:
        self.sent.append((receive_id, receive_id_type, card))
        return self.results.pop(0) if self.results else True


class FakeImageCardSender(FakeCardSender):
    def __init__(
        self,
        image_key: str = "img_v2_chart",
        error: RuntimeError | None = None,
    ) -> None:
        super().__init__()
        self.image_key = image_key
        self.error = error
        self.uploads: list[bytes] = []

    def upload_image(self, image_bytes: bytes) -> str:
        self.uploads.append(image_bytes)
        if self.error is not None:
            raise self.error
        return self.image_key


def test_app_notifier_sends_to_open_id_with_prefix_detection() -> None:
    from quant_signal.notifier.feishu import FeishuAppNotifier

    sender = FakeCardSender()
    notifier = FeishuAppNotifier(sender, "ou_owner123")
    assert notifier.send(alert_card("t", "b")) is True
    assert sender.sent[0][0] == "ou_owner123"
    assert sender.sent[0][1] == "open_id"

    group_sender = FakeCardSender()
    FeishuAppNotifier(group_sender, "oc_group456").send(alert_card("t", "b"))
    assert group_sender.sent[0][1] == "chat_id"


def test_app_notifier_upload_image_passes_bytes_through_without_sending() -> None:
    from quant_signal.notifier.feishu import FeishuAppNotifier

    sender = FakeImageCardSender()
    notifier = FeishuAppNotifier(sender, "ou_owner123")

    assert notifier.upload_image(b"\x89PNG chart") == "img_v2_chart"
    assert sender.uploads == [b"\x89PNG chart"]
    assert sender.sent == []


def test_app_notifier_upload_error_does_not_send_a_card() -> None:
    from quant_signal.notifier.feishu import FeishuAppNotifier

    sender = FakeImageCardSender(error=RuntimeError("upload denied"))
    notifier = FeishuAppNotifier(sender, "ou_owner123")

    with pytest.raises(RuntimeError, match="upload denied"):
        notifier.upload_image(b"chart")

    assert sender.uploads == [b"chart"]
    assert sender.sent == []


def test_app_notifier_retries_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    from quant_signal.notifier.feishu import FeishuAppNotifier

    monkeypatch.setattr("quant_signal.notifier.feishu._BACKOFF", [0, 0, 0])
    sender = FakeCardSender(results=[False, False, False])
    notifier = FeishuAppNotifier(sender, "ou_owner123")
    assert notifier.send(alert_card("t", "b")) is False
    assert len(sender.sent) == 3

    recovering = FakeCardSender(results=[False, True])
    monkeypatch.setattr("quant_signal.notifier.feishu._BACKOFF", [0, 0, 0])
    assert FeishuAppNotifier(recovering, "ou_owner123").send(alert_card("t", "b")) is True


def test_get_notifier_prefers_app_bot_when_fully_configured() -> None:
    from quant_signal.config import FeishuBotSettings
    from quant_signal.notifier.feishu import FeishuAppNotifier

    settings = make_test_settings(
        feishu_webhook="https://open.feishu.cn/hook/xxx",
        feishu_app_id="cli_x",
        feishu_app_secret="secret_x",
        feishu_bot=FeishuBotSettings(
            enabled=True,
            allowed_open_ids=["ou_owner123"],
            push_receive_id="ou_owner123",
        ),
    )
    assert isinstance(get_notifier(settings), FeishuAppNotifier)

    # push_receive_id 为空 → 回退 webhook
    settings_webhook = make_test_settings(
        feishu_webhook="https://open.feishu.cn/hook/xxx",
        feishu_app_id="cli_x",
        feishu_app_secret="secret_x",
        feishu_bot=FeishuBotSettings(enabled=True),
    )
    assert isinstance(get_notifier(settings_webhook), FeishuNotifier)


def test_feishu_renders_structured_sections_as_separate_divs() -> None:
    card = Card(
        kind=CardKind.REPORT,
        title="行动卡",
        body_md="账户\n计划",
        sections=(CardSection("账户"), CardSection("计划")),
    )
    payload = _to_feishu_payload(card)
    elements = payload["card"]["elements"]
    divs = [element for element in elements if element["tag"] == "div"]
    assert [div["text"]["content"] for div in divs] == ["账户", "计划"]


def test_feishu_card_includes_image_after_first_text_when_key_exists() -> None:
    card = Card(
        kind=CardKind.REPORT,
        title="Fear DCA",
        body_md="identity\nrecommendation",
        sections=(CardSection("identity"), CardSection("recommendation")),
        image_key="img_v2_fear_chart",
    )

    elements = _to_feishu_payload(card)["card"]["elements"]

    assert [element["tag"] for element in elements] == [
        "div",
        "img",
        "hr",
        "div",
    ]
    assert elements[1] == {
        "tag": "img",
        "img_key": "img_v2_fear_chart",
        "alt": {"tag": "plain_text", "content": "Fear index chart"},
    }


def test_feishu_card_without_image_key_keeps_existing_elements() -> None:
    card = Card(
        kind=CardKind.REPORT,
        title="Text only",
        body_md="identity\nrecommendation",
        sections=(CardSection("identity"), CardSection("recommendation")),
    )

    elements = _to_feishu_payload(card)["card"]["elements"]

    assert elements == [
        {"tag": "div", "text": {"tag": "lark_md", "content": "identity"}},
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "recommendation"},
        },
    ]
