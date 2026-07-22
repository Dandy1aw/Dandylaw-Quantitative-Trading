# 持仓期权监控与统一每日行动简报实施计划

> 实施约束：在当前工作区原地开发，严格测试先行；不调用用户禁用的 skill，不改写无关的既有修改。

**目标：** 把真实券商持仓加入 Cboe 期权热度卡，压缩 AI 观点，并将收盘、亚洲确认、盘前与行动计划合并成唯一的盘前每日简报。

**架构：** Cboe most-active 与真实持仓期权链保持两个独立数据域；持仓链用 Alpaca 快照按标的聚合 Call/Put 日成交量并落库。收盘和亚洲阶段只生成版本化快照，08:15 ET 读取快照并刷新账户、行情和执行计划，构建唯一 `DAILY_ACTION` 卡。AI 只作为最多三行、180 字的可选说明层。

**技术栈：** Python 3.12、Pydantic、SQLAlchemy/SQLite、APScheduler、pytest、mypy、飞书卡片 JSON。

---

## 任务 1：配置和持仓期权领域模型

**文件：**

- 修改：`src/quant_signal/config.py`
- 修改：`src/quant_signal/options_flow.py`
- 修改：`config/settings.yaml`
- 测试：`tests/test_config.py`
- 测试：`tests/test_options_flow.py`

1. 先添加失败测试，覆盖五个持仓监控配置默认值和校验边界。
2. 添加 `HoldingOptionFlow`、`HoldingOptionFlowSnapshot`、`HoldingOptionFlowChange` 的构造与不可变性测试。
3. 添加 Call/Put 比值、占优比例、零分母、同交易日增量、跨日和累计量回退重置测试。
4. 运行聚焦测试，确认因字段/类型不存在而失败。
5. 实现最小模型、聚合与变化检测函数，再运行聚焦测试至通过。

## 任务 2：Alpaca 轻量持仓期权快照

**文件：**

- 修改：`src/quant_signal/datafeed/alpaca_options.py`
- 测试：`tests/test_alpaca_options.py`

1. 添加失败测试：`include_open_interest=False` 时只请求 option snapshots，不请求 OI endpoint。
2. 添加 0–14 DTE、Call/Put 合约日成交量与分页测试。
3. 实现兼容现有调用的可选参数，原有 60 DTE 情报默认行为不变。
4. 运行 `tests/test_alpaca_options.py`。

## 任务 3：持久化持仓期权扫描

**文件：**

- 修改：`src/quant_signal/ledger.py`
- 测试：`tests/test_ledger.py`
- 测试：`tests/test_option_intel_ledger.py`

1. 添加失败测试：保存/读取最近持仓流快照、同 slot 幂等、按 retention 清理。
2. 新增 `holding_option_flow_scans` 与 `holding_option_flow_rows` 表及数据访问方法。
3. 保证迁移对既有 SQLite 数据库为增量建表，不破坏历史记录。
4. 运行 ledger 聚焦测试。

## 任务 4：接入期权热度 pipeline 与卡片

**文件：**

- 修改：`src/quant_signal/pipelines/option_flow.py`
- 修改：`src/quant_signal/notifier/cards.py`
- 修改：`src/quant_signal/feishu_bot.py`
- 测试：`tests/test_options_flow.py`
- 测试：`tests/test_cards.py`
- 测试：`tests/test_feishu_bot.py`

1. 添加失败测试：持仓集合只来自 `active_observed_positions()` 且数量大于零，不包含策略虚拟持仓。
2. 添加失败测试：逐持仓抓取失败只降级该行，全部失败不阻断 Cboe 主卡。
3. 添加失败测试：持仓变化可触发共享 alert gate，重跑不重复发送。
4. 添加卡片快照测试：`我的持仓期权` 在全市场 Top10 前，文案完整写 `Call` / `Put`，无链和失败状态明确显示。
5. 实现采集、聚合、上一档增量、持久化、告警合并及 `/options` 最近快照读取。
6. 运行上述聚焦测试。

## 任务 5：AI 三行与 180 字硬限制

**文件：**

- 修改：`src/quant_signal/config.py`
- 修改：`src/quant_signal/ai_briefing.py`
- 修改：`src/quant_signal/pipelines/premarket.py`
- 修改：`config/settings.yaml`
- 测试：`tests/test_ai_briefing.py`
- 测试：`tests/test_ai_briefing_price_guard.py`

1. 添加失败测试：输出仅允许 `主线：`、`持仓：`、`动作：` 三行，总长度不超过 `output_max_chars=180`。
2. 添加超长、缺行、多余 Markdown/表格、未知 ticker 和异常返回的降级测试。
3. 实现输出清洗和校验；不让 AI 失败影响确定性简报。
4. 停止普通盘前流程单独发送 AI 卡，保留结构化结果供统一卡复用。
5. 运行 AI 聚焦测试。

## 任务 6：统一每日行动简报

**文件：**

- 修改：`src/quant_signal/pipelines/us_briefing.py`
- 修改：`src/quant_signal/pipelines/execution_plan.py`
- 修改：`src/quant_signal/notifier/cards.py`
- 修改：`src/quant_signal/engine.py`
- 测试：`tests/test_us_briefing_pipeline.py`
- 测试：`tests/test_execution_pipeline.py`
- 测试：`tests/test_cards.py`

1. 添加失败测试：`US_CLOSE` 与 `ASIA_CONFIRM` 只生成/保存快照，不直接通知。
2. 添加失败测试：`DAILY_ACTION` 读取两阶段最新快照，刷新盘前价格、真实持仓和执行计划，只发送一张卡。
3. 添加六节卡片测试；普通 HOLD 合并，需动作持仓展开，候选最多三只，缺失与 PARTIAL 状态清楚标注。
4. 将 execution brief 的确定性结果改造成可复用 payload，AI 摘要仅调用一次并嵌入卡片。
5. 实现统一 pipeline 和 engine 入口，保留盘中事件及 postmarket 复盘。
6. 运行聚焦测试。

## 任务 7：版本化运行记录、幂等和调度切换

**文件：**

- 修改：`src/quant_signal/ledger.py`
- 修改：`src/quant_signal/scheduler.py`
- 修改：`config/settings.yaml`
- 测试：`tests/test_ledger.py`
- 测试：`tests/test_scheduler.py`

1. 添加失败测试：运行记录包含 report date、rule/model/data/account 版本、输入语义哈希、payload 和通知状态。
2. 添加失败测试：同日期同输入不重复推送；新输入可替换未发送记录，已发送记录保留审计状态。
3. 实现运行记录与幂等查询。
4. 调度改为：收盘/亚洲只落库，08:15 ET 运行 `daily_action_briefing`；移除旧 rotation、premarket 和 execution brief 正式发送任务。
5. 保持 `execution_watch`、期权异动、风控、健康和 postmarket 任务不变。
6. 运行 scheduler 与 ledger 聚焦测试。

## 任务 8：回归、真实路径验收与文档

**文件：**

- 修改：`README.md`
- 修改：`config/.env.example`
- 修改：相关运维文档

1. 运行全部聚焦测试并修复回归。
2. 运行完整 `pytest`、`mypy`、`git diff --check`。
3. 按仓库 `verify` 配方在生产调度器路径执行真实数据 dry-run，确认 Cboe 与 Alpaca 持仓数据边界。
4. 用户要求真实推送时，再通过 durable outbox 向飞书发送期权热度卡和统一每日行动简报。
5. 汇总迁移影响、保留任务、停用任务、验证证据和回滚点。
