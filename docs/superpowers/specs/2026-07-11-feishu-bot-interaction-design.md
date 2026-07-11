# 飞书机器人交互设计（截图更新持仓 + 指令查询）

## 1. 目标

把飞书从单向推送升级为双向交互入口：

1. 直接把券商持仓截图发给机器人 → 自动解析、校验、更新账户快照（复用现有
   `portfolio_import` 链路），不再需要在电脑上跑 CLI；
2. 文本指令查询系统状态：持仓、执行计划、期权榜、系统健康；
3. 保持项目铁律：**不下单、不改价格数量、fail closed、凭据不进日志**。

## 2. 关键技术约束（决定架构的事实）

- 现有 `FEISHU_WEBHOOK` 是**自定义机器人**，只能向群里发消息，**无法接收**任何
  消息或图片。接收必须使用**企业自建应用**的机器人能力 + 事件订阅。
- 事件订阅两种模式：HTTP 回调（需要公网 URL——本机是家用 Windows，无公网 IP，
  排除）；**长连接模式**（官方 `lark-oapi` Python SDK 的 WebSocket 客户端，
  出站连接即可，NAT 后可用）→ **采用长连接**。
- 事件推送是 at-least-once，可能重复投递 → 必须按 `message_id` 幂等去重。
- Codex 截图解析单次 20–180 秒 → 不能阻塞事件循环线程。

### 用户需要做的一次性配置（代码无法代劳）

