# 飞书机器人查询完善设计（期权回退 + 新指令 + PARTIAL 持久化）

## 1. 目标

1. `期权` 指令在非交易时段（周末/盘后无当日扫描）回退到**最近一个交易日**的
   最新扫描，并在卡片数据身份段如实标注扫描时间（现状：周末回"今日暂无"）；
2. 新增三个只读指令：`信号`（今日各策略信号）、`扫描`（最新 market_scan
   Top20 观察榜）、`健康`（各定时任务最近成功时间/时长/是否卡死）；
3. PARTIAL 待确认导入从内存态改为 sqlite 持久化，进程重启不丢。

## 2. 设计

### 期权回退

`_reply_options`：先查今天 ET 的 `latest_option_flow_snapshot`，为空则沿
`previous_trading_day` 回退（最多 5 个交易日，防无限循环），仍无则回"近 5 个
交易日无期权扫描数据"。卡片本身已展示 `captured_at`，无需额外改动。

### 新指令

- `信号` / `signals`：`ledger.signals_on(今天ET)` → 按策略分组的文本列表
  （标的/方向/价格/是否已推送），空则"今日无信号"
- `扫描` / `scan`：`ledger.latest_scan_candidates()` → Top20 观察榜文本
  （排名/标的/得分），空则"暂无扫描数据"
- `健康` / `health`：读 `JobRuntime` 快照 → 每个 job 的最近成功时间(ET)、
  时长、当前是否运行中/卡死（运行超 10 分钟标 ⚠️）。
  **接线**：`JobRuntime` 现在是 `build_scheduler` 的局部变量。改为
  `build_scheduler(..., runtime: JobRuntime | None = None)`（缺省自建，
  向后兼容），main 里先建 `JobRuntime` 传给 bot 和 scheduler 共享。
  `JobRuntime` 加只读方法 `snapshot() -> dict[str, dict[str, object]]`。
  bot 未注入 runtime 时（如测试）回"运行状态不可用"。

三个指令加入群 @ 只读集合（与 状态/计划/期权 同权限，回复发群里）。

### PARTIAL 持久化

sqlite 新表 `feishu_pending_imports(id INTEGER PRIMARY KEY CHECK (id = 1),
payload_json TEXT NOT NULL, stored_at TEXT NOT NULL)`——单行表，语义就是
"最多一个待确认导入"，新的覆盖旧的。ledger 方法：
`save_pending_import(record, now)` / `pop_pending_import() ->
tuple[ValidatedPortfolioImport, datetime] | None`（读取即删除，保持一次性
消费语义）。`ValidatedPortfolioImport` 是 pydantic 模型，用
`model_dump_json()` / `model_validate_json()` 序列化。
服务里 `self._pending_partial` 全部替换为台账读写；确认窗口判断不变。

## 3. 测试

- 期权回退：昨日有扫描/今日有扫描/5 日内全无三种情形
- 三个新指令的渲染与空态；`健康` 无 runtime 时的降级
- 群 @ 新指令走群回复
- PARTIAL：存→重建 service（模拟重启）→确认导入成功；过期路径不变；
  新截图覆盖旧待确认

## 4. 非目标

- `健康` 不展示 JobHealth 的错误队列（那是心跳告警的消费品，避免双消费）
- 不做多个 PARTIAL 排队（单行表，新的覆盖旧的并在回执中说明）
