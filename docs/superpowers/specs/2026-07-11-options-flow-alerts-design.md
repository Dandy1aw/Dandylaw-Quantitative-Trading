# 美股期权异动提醒设计

## 1. 目标

新增一个只观察、不下单的期权热度频道，回答三个问题：

1. 当前哪些标准美股期权合约进入 Call / Put 成交量 Top10；
2. 哪些合约相较上一轮出现了新进榜、排名跃升或成交量加速；
3. 数据覆盖、延迟和解释边界是什么。

首版不输出期权买入价、张数或确定性多空方向，也不把 Call 成交等同于看涨、Put 成交等同于看跌。

## 2. 已验证的数据边界

当前 Alpaca 账户可以读取 Option Contracts、Indicative Chain、Snapshots、Latest Trade/Quote、Greeks 和 OI；显式请求 OPRA 返回 `403 OPRA agreement is not signed`。Basic 期权成交约延迟 15 分钟，Indicative Quote 不是实际 OPRA NBBO。Alpaca 也没有全市场期权成交量 Top10 接口，当前账户通配订阅全部期权成交实测被 `405 symbol limit exceeded` 拒绝。

官方边界：

- Alpaca 套餐与期权覆盖：<https://docs.alpaca.markets/us/docs/about-market-data-api>
- Alpaca 历史期权数据：<https://docs.alpaca.markets/us/docs/historical-option-data>
- Alpaca Option Chain：<https://docs.alpaca.markets/us/reference/optionchain>
- Alpaca Snapshots：<https://docs.alpaca.markets/us/reference/optionsnapshots>
- Cboe Most Active 页面：<https://www.cboe.com/markets/us/options/market-statistics/most-active/>

Cboe 官方页面当前使用公开 JSON 数据接口，分别提供 Cboe Options、C2、BZX Options、EDGX Options 的合约成交量榜。首版用这四个市场做候选发现并合并同一合约成交量，再用 Alpaca Indicative 做可选补全。该结果必须命名为“Cboe 四市场”，不能写成“全 OPRA”或“全美实时”。

## 3. 方案比较与决策

### 方案 A：Cboe 四市场发现 + Alpaca 补全（采用）

- 优点：不增加订阅；直接得到广覆盖的合约成交量榜；网络量小；最符合用户要求的 Call / Put Top10。
- 缺点：只覆盖 Cboe 四个交易所，不包含 Nasdaq、NYSE、MIAX、BOX、MEMX 等期权成交；Cboe 页面 JSON 是网站接口而非承诺稳定的商业 API。
- 缓解：Provider 独立封装、严格响应校验、四市场缺一即 fail closed、卡片明确覆盖范围，未来可替换专业数据源。

### 方案 B：Alpaca 动态标的池

- 优点：合约、OI、Greeks、快照字段完整，数据边界明确。
- 缺点：只能扫描持仓、watchlist 和指数候选池，不能称为市场 Top10；约 15 分钟延迟。

### 方案 C：OPRA / 专业期权流供应商

- 优点：可做全市场实时逐笔聚合、成交方向近似和扫单分析。
- 缺点：需要新订阅、协议和供应商适配。

首版实施方案 A，同时保留 `OptionFlowSource` / `OptionEnricher` 接口，后续切换方案 C 时不重写排名、台账、卡片和调度。

## 4. 范围与过滤

默认范围是标准美股个股与 ETF 期权：

- Cboe Options 使用 `equity` 分类；C2、BZX、EDGX 使用 `all` 后排除配置中的指数根代码。
- 合约根代码必须是 1–6 位纯英文字母；以数字开头、含数字后缀或公司行动调整合约不进入正式 Top10。
- 到期日不得早于扫描交易日；0DTE 保留但明确标记。
- 每个市场每侧抓取前 50，按 `(root, expiry, strike, call/put)` 聚合四市场成交量，再分别取前 10。
- 排名使用 Cboe 四市场累计合约张数；Alpaca 字段不能反向改变原始 Top10，只用于解释。

由于每个市场只暴露榜单前 50，同一合约在某市场跌出前 50 时，该市场的小额成交不会进入合计；因此卡片中的张数是“四个 Cboe 榜单可见量合计”，属于下限近似，不得称为全市场 consolidated volume。

首版不对 ETF 做隐藏过滤，因为 SPY/QQQ 本身是重要的市场温度；“异动聚焦”最多展示同一标的两张合约，防止单一 0DTE 链淹没信息。

## 5. 数据模型

`OptionContractVolume`：

- `underlying`
- `contract_symbol`（标准 OCC symbol）
- `side`：call / put
- `expiration`
- `strike`
- `volume`
- `rank`
- `venues`
- `captured_at`

`OptionEnrichment`（所有字段允许缺失）：

- latest trade price/time
- bid/ask/time
- implied volatility、delta/gamma/theta/vega
- open interest、OI date、contract multiplier
- 估算累计权利金 `Cboe volume × latest trade × multiplier`
- `volume / OI`，仅在 OI 大于 0 且日期有效时计算

缺失字段不得用 0 或 100 倍乘数猜测。Volume/OI 只是相对前一结算日持仓的换手参考，不代表新开仓。

## 6. 异动判定

原始 Top10 是浏览榜；盘中通知必须满足“实质变化”。每 15 分钟与上一轮 50 名候选比较：