1. 在[飞书开放平台](https://open.feishu.cn)创建**企业自建应用** → 启用**机器人**能力
2. 权限：`im:message`（收发消息）、`im:message.p2p_msg`（读单聊）、`im:resource`（下载图片）
3. 事件订阅选择**长连接**模式，订阅 `im.message.receive_v1`
4. 发布应用（企业内可用），把 `App ID` / `App Secret` 填入 `config/.env`：
   `FEISHU_APP_ID=cli_xxx` / `FEISHU_APP_SECRET=xxx`
5. 单聊里给机器人发一条消息，从日志里拿到自己的 `open_id` 填进
   `settings.yaml` 的 `feishu_bot.allowed_open_ids`（首次为空名单时，机器人对
   任何消息只回复"你的 open_id 是 xxx，请加入白名单"，不执行任何操作）

未配置凭据或 `feishu_bot.enabled=false` 时功能完全不启动，现有 webhook 推送
不受任何影响（两条通道独立共存）。

## 3. 方案比较

### 方案 A：lark-oapi 长连接 + 单模块服务（采用）

- 主进程内起一个 daemon 线程跑 `lark.ws.Client`（SDK 自带断线重连），事件
  只做解析和入队，实际处理丢给单独的 worker 线程池（size=1，串行足够）
- 优点：无公网依赖；一个进程管所有事；SDK 官方维护
- 缺点：新增 `lark-oapi` 依赖（较重，但官方且纯 Python 侧可控）

### 方案 B：HTTP 回调 + 内网穿透（frp/ngrok）

- 排除：引入第三方穿透服务的可用性和安全面，违背"本机自治"的部署哲学。

### 方案 C：轮询群消息

- 排除：飞书没有为自建应用提供拉取历史单聊消息的轮询友好接口，且延迟高。

## 4. 组件设计

新增 `src/quant_signal/feishu_bot.py`（单模块，职责内聚）：

```
┌────────────────────────────────────────────────────────┐
│ FeishuBotService                                        │
│  ├─ lark.ws.Client (daemon thread, SDK自动重连)          │
│  ├─ worker thread (queue, 串行处理, Codex解析在此执行)     │
│  ├─ BotTransport (REST: 发消息/下载图片, 可注入fake)       │
│  └─ handle_event → route_message (纯函数, 全部单测)       │
└────────────────────────────────────────────────────────┘
        │ 读写                          │ 复用
        ▼                              ▼
  SignalLedger (幂等表+查询)      portfolio_import (解析/校验/应用)
```

- **BotTransport 协议**：`send_card(chat_id, card)`、`send_text(chat_id, text)`、
  `download_image(message_id, image_key) -> bytes`。生产实现包 `lark-oapi` REST
  client；测试注入 fake。事件处理与 SDK 完全解耦，`lark-oapi` 仅在生产实现内 import。
- **消息路由（纯函数）**：输入 `(sender_open_id, chat_type, message_type, content)`，
  输出动作枚举。只处理 `chat_type == "p2p"` 的私聊；群聊消息一律忽略（webhook
  推送群保持只读，避免群里误触发）。
- **幂等**：sqlite 新表 `feishu_processed_messages(message_id TEXT PRIMARY KEY,
  processed_at TEXT)`，处理前 INSERT OR IGNORE，已存在则跳过。
- **白名单**：非白名单 open_id 的消息只回其 open_id（方便首次配置），其余动作
  一律拒绝并记日志。

## 5. 指令集（首版）

| 指令 | 行为 |
|---|---|
| `帮助` / `help` | 列出指令 |
| `状态` / `status` | 进程启动时间、今日信号数、最新期权扫描槽位、账户快照年龄、活跃计划数 |
| `持仓` / `holdings` | 最新截图导入的账户概要 + 持仓明细（权益/现金/各标的市值） |
| `计划` / `plans` | 当前活跃执行计划及状态机阶段、阻断原因 |
| `期权` / `options` | 从台账读最新期权榜渲染一张卡（**不新抓数据**，标注扫描时间） |
| （图片消息） | 截图导入流程（见下） |
| `确认导入` | 应用最近一次 PARTIAL 状态的导入（15 分钟内有效） |

未识别文本回复帮助提示。所有回复走自建应用单聊消息（不占用群 webhook）。

## 6. 截图导入流程

1. 收到图片消息（白名单内）→ 立即回"已收到，解析中（约1-3分钟）"
2. worker 线程：下载图片到临时目录 → `CodexPortfolioExtractor.extract` →
   `validate_extraction`（沿用 capital_limit / max_financing_ratio 配置，新增
   `feishu_bot.capital_limit`/`max_financing_ratio`，默认与 CLI 相同 6000/0.20）
3. 按校验结果分派：
   - `VALIDATED` → `apply_validated_import` 直接应用（与 CLI `--apply` 同语义），
     回执卡：权益/现金/持仓数/标的清单/"计划已按 ACCOUNT_CHANGED 失效重算"
   - `PARTIAL` → 不应用，回执卡列出 `validation_errors`，提示"回复'确认导入'
     可在 15 分钟内强制应用（谨慎）"；确认状态存内存（单 worker 串行，无并发）
   - `REJECTED` → 不应用，回执卡说明原因
   - 解析失败/超时 → 回执"解析失败：{原因}"，不留任何半成品状态
4. 多张截图：飞书单条消息一张图；15 分钟窗口内收到的多张图**不**自动合并
   （合并语义复杂且易错，首版一张图=一次导入；文档明确此限制）

## 7. 配置

```yaml
feishu_bot:
  enabled: false                # 默认关闭；凭据缺失时即使 true 也不启动(记警告)
  allowed_open_ids: []          # 白名单，空=只回显 open_id 不执行任何操作
  capital_limit: 6000
  max_financing_ratio: 0.20
  confirm_window_minutes: 15    # PARTIAL 确认窗口
  codex_timeout_seconds: 180
```

`.env` 新增 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`（沿用现有 dotenv 加载，凭据
不进日志/repr/异常，与 `ALPACA_*` 同标准）。

## 8. 可靠性与安全

- ws 线程崩溃：外层 supervisor 循环按退避重启（SDK 内部重连之外的兜底）；
  机器人挂掉**不影响**调度器和 webhook 推送（独立线程，异常不外溢）
- worker 单线程串行：天然避免两次导入并发写账户的竞态
- 图片临时文件用完即删；图片字节不落日志
- 机器人没有任何下单/撤单路径可触达（系统本身不存在此类代码）
- `确认导入` 强制应用的也仅是"账户快照"，后续 sizing 仍走全部风控硬门槛

## 9. 测试与验收

- 路由纯函数：指令识别、白名单拒绝、群聊忽略、未知文本、图片分派
- 幂等：同 message_id 重复投递只处理一次
- 导入流程：fake transport + fake extractor 覆盖 VALIDATED/PARTIAL+确认/
  REJECTED/超时四条路径；确认窗口过期
- `期权`/`持仓`/`计划`/`状态` 指令的台账读取与卡片渲染
- mypy strict；全量 pytest
- 真实验收（需用户配置好自建应用后手动执行）：发一张真实截图 → 收到回执 →
  `python -m quant_signal.portfolio_import` 查询确认账户已更新

## 10. 非目标（首版明确不做）

- 群聊指令、@机器人触发（避免群误触发；后续可加）
- 多图合并导入、PDF/长图切分
- 通过飞书修改策略参数/标的池（改配置必须走 git，保持可审计）
- 把 webhook 推送迁移到自建应用（两通道共存，降低迁移风险）
