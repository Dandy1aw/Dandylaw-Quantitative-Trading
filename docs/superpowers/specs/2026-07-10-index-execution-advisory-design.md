# 指数发现池与交易执行建议设计

日期：2026-07-10  
状态：已批准，待实施  
范围：纳指100与标普500发现池、只读账户同步、确定性买卖执行建议、盘中状态提醒

## 1. 目标

系统需要从“价格到点提醒器”升级为只读交易执行建议系统，明确回答：

1. 今天观察哪些股票；
2. 何时满足买入条件；
3. 建议使用什么限价；
4. 在当前账户现金、持仓和风险约束下买多少股；
5. 哪些条件使计划失效，以及持仓何时需要止损、止盈或轮出。

本阶段不自动下单。所有价格、金额和数量由确定性规则计算，AI 只解释结构化计划。

## 2. 已核实现状

- 当前 `market_scan` 每天列举全部 active/tradable 美股，本机实测为 12,420 个代码；两段式 yfinance 抓取可能运行超过 72 分钟并与早报重叠。
- 当前 `holdings` 表只有 `strategy_id + ticker`，盘前策略会把目标名单直接写成持仓，因此它不是券商实际持仓。
- 当前到价提醒仅判断 `live <= target_buy * 1.002`，没有趋势确认、计划有效期、真实成交或状态迁移。
- 当前 Alpaca 凭证可只读访问 ACTIVE 的 paper 账户、现金、持仓和订单；live 账户不可访问。
- 当前凭证可读取结束时间早于当前至少 15 分钟的 Alpaca SIP 历史日线，适合使用完整市场成交量进行指数横截面扫描。
- 当前基线为 252 个测试通过，`mypy src` 通过，但没有大池负载、成分源降级、账户陈旧和执行状态机覆盖。

## 3. 非目标

- 不自动提交、修改或取消券商订单。
- 不把纳指100和标普500全部加入现有核心动量池、RSI/MACD/布林策略池或盘中 breakout watchlist。
- 不用当前指数成分回测多年历史后宣称无偏绩效；没有历史时点成分数据时只做明确标注的研究或上线后的前向验证。
- 第一阶段不计算港股、韩股的实际股数；它们涉及券商覆盖、FX、每手股数和最小报价单位，继续输出观察价位和仓位比例。
- 不让 AI 生成或修改买入价、止损价、金额和股数。

## 4. 总体架构

数据和语义必须分成五层：

1. **核心策略池**：保留当前 `settings.tickers`，继续服务已验证的日线策略。
2. **指数发现池**：纳指100与标普500成分股的去重并集，只服务每日扫描。
3. **候选池**：指数发现池经数据质量、流动性、趋势和横截面评分后产生的 Top20。
4. **执行池**：风险门控通过的最多 3 至 5 个候选，生成持久化执行计划。
5. **券商实际状态**：账户、持仓、未成交订单和成交，由只读账户适配器提供。

核心策略目标与券商实际持仓不得共用同一张表或同一字段。

## 5. 指数发现池

### 5.1 成分来源与缓存

新增独立 `IndexUniverseProvider`，不挂在行情 `DataSource` 上。它负责：

- 从 Nasdaq 官方完整成分页读取纳指100；
- 从 S&P 官方成分页面读取标普500；
- 合并重复证券并保留 `memberships`，例如同一股票可同时属于两个指数；
- 保存 `as_of`、`fetched_at`、`source`、内容哈希和成员列表；
- 原子写入最后一次成功快照；
- 源站失败或解析异常时使用最后一次成功快照。

更新快照前执行硬校验：

- 纳指100证券数在 95 至 110 之间；
- 标普500证券数在 490 至 510 之间；
- 去重并集在 450 至 650 之间；
- 所有代码非空且通过规范化；
- 单次变更数量超过 25 时拒绝覆盖并告警。

缓存超过 14 天仍无法更新时，指数扫描进入 `STALE_UNIVERSE`，只输出系统告警，不产生新的可执行买入计划。

### 5.2 代码规范化

内部使用 canonical symbol，并为数据提供商维护别名：

- canonical/Alpaca：`BRK.B`、`BF.B`；
- yfinance（仅降级）：`BRK-B`、`BF-B`。

成分合并、行情查询、计划唯一键和账户持仓匹配都使用 canonical symbol。任何无法映射或不可交易的证券保留在覆盖率报告中，但不进入候选排名。

### 5.3 行情策略

扫描日线首选 Alpaca SIP 历史数据，`end` 固定为上一已完成交易日，避免使用 IEX 部分成交量。首次 bootstrap 读取至少 210 个日历日并写入 BarStore；后续每天只补最近 10 个日历日。

每日扫描只从 BarStore 读取完整窗口，不再每天重拉全历史。每个证券必须满足：

- 最新 bar 等于上一预期交易日；
- OHLC 为有限正数，成交量非负；
- 至少 130 个有效交易日；
- 20 日平均成交额达到配置阈值；
- 全池有效行情覆盖率至少 98%。

