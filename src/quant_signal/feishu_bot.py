"""自建应用机器人交互：长连接接收单聊消息，指令查询 + 截图导入。

lark-oapi 只允许出现在生产 Transport/WS 封装内；路由与服务逻辑零 SDK 依赖。
"""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog

from quant_signal.notifier.base import Card

if TYPE_CHECKING:
    from quant_signal.config import Settings
    from quant_signal.engine import Engine
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
    "期权 <代码> — 单标的期权情报(现场拉取, 如: 期权 MU)\n"
    "信号 / signals — 今日各策略信号\n"
    "扫描 / scan — 最新指数池 Top20 观察榜\n"
    "健康 / health — 定时任务运行状态\n"
    "定投 — 重发最新已完成的恐慌指数定投观察（不重新抓取）\n"
    "定投规则 — 查看恐慌指数定投规则\n"
    "发送券商账户原图 — 需含总资产/持仓市值/现金/购买力及完整持仓\n"
    "确认导入 — 应用最近一次校验不完整(PARTIAL)的导入"
)
_HELP_TEXT += (
    "\n异动榜 [20|60|252] — 累计入榜个股 Top 榜"
    "\n异动板块 [板块] — 板块累计异动 Top5"
    "\n异动 <代码> — 查询单只股票的入榜记录"
    "\n监控 [代码] / 取消监控 <代码> — 管理实时价格监控"
    "\n重推 / 重推 异动榜 — 重新发送最新持仓或异动榜"
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
    OPTION_INTEL = "option_intel"
    SIGNALS = "signals"
    SCAN = "scan"
    HEALTH = "health"
    FEAR_DCA = "fear_dca"
    FEAR_DCA_RULES = "fear_dca_rules"
    MOVERS = "movers"
    MOVER_SECTORS = "mover_sectors"
    MOVER_TICKER = "mover_ticker"
    MONITORS = "monitors"
    MONITOR_ADD = "monitor_add"
    MONITOR_REMOVE = "monitor_remove"
    REPUSH = "repush"
    REPUSH_MOVERS = "repush_movers"
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
    "定投": BotIntent.FEAR_DCA,
    "定投规则": BotIntent.FEAR_DCA_RULES,
    "确认导入": BotIntent.CONFIRM_IMPORT,
    "异动榜": BotIntent.MOVERS,
    "异动板块": BotIntent.MOVER_SECTORS,
    "监控": BotIntent.MONITORS,
    "重推": BotIntent.REPUSH,
    "重推 异动榜": BotIntent.REPUSH_MOVERS,
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


_OPTION_TICKER = re.compile(r"^[A-Z]{1,6}$")
_MOVER_WINDOW = re.compile(r"^异动榜\s+(20|60|252)$")
_MOVER_SECTOR = re.compile(r"^异动板块\s+\S.+$")
_MOVER_TICKER = re.compile(r"^异动\s+([A-Za-z]{1,6})$")
_MONITOR_ADD = re.compile(r"^监控\s+([A-Za-z]{1,6})$")
_MONITOR_REMOVE = re.compile(r"^取消监控\s+([A-Za-z]{1,6})$")


def parse_option_ticker(text: str) -> str | None:
    """`期权 MU` / `options nvda` → "MU"；不满足两段式或代码非法返回 None。"""
    parts = text.split()
    if len(parts) != 2 or parts[0].lower() not in ("期权", "options"):
        return None
    ticker = parts[1].upper()
    return ticker if _OPTION_TICKER.fullmatch(ticker) else None


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
    exact = _TEXT_COMMANDS.get(text.lower())
    if exact is not None:
        return exact
    if parse_option_ticker(text) is not None:
        return BotIntent.OPTION_INTEL
    if _MOVER_WINDOW.fullmatch(text):
        return BotIntent.MOVERS
    if _MOVER_SECTOR.fullmatch(text):
        return BotIntent.MOVER_SECTORS
    if _MOVER_TICKER.fullmatch(text):
        return BotIntent.MOVER_TICKER
    if _MONITOR_ADD.fullmatch(text):
        return BotIntent.MONITOR_ADD
    if _MONITOR_REMOVE.fullmatch(text):
        return BotIntent.MONITOR_REMOVE
    return BotIntent.UNKNOWN


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
        engine: "Engine | None" = None,
    ) -> None:
        self._ledger = ledger
        self._settings = settings
        self._cfg = settings.feishu_bot
        self._transport = transport
        self._extractor = extractor
        self._runtime = runtime
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_at = self._clock()
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
            if intent in (
                BotIntent.IMPORT_IMAGE,
                BotIntent.CONFIRM_IMPORT,
                BotIntent.MONITOR_ADD,
                BotIntent.MONITOR_REMOVE,
                BotIntent.REPUSH,
                BotIntent.REPUSH_MOVERS,
            ):
                self._transport.send_text(
                    message.chat_id, "该操作会修改状态或重新推送，请在与机器人的单聊中进行。"
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
        elif intent is BotIntent.OPTION_INTEL:
            self._reply_option_intel(
                message.chat_id, parse_text(message.content_json), now
            )
        elif intent is BotIntent.SIGNALS:
            self._transport.send_text(message.chat_id, self._signals_text(now))
        elif intent is BotIntent.SCAN:
            self._transport.send_text(message.chat_id, self._scan_text())
        elif intent is BotIntent.HEALTH:
            self._transport.send_text(message.chat_id, self._health_text(now))
        elif intent is BotIntent.FEAR_DCA:
            self._reply_fear_dca(message.chat_id)
        elif intent is BotIntent.FEAR_DCA_RULES:
            from quant_signal.notifier.cards import fear_dca_rules_card

            self._transport.send_card(message.chat_id, fear_dca_rules_card())
        elif intent in (BotIntent.MOVERS, BotIntent.MOVER_SECTORS):
            self._reply_movers(
                message.chat_id,
                parse_text(message.content_json),
            )
        elif intent is BotIntent.MOVER_TICKER:
            self._transport.send_text(
                message.chat_id,
                self._mover_ticker_text(parse_text(message.content_json)),
            )
        elif intent is BotIntent.MONITORS:
            self._transport.send_text(message.chat_id, self._monitors_text())
        elif intent is BotIntent.MONITOR_ADD:
            self._handle_monitor_add(
                message.chat_id, parse_text(message.content_json), now
            )
        elif intent is BotIntent.MONITOR_REMOVE:
            self._handle_monitor_remove(
                message.chat_id, parse_text(message.content_json), now
            )
        elif intent is BotIntent.REPUSH:
            self._handle_repush(message.chat_id)
        elif intent is BotIntent.REPUSH_MOVERS:
            self._handle_repush_movers(message.chat_id)
        elif intent is BotIntent.IMPORT_IMAGE:
            self._handle_import(message, now)
        elif intent is BotIntent.CONFIRM_IMPORT:
            self._handle_confirm(message.chat_id, now)
        else:
            self._transport.send_text(
                message.chat_id, f"未识别指令。\n\n{_HELP_TEXT}"
            )

    # ---- 查询指令 ----

    def _reply_fear_dca(self, chat_id: str) -> None:
        card = self._ledger.latest_complete_fear_dca_card()
        if card is None:
            self._transport.send_text(chat_id, "暂无已完成的恐慌指数定投报告。")
            return
        self._transport.send_card(
            chat_id,
            replace(card, message_uuid=str(uuid4())),
        )

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

    def _mover_window(self, text: str) -> int:
        match = _MOVER_WINDOW.fullmatch(text)
        return int(match.group(1)) if match else self._settings.extreme_movers.default_window

    def _mover_card(self, text: str) -> Card | None:
        from quant_signal.extreme_movers import (
            Eligibility,
            MoverDirection,
            rank_movers,
            rank_sectors,
        )
        from quant_signal.notifier.cards import extreme_movers_premarket_card

        session = self._ledger.latest_complete_extreme_mover_session()
        if session is None:
            return None
        window = self._mover_window(text)
        events = self._ledger.extreme_mover_events(session, window_sessions=window)
        window_summaries: dict[int, tuple[int, int]] = {}
        for summary_window in self._settings.extreme_movers.windows:
            summary_events = self._ledger.extreme_mover_events(
                session, window_sessions=summary_window
            )
            window_summaries[summary_window] = (
                sum(
                    event.eligibility is Eligibility.ELIGIBLE
                    and event.direction is MoverDirection.UP
                    for event in summary_events
                ),
                sum(
                    event.eligibility is Eligibility.ELIGIBLE
                    and event.direction is MoverDirection.DOWN
                    for event in summary_events
                ),
            )
        return extreme_movers_premarket_card(
            session=session,
            window_sessions=window,
            movers=rank_movers(events, window_sessions=window),
            sectors=rank_sectors(events, window_sessions=window),
            backfill_warning=any(event.backfilled for event in events),
            top_stocks=self._settings.extreme_movers.top_stocks,
            top_sectors=self._settings.extreme_movers.top_sectors,
            source_label=(
                "best-effort IEX 初筛 + Yahoo adjusted 确认"
                if self._settings.extreme_movers.feed == "hybrid"
                else "Alpaca SIP adjusted 严格模式"
            ),
            window_summaries=window_summaries,
        )

    def _reply_movers(self, chat_id: str, text: str) -> None:
        if text.startswith("异动板块"):
            from quant_signal.extreme_movers import rank_sectors
            from quant_signal.notifier.cards import extreme_mover_sectors_card

            session = self._ledger.latest_complete_extreme_mover_session()
            if session is None:
                self._transport.send_text(chat_id, "暂无已完成的极端异动统计。")
                return
            window = self._settings.extreme_movers.default_window
            events = self._ledger.extreme_mover_events(
                session, window_sessions=window
            )
            query = text.removeprefix("异动板块").strip()
            aliases = {
                "科技": "Information Technology",
                "信息技术": "Information Technology",
                "医疗": "Health Care",
                "金融": "Financials",
                "能源": "Energy",
                "工业": "Industrials",
                "材料": "Materials",
                "地产": "Real Estate",
                "公用事业": "Utilities",
                "通信": "Communication Services",
                "可选消费": "Consumer Discretionary",
                "必选消费": "Consumer Staples",
                "未分类": "未分类",
            }
            sector_filter = aliases.get(query, query) if query else None
            self._transport.send_card(
                chat_id,
                extreme_mover_sectors_card(
                    session=session,
                    window_sessions=window,
                    sectors=rank_sectors(events, window_sessions=window),
                    sector_filter=sector_filter,
                ),
            )
            return
        card = self._mover_card(text)
        if card is None:
            self._transport.send_text(chat_id, "暂无已完成的极端异动统计。")
            return
        self._transport.send_card(chat_id, card)

    def _mover_ticker_text(self, text: str) -> str:
        from decimal import Decimal

        match = _MOVER_TICKER.fullmatch(text)
        if match is None:
            return "用法：异动 <美股代码>，例如「异动 AAOI」。"
        ticker = match.group(1).upper()
        session = self._ledger.latest_complete_extreme_mover_session()
        if session is None:
            return "暂无已完成的极端异动统计。"
        lines = [f"{ticker}｜±10% 入榜统计"]
        found = False
        for window in self._settings.extreme_movers.windows:
            events = [
                event
                for event in self._ledger.extreme_mover_events(
                    session, window_sessions=window
                )
                if event.ticker == ticker
            ]
            compound = Decimal("1")
            for event in events:
                compound *= Decimal("1") + event.daily_return
            found = found or bool(events)
            lines.append(
                f"{window}日: 入榜 {len(events)} 天｜事件日复合 "
                f"{compound - Decimal('1'):+.2%}"
            )
        if not found:
            lines.append("最近 252 个已统计交易日内未进入榜单。")
        else:
            latest_events = [
                event
                for event in self._ledger.extreme_mover_events(
                    session,
                    window_sessions=max(self._settings.extreme_movers.windows),
                )
                if event.ticker == ticker
            ]
            lines.append(
                f"最近一次: {max(event.session for event in latest_events).isoformat()}"
            )
        return "\n".join(lines)

    def _held_symbols(self) -> list[str]:
        from decimal import Decimal

        symbols: list[str] = []
        for row in self._ledger.active_observed_positions():
            try:
                positive = Decimal(str(row.get("qty") or "0")) > 0
            except Exception:  # noqa: BLE001
                positive = False
            if positive:
                symbols.append(str(row.get("symbol") or "").upper())
        return sorted({symbol for symbol in symbols if symbol})

    def _monitors_text(self) -> str:
        holdings = self._held_symbols()
        manual = self._ledger.active_manual_monitors()
        return (
            "实时价格监控\n"
            f"持仓强制监控: {', '.join(holdings) if holdings else '无'}\n"
            f"手动监控: {', '.join(manual) if manual else '无'}"
        )

    def _handle_monitor_add(self, chat_id: str, text: str, now: datetime) -> None:
        match = _MONITOR_ADD.fullmatch(text)
        assert match is not None
        ticker = match.group(1).upper()
        if ticker in self._held_symbols():
            self._transport.send_text(chat_id, f"{ticker} 是当前持仓，已经强制实时监控。")
            return
        limit = max(
            0,
            self._settings.holding_price_alert.max_tickers
            - len(self._held_symbols()),
        )
        if len(self._ledger.active_manual_monitors()) >= limit:
            self._transport.send_text(chat_id, f"手动监控已达到上限 {limit} 个。")
            return
        changed = self._ledger.enable_manual_monitor(ticker, now=now)
        suffix = "已加入" if changed else "已在"
        self._transport.send_text(chat_id, f"{ticker} {suffix}实时价格监控。")

    def _handle_monitor_remove(self, chat_id: str, text: str, now: datetime) -> None:
        match = _MONITOR_REMOVE.fullmatch(text)
        assert match is not None
        ticker = match.group(1).upper()
        if ticker in self._held_symbols():
            self._transport.send_text(chat_id, f"{ticker} 是当前持仓，持仓实时监控仍会保留。")
            return
        changed = self._ledger.disable_manual_monitor(ticker, now=now)
        self._transport.send_text(
            chat_id, f"{ticker} {'已取消' if changed else '不在'}手动监控。"
        )

    def _handle_repush(self, chat_id: str) -> None:
        receive_id = self._cfg.push_receive_id
        if not receive_id:
            self._transport.send_text(chat_id, "未配置推送接收群。")
            return
        sent = self._transport.send_text_to(
            receive_id, "chat_id", self._holdings_text()
        )
        self._transport.send_text(
            chat_id, "最新持仓已重新推送。" if sent else "重新推送失败。"
        )

    def _handle_repush_movers(self, chat_id: str) -> None:
        card = self._mover_card("异动榜")
        if card is None:
            self._transport.send_text(chat_id, "暂无已完成的极端异动统计。")
            return
        receive_id = self._cfg.push_receive_id
        sent = bool(receive_id) and self._transport.send_card(receive_id, card)
        self._transport.send_text(
            chat_id, "最新异动榜已重新推送。" if sent else "重新推送失败。"
        )

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
            raw_failure_count = state.get("consecutive_failures")
            failure_count = (
                raw_failure_count if isinstance(raw_failure_count, int) else 0
            )
            last_failure = state.get("last_failure")
            if failure_count and isinstance(last_failure, datetime):
                failed_when = f"{last_failure.astimezone(_ET):%m/%d %H:%M ET}"
                last_error = str(state.get("last_error") or "未知错误")[:160]
                lines.append(
                    f"· {job_id}: ⚠️连续失败 {failure_count} 次，最近 {failed_when}；"
                    f"最后成功 {when}{duration_text}；{last_error}"
                )
            else:
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
            holding_snapshot=self._ledger.latest_holding_option_flow_snapshot(session),
            etf_underlyings=frozenset(cfg.etf_roots),
        )
        self._transport.send_card(chat_id, card)

    def _reply_option_intel(self, chat_id: str, text: str, now: datetime) -> None:
        # 与 `期权`(读台账)不同：这里现场拉取该标的期权链,耗时数秒
        ticker = parse_option_ticker(text)
        if ticker is None:
            self._transport.send_text(
                chat_id, "用法：期权 <美股代码>，例如「期权 MU」。"
            )
            return
        if self._engine is None or self._engine.option_chain_source is None:
            self._transport.send_text(
                chat_id,
                "期权链查询未启用：需要配置 ALPACA_KEY/ALPACA_SECRET 并重启。",
            )
            return
        from quant_signal.pipelines import option_intel as intel_pipeline

        try:
            intel = intel_pipeline.build_intel(self._engine, ticker, now)
        except Exception as error:  # noqa: BLE001 - 单次查询失败只回错误文案
            log.warning(
                "feishu_bot.option_intel_failed", ticker=ticker, error=str(error)
            )
            self._transport.send_text(
                chat_id, f"{ticker} 期权数据拉取失败：{type(error).__name__}"
            )
            return
        if intel is None:
            self._transport.send_text(
                chat_id, f"取不到 {ticker} 的现价，无法计算期权情报。"
            )
            return
        from quant_signal.notifier.cards import option_intel_card

        card = option_intel_card(
            [intel],
            session=intel.session,
            iv_rv_warn_ratio=self._settings.option_intel.iv_rv_warn_ratio,
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
            labels = {
                "MISSING_ACCOUNT_SUMMARY": "截图缺少账户总资产、持仓市值、现金或购买力",
                "MISSING_ACCOUNT_CURRENCY": "无法确认账户币种",
                "INVALID_ACCOUNT_VALUES": "账户金额无效",
                "ACCOUNT_RECONCILIATION_FAILED": "账户总资产与现金、持仓市值无法对账",
                "POSITION_COUNT_MISMATCH": "页面持仓数量与可见持仓不一致",
                "POSITION_DETAILS_INCOMPLETE": "部分持仓缺少数量、成本价、现价或市值",
            }
            errors = "；".join(
                labels.get(error, error) for error in record.validation_errors
            ) or "未知原因"
            guidance = ""
            if "MISSING_ACCOUNT_SUMMARY" in record.validation_errors:
                guidance = (
                    "\n请发送券商账户资产页的完整原图，确保账户总资产、持仓市值、"
                    "现金/可用资金和持仓数量清晰可见；不要发送聊天窗口的二次截图。"
                )
            self._transport.send_text(
                message.chat_id,
                f"导入被拒绝（REJECTED）：{errors}。账户未更新。{guidance}",
            )
            return
        if record.status is ImportStatus.VALIDATED:
            applied = apply_validated_import(self._ledger, record, now=now)
            if not applied:
                self._transport.send_text(
                    message.chat_id, "该截图此前已导入过，账户未变化。"
                )
                return
            refresh_status = self._refresh_execution_plan(now)
            self._transport.send_text(
                message.chat_id,
                self._import_receipt(
                    record, applied=True, refresh_status=refresh_status
                ),
            )
            return
        # PARTIAL：不自动应用，等待明确确认
        replaced = self._ledger.save_pending_import(record, now)
        errors = "、".join(record.validation_errors)
        lines = [
            f"解析完成但校验不完整（PARTIAL）：{errors}。",
            self._import_receipt(record, applied=False),
        ]
        if replaced:
            lines.append("⚠️ 已覆盖此前待确认的导入。")
        lines.append(
            f"回复「确认导入」可在 {self._cfg.confirm_window_minutes} 分钟内强制应用（谨慎）。"
        )
        self._transport.send_text(
            message.chat_id,
            "\n".join(lines),
        )

    def _handle_confirm(self, chat_id: str, now: datetime) -> None:
        from datetime import timedelta

        from quant_signal.portfolio_import import apply_validated_import

        pending = self._ledger.pop_pending_import()
        if pending is None:
            self._transport.send_text(chat_id, "当前没有待确认的导入。")
            return
        record, stored_at = pending
        window = timedelta(minutes=self._cfg.confirm_window_minutes)
        if now - stored_at > window:
            self._transport.send_text(
                chat_id, "待确认导入已过期，请重新发送截图。"
            )
            return
        applied = apply_validated_import(self._ledger, record, now=now)
        if applied:
            refresh_status = self._refresh_execution_plan(now)
            self._transport.send_text(
                chat_id,
                "已应用 PARTIAL 导入。\n"
                + self._import_receipt(
                    record, applied=True, refresh_status=refresh_status
                ),
            )
        else:
            self._transport.send_text(chat_id, "应用失败：该截图此前已导入过。")

    def _import_receipt(
        self,
        record: "ValidatedPortfolioImport",
        *,
        applied: bool,
        refresh_status: str | None = None,
    ) -> str:
        account = record.extraction.account
        symbols = "、".join(row.symbol for row in record.positions) or "无"
        lines = [
            f"权益: {account.equity} {account.currency}｜现金: {account.cash}",
            f"持仓({len(record.positions)}): {symbols}",
        ]
        if applied:
            lines.insert(
                0,
                refresh_status
                or "账户快照已更新，现有执行计划已按 ACCOUNT_CHANGED 失效。",
            )
        return "\n".join(lines)

    def _refresh_execution_plan(self, now: datetime) -> str:
        if self._engine is None:
            return (
                "账户快照已更新，现有执行计划已按 ACCOUNT_CHANGED 失效；"
                "执行引擎不可用，尚未重算。"
            )
        if not self._settings.execution_plan.enabled:
            return "账户快照已更新，执行计划功能未启用。"
        from quant_signal.calendar import is_trading_day

        now_et = now.astimezone(_ET)
        current_minute = now_et.hour * 60 + now_et.minute
        if (
            not is_trading_day(now_et.date())
            or current_minute < 8 * 60 + 15
            or current_minute > 15 * 60 + 45
        ):
            return (
                "账户快照已更新，现有执行计划已失效；"
                "当前不在自动重算时段（08:15–15:45 ET），"
                "将在下一次定时任务重算。"
            )
        try:
            if (
                self._settings.us_briefing.enabled
                and self._settings.us_briefing.delivery_mode == "live"
            ):
                delivered = self._engine.run_daily_action_briefing(now)
            else:
                delivered = self._engine.run_execution_brief(now)
        except Exception as error:  # noqa: BLE001 - account import must remain committed
            log.warning(
                "feishu_bot.execution_refresh_failed",
                error=str(error),
            )
            return (
                "账户快照已更新，现有执行计划已失效；"
                "自动重算失败，将由定时任务再试。"
            )
        if delivered:
            return "账户快照已更新，执行计划已按新持仓重算，并已重新推送今日行动计划。"
        return (
            "账户快照已更新，执行计划已按新持仓重算；"
            "今日行动计划推送失败。"
        )


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

    def __init__(
        self, app_id: str, app_secret: str, proxy_url: str = ""
    ) -> None:
        import lark_oapi as lark

        configure_lark_proxy(proxy_url)
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
        message_uuid: str | None = None,
    ) -> bool:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body_builder = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
        )
        if message_uuid is not None:
            body_builder = body_builder.uuid(message_uuid)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body_builder.build())
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
            chat_id,
            "interactive",
            json.dumps(payload, ensure_ascii=False),
            message_uuid=card.message_uuid,
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
            message_uuid=card.message_uuid,
        )

    def upload_image(self, image_bytes: bytes) -> str:
        from io import BytesIO

        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
        )

        class NamedImageStream(BytesIO):
            name = "fear-dca.png"

        with NamedImageStream(image_bytes) as image_stream:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(("fear-dca.png", image_stream, "image/png"))
                    .build()
                )
                .build()
            )
            try:
                response = self._client.im.v1.image.create(request)
            except Exception as error:
                raise RuntimeError(
                    f"Feishu image upload failed: {error}"
                ) from error
        if not response.success():
            raise RuntimeError(
                f"Feishu image upload failed: {response.code} {response.msg}"
            )
        image_key = response.data.image_key if response.data is not None else None
        if not image_key:
            raise RuntimeError(
                "Feishu image upload succeeded but no image_key was returned"
            )
        return str(image_key)

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