- `NEW_TOP10`：上一轮排名大于 10 或不存在，本轮进入前 10；
- `RANK_JUMP`：排名上升至少 3；
- `VOLUME_SURGE`：普通合约 15 分钟增量至少 10,000 张；ETF 或 0DTE 至少 20,000 张；
- `HIGH_TURNOVER`：在 OI 有效且至少 100 时，Volume/OI 至少 2；该条件不能单独触发通知。

解释分只用于排序聚焦项，不替代硬门槛：

```text
score = 30 * NEW_TOP10
      + min(25, 5 * rank_jump)
      + min(35, 35 * volume_delta / surge_threshold)
      + 10 * HIGH_TURNOVER
```

盘中卡至少有一项 `score >= 50` 才生成。首次扫描没有历史比较，只生成基线榜；收盘扫描固定生成最终榜。

## 7. 调度与降噪

- 交易日 `10:00–15:45 ET` 每 15 分钟扫描；避开开盘最初 30 分钟噪声。
- `16:20 ET` 再扫描一次并生成收盘榜，使约 15 分钟延迟的 Alpaca 补全覆盖常规收盘。
- 当日第一份有效扫描：固定发送基线榜。
- 盘中：只有实质变化才发送，普通变化冷却 60 分钟。
- 收盘：固定发送最终榜。
- 每日最多 4 张，包括基线与收盘，因此盘中变化最多 2 张。
- 同一 15 分钟时间槽使用稳定 key，任务重跑不得重复保存或推送。
- 待发卡过期时间默认 45 分钟；过期的盘中榜取消，不在下午补发上午旧排名。收盘榜过期时间 12 小时。

`action_card_only` 不抑制本频道，因为 `option_flow.enabled=true` 代表用户明确订阅；期权榜不重复塞入每日股票行动卡。

## 8. 持久化与可靠性

SQLite schema 升级为 v5，新增独立表：

- `option_flow_scans`：时间槽、扫描类型、数据源、市场覆盖、抓取时间、行数；
- `option_flow_rows`：每轮候选 50 名的合约、侧别、排名、成交量与可选补全 JSON；
- `option_flow_outbox`：卡片 payload、状态、重试次数、下次重试、过期时间、错误和发送时间。

不复用现有 plan outbox：账户截图更新会取消 plan 的全部 PENDING 事件，不能误取消期权榜。

保存扫描与待发卡必须在一个 SQLite 事务内完成。任务开始先重试未过期 outbox，抓取和校验完成后再原子保存当前扫描与可选卡片，最后发送。网络/TLS/5xx 做三次有界重试；429 遵守 `Retry-After`；其他 4xx 不重试；四个 Cboe 市场任何一个缺失、解析不完整或 Call/Put 候选不足 10 时不保存、不推残缺榜，并让异常进入 JobHealth。

## 9. 飞书卡片

单张结构化移动端卡，不使用 Markdown 表格，最多五段：

1. 数据身份：扫描时间、`Cboe C1/C2/BZX/EDGX`、Alpaca `INDICATIVE ≈15m`、覆盖状态；
2. 异动聚焦：最多 5 张合约，展示新进榜/排名变化/15 分钟增量、Volume/OI、估算权利金和风险标签；
3. CALL Top10：每行 `排名 · 标的 到期日 行权价C · 成交量 · 增量/DTE`；
4. PUT Top10：同上；
5. 解释边界：Call 不等于看涨、Put 不等于看跌；可能是平仓、备兑、保护、价差或做市对冲；不构成交易建议。

标题使用“美股期权热度 · Cboe四市场”，不用“主力买入”“扫货”“强烈看涨/看跌”等无法由当前数据证明的措辞。

## 10. 配置

新增 `option_flow`：

- `enabled: true`
- `feed: indicative`
- `top_n: 10`
- `discovery_limit: 50`
- `venues: [cone, ctwo, opt, exo]`
- `excluded_index_roots`
- `min_volume: 5000`
- `surge_volume: 10000`
- `zero_dte_surge_volume: 20000`
- `rank_jump: 3`
- `cooldown_minutes: 60`
- `max_alerts_per_day: 4`
- `intraday_expiry_minutes: 45`
- `min_venue_coverage: 1.0`

配置只允许 `indicative` 作为当前生产默认值；未来 OPRA 开通后显式切换，不做 403 静默降级。

## 11. 测试与验收

必须覆盖：

- 四市场解析、相同合约聚合、标准 OCC symbol、指数/调整合约过滤、分页/缺字段、TLS 重试、4xx 不重试；
- Alpaca 100 合约分批、Indicative 解析、OI/乘数/Greeks 缺失、OPRA 403 不降级；
- Top10 稳定排序、增量、排名跃升、0DTE 阈值、冷启动、冷却和每日上限；
- SQLite 原子保存、时间槽幂等、outbox 失败重试、过期取消、账户更新不影响期权 outbox；
- 飞书卡有两个 Top10、无表格、覆盖/延迟/风险说明齐全；
- Scheduler 开关、ET 时区、交易日门控、JobHealth；
- 全量 pytest、mypy、真实 Cboe 只读抓取、真实 Alpaca Indicative 补全、隔离 SQLite E2E 和测试标记飞书发送。

## 12. 后续阶段

收集至少 20 个交易日后，再加入同一合约结构在同一盘中时刻的历史分位、IV 变化和 5 分钟季节性基线。只有接入可用的完整 OPRA/专业期权流后，才增加逐笔 premium、bid/ask side 近似、complex order 过滤和低延迟提醒。即使升级，也不声称能可靠识别开仓/平仓或最终投资方向。
