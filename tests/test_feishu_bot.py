from __future__ import annotations

import json

from quant_signal.feishu_bot import (
    BotIntent,
    BotMessage,
    parse_image_key,
    parse_text,
    route,
)

ALLOWED = frozenset({"ou_owner"})


def msg(
    *,
    message_id: str = "om_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    content: object = None,
    sender: str = "ou_owner",
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
    )


def test_group_messages_are_ignored() -> None:
    assert route(msg(chat_type="group"), ALLOWED) is BotIntent.IGNORE


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
    assert parse_image_key('{"image_key": "img_1"}') == "img_1"
    assert parse_image_key("{not json") is None
    assert parse_image_key('{"text": "x"}') is None