覆盖率不足、数据日期错误或抓取超时均 fail closed：保留观察榜单诊断，但不产生新的可执行计划。

### 5.4 候选评分

沿用已有可解释评分作为首版：

- 60 日动量 40%；
- 距 20 日高点近度 30%；
- 5 日量比 30%；
- 各因子做横截面 5%/95% 截尾和百分位排名。

输出 Top20 观察候选，最多前 5 个进入执行风控。候选卡和执行计划必须标注指数归属、行情日期、评分与阻断原因。

## 6. 只读账户模型

定义 `AccountProvider` 协议，首个实现为 `AlpacaPaperAccountProvider`，只调用 GET 接口：

- account：币种、equity、cash、buying_power、时间；
- positions：ticker、qty、avg_cost、market_value、unrealized_pl；
- open orders：方向、数量、限价、状态；
- recent closed/filled orders：最近成交摘要。

卡片和日志必须明确标注 `PAPER`。账户快照超过 60 秒、接口失败或关键字段无效时，计划仍可展示观察价位，但 `suggested_qty` 和 `suggested_notional` 为空，并写明“账户数据不足，未计算股数”。

新增以下持久化实体：

- `strategy_targets`：策略目标组合，不代表成交；
- `account_snapshots`：账户权益和现金快照；
- `broker_positions`：只读券商持仓；
- `broker_orders`：只读订单；
- `execution_plans`：价格、数量、有效期、状态和规则版本；
- `plan_events`：状态迁移和通知 outbox，保证幂等。

数据库采用 additive migration 和 `schema_version`，不破坏现有 signals/holdings 数据。

## 7. 执行计划

### 7.1 计划生成

盘前信号和指数候选先按 ticker 聚合。多策略同向时形成一个 ticker 级计划并记录 `source_strategies`，不能按策略重复计算仓位；相反方向或买入区间无交集时计划进入 `BLOCKED_CONFLICT`。

计划字段至少包括：

- `plan_id`、`plan_version`、`plan_date`、ticker、currency；
- 来源策略、指数归属和候选评分；
- `entry_low`、`entry_high`、`limit_price`；
- `stop_loss`、`take_profit`；
- `target_weight`、`suggested_qty`、`suggested_notional`；
- `valid_from`、`expires_at`、行情时间、账户时间；
- state、block_reason、rule_version。

`plan_id` 由交易日、ticker 和计划输入哈希稳定生成。输入价格或规则改变时递增 `plan_version`。

### 7.2 买入状态机

状态迁移为：

```text
CANDIDATE -> ARMED -> IN_ENTRY_ZONE -> ACTIONABLE
             |            |               |
             +-------> INVALIDATED <-------+
             +-------> EXPIRED

ACTIONABLE -> AWAITING_FILL -> OPEN -> EXIT_DUE -> CLOSED
```

首版不自动下单，因此：

- 检测到 paper 持仓增加或对应订单成交后才进入 `OPEN`；
- 仅发送通知不代表成交；
- 同一 `plan_id + plan_version + event_type` 最多成功通知一次。

美股首版只在正常交易日 09:45 至 15:45 ET 评估入场，半日市按日历缩短。进入 `ACTIONABLE` 必须同时满足：

1. 价格从买入区上方进入 `[entry_low, entry_high]`；
2. 一根完整 5 分钟 bar 收盘仍位于买入区且没有跌破止损；
3. 日线趋势门仍为 LONG；
4. 行情时间和账户时间未过期；
5. 不处于财报阻断窗口；
6. 没有重复未成交买单；
7. 风险和集中度允许数量大于零。

当前价已低于止损、趋势转 FLAT、计划超过有效期或数据过期时进入 `INVALIDATED/EXPIRED`，禁止 BUY。

### 7.3 价格语义

- `entry_low/entry_high` 延用各策略的结构化买入区间；
- `limit_price = entry_high`，表示触发后可接受的最高买入限价，不承诺成交；
- `stop_loss` 必须严格低于 `entry_low`；
- `take_profit` 必须严格高于 `entry_high`；
- 止损距离低于 2% 或高于 20% 时计划阻断，避免异常数据产生巨大或无意义数量；
- 价格必须携带币种和行情时间。

### 7.4 数量计算

美国股票首版按整股计算：

```text
target_qty = floor(account_equity * target_weight / limit_price)
gap_qty = target_qty - current_qty - open_buy_qty + open_sell_qty
risk_qty = floor(account_equity * risk_per_trade / (limit_price - stop_loss))
cash_qty = floor((available_cash - account_equity * cash_reserve - frozen_cash) / limit_price)
suggested_qty = max(0, min(gap_qty, risk_qty, cash_qty, concentration_qty))
```

指数扫描候选没有策略目标权重时，使用风险数量并受 `max_position_weight` 限制。

初始可配置风险上限：