def configure_lark_proxy(proxy_url: str) -> None:
    """Apply a Feishu-only proxy to the SDK REST and WebSocket transports."""
    if not proxy_url:
        return
    import requests
    import lark_oapi.core.http.transport as lark_http
    import lark_oapi.ws.client as lark_ws

    direct_request: Callable[..., object] = requests.request

    def proxied_request(*args: object, **kwargs: object) -> object:
        kwargs.setdefault(
            "proxies", {"http": proxy_url, "https": proxy_url}
        )
        return direct_request(*args, **kwargs)

    # Replace only the SDK module's requests reference; other providers keep
    # their own network routing and cannot accidentally inherit this proxy.
    lark_http.requests = SimpleNamespace(request=proxied_request)
    ws_kwargs = lambda: {"proxy": proxy_url}
    if hasattr(lark_ws, "_ws_connect_kwargs"):
        lark_ws._ws_connect_kwargs = ws_kwargs
    elif hasattr(lark_ws, "_get_ws_connect_kwargs"):
        lark_ws._get_ws_connect_kwargs = ws_kwargs
    else:
        raise RuntimeError("installed lark-oapi does not expose WebSocket proxy hooks")


def run_ws_forever(
    service: FeishuBotService,
    app_id: str,
    app_secret: str,
    proxy_url: str = "",
) -> None:
    """长连接事件循环：SDK 自带重连之外的兜底重启（退避），永不外抛。"""
    import time as time_module

    import lark_oapi as lark

    configure_lark_proxy(proxy_url)

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
