"""自建应用机器人交互：长连接接收单聊消息，指令查询 + 截图导入。

lark-oapi 只允许出现在生产 Transport/WS 封装内；路由与服务逻辑零 SDK 依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class BotMessage:
    message_id: str
    chat_id: str
    chat_type: str  # "p2p" | "group"
    message_type: str  # "text" | "image" | ...
    content_json: str  # 事件里的原始 content 字段
    sender_open_id: str


class BotIntent(str, Enum):
    IGNORE = "ignore"
    ECHO_OPEN_ID = "echo_open_id"
    HELP = "help"
    STATUS = "status"
    HOLDINGS = "holdings"
    PLANS = "plans"
    OPTIONS = "options"
    IMPORT_IMAGE = "import_image"
    CONFIRM_IMPORT = "confirm_import"
    UNKNOWN = "unknown"


_TEXT_COMMANDS = {
    "帮助": BotIntent.HELP,
    "help": BotIntent.HELP,
    "状态": BotIntent.STATUS,
    "status": BotIntent.STATUS,
    "持仓": BotIntent.HOLDINGS,
    "holdings": BotIntent.HOLDINGS,
    "计划": BotIntent.PLANS,
    "plans": BotIntent.PLANS,
    "期权": BotIntent.OPTIONS,
    "options": BotIntent.OPTIONS,
    "确认导入": BotIntent.CONFIRM_IMPORT,
}


def parse_text(content_json: str) -> str:
    try:
        payload = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    text = payload.get("text")
    return str(text).strip() if isinstance(text, str) else ""


def parse_image_key(content_json: str) -> str | None:
    try:
        payload = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("image_key")
    return str(key) if isinstance(key, str) and key else None


def route(message: BotMessage, allowed_open_ids: frozenset[str]) -> BotIntent:
    """纯函数路由：群聊忽略、白名单外回显 open_id、文本按表匹配。"""
    if message.chat_type != "p2p":
        return BotIntent.IGNORE
    if message.sender_open_id not in allowed_open_ids:
        return BotIntent.ECHO_OPEN_ID
    if message.message_type == "image":
        return BotIntent.IMPORT_IMAGE
    if message.message_type != "text":
        return BotIntent.IGNORE
    text = parse_text(message.content_json)
    return _TEXT_COMMANDS.get(text.lower(), BotIntent.UNKNOWN)
