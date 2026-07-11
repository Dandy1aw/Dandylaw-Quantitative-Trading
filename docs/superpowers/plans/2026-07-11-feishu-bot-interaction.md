# Feishu Bot Interaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 自建应用机器人长连接接收单聊消息：截图→复用 portfolio_import 更新账户；文本指令查询状态/持仓/计划/期权。

**Architecture:** 单模块 `feishu_bot.py`：纯函数路由 + `FeishuBotService`（worker 串行队列）+ `BotTransport` 协议（生产实现封装 lark-oapi，测试注入 fake）。sqlite 表按 message_id 幂等。默认关闭，凭据缺失不启动。

**Tech Stack:** Python 3.12, lark-oapi(新依赖), sqlite, pytest, mypy strict.

## Global Constraints

- 群聊消息一律忽略；非白名单只回 open_id（spec §4/§5）
- VALIDATED 自动应用 / PARTIAL 需 15 分钟内回复"确认导入" / REJECTED 不应用（spec §6）
- 凭据不进日志/repr/异常；图片字节不落日志；临时文件用完即删（spec §8）
- lark-oapi 只允许在生产 Transport/WS 封装内 import，路由与服务逻辑零 SDK 依赖
- bot 线程异常不得影响调度器与 webhook 推送
- mypy strict、全量 pytest 全绿

---

### Task 1: 配置与凭据

**Files:**
- Modify: `src/quant_signal/config.py`, `config/settings.yaml`, `config/.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `FeishuBotSettings(enabled: bool=False, allowed_open_ids: list[str]=[], capital_limit: Decimal=6000, max_financing_ratio: Decimal=0.20, confirm_window_minutes: int=15, codex_timeout_seconds: float=180)`；`Settings.feishu_bot`；`Settings.feishu_app_id/feishu_app_secret`（来自 env `FEISHU_APP_ID`/`FEISHU_APP_SECRET`）

- [x] Step 1: 失败测试（默认值、env 加载、open_ids 去空白）
- [x] Step 2: RED → 实现 → GREEN
- [x] Step 3: commit `feat: configure feishu bot settings`

### Task 2: 消息模型与路由纯函数

**Files:**
- Create: `src/quant_signal/feishu_bot.py`
- Test: `tests/test_feishu_bot.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class BotMessage:
    message_id: str
    chat_id: str
    chat_type: str          # "p2p" | "group"
    message_type: str       # "text" | "image" | ...
    content_json: str       # 原始 content 字段
    sender_open_id: str

class BotIntent(str, Enum):
    IGNORE = "ignore"; ECHO_OPEN_ID = "echo_open_id"; HELP = "help"
    STATUS = "status"; HOLDINGS = "holdings"; PLANS = "plans"
    OPTIONS = "options"; IMPORT_IMAGE = "import_image"
    CONFIRM_IMPORT = "confirm_import"; UNKNOWN = "unknown"

def route(message: BotMessage, allowed_open_ids: frozenset[str]) -> BotIntent
def parse_text(content_json: str) -> str   # {"text": "..."} → 去 @、去空白
def parse_image_key(content_json: str) -> str | None
```
- 路由规则：group→IGNORE；不在白名单→ECHO_OPEN_ID；image→IMPORT_IMAGE；text 按表匹配（帮助/help、状态/status、持仓/holdings、计划/plans、期权/options、确认导入）；其余 UNKNOWN

- [x] Step 1: 失败测试覆盖上述全部分支 + content JSON 解析容错（坏 JSON → IGNORE/UNKNOWN 不抛异常）
- [x] Step 2: RED → 实现 → GREEN → commit `feat: route feishu bot messages`

### Task 3: 幂等表

**Files:**
- Modify: `src/quant_signal/ledger.py`（schema + 方法）
- Test: `tests/test_ledger.py`

**Interfaces:**
- Produces: `SignalLedger.try_mark_feishu_message(message_id: str, *, now: datetime) -> bool`（首次 True，重复 False；表 `feishu_processed_messages(message_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL)`）

- [x] Step 1: 失败测试（首次 True/重复 False/不同 id 互不影响）
- [x] Step 2: RED → 实现（INSERT OR IGNORE + rowcount）→ GREEN → commit `feat: dedupe feishu bot messages`

### Task 4: FeishuBotService 指令处理

**Files:**
- Modify: `src/quant_signal/feishu_bot.py`
- Test: `tests/test_feishu_bot.py`

**Interfaces:**
- Consumes: Task 2 路由、Task 3 幂等、`SignalLedger.latest_observed_account/active_observed_positions/active_execution_plans/latest_option_flow_snapshot/signals_on`、`notifier.cards.option_flow_card`
- Produces:
```python
class BotTransport(Protocol):
    def send_text(self, chat_id: str, text: str) -> bool: ...
    def send_card(self, chat_id: str, card: Card) -> bool: ...
    def download_image(self, message_id: str, image_key: str) -> bytes: ...

