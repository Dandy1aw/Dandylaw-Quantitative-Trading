"""bot 指令 `期权 <ticker>`：按需拉取单标的期权情报。"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_test_settings
from quant_signal.config import FeishuBotSettings
from quant_signal.feishu_bot import (
    BotIntent,
    BotMessage,
    FeishuBotService,
    parse_option_ticker,
    route,
)
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card
from quant_signal.options_intel import OptionIntel

NOW = datetime(2026, 7, 10, 20, 45, tzinfo=UTC)
ALLOWED = frozenset({"ou_owner"})


class FakeTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, Card]] = []

    def send_text(self, chat_id: str, text: str) -> bool:
        self.texts.append((chat_id, text))
        return True

    def send_text_to(self, receive_id: str, receive_id_type: str, text: str) -> bool:
        return True

    def send_card(self, chat_id: str, card: Card) -> bool:
        self.cards.append((chat_id, card))
        return True

    def download_image(self, message_id: str, image_key: str) -> bytes:
        return b""


def msg(text: str, *, message_id: str = "om_1") -> BotMessage:
    return BotMessage(
        message_id=message_id,
        chat_id="oc_chat",
        chat_type="p2p",
        message_type="text",
        content_json=json.dumps({"text": text}),
        sender_open_id="ou_owner",
    )


def make_service(
    tmp_path: Path, *, engine: object | None = None
) -> tuple[FeishuBotService, FakeTransport]:
    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    transport = FakeTransport()
    service = FeishuBotService(
        ledger, settings, transport, clock=lambda: NOW, engine=engine
    )
    return service, transport


def intel() -> OptionIntel:
    return OptionIntel(
        symbol="MU",
        spot=Decimal("100"),
        session=date(2026, 7, 10),
        expected_move_pct=0.05,
        expected_move_expiry=date(2026, 7, 24),
        earnings_move_pct=None,
        earnings_move_expiry=None,
        atm_iv=0.5,
        realized_vol_20d=0.4,
        pc_volume_ratio=0.8,
        pc_oi_ratio=0.9,
        top_oi_strikes=(),
        earnings_date=None,
        data_note=None,
    )


class TestParsing:
    def test_option_with_ticker_routes_to_intel(self) -> None:
        assert route(msg("期权 MU"), ALLOWED) is BotIntent.OPTION_INTEL
        assert route(msg("options nvda"), ALLOWED) is BotIntent.OPTION_INTEL

    def test_bare_option_keeps_flow_snapshot_intent(self) -> None:
        assert route(msg("期权"), ALLOWED) is BotIntent.OPTIONS

    def test_parse_option_ticker(self) -> None:
        assert parse_option_ticker("期权 mu") == "MU"
        assert parse_option_ticker("options NVDA") == "NVDA"
        assert parse_option_ticker("期权") is None
        assert parse_option_ticker("期权 123") is None
        assert parse_option_ticker("期权 MU SMH") is None


class TestHandler:
    def test_replies_card_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = SimpleNamespace(
            settings=make_test_settings(), option_chain_source=object()
        )
        service, transport = make_service(tmp_path, engine=engine)
        monkeypatch.setattr(
            "quant_signal.pipelines.option_intel.build_intel",
            lambda eng, symbol, now: intel(),
        )
        service.handle(msg("期权 MU"))
        assert len(transport.cards) == 1
        assert "MU" in transport.cards[0][1].body_md

    def test_replies_error_when_engine_missing(self, tmp_path: Path) -> None:
        service, transport = make_service(tmp_path, engine=None)
        service.handle(msg("期权 MU"))
        assert transport.cards == []
        assert any("未启用" in text for _, text in transport.texts)

    def test_replies_error_when_no_spot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = SimpleNamespace(
            settings=make_test_settings(), option_chain_source=object()
        )
        service, transport = make_service(tmp_path, engine=engine)
        monkeypatch.setattr(
            "quant_signal.pipelines.option_intel.build_intel",
            lambda eng, symbol, now: None,
        )
        service.handle(msg("期权 ZZZZ"))
        assert transport.cards == []
        assert any("ZZZZ" in text for _, text in transport.texts)
