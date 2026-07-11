# 期权任务正确性与数据治理设计（半日市 + 保留策略）

## 1. 目标

1. **半日市正确性**：提前收盘日（如 7/3、感恩节次日，13:00 ET 收盘）：
   盘中扫描不再在收盘后空转到 15:45；收盘榜在 13:20 发（而不是 16:20 发一份
   冻结了 3 小时的"收盘榜"）；
2. **数据保留**：`option_flow_scans/rows/outbox` 增长无界（每 15 分钟约 100
   行，一年约 60 万行），maintenance 任务按保留期清理。

## 2. 设计

### 日历：收盘时间查询

`calendar.py` 新增（与现有 `_sessions_for_year` 同样的 lru_cache 模式）：

```python
def session_close_utc(d: date) -> datetime | None
```

来源 `pandas_market_calendars` schedule 的 `market_close` 列（UTC aware）。
非交易日返回 None。

### 调度门控（scheduler.py）

- `option_flow` 盘中任务：cron 不变（10:00–15:45 每 15 分钟），任务体内加
  `now > session_close_utc(today)` 即跳过（log `skip.after_close`）——半日市
  13:00 后的触发全部空跳，正常日无影响；
- `option_flow_close`：cron 改为**双时点** `13:20 与 16:20 ET` 触发，任务体内
  校验"当前时点落在 `close + 15min ~ close + 55min` 窗口内"才执行——正常日
  只有 16:20 命中，半日市只有 13:20 命中，同一天不会双发（且 outbox 的
  `UNIQUE(slot, alert_type)` 兜底）；
- `option_flow_drain` 不动（收盘后排空，半日市照样有效）。

### 保留策略（maintenance）

- 配置 `option_flow.retention_days: int = 120`（ge=30——保留期必须覆盖二期
  历史分位所需的 20 个交易日样本，留足余量）；
- ledger 新方法 `prune_option_flow(before: datetime) -> int`：单事务删除
  `captured_at < before` 的 scans + 级联 rows + 对应 slot 的 outbox
  （outbox 里终态行一并清理；PENDING 行不可能超过 12 小时过期窗口，不受影响）；
- `feishu_processed_messages` 顺带清理 `processed_at < before`（同样无界增长）；
- maintenance job（03:00 ET，已有）里调用，删除量记日志。

## 3. 测试

- `session_close_utc`：正常日 16:00 ET、半日市 13:00 ET（用 2026-11-27 感恩节
  次日）、周末 None
- 盘中任务收盘后跳过；`option_flow_close` 正常日 13:20 不执行/16:20 执行，
  半日市反之
- `prune_option_flow`：边界内外各留一条验证；outbox/rows 级联；返回删除数
- retention_days 配置校验（<30 拒绝）

## 4. 非目标

- 不做日级聚合摘要表（明细保 120 天已覆盖二期需求，YAGNI）
- 二期"历史分位/IV 变化/5 分钟季节性"另立方案（触发条件：累计 ≥20 个交易日数据）
