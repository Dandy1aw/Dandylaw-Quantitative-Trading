"""自建应用机器人交互：长连接接收单聊消息，指令查询 + 截图导入。

lark-oapi 只允许出现在生产 Transport/WS 封装内；路由与服务逻辑零 SDK 依赖。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import queue
import threading
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

import structlog

from quant_signal.notifier.base import Card

if TYPE_CHECKING:
    from quant_signal.config import Settings
    from quant_signal.ledger import SignalLedger
    from quant_signal.portfolio_import import (
        PortfolioExtraction,
        ValidatedPortfolioImport,
    )

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")

_HELP_TEXT = (
    "可用指令：\n"
    "状态 / status — 系统概况\n"
    "持仓 / holdings — 最新截图账户与持仓\n"
    "计划 / plans — 活跃执行计划\n"
    "期权 / options — 最新期权热度榜(不新抓)\n"
    "发送券商持仓截图 — 解析并更新账户快照\n"
    "确认导入 — 应用最近一次校验不完整(PARTIAL)的导入"
)


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


class BotTransport(Protocol):
    def send_text(self, chat_id: str, text: str) -> bool: ...

    def send_card(self, chat_id: str, card: Card) -> bool: ...

    def download_image(self, message_id: str, image_key: str) -> bytes: ...


class PortfolioExtractor(Protocol):
    def extract(self, images: "Sequence[Path]") -> "PortfolioExtraction": ...


class FeishuBotService:
    """worker 串行处理单聊消息；异常不外溢，不影响调度器与 webhook 推送。"""

    def __init__(
        self,
        ledger: "SignalLedger",
        settings: "Settings",
        transport: BotTransport,
        *,
        extractor: PortfolioExtractor | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ledger = ledger
        self._settings = settings
        self._cfg = settings.feishu_bot
        self._transport = transport
        self._extractor = extractor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_at = self._clock()
        self._pending_partial: "tuple[ValidatedPortfolioImport, datetime] | None" = None
        self._queue: "queue.Queue[BotMessage]" = queue.Queue()

    # ---- worker 入口 ----

    def submit(self, message: BotMessage) -> None:
        self._queue.put(message)

    def start(self) -> None:
        thread = threading.Thread(
            target=self._worker_loop, name="feishu-bot-worker", daemon=True
        )
        thread.start()

    def _worker_loop(self) -> None:
        while True:
            message = self._queue.get()
            try:
                self.handle(message)
            except Exception:  # noqa: BLE001 - worker 永不因单条消息死亡
                log.exception("feishu_bot.worker_error", message_id=message.message_id)

    # ---- 消息处理 ----

    def handle(self, message: BotMessage) -> None:
        now = self._clock()
        if not self._ledger.try_mark_feishu_message(message.message_id, now=now):
            log.info("feishu_bot.duplicate", message_id=message.message_id)
            return
        intent = route(message, frozenset(self._cfg.allowed_open_ids))
        if intent is BotIntent.IGNORE:
            return
        try:
            self._dispatch(intent, message, now)
        except Exception as error:  # noqa: BLE001 - 回执失败原因，绝不外抛
            log.exception(
                "feishu_bot.handle_failed",
                message_id=message.message_id,
                intent=intent.value,
            )
            self._transport.send_text(
                message.chat_id, f"处理失败：{type(error).__name__}"
            )

    def _dispatch(self, intent: BotIntent, message: BotMessage, now: datetime) -> None:
        if intent is BotIntent.ECHO_OPEN_ID:
            self._transport.send_text(
                message.chat_id,
                f"你的 open_id 是 {message.sender_open_id}。"
                "如需使用，请加入 settings.yaml 的 feishu_bot.allowed_open_ids 后重启。",
            )
        elif intent is BotIntent.HELP:
            self._transport.send_text(message.chat_id, _HELP_TEXT)
        elif intent is BotIntent.STATUS:
            self._transport.send_text(message.chat_id, self._status_text(now))
        elif intent is BotIntent.HOLDINGS:
            self._transport.send_text(message.chat_id, self._holdings_text())
        elif intent is BotIntent.PLANS:
            self._transport.send_text(message.chat_id, self._plans_text())
        elif intent is BotIntent.OPTIONS:
            self._reply_options(message.chat_id, now)
        elif intent is BotIntent.IMPORT_IMAGE:
            self._handle_import(message, now)
        elif intent is BotIntent.CONFIRM_IMPORT:
            self._handle_confirm(message.chat_id, now)
        else:
            self._transport.send_text(
                message.chat_id, f"未识别指令。\n\n{_HELP_TEXT}"
            )

    # ---- 查询指令 ----

    def _status_text(self, now: datetime) -> str:
        session = now.astimezone(_ET).date()
        signals = len(self._ledger.signals_on(session))
        plans = len(self._ledger.active_execution_plans())
        account = self._ledger.latest_observed_account()
        account_text = (
            str(account.get("observed_at")) if account is not None else "无"
        )
        option = self._ledger.latest_option_flow_snapshot(session)
        option_text = option.slot if option is not None else "今日无"
        return (
            "系统状态\n"
            f"机器人启动: {self._started_at.astimezone(_ET):%m/%d %H:%M ET}\n"
            f"今日信号: {signals} 条\n"
            f"活跃计划: {plans} 个\n"
            f"账户快照: {account_text}\n"
            f"期权最新扫描: {option_text}"
        )

    def _holdings_text(self) -> str:
        account = self._ledger.latest_observed_account()
        if account is None:
            return "暂无账户快照：请先发送一张券商持仓截图。"
        lines = [
            "账户快照（截图导入）",
            f"观察时间: {account.get('observed_at')}",
            f"权益: {account.get('equity')} {account.get('currency')}",
            f"现金: {account.get('cash')}｜持仓市值: {account.get('market_value')}",
        ]
        positions = self._ledger.active_observed_positions()
        if positions:
            lines.append("持仓:")
            for row in positions:
                value = row.get("market_value") or row.get("estimated_market_value")
                weight = row.get("weight_pct")
                weight_text = f"｜{weight}%" if weight is not None else ""
                lines.append(f"· {row.get('symbol')}: {value}{weight_text}")
        else:
            lines.append("持仓: 无记录")
        return "\n".join(lines)

    def _plans_text(self) -> str:
        plans = self._ledger.active_execution_plans()
        if not plans:
            return "当前无活跃执行计划。"
        lines = ["活跃执行计划:"]
        for plan in plans:
            lines.append(f"· {plan.ticker} — {plan.state.value}")
        return "\n".join(lines)

    def _reply_options(self, chat_id: str, now: datetime) -> None:
        from quant_signal.notifier.cards import option_flow_card

        session = now.astimezone(_ET).date()
        snapshot = self._ledger.latest_option_flow_snapshot(session)
        if snapshot is None:
            self._transport.send_text(chat_id, "今日暂无期权扫描数据。")
            return
        cfg = self._settings.option_flow
        enrichment = (
            "ok" if any(row.enrichment is not None for row in snapshot.rows) else "off"
        )
        card = option_flow_card(
            snapshot,
            (),
            "query",
            enrichment_status=enrichment,
            display_dedupe=cfg.display_dedupe_underlying,
            display_sort_by_expiry=cfg.display_sort_by_expiry,
        )
        self._transport.send_card(chat_id, card)

    # ---- 截图导入（Task 5 实现） ----

    def _handle_import(self, message: BotMessage, now: datetime) -> None:
        self._transport.send_text(message.chat_id, "已收到截图。")

    def _handle_confirm(self, chat_id: str, now: datetime) -> None:
        self._transport.send_text(chat_id, "当前没有待确认的导入。")