class FeishuBotService:
    def __init__(self, ledger, settings, transport, *, extractor=None, clock=datetime.now) -> None
    def handle(self, message: BotMessage) -> None   # 同步处理一条(worker线程内调用)
    def start(self) -> None                          # 起 worker 线程(队列消费)
    def submit(self, message: BotMessage) -> None    # ws 回调入队
```
- `handle` 流程：幂等检查→route→分派。STATUS/HOLDINGS/PLANS 用台账数据渲染文本；OPTIONS 读 `latest_option_flow_snapshot(今天ET)` 渲染 `option_flow_card`（无数据回"今日暂无扫描"）；异常捕获后回"处理失败:{类型}"并记日志，绝不外抛。

- [x] Step 1: 失败测试（fake transport 记录回复）：help/status/holdings/plans/options/unknown/白名单外/重复消息
- [x] Step 2: RED → 实现 → GREEN → commit `feat: handle feishu bot commands`

### Task 5: 截图导入流程（含 PARTIAL 确认）

**Files:**
- Modify: `src/quant_signal/feishu_bot.py`
- Test: `tests/test_feishu_bot.py`

**Interfaces:**
- Consumes: `CodexPortfolioExtractor`(可注入 fake)、`validate_extraction`、`apply_validated_import`、`image_digest`
- Produces: `handle` 对 IMPORT_IMAGE/CONFIRM_IMPORT 的实现；内存态 `self._pending_partial: tuple[ValidatedPortfolioImport, datetime] | None`

- [x] Step 1: 失败测试：
  - VALIDATED → 自动应用（fake ledger 断言 save 调用）+ 回执含权益/标的
  - PARTIAL → 不应用 + 回执含 errors；15 分钟内"确认导入"→应用；过期→拒绝
  - REJECTED → 不应用；extractor 抛异常 → 回"解析失败"不留状态
  - 图片临时文件在处理后被删除
- [x] Step 2: RED → 实现（下载→NamedTemporaryFile→extract→validate→分派；finally 删除文件）→ GREEN → commit `feat: import portfolio screenshots via feishu`

### Task 6: lark-oapi 封装 + main 装配 + 文档

**Files:**
- Modify: `src/quant_signal/feishu_bot.py`（`LarkTransport`、`run_ws_forever(service, app_id, app_secret)`）
- Modify: `src/quant_signal/main.py`、`pyproject.toml`(+lark-oapi)、`README.md`、`deploy/README.md`
- Test: `tests/test_feishu_bot.py`（事件解包纯函数 `message_from_event(payload: dict) -> BotMessage | None` 用字典测，不 mock SDK 对象）

**Interfaces:**
- Consumes: Task 4/5 的 service
- Produces: main.py 在 `settings.feishu_bot.enabled and app_id and secret` 时 `service.start()` + daemon 线程跑 `run_ws_forever`（supervisor 循环 + 退避，异常只记日志）

- [x] Step 1: `uv add lark-oapi` 并锁定版本
- [x] Step 2: `message_from_event` 失败测试（真实事件 JSON 结构样例：p2p 文本、图片、群聊、缺字段）→ RED → 实现 → GREEN
- [x] Step 3: `LarkTransport`：`lark.Client` 发消息(interactive card 复用 `notifier.feishu._to_feishu_payload`)、`GetMessageResourceRequest` 下载；`run_ws_forever`：`lark.ws.Client(...,event_handler=dispatcher)` + while True 重启退避（本步无单测，SDK 边界，靠真实验收）
- [x] Step 4: main.py 装配 + README「飞书机器人交互」章节（自建应用配置步骤照抄 spec §2）
- [x] Step 5: 全量 pytest + mypy → commit `feat: run feishu bot over lark long connection`

### Task 7: 全量验证与真实验收清单

- [x] `pytest -q` 全绿、`mypy src/` 全绿
- [x] `enabled=false`（默认）时：调度器启动日志无 bot 线程、行为与现状完全一致
- [x] 文档给出用户手动验收步骤：配置自建应用→填凭据→enabled:true→重启→发"状态"收到回复→发截图收到解析回执→CLI 查询账户已更新
- [x] commit + push
