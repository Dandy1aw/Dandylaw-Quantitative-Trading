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
import re
import queue
import threading
from typing import TYPE_CHECKING, Literal, Protocol
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
    from quant_signal.scheduler import JobRuntime

log = structlog.get_logger()
_ET = ZoneInfo("America/New_York")

_HELP_TEXT = (
    "可用指令：\n"
    "状态 / status — 系统概况\n"
    "持仓 / holdings — 最新截图账户与持仓\n"
    "计划 / plans — 活跃执行计划\n"
    "期权 / options — 最新期权热度榜(不新抓)\n"
    "信号 / signals — 今日各策略信号\n"
    "扫描 / scan — 最新指数池 Top20 观察榜\n"
    "健康 / health — 定时任务运行状态\n"
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
    mentioned: bool = False  # 群消息里是否 @ 了机器人(群 @ 事件 scope 下恒真)


class BotIntent(str, Enum):
    IGNORE = "ignore"
    ECHO_OPEN_ID = "echo_open_id"
    HELP = "help"
    STATUS = "status"
    HOLDINGS = "holdings"
    PLANS = "plans"
    OPTIONS = "options"
    SIGNALS = "signals"
    SCAN = "scan"
    HEALTH = "health"
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
    "信号": BotIntent.SIGNALS,
    "signals": BotIntent.SIGNALS,
    "扫描": BotIntent.SCAN,
    "scan": BotIntent.SCAN,
    "健康": BotIntent.HEALTH,
    "health": BotIntent.HEALTH,
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
    if not isinstance(text, str):
        return ""
    return re.sub(r"@_user_\d+", "", text).strip()


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
    """纯函数路由。

    单聊：白名单外回显 open_id；图片走导入；文本按表匹配。
    群聊：必须 @ 机器人且发送者在白名单，否则静默忽略（不回显、不刷屏）。
    """
    if message.chat_type == "group":
        if not message.mentioned:
            return BotIntent.IGNORE
        if message.sender_open_id not in allowed_open_ids:
            return BotIntent.IGNORE
    elif message.chat_type != "p2p":
        return BotIntent.IGNORE
    elif message.sender_open_id not in allowed_open_ids:
        return BotIntent.ECHO_OPEN_ID
    if message.message_type == "image":
        return BotIntent.IMPORT_IMAGE
    if message.message_type != "text":
        return BotIntent.IGNORE
    text = parse_text(message.content_json)
    return _TEXT_COMMANDS.get(text.lower(), BotIntent.UNKNOWN)


class BotTransport(Protocol):
    def send_text(self, chat_id: str, text: str) -> bool: ...

    def send_text_to(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> bool: ...

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
        runtime: "JobRuntime | None" = None,
    ) -> None:
        self._ledger = ledger
        self._settings = settings
        self._cfg = settings.feishu_bot
        self._transport = transport
        self._extractor = extractor
        self._runtime = runtime
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
        if message.chat_type == "group":
            # 群里只提供只读查询；改状态的操作一律引导回单聊
            if intent in (BotIntent.IMPORT_IMAGE, BotIntent.CONFIRM_IMPORT):
                self._transport.send_text(
                    message.chat_id, "导入相关操作请在与机器人的单聊中进行。"
                )
                return
            if intent is BotIntent.HOLDINGS:
                # 持仓含权益/市值明细，不贴群里：私发详情，群里只留提示
                self._transport.send_text_to(
                    message.sender_open_id, "open_id", self._holdings_text()
                )
                self._transport.send_text(message.chat_id, "持仓明细已私发给你。")
                return
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
        elif intent is BotIntent.SIGNALS:
            self._transport.send_text(message.chat_id, self._signals_text(now))
        elif intent is BotIntent.SCAN:
            self._transport.send_text(message.chat_id, self._scan_text())
        elif intent is BotIntent.HEALTH:
            self._transport.send_text(message.chat_id, self._health_text(now))
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

    def _signals_text(self, now: datetime) -> str:
        session = now.astimezone(_ET).date()
        rows = self._ledger.signals_on(session)
        if not rows:
            return "今日无信号。"
        by_strategy: dict[str, list[str]] = {}
        for row in rows:
            strategy = str(row.get("strategy_id"))
            pushed = "已推" if row.get("pushed") else "未推"
            by_strategy.setdefault(strategy, []).append(
                f"· {row.get('ticker')} {str(row.get('direction')).upper()}"
                f" @ {row.get('price')}（{pushed}）"
            )
        lines = [f"今日信号（{session:%m/%d}）:"]
        for strategy, items in by_strategy.items():
            lines.append(f"[{strategy}]")
            lines.extend(items)
        return "\n".join(lines)

    def _scan_text(self) -> str:
        rows = self._ledger.latest_scan_candidates()
        if not rows:
            return "暂无扫描数据。"
        lines = [f"指数池观察榜（{rows[0].get('scan_date')}）:"]
        for row in rows[:20]:
            lines.append(
                f"{row.get('rank')}. {row.get('ticker')}"
                f" · 得分 {row.get('score')} · {row.get('price')}"
            )
        return "\n".join(lines)

    def _health_text(self, now: datetime) -> str:
        if self._runtime is None:
            return "运行状态不可用（未接入 JobRuntime）。"
        snapshot = self._runtime.snapshot()
        if not snapshot:
            return "尚无任务运行记录（进程可能刚启动）。"
        lines = ["定时任务运行状态:"]
        for job_id, state in snapshot.items():
            running_since = state.get("running_since")
            if isinstance(running_since, datetime):
                minutes = (now - running_since).total_seconds() / 60
                marker = " ⚠️卡死?" if minutes > 10 else ""
                lines.append(f"· {job_id}: 运行中 {minutes:.0f} 分钟{marker}")
                continue
            last_success = state.get("last_success")
            duration = state.get("last_duration")
            when = (
                f"{last_success.astimezone(_ET):%m/%d %H:%M ET}"
                if isinstance(last_success, datetime)
                else "无成功记录"
            )
            duration_text = (
                f"，耗时 {duration:.0f}s" if isinstance(duration, float) else ""
            )
            lines.append(f"· {job_id}: 最近成功 {when}{duration_text}")
        return "\n".join(lines)

    def _reply_options(self, chat_id: str, now: datetime) -> None:
        from quant_signal.calendar import previous_trading_day
        from quant_signal.notifier.cards import option_flow_card

        # 非交易时段回退到最近一个有扫描的交易日（最多 5 个）
        session = now.astimezone(_ET).date()
        snapshot = self._ledger.latest_option_flow_snapshot(session)
        for _ in range(5):
            if snapshot is not None:
                break
            session = previous_trading_day(session)
            snapshot = self._ledger.latest_option_flow_snapshot(session)
        if snapshot is None:
            self._transport.send_text(chat_id, "近 5 个交易日无期权扫描数据。")
            return
        cfg = self._settings.option_flow
        enrichment: Literal["ok", "off"] = (
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

    # ---- 截图导入 ----

    def _handle_import(self, message: BotMessage, now: datetime) -> None:
        import tempfile

        from quant_signal.portfolio_import import (
            CodexPortfolioExtractor,
            image_digest,
            validate_extraction,
        )

        image_key = parse_image_key(message.content_json)
        if image_key is None:
            self._transport.send_text(message.chat_id, "图片消息缺少 image_key，无法处理。")
            return
        self._transport.send_text(
            message.chat_id, "已收到截图，解析中（约1-3分钟）…"
        )
        data = self._transport.download_image(message.message_id, image_key)
        extractor = self._extractor or CodexPortfolioExtractor(
            timeout_seconds=self._cfg.codex_timeout_seconds
        )
        handle, raw_path = tempfile.mkstemp(prefix="feishu-import-", suffix=".png")
        path = Path(raw_path)
        try:
            import os

            with os.fdopen(handle, "wb") as file:
                file.write(data)
            extraction = extractor.extract([path])
            record = validate_extraction(
                extraction,
                image_sha256=image_digest([path]),
                uploaded_at=now,
                capital_limit=self._cfg.capital_limit,
                max_financing_ratio=self._cfg.max_financing_ratio,
            )
        except Exception as error:  # noqa: BLE001 - 解析失败必须回执且不留状态
            log.warning("feishu_bot.extract_failed", error=str(error))
            self._transport.send_text(
                message.chat_id, f"解析失败：{type(error).__name__}: {error}"
            )
            return
        finally:
            path.unlink(missing_ok=True)

        from quant_signal.portfolio_import import ImportStatus, apply_validated_import

        if record.status is ImportStatus.REJECTED:
            errors = "、".join(record.validation_errors) or "未知原因"
            self._transport.send_text(
                message.chat_id, f"导入被拒绝（REJECTED）：{errors}。账户未更新。"
            )
            return
        if record.status is ImportStatus.VALIDATED:
            applied = apply_validated_import(self._ledger, record, now=now)
            if not applied:
                self._transport.send_text(
                    message.chat_id, "该截图此前已导入过，账户未变化。"
                )
                return
            self._transport.send_text(
                message.chat_id, self._import_receipt(record, applied=True)
            )
            return
        # PARTIAL：不自动应用，等待明确确认
        self._pending_partial = (record, now)
        errors = "、".join(record.validation_errors)
        self._transport.send_text(
            message.chat_id,
            f"解析完成但校验不完整（PARTIAL）：{errors}。\n"
            f"{self._import_receipt(record, applied=False)}\n"
            f"回复「确认导入」可在 {self._cfg.confirm_window_minutes} 分钟内强制应用（谨慎）。",
        )

    def _handle_confirm(self, chat_id: str, now: datetime) -> None:
        from datetime import timedelta

        from quant_signal.portfolio_import import apply_validated_import

        pending = self._pending_partial
        if pending is None:
            self._transport.send_text(chat_id, "当前没有待确认的导入。")
            return
        record, stored_at = pending
        self._pending_partial = None
        window = timedelta(minutes=self._cfg.confirm_window_minutes)
        if now - stored_at > window:
            self._transport.send_text(
                chat_id, "待确认导入已过期，请重新发送截图。"
            )
            return
        applied = apply_validated_import(self._ledger, record, now=now)
        if applied:
            self._transport.send_text(
                chat_id, f"已应用 PARTIAL 导入。\n{self._import_receipt(record, applied=True)}"
            )
        else:
            self._transport.send_text(chat_id, "应用失败：该截图此前已导入过。")

    def _import_receipt(
        self, record: "ValidatedPortfolioImport", *, applied: bool
    ) -> str:
        account = record.extraction.account
        symbols = "、".join(row.symbol for row in record.positions) or "无"
        lines = [
            f"权益: {account.equity} {account.currency}｜现金: {account.cash}",
            f"持仓({len(record.positions)}): {symbols}",
        ]
        if applied:
            lines.insert(0, "账户快照已更新，现有执行计划已按 ACCOUNT_CHANGED 失效重算。")
        return "\n".join(lines)


# ---- lark-oapi 边界：事件解包（纯 dict，可单测） ----


def message_from_event(payload: object) -> BotMessage | None:
    """把 im.message.receive_v1 事件 JSON 解包成 BotMessage；缺字段返回 None。"""
    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    message = event.get("message")
    sender = event.get("sender")
    if not isinstance(message, dict) or not isinstance(sender, dict):
        return None
    sender_id = sender.get("sender_id")
    open_id = sender_id.get("open_id") if isinstance(sender_id, dict) else None
    fields = (
        message.get("message_id"),
        message.get("chat_id"),
        message.get("chat_type"),
        message.get("message_type"),
        message.get("content"),
        open_id,
    )
    if not all(isinstance(value, str) and value for value in fields):
        return None
    message_id, chat_id, chat_type, message_type, content, sender_open_id = fields
    mentions = message.get("mentions")
    return BotMessage(
        message_id=str(message_id),
        chat_id=str(chat_id),
        chat_type=str(chat_type),
        message_type=str(message_type),
        content_json=str(content),
        sender_open_id=str(sender_open_id),
        mentioned=isinstance(mentions, list) and len(mentions) > 0,
    )


# ---- lark-oapi 边界：生产实现（无单测，靠真实验收） ----


class LarkTransport:
    """自建应用 REST：发单聊消息、下载图片。凭据不进日志。"""

    def __init__(self, app_id: str, app_secret: str) -> None:
        import lark_oapi as lark

        self._client = (
            lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        )

    def _send(
        self,
        receive_id: str,
        msg_type: str,
        content: str,
        *,
        receive_id_type: str = "chat_id",
    ) -> bool:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            log.warning(
                "feishu_bot.send_failed", code=response.code, msg=response.msg
            )
            return False
        return True

    def send_text(self, chat_id: str, text: str) -> bool:
        return self._send(
            chat_id, "text", json.dumps({"text": text}, ensure_ascii=False)
        )

    def send_text_to(
        self, receive_id: str, receive_id_type: str, text: str
    ) -> bool:
        return self._send(
            receive_id,
            "text",
            json.dumps({"text": text}, ensure_ascii=False),
            receive_id_type=receive_id_type,
        )

    def send_card(self, chat_id: str, card: Card) -> bool:
        from quant_signal.notifier.feishu import _to_feishu_payload

        payload = _to_feishu_payload(card)["card"]
        return self._send(
            chat_id, "interactive", json.dumps(payload, ensure_ascii=False)
        )

    def send_card_to(
        self, receive_id: str, receive_id_type: str, card: Card
    ) -> bool:
        from quant_signal.notifier.feishu import _to_feishu_payload

        payload = _to_feishu_payload(card)["card"]
        return self._send(
            receive_id,
            "interactive",
            json.dumps(payload, ensure_ascii=False),
            receive_id_type=receive_id_type,
        )

    def download_image(self, message_id: str, image_key: str) -> bytes:
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(image_key)
            .type("image")
            .build()
        )
        response = self._client.im.v1.message_resource.get(request)
        if not response.success() or response.file is None:
            raise RuntimeError(f"下载图片失败: {response.code} {response.msg}")
        data = response.file.read()
        return bytes(data)


def run_ws_forever(service: FeishuBotService, app_id: str, app_secret: str) -> None:
    """长连接事件循环：SDK 自带重连之外的兜底重启（退避），永不外抛。"""
    import time as time_module

    import lark_oapi as lark

    def on_message(data: object) -> None:
        try:
            payload = json.loads(lark.JSON.marshal(data))
            message = message_from_event(payload)
            if message is not None:
                service.submit(message)
        except Exception:  # noqa: BLE001 - 单条事件解析失败不影响连接
            log.exception("feishu_bot.event_error")

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    delay = 5.0
    while True:
        try:
            client = lark.ws.Client(
                app_id,
                app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.WARNING,
            )
            client.start()
        except Exception:  # noqa: BLE001 - ws 崩溃走退避重启
            log.exception("feishu_bot.ws_crashed")
        time_module.sleep(delay)
        delay = min(delay * 2, 300.0)
