from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path

from conftest import make_test_settings
from quant_signal.config import FeishuBotSettings
from quant_signal.feishu_bot import (
    BotIntent,
    BotMessage,
    FeishuBotService,
    parse_image_key,
    parse_text,
    route,
)
from quant_signal.ledger import SignalLedger
from quant_signal.notifier.base import Card

ALLOWED = frozenset({"ou_owner"})
NOW = datetime(2026, 7, 10, 14, 15, tzinfo=UTC)  # 10:15 ET


class FakeTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, Card]] = []
        self.downloads: list[tuple[str, str]] = []
        self.private_texts: list[tuple[str, str, str]] = []
        self.image_bytes = b"\x89PNG fake"

    def send_text(self, chat_id: str, text: str) -> bool:
        self.texts.append((chat_id, text))
        return True

    def send_text_to(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> bool:
        self.private_texts.append((receive_id, receive_id_type, text))
        return True

    def send_card(self, chat_id: str, card: Card) -> bool:
        self.cards.append((chat_id, card))
        return True

    def download_image(self, message_id: str, image_key: str) -> bytes:
        self.downloads.append((message_id, image_key))
        return self.image_bytes


def make_service(
    tmp_path: Path,
    *,
    transport: FakeTransport | None = None,
    extractor: object | None = None,
) -> tuple[FeishuBotService, FakeTransport, SignalLedger]:
    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = transport or FakeTransport()
    service = FeishuBotService(
        ledger, settings, out, extractor=extractor, clock=lambda: NOW
    )
    return service, out, ledger


def msg(
    *,
    message_id: str = "om_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    content: object = None,
    sender: str = "ou_owner",
    mentioned: bool = False,
) -> BotMessage:
    if content is None:
        content = {"text": "帮助"}
    return BotMessage(
        message_id=message_id,
        chat_id="oc_chat",
        chat_type=chat_type,
        message_type=message_type,
        content_json=json.dumps(content, ensure_ascii=False)
        if not isinstance(content, str)
        else content,
        sender_open_id=sender,
        mentioned=mentioned,
    )


def test_group_without_mention_is_ignored() -> None:
    assert route(msg(chat_type="group"), ALLOWED) is BotIntent.IGNORE
    assert (
        route(msg(chat_type="group", content={"text": "期权"}), ALLOWED)
        is BotIntent.IGNORE
    )


def test_group_mention_routes_readonly_commands_for_allowed_sender() -> None:
    message = msg(
        chat_type="group", mentioned=True, content={"text": "@_user_1 期权"}
    )
    assert route(message, ALLOWED) is BotIntent.OPTIONS
    stranger = msg(
        chat_type="group",
        mentioned=True,
        sender="ou_stranger",
        content={"text": "@_user_1 期权"},
    )
    assert route(stranger, ALLOWED) is BotIntent.IGNORE  # 群里不回显 open_id，静默


def test_unlisted_sender_gets_open_id_echo() -> None:
    assert route(msg(sender="ou_stranger"), ALLOWED) is BotIntent.ECHO_OPEN_ID


def test_text_commands_route_case_insensitively() -> None:
    cases = {
        "帮助": BotIntent.HELP,
        "HELP": BotIntent.HELP,
        " 状态 ": BotIntent.STATUS,
        "status": BotIntent.STATUS,
        "持仓": BotIntent.HOLDINGS,
        "holdings": BotIntent.HOLDINGS,
        "计划": BotIntent.PLANS,
        "plans": BotIntent.PLANS,
        "期权": BotIntent.OPTIONS,
        "options": BotIntent.OPTIONS,
        "确认导入": BotIntent.CONFIRM_IMPORT,
    }
    for text, expected in cases.items():
        assert route(msg(content={"text": text}), ALLOWED) is expected, text


def test_image_message_routes_to_import() -> None:
    message = msg(message_type="image", content={"image_key": "img_v3_x"})
    assert route(message, ALLOWED) is BotIntent.IMPORT_IMAGE


def test_unknown_text_and_types() -> None:
    assert route(msg(content={"text": "买点啥"}), ALLOWED) is BotIntent.UNKNOWN
    assert route(msg(message_type="sticker", content="{}"), ALLOWED) is BotIntent.IGNORE
    assert route(msg(content="{not json"), ALLOWED) is BotIntent.UNKNOWN


def test_content_parsers_are_defensive() -> None:
    assert parse_text('{"text": " 状态 "}') == "状态"
    assert parse_text("{not json") == ""
    assert parse_text('{"other": 1}') == ""
    assert parse_text('{"text": "@_user_1 持仓"}') == "持仓"  # 剥掉 @ 占位符
    assert parse_text('{"text": "@_user_1 @_user_2 期权 "}') == "期权"
    assert parse_image_key('{"image_key": "img_1"}') == "img_1"
    assert parse_image_key("{not json") is None
    assert parse_image_key('{"text": "x"}') is None


def test_service_replies_help_and_dedupes_repeated_delivery(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    message = msg(content={"text": "帮助"})
    service.handle(message)
    service.handle(message)  # at-least-once 重复投递
    assert len(out.texts) == 1
    assert "指令" in out.texts[0][1] and "持仓" in out.texts[0][1]


def test_service_echoes_open_id_for_stranger(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    service.handle(msg(sender="ou_stranger"))
    assert "ou_stranger" in out.texts[0][1]
    assert "allowed_open_ids" in out.texts[0][1]


def test_service_status_reports_ledger_facts(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    service.handle(msg(content={"text": "状态"}))
    reply = out.texts[0][1]
    assert "信号" in reply and "计划" in reply and "账户" in reply


def test_service_holdings_without_import(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    service.handle(msg(content={"text": "持仓"}))
    assert "暂无账户快照" in out.texts[0][1]


def test_service_plans_without_active_plans(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    service.handle(msg(content={"text": "计划"}))
    assert "无活跃执行计划" in out.texts[0][1]


def test_service_options_without_scan_then_with_scan(tmp_path: Path) -> None:
    from quant_signal.options_flow import (
        OptionContractVolume,
        OptionFlowSnapshot,
        scan_slot,
    )

    service, out, ledger = make_service(tmp_path)
    service.handle(msg(message_id="om_a", content={"text": "期权"}))
    assert "无期权扫描数据" in out.texts[0][1]

    rows = tuple(
        OptionContractVolume(
            contract_symbol=f"{root}260717{side}00210000",
            underlying=root,
            side="call" if side == "C" else "put",
            expiration=date(2026, 7, 17),
            strike=Decimal("210"),
            volume=10_000,
            rank=1,
            venues=("cone",),
            captured_at=NOW,
        )
        for root, side in (("NVDA", "C"), ("TSLA", "P"))
    )
    snapshot = OptionFlowSnapshot(
        slot=scan_slot(NOW),
        captured_at=NOW,
        provider="cboe-four-venues",
        venue_coverage=1.0,
        rows=rows,
    )
    ledger.save_option_flow_scan(snapshot, "quiet", None, now=NOW, expires_at=None)
    service.handle(msg(message_id="om_b", content={"text": "期权"}))
    assert len(out.cards) == 1
    assert "Cboe四市场" in out.cards[0][1].title


def portfolio_extraction(
    *, reported: int = 1, equity: str = "1000"
) -> object:
    from quant_signal.portfolio_import import (
        ExtractedAccount,
        ExtractedPosition,
        PortfolioExtraction,
    )

    return PortfolioExtraction(
        account=ExtractedAccount(
            equity=Decimal(equity),
            market_value=Decimal("600"),
            cash=Decimal("400"),
            buying_power=Decimal("400"),
            reported_position_count=reported,
        ),
        positions=(
            ExtractedPosition(
                symbol="NVDA",
                qty=Decimal("2"),
                avg_entry_price=Decimal("250"),
                current_price=Decimal("300"),
                market_value=Decimal("600"),
                weight_pct=Decimal("60.00"),
            ),
        ),
    )


class FakeExtractor:
    def __init__(self, extraction: object = None, error: Exception | None = None) -> None:
        self.extraction = extraction
        self.error = error
        self.seen_paths: list[Path] = []

    def extract(self, images):  # type: ignore[no-untyped-def]
        self.seen_paths.extend(images)
        if self.error is not None:
            raise self.error
        return self.extraction


def image_msg(message_id: str = "om_img") -> BotMessage:
    return msg(
        message_id=message_id,
        message_type="image",
        content={"image_key": "img_v3_key"},
    )


def test_validated_screenshot_is_applied_automatically(tmp_path: Path) -> None:
    extractor = FakeExtractor(portfolio_extraction())
    service, out, ledger = make_service(tmp_path, extractor=extractor)
    service.handle(image_msg())

    assert out.downloads == [("om_img", "img_v3_key")]
    assert "解析中" in out.texts[0][1]
    account = ledger.latest_observed_account()
    assert account is not None and account["equity"] == "1000"
    receipt = out.texts[-1][1]
    assert "NVDA" in receipt and "1000" in receipt
    assert extractor.seen_paths and not extractor.seen_paths[0].exists()  # 临时文件已删


def test_partial_screenshot_needs_confirmation(tmp_path: Path) -> None:
    extractor = FakeExtractor(portfolio_extraction(reported=2))  # 数量不一致 → PARTIAL
    service, out, ledger = make_service(tmp_path, extractor=extractor)
    service.handle(image_msg())

    assert ledger.latest_observed_account() is None  # 未应用
    receipt = out.texts[-1][1]
    assert "POSITION_COUNT_MISMATCH" in receipt and "确认导入" in receipt

    service.handle(msg(message_id="om_confirm", content={"text": "确认导入"}))
    assert ledger.latest_observed_account() is not None
    assert "已应用" in out.texts[-1][1]

    service.handle(msg(message_id="om_confirm2", content={"text": "确认导入"}))
    assert "没有待确认" in out.texts[-1][1]  # 一次性消费


def test_partial_confirmation_expires(tmp_path: Path) -> None:
    from datetime import timedelta

    times = [NOW]
    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = FakeTransport()
    service = FeishuBotService(
        ledger,
        settings,
        out,
        extractor=FakeExtractor(portfolio_extraction(reported=2)),
        clock=lambda: times[-1],
    )
    service.handle(image_msg())
    times.append(NOW + timedelta(minutes=16))  # 超过 15 分钟窗口
    service.handle(msg(message_id="om_late", content={"text": "确认导入"}))
    assert "过期" in out.texts[-1][1]
    assert ledger.latest_observed_account() is None


def test_rejected_screenshot_is_never_applied(tmp_path: Path) -> None:
    extraction = portfolio_extraction(equity="5000")  # 对账不平 → REJECTED
    extractor = FakeExtractor(extraction)
    service, out, ledger = make_service(tmp_path, extractor=extractor)
    service.handle(image_msg())
    assert ledger.latest_observed_account() is None
    assert "拒绝" in out.texts[-1][1]

    service.handle(msg(message_id="om_c", content={"text": "确认导入"}))
    assert "没有待确认" in out.texts[-1][1]  # REJECTED 不进入待确认


def test_extraction_failure_reports_and_leaves_no_state(tmp_path: Path) -> None:
    extractor = FakeExtractor(error=RuntimeError("codex down"))
    service, out, ledger = make_service(tmp_path, extractor=extractor)
    service.handle(image_msg())
    assert "解析失败" in out.texts[-1][1]
    assert ledger.latest_observed_account() is None


def event_payload(
    *,
    chat_type: str = "p2p",
    message_type: str = "text",
    content: str = '{"text":"帮助"}',
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_id": "ev_1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_owner"},
                "sender_type": "user",
            },
            "message": {
                "message_id": "om_evt",
                "chat_id": "oc_chat",
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content,
            },
        },
    }


def test_signals_command_lists_today_by_strategy(tmp_path: Path) -> None:
    from quant_signal.strategies.base import Direction, Signal

    service, out, ledger = make_service(tmp_path)
    ledger.insert(
        Signal(
            ticker="NVDA",
            direction=Direction.BUY,
            price=180.5,
            reason="动量第1",
            strategy_id="momentum_rotation",
            ts=NOW,
        ),
        pushed=True,
        now=NOW,
    )
    service.handle(msg(content={"text": "信号"}))
    reply = out.texts[0][1]
    assert "NVDA" in reply and "momentum_rotation" in reply

    empty_service, empty_out, _ = make_service(tmp_path / "empty")
    empty_service.handle(msg(content={"text": "signals"}))
    assert "今日无信号" in empty_out.texts[0][1]


def test_scan_command_shows_latest_watchboard(tmp_path: Path) -> None:
    service, out, ledger = make_service(tmp_path)
    ledger.replace_scan_candidates(
        NOW.date(),
        [
            {"ticker": "SMCI", "rank": 1, "score": 0.91, "price": 31.5},
            {"ticker": "MARA", "rank": 2, "score": 0.88, "price": 13.0},
        ],
        as_of=NOW,
    )
    service.handle(msg(content={"text": "扫描"}))
    reply = out.texts[0][1]
    assert "SMCI" in reply and "MARA" in reply

    empty_service, empty_out, _ = make_service(tmp_path / "empty")
    empty_service.handle(msg(content={"text": "scan"}))
    assert "暂无扫描数据" in empty_out.texts[0][1]


def test_health_command_renders_runtime_or_degrades(tmp_path: Path) -> None:
    from quant_signal.scheduler import JobRuntime

    runtime = JobRuntime(now_fn=lambda: NOW)
    runtime.wrap("market_scan", lambda: None)()

    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = FakeTransport()
    service = FeishuBotService(
        ledger, settings, out, clock=lambda: NOW, runtime=runtime
    )
    service.handle(msg(content={"text": "健康"}))
    assert "market_scan" in out.texts[0][1]

    bare, bare_out, _ = make_service(tmp_path / "bare")
    bare.handle(msg(content={"text": "health"}))
    assert "运行状态不可用" in bare_out.texts[0][1]


def test_options_query_falls_back_to_last_trading_day(tmp_path: Path) -> None:
    from datetime import timedelta

    from quant_signal.options_flow import (
        OptionContractVolume,
        OptionFlowSnapshot,
        scan_slot,
    )

    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = FakeTransport()
    saturday = NOW + timedelta(days=1)  # 2026-07-11 周六
    service = FeishuBotService(ledger, settings, out, clock=lambda: saturday)

    rows = (
        OptionContractVolume(
            contract_symbol="NVDA260717C00210000",
            underlying="NVDA",
            side="call",
            expiration=date(2026, 7, 17),
            strike=Decimal("210"),
            volume=10_000,
            rank=1,
            venues=("cone",),
            captured_at=NOW,
        ),
    )
    ledger.save_option_flow_scan(
        OptionFlowSnapshot(
            slot=scan_slot(NOW),  # 周五的扫描
            captured_at=NOW,
            provider="cboe-four-venues",
            venue_coverage=1.0,
            rows=rows,
        ),
        "quiet",
        None,
        now=NOW,
        expires_at=None,
    )
    service.handle(msg(content={"text": "期权"}))
    assert len(out.cards) == 1  # 周六查询回退到周五榜单
    assert "07/10" in out.cards[0][1].body_md


def test_options_query_gives_up_after_five_sessions(tmp_path: Path) -> None:
    from datetime import timedelta

    ledger = SignalLedger(tmp_path / "signals.db")
    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = FakeTransport()
    service = FeishuBotService(
        ledger, settings, out, clock=lambda: NOW + timedelta(days=1)
    )
    service.handle(msg(content={"text": "期权"}))
    assert out.cards == []
    assert "无期权扫描数据" in out.texts[0][1]


def test_group_options_replies_in_group(tmp_path: Path) -> None:
    from quant_signal.options_flow import (
        OptionContractVolume,
        OptionFlowSnapshot,
        scan_slot,
    )

    service, out, ledger = make_service(tmp_path)
    rows = (
        OptionContractVolume(
            contract_symbol="NVDA260717C00210000",
            underlying="NVDA",
            side="call",
            expiration=date(2026, 7, 17),
            strike=Decimal("210"),
            volume=10_000,
            rank=1,
            venues=("cone",),
            captured_at=NOW,
        ),
    )
    ledger.save_option_flow_scan(
        OptionFlowSnapshot(
            slot=scan_slot(NOW),
            captured_at=NOW,
            provider="cboe-four-venues",
            venue_coverage=1.0,
            rows=rows,
        ),
        "quiet",
        None,
        now=NOW,
        expires_at=None,
    )
    service.handle(
        msg(chat_type="group", mentioned=True, content={"text": "@_user_1 期权"})
    )
    assert len(out.cards) == 1 and out.cards[0][0] == "oc_chat"  # 回在群里


def test_group_holdings_goes_private_with_group_notice(tmp_path: Path) -> None:
    service, out, _ = make_service(tmp_path)
    service.handle(
        msg(chat_type="group", mentioned=True, content={"text": "@_user_1 持仓"})
    )
    assert out.private_texts and out.private_texts[0][0] == "ou_owner"
    assert out.private_texts[0][1] == "open_id"
    assert "暂无账户快照" in out.private_texts[0][2]
    assert out.texts and "私发" in out.texts[0][1]  # 群里只提示


def test_group_import_and_confirm_are_redirected_to_p2p(tmp_path: Path) -> None:
    service, out, ledger = make_service(
        tmp_path, extractor=FakeExtractor(portfolio_extraction())
    )
    service.handle(
        msg(
            message_id="om_g1",
            chat_type="group",
            mentioned=True,
            message_type="image",
            content={"image_key": "img_x"},
        )
    )
    service.handle(
        msg(
            message_id="om_g2",
            chat_type="group",
            mentioned=True,
            content={"text": "@_user_1 确认导入"},
        )
    )
    assert ledger.latest_observed_account() is None  # 群里绝不触发导入
    assert out.downloads == []
    assert all("单聊" in text for _, text in out.texts)


def test_message_from_event_parses_text_and_image() -> None:
    from quant_signal.feishu_bot import message_from_event

    message = message_from_event(event_payload())
    assert message is not None
    assert message.message_id == "om_evt"
    assert message.chat_type == "p2p"
    assert message.sender_open_id == "ou_owner"
    assert parse_text(message.content_json) == "帮助"

    image = message_from_event(
        event_payload(message_type="image", content='{"image_key":"img_1"}')
    )
    assert image is not None and image.message_type == "image"

    group = message_from_event(event_payload(chat_type="group"))
    assert group is not None and group.chat_type == "group"
    assert group.mentioned is False

    mentioned = event_payload(chat_type="group", content='{"text":"@_user_1 期权"}')
    mentioned["event"]["message"]["mentions"] = [
        {"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "量化交易助手"}
    ]
    parsed = message_from_event(mentioned)
    assert parsed is not None and parsed.mentioned is True


def test_message_from_event_rejects_malformed_payloads() -> None:
    from quant_signal.feishu_bot import message_from_event

    assert message_from_event({}) is None
    assert message_from_event({"event": {}}) is None
    broken = event_payload()
    del broken["event"]["message"]["message_id"]
    assert message_from_event(broken) is None
    no_sender = event_payload()
    del no_sender["event"]["sender"]
    assert message_from_event(no_sender) is None


def test_service_never_raises_and_reports_failure(tmp_path: Path) -> None:
    class ExplodingLedger:
        def try_mark_feishu_message(self, message_id: str, *, now: datetime) -> bool:
            return True

        def __getattr__(self, name: str) -> object:
            raise RuntimeError("db down")

    settings = make_test_settings(
        feishu_bot=FeishuBotSettings(enabled=True, allowed_open_ids=["ou_owner"])
    )
    out = FakeTransport()
    service = FeishuBotService(
        ExplodingLedger(), settings, out, clock=lambda: NOW  # type: ignore[arg-type]
    )
    service.handle(msg(content={"text": "持仓"}))
    assert "处理失败" in out.texts[0][1]