- `risk_per_trade = 0.005`；
- `max_daily_new_risk = 0.01`；
- `max_position_weight = 0.12`；
- `max_cluster_weight = 0.35`；
- `cash_reserve = 0.20`；
- `max_new_positions_per_day = 2`。

已有持仓、未成交订单、同主题暴露和当日已使用风险必须从剩余额度扣除。任何关键输入缺失都不得由 AI 或默认本金补算。

## 8. 通知与 AI

### 8.1 盘前执行计划卡

每天输出一张结构化卡，包含：

- PAPER 账户权益、现金、总风险暴露；
- 当前持仓数量、成本、浮盈亏；
- 未成交订单和最近成交；
- 可执行/等待/阻断候选；
- 每个候选的触发条件、买入区间、最高限价、数量、金额、止损、止盈和有效期；
- 数据源、行情时间、账户时间和不操作理由。

### 8.2 盘中提醒

关闭普通正负价格偏离通知。盘中只推状态变化：

- `ACTIONABLE`：确认进入买区，可按结构化计划观察下单；
- `INVALIDATED/EXPIRED`：计划失效；
- `STOP_BREACH`：真实持仓触发止损；
- `TAKE_PROFIT`：真实持仓触及止盈参考；
- `REBALANCE_DUE`：策略轮出或目标仓位下降。

### 8.3 AI 边界

AI 接收执行计划 JSON 和观察榜单，只能解释市场主线、计划优先级、风险和不操作理由。Prompt 明确禁止：

- 推导输入中不存在的价格、金额或股数；
- 改写结构化 limit/stop/take-profit/qty；
- 把 PAPER 账户描述成真实实盘账户；
- 把观察候选描述成确定交易指令。

AI 失败、超时或输出不合规不影响结构化执行计划卡。

## 9. 调度、可靠性与发布

- 指数成分更新每周一次，失败使用最后成功快照。
- 行情 bootstrap 在非关键时段单独执行，并设置总 deadline。
- 日常指数扫描应在早报前完成，设置最大执行时间 10 分钟；超时取消并告警。
- 扫描、早报和执行监控不得并发使用同一个 yfinance/BarStore 重任务资源。
- 监听 APScheduler 的 ERROR、MISSED 和 MAX_INSTANCES，并记录 `started_at/last_success/duration`；健康检查不得恒为 True。
- 日志必须脱敏 webhook 和凭证。
- 新功能均有独立 feature flag：`index_universe.enabled`、`execution_plan.enabled`、`legacy_price_deviation.enabled`。
- 发布前形成 clean release commit/tag 或 wheel，备份配置和 SQLite；生产不再依赖不可追踪的 editable 脏工作区作为回滚点。

首版以 `PAPER/观察` 标签发布，不自动下单。关闭新 feature flag 即可回退到旧早报；旧普通偏离提醒保持关闭，避免恢复无效噪音。

## 10. 测试与验收

### 10.1 自动化测试

- 成分解析、数量异常、原子缓存和陈旧降级；
- `BRK.B <-> BRK-B` 映射、重叠去重和指数归属；
- 600 证券负载、分块、SIP 分页、超时和部分失败；
- 行情覆盖率和上一交易日新鲜度；
- 账户字段解析、空账户、陈旧账户、未成交订单和部分成交；
- 数量公式、现金/风险/集中度上限、止损过近和零数量；
- 状态机所有合法/非法迁移和重复通知幂等；
- 半日市、DST、开盘前/收盘后和跳空穿止损；
- AI prompt 价格/数量不可改写；
- 调度 deadline、MISSED/MAX_INSTANCES 和 feature flag 回滚。

### 10.2 回放与绩效口径

- 用历史 5 分钟 bar 回放旧 target-hit 与新确认状态机，比较提醒数量、入场率、次日/5日/20日收益、最大不利变动和滑点敏感性；
- 使用当前成分做历史研究时显式标注幸存者偏差，不将其作为正式 alpha 证据；
- 从首个成分快照开始保存 point-in-time membership，后续前向绩效使用当日真实成员。

### 10.3 发布门槛

- 全量 pytest 和 mypy 通过；
- 600 证券模拟扫描在 10 分钟内完成；
- 指数行情覆盖率至少 98%；
- 扫描不跨越 08:00 ET 早报；
- stale 行情/账户不会输出股数或 ACTIONABLE；
- 同一计划同一事件无重复推送；
- paper 账户端到端只读 smoke 通过；
- Scheduled Task 重启后服务、任务列表、last-success 和错误日志正常。

## 11. 实施拆分

本设计拆成两个可以独立验收的子项目，但在同一版本集成：

1. **指数发现池**：成分缓存、SIP 增量行情、扫描候选、运行 deadline 和诊断卡；
2. **交易执行建议**：账户适配器、执行账本、状态机、数量计算、飞书卡和 AI 约束。

先完成并验证指数发现池，再让执行建议只消费小候选池。任何阶段失败都不应影响现有核心早报。
