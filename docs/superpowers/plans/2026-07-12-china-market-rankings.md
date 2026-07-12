# China Market Rankings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不污染现有美股信号与 USD 执行计划的前提下，新增沪深北 A 股、场内 ETF/LOF 和场外公募基金的点时榜单、回测、持久化、飞书紧凑推送与机器人查询。

**Architecture:** 在同一服务内增加独立 China vertical。中国市场使用自己的 canonical ID、Asia/Shanghai 日历、`china.duckdb`、主/备数据源、数据质量门、三类排名器以及 `china_ranking_*` ledger/outbox；只复用 Engine 注入、scheduler、Notifier、飞书传输、JobRuntime/Heartbeat 和备份框架。首版 `shadow_only=true`，只观察，不生成数量或订单。

**Tech Stack:** 项目当前生产 Python（保持 `requires-python >=3.11`，本功能不升级解释器）、Pydantic 2、httpx、pandas、DuckDB、SQLite、APScheduler、pytest、mypy、Tushare Pro HTTP API、经能力预检的 AKShare 整批回退、飞书交互卡片。

**Design:** `docs/superpowers/specs/2026-07-12-china-market-rankings-design.md`

---

## File map

新增领域与基础设施：

- `src/quant_signal/china_rankings.py`：身份、榜单、来源、可交易性模型和序列化。
- `src/quant_signal/china_calendar.py`：中国交易日历和目标行情日。
- `src/quant_signal/china_universe.py`：点时证券池、资格过滤和类别聚类。
- `src/quant_signal/china_scanner.py`：A 股纯函数排名器。
- `src/quant_signal/china_exchange_funds.py`：ETF/LOF 分类、去重与排名。
- `src/quant_signal/china_public_funds.py`：公募份额归并、NAV 可见性与分类排名。
- `src/quant_signal/china_dataqa.py`：覆盖率、日期、单位与跨源校验。
- `src/quant_signal/china_performance.py`：独立中国榜单前向绩效。
- `src/quant_signal/china_runtime.py`：聚合 China store/source/calendar/pipelines，供 Engine 薄代理调用。
- `src/quant_signal/pipelines/china_backfill.py`：历史回填、断点恢复与覆盖报告。
- `src/quant_signal/datafeed/china_base.py`：中国数据源协议与错误类型。
- `src/quant_signal/datafeed/tushare_china.py`：Tushare 主源。
- `src/quant_signal/datafeed/akshare_china.py`：按 capability matrix 启用的 AKShare 整批回退。
- `src/quant_signal/datafeed/china_store.py`：独立 DuckDB 点时存储。
- `src/quant_signal/notifier/china_cards.py`：移动端紧凑榜单和周报卡。
- `src/quant_signal/pipelines/china_ingest.py`：交易所/基金增量采集。
- `src/quant_signal/pipelines/china_rankings.py`：三类榜单构建、持久化和投递。
- `src/quant_signal/pipelines/china_performance.py`：绩效周报。
- `research/backtest_china_rankings.py`：walk-forward 与候选规则比较。

修改集成点：

- `src/quant_signal/config.py`、`config/settings.yaml`：独立 China 配置和环境凭证。
- `src/quant_signal/logging_setup.py`：Tushare token 键名和值脱敏。
- `src/quant_signal/ledger.py`：不可变 revision 榜单表和 durable outbox。
- `src/quant_signal/engine.py`、`src/quant_signal/main.py`：可选 China runtime 注入和薄 facade。
- `src/quant_signal/scheduler.py`：Asia/Shanghai 调度、SLA 和重试投递。
- `src/quant_signal/feishu_bot.py`：只读中国榜单命令。
- `src/quant_signal/backup.py`、`deploy/status.ps1`、`deploy/README.md`：备份、状态和恢复。
- `pyproject.toml`、`uv.lock`：AKShare 依赖与类型检查配置。

测试文件与任务一一对应，公共 fixture 放在 `tests/china_fixtures.py`。

---

### Task 1: 预检、领域模型与配置边界

**Files:**

- Create: `src/quant_signal/china_rankings.py`
- Create: `tests/test_china_rankings.py`
- Create: `tests/china_fixtures.py`
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `tests/test_config.py`
- Modify: `src/quant_signal/logging_setup.py`
- Create: `tests/test_logging_setup.py`

- [ ] **Step 1: 固化改动前基线**

Run:

```powershell
uv run pytest -q
uv run mypy src/
uv lock --check
```

Expected: 全部通过；将测试数和当前 commit 写入任务记录。

- [ ] **Step 2: 先写 RED 领域与配置测试**

覆盖：

- `XSHE:STOCK:000001`、`XSHG:STOCK:600000`、`XSHG:ETF:510300`、`XBSE:STOCK:430047`、`CNMF:FUND:000001` 不碰撞；份额类别是字段，不虚构 ID 后缀；
- 三类 `RankingKind` 不能混入同一 snapshot；
- 公募行只接受 NAV，不接受 `trade_price/stop/qty`；
- rank 在每个类别内连续且 tie-break 使用 canonical ID；
- `ChinaRankingsSettings` 默认关闭，覆盖率和 TopN 越界拒绝；
- `TUSHARE_TOKEN` 只来自环境，`model_dump()` 和错误信息不包含 token；
- structlog 事件和异常里的 `TUSHARE_TOKEN/token/tushare_token` 键及实际 token 值均被脱敏；
- 中国配置不扩展 `settings.universe` 和 `settings.tickers`；
- 关闭中国功能时 AKShare 未安装也不影响加载美股配置。

- [ ] **Step 3: 实现最小领域与设置**

定义 `RankingKind`、`ChinaBoard`、`InstrumentType`、`SourceTier`、`Tradability`、`ChinaInstrument`、`ChinaRankingRow`、`ChinaRankingSnapshot` 和 JSON helpers。canonical ID 固定为 `市场:资产类型:真实代码`。新增 `ChinaRankingsSettings`，包含功能开关、三类覆盖率、`stored_top_n=10`、`push_top_n=3`、历史/流动性门、源、存储路径、shadow 和调度设置。

Tushare 使用现有 `httpx`，不引入 SDK。AKShare 依赖推迟到 Task 5 的 capability 合同；关闭中国功能时不得 import。扩展日志处理器，按敏感键名和运行时 token 值同时脱敏。

- [ ] **Step 4: 验证并提交**

Run:

```powershell
uv run pytest tests/test_china_rankings.py tests/test_config.py tests/test_logging_setup.py -q
uv run mypy src/quant_signal/china_rankings.py src/quant_signal/config.py src/quant_signal/logging_setup.py
uv lock --check
```

Commit: `feat: define China ranking domain and settings`

### Task 2: 中国交易日历

**Files:**

- Create: `src/quant_signal/china_calendar.py`
- Create: `tests/test_china_calendar.py`
- Create: `tests/fixtures/china_trade_calendar_2026.json`
- Create: `config/china_calendar_overrides.yaml`

- [ ] **Step 1: 写 RED 日历测试**

覆盖 provider 缓存、年度官方节假日 fixture 与人工 override 合并后的 `trade_cal`，而非 weekday 推算；测试节假日、fixture 冲突、上一/下一 session、18:00 前后的最新完整交易日、沪深北共同开市日、naive datetime 拒绝和时区转换。

- [ ] **Step 2: 实现 `ChinaTradingCalendar`**

提供 `is_session()`、`previous_session()`、`next_session()`、`latest_complete_session()`；输入由 Tushare `trade_cal`、版本化的交易所年度节假日 fixture 和人工维护的特殊休市/开市 override 合并校验。fixture 在每年/发布升级时按上交所、深交所和北交所公告复核，运行时不抓网页。所有时间显式为 `Asia/Shanghai`，不得导入现有 NYSE `calendar.py` 的交易判断。缓存不存在/过期或 fixture 冲突时显式返回 unavailable，不能以周一至周五代替。

- [ ] **Step 3: 验证并提交**

Run: `uv run pytest tests/test_china_calendar.py -q`  
Run: `uv run mypy src/quant_signal/china_calendar.py`  
Commit: `feat: add China exchange calendar`

### Task 3: 独立点时行情库

**Files:**

- Create: `src/quant_signal/datafeed/china_store.py`
- Create: `tests/test_china_store.py`

- [ ] **Step 1: 写 RED 存储测试**

覆盖原始/调整价格与人民币成交额 round-trip、同代码跨市场不碰撞、同日 upsert、证券元数据有效期、`ann_date/available_at` 可见性、并发串行写、显式列插入可承受新增列。

- [ ] **Step 2: 创建 `china.duckdb` schema**

表：

- `china_trade_calendar`、`china_instrument_snapshots`、`china_universe_snapshots`；
- `china_exchange_bars`、`china_industry_status_history`；
- `china_public_fund_profiles`、`china_public_fund_nav`、`china_product_metadata`；
- `china_market_rule_versions`、`china_benchmark_bars`；
- `china_source_snapshots`、`china_source_capabilities`、`china_backfill_checkpoints`。

产品表必须能保存跟踪指数、类别、明确份额关系、规模、申赎状态、结算、交易单位、费用和有效期。所有单位在列名/模型中明确；所有 INSERT 列出列名，不使用 `SELECT *`。

- [ ] **Step 3: 实现点时查询**

实现截至 `as_of/available_at` 的读取、批量 upsert、source snapshot 哈希、覆盖率计数和事务回滚。因子读取调整价格，交易性读取原始价格/昨收/上下限。实现 `backup_to()`：在 store 锁和一致性读事务内使用 DuckDB 原生 export/copy 创建目的库并回读校验，禁止直接复制开放中的文件。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_china_store.py -q`  
Run: `uv run mypy src/quant_signal/datafeed/china_store.py`  
Commit: `feat: persist point-in-time China market data`

### Task 4: Tushare 主源

**Files:**

- Create: `src/quant_signal/datafeed/china_base.py`
- Create: `src/quant_signal/datafeed/tushare_china.py`
- Create: `tests/test_tushare_china.py`

- [ ] **Step 1: 写 RED provider 合同测试**

覆盖分页去重、字段名映射、交易日历、`stock_basic + daily + adj_factor + daily_basic + suspend_d + stk_limit` 合并、`fund_basic + fund_daily + fund_nav`、量/额单位归一化、NAV 公告日期、限频/权限错误、TLS timeout/429/5xx 有界重试、token 永不出现在错误和日志。另测 capability probe：历史行业、历史风险警示、份额关系、完整费率或公告时间缺失时必须明确标为 unsupported。

- [ ] **Step 2: 实现协议与 HTTP client**

`ChinaDataSource` 提供 `probe_capabilities`、`fetch_trade_calendar`、`fetch_instruments`、`fetch_exchange_snapshot`、`fetch_public_fund_snapshot`。Tushare client 使用独立 connect/read/write/pool timeout、指数退避和 deadline；非重试型 4xx 立即失败。

- [ ] **Step 3: 实现完整快照组装**

每个资产类别一次请求得到同一 `as_of`、同一 provider 的完整快照。供应商单位在 adapter 边界归一化为股/份、人民币元和 Decimal NAV，原字段名记录在 fixture 合同测试中。提供 `bootstrap_calendar(previous_year, next_year)` 与增量 refresh；历史行业/ST/主份额/费率只在 capability 通过的接口上抓取，禁止用当前 `stock_basic/fund_basic` 回填历史。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_tushare_china.py -q`  
Run: `uv run mypy src/quant_signal/datafeed/china_base.py src/quant_signal/datafeed/tushare_china.py`  
Commit: `feat: add Tushare China market source`

### Task 5: AKShare 整批回退与数据质量门

**Files:**

- Create: `src/quant_signal/datafeed/akshare_china.py`
- Create: `src/quant_signal/china_dataqa.py`
- Create: `tests/test_akshare_china.py`
- Create: `tests/test_china_dataqa.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: 写 RED 路由和质量测试**

覆盖日常主源成功不触发备源；主源 partial 全批丢弃后，仅当 AKShare capability 对该榜单完整时才全批重跑；禁止逐票混源；日期/单位不一致阻断；A 股 98%/场内基金 97%/公募类别 95% 全池覆盖门；列名漂移失败而非位置猜测。独立 `source_audit` 模式才抽样调用两源，并在收盘价中位误差 >0.1% 或成交额 >5% 时把对应 capability 标为不健康。

- [ ] **Step 2: 实现 `AKShareChinaSource` 和 `ChinaSourceRouter`**

先用当前已核实发布版 `akshare==1.18.64` 跑固定响应/真实只读 capability 合同，通过后以精确版本加入 `pyproject.toml` 和 `uv.lock`；未来升级必须重新跑这些合同，不使用宽泛 `<2` 范围。AKShare 只在配置开启且主源不可用时延迟导入。先保存按资产类别/接口/字段的 capability matrix；缺限价、公告时间、点时费用等任一必需字段时，该榜单不允许 AKShare 回退。一次 run 只能有一个 `source_snapshot_id`；回退快照记录 `FALLBACK`，不能与主源 DataFrame concat。

- [ ] **Step 3: 实现 fail-closed QA**

输出结构化诊断：预期/实际证券数、有效数、日期、单位、缺失接口、capability、定期审计的跨源误差、源层级。失败快照可以落审计库，但不能进入排名或 outbox。新增 `run_source_audit()`，由每周任务调用，不嵌入每日主源成功路径。只有两个聚合源不一致时记录 `CROSS_SOURCE_DISAGREEMENT` 并阻断相关榜单；除非交易所第三源或人工复核确认，不单方面把某一 provider 判错。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_akshare_china.py tests/test_china_dataqa.py -q`  
Run: `uv run mypy src/quant_signal/datafeed/akshare_china.py src/quant_signal/china_dataqa.py`  
Run: `uv lock --check`
Commit: `feat: add fail-closed China data fallback`

### Task 6: 不可变榜单台账与 durable outbox

**Files:**

- Modify: `src/quant_signal/ledger.py`
- Modify: `tests/test_ledger.py`
- Create: `tests/test_china_ranking_ledger.py`

- [ ] **Step 1: 写 RED schema/事务测试**

覆盖旧生产 schema version 7 无损升级并提升版本；三类榜单同日不覆盖；相同 input hash 幂等；修订输入创建新 revision 并 supersede 旧 run；失败空榜阻止静默读取昨日榜；多个 run 原子组成一个 delivery bundle；outbox 通过 `bundle_id` 引用综合卡；outbox 重试/过期；prune 保留 pending bundle 及其 runs。

- [ ] **Step 2: 只做 additive schema 升级**

将 `_SCHEMA_VERSION` 从 7 提升，新增 `china_ranking_runs`、`china_ranking_rows`、`china_ranking_bundles`、`china_ranking_bundle_runs`、`china_ranking_outbox`；run 保存 `source_snapshot_id/input_hash/revision/supersedes_run_id`，outbox 保存 `bundle_id` 而不是单一 `run_id`。不要修改 `scan_candidates`、`signals` 或 `execution_plans`。

- [ ] **Step 3: 实现 ledger API**

实现 `save_china_ranking()`、`china_ranking_for()`、`latest_china_ranking()`、`save_china_ranking_bundle(run_ids, card)`、`due_china_ranking_cards()`、`mark_china_ranking_sent/failed()`、`prune_china_rankings()`。ranking run 单独原子保存；08:25 delivery 再把多个当前有效 revision 原子组成 bundle/outbox。机器人可读取带 stale/failed 状态的诊断。

- [ ] **Step 4: 用生产 DB 副本验证并提交**

Run:

```powershell
uv run pytest tests/test_ledger.py tests/test_china_ranking_ledger.py -q
uv run mypy src/quant_signal/ledger.py
```

复制 `data/signals.db` 到临时目录，打开后确认旧 signals、账户、期权和执行计划数量不变。  
Commit: `feat: persist versioned China ranking snapshots`

### Task 7: A 股资格过滤与 baseline 排名

**Files:**

- Create: `src/quant_signal/china_universe.py`
- Create: `src/quant_signal/china_scanner.py`
- Create: `tests/test_china_universe.py`
- Create: `tests/test_china_a_ranking.py`

- [ ] **Step 1: 写 RED 资格测试**

覆盖沪深北全量入库但北交所独立组；上市和有效历史均至少 130 个预期交易日且窗口覆盖率 ≥98%；排除当时已知的 ST/退市/停牌；近 20 日至少 18 日；20 日成交额中位数、价格和流通市值门；一字涨跌停不可交易；调整价算因子但原始价展示；只读取 `as_of` 前数据。历史状态 capability 缺失的区间必须拒绝点时回测，不能用今日状态回填。

- [ ] **Step 2: 写 RED 排名测试**

覆盖 60 日动量 40%/距 20 日高点 30%/5 日量能 30%、板块/流通市值桶稳健百分位、tie-break、北交所单独 TopN。只有点时行业 capability 可用时才测行业中性和行业最多 2 只；实时行业仅可影响实时榜，不得回填历史。

- [ ] **Step 3: 实现 `cn_a_v1_baseline`**

纯函数先过滤再评分，返回完整 factor map、淘汰原因、使用的中性化维度和 tradability。`cn_a_v2_candidate` 只定义配置/模型，不接生产输出。provider readiness 结果决定是否启用历史行业/ST 分支；不支持时使用板块/市值桶或缩短合格回测区间。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_china_universe.py tests/test_china_a_ranking.py -q`  
Run: `uv run mypy src/quant_signal/china_universe.py src/quant_signal/china_scanner.py`  
Commit: `feat: rank eligible A-share candidates`

### Task 8: A 股流水线与移动端卡片

**Files:**

- Create: `src/quant_signal/pipelines/china_ingest.py`
- Create: `src/quant_signal/pipelines/china_rankings.py`
- Create: `src/quant_signal/notifier/china_cards.py`
- Create: `tests/test_china_ingest_pipeline.py`
- Create: `tests/test_china_a_pipeline.py`
- Create: `tests/test_china_cards.py`

- [ ] **Step 1: 写 RED pipeline/outbox 测试**

覆盖 ingest → QA → store → rank → ledger run 顺序、预期 session 缺失时 fail closed、相同 run 幂等、失败 run 不可进入 bundle。另测 delivery 从 A 股/场内基金/周一公募多个 run 构造单一 bundle/outbox，投递失败可重试，bundle 中任一 run 被 supersede 后不再新投递旧组合。

- [ ] **Step 2: 写 RED 移动卡测试**

主动卡只展示沪深 Top3、北交所独立摘要和数据状态；不使用宽 Markdown 表；含 `as_of/source/coverage/rule_version/观察模式`；价格为 `¥` 两位小数；不得出现建议数量、融资或自动下单语句；Top4–10 只通过查询返回。

- [ ] **Step 3: 实现 pipeline 和卡片**

`shadow_only=true` 时保存 run/rows/绩效输入但不生成主动推送 bundle/outbox；非 shadow 时也只有 08:25 delivery job 可以创建综合 bundle。显式测试关闭 flag 后美股路径不调用任何中国 provider。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_china_ingest_pipeline.py tests/test_china_a_pipeline.py tests/test_china_cards.py -q`  
Run: `uv run mypy src/quant_signal/pipelines/china_ingest.py src/quant_signal/pipelines/china_rankings.py src/quant_signal/notifier/china_cards.py`  
Commit: `feat: build mobile A-share rankings in shadow mode`

### Task 9: 场内 ETF/LOF 分类榜

**Files:**

- Create: `src/quant_signal/china_exchange_funds.py`
- Create: `tests/test_china_exchange_funds.py`
- Create: `tests/test_china_exchange_fund_pipeline.py`
- Modify: `src/quant_signal/datafeed/tushare_china.py`
- Modify: `src/quant_signal/datafeed/akshare_china.py`
- Modify: `src/quant_signal/datafeed/china_store.py`
- Modify: `src/quant_signal/pipelines/china_rankings.py`
- Modify: `src/quant_signal/notifier/china_cards.py`

- [ ] **Step 1: 写 RED 分类、去重和门槛测试**

覆盖宽基/行业主题/Smart Beta/债券/黄金商品/跨境 QDII/指数 LOF/主动 LOF 不混排；同跟踪指数只保留最佳代表；ETF/LOF 使用不同规模、成交额和折溢价门；T+0/T+1 只能来自点时产品元数据，名称不得推断；整批 run 覆盖率 ≥97%，单产品排名窗口历史完整率 ≥90%。

- [ ] **Step 2: 写 RED 评分/展示测试**

确定性 baseline：

- 指数 ETF/LOF：60 日类别相对动量 25%、20 日类别相对动量 15%、流动性 15%、跟踪质量 15%、低波动 10%、低最大回撤 10%、低溢折价波动 10%；
- 主动 LOF：60 日类别相对动量 30%、20 日类别相对动量 20%、流动性 15%、低波动 10%、低最大回撤 15%、低溢折价波动 10%。

所有因子在同类别做截尾百分位；必需点时字段缺失时不进入综合名次，不用零值或重归一化补分。场内基金价格三位小数，完整 Top10 按需读取。

- [ ] **Step 3: 实现并接入同一紧凑总览**

ETF 默认上市 ≥120 日、规模 ≥2 亿元、20 日成交额中位数 ≥1000 万元；LOF 默认规模 ≥1 亿元、成交额 ≥300 万元、折溢价绝对值 ≤3%。把产品元数据、跟踪指数和结算规则按有效期写入 `ChinaMarketStore`；上述确定性权重保存在 versioned rule，并先进入 shadow 对比。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_china_exchange_funds.py tests/test_china_exchange_fund_pipeline.py tests/test_china_cards.py -q`  
Run: `uv run mypy src/quant_signal/china_exchange_funds.py src/quant_signal/pipelines/china_rankings.py`  
Commit: `feat: rank listed China funds by category`

### Task 10: 场外公募基金周榜

**Files:**

- Create: `src/quant_signal/china_public_funds.py`
- Create: `tests/test_china_public_funds.py`
- Create: `tests/test_china_public_fund_pipeline.py`
- Modify: `src/quant_signal/datafeed/tushare_china.py`
- Modify: `src/quant_signal/datafeed/akshare_china.py`
- Modify: `src/quant_signal/datafeed/china_store.py`
- Modify: `src/quant_signal/pipelines/china_rankings.py`
- Modify: `src/quant_signal/notifier/china_cards.py`

- [ ] **Step 1: 写 RED 点时 NAV 与份额测试**

覆盖只读取 `ann_date/available_at` 已公开 NAV、境内与 QDII 不同延迟门、成立不足 3 年不进入综合名次（2–3 年仅短历史观察）、暂停申购排除。A/C/I 只有 capability 提供显式 `master_fund_id` 与完整点时费率表时才合并并按预计 365 日费用选代表；名称相似不猜母基金，能力缺失时真实份额代码保持独立并标记关系未知。

- [ ] **Step 2: 写 RED 分类排名和卡片测试**

被动指数、增强指数、主动权益、混合、债券、QDII 独立比较；使用当时官方基准或类别基准。确定性 `cn_public_fund_v1` 权重为：1 年相对收益 20%、3 年年化相对收益 20%、3 年 Sharpe 15%、低 3 年最大回撤 15%、低 3 年下行波动 10%、36 月月度胜率 10%、36 月收益一致性 10%。任一必需因子缺失不进入综合名次。卡片含 NAV 日期/申购状态/确认周期/可得费用且不含买入价、止损或数量。货币、FOF、REIT 首版不排名。

- [ ] **Step 3: 实现周榜**

将 NAV、公告时间、基准、明确份额关系、费率与申赎状态按有效期写入 `ChinaMarketStore`。经理、规模、费率和申赎状态在缺少点时历史前只作硬门/标签。周一周榜替代同日普通日榜，不增加第二张主动卡。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_china_public_funds.py tests/test_china_public_fund_pipeline.py tests/test_china_cards.py -q`  
Run: `uv run mypy src/quant_signal/china_public_funds.py src/quant_signal/pipelines/china_rankings.py`  
Commit: `feat: rank open-end public funds without price advice`

### Task 11: Engine 装配、独立调度与健康 SLA

**Files:**

- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/main.py`
- Modify: `src/quant_signal/scheduler.py`
- Create: `src/quant_signal/china_runtime.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_scheduler.py`
- Create: `tests/test_china_runtime.py`

- [ ] **Step 1: 写 RED 装配隔离测试**

覆盖 disabled 时不构造 provider/store；中国源不替换 `engine.source`；中国任务失败不阻断美股 jobs；Engine 的 China 方法全部只代理到独立 `ChinaRankingRuntime`。

- [ ] **Step 2: 写 RED 调度测试**

覆盖启用服务时 calendar bootstrap、周日 09:00 refresh、所有 China job 显式 Asia/Shanghai、Tushare 日历 + 年度官方节假日 fixture + 人工 override 门控、`max_instances=1/coalesce=True`、独立锁、18:30 ingest、19:15 exchange rank、22:30 public fund ingest、07:40 catch-up、08:25 单 bundle 投递、每周 source audit、周六绩效；不得占用现有 08:00/08:05/08:10 时点。日历缺失/过期或 fixture 冲突时全部 rank/push fail closed。

- [ ] **Step 3: 扩展 `JobRuntime` SLA**

`ChinaRankingRuntime` 提供 `calendar_bootstrap/refresh`、`exchange_ingest`、`exchange_rank`、`public_fund_ingest/catchup`、`delivery`、`source_audit` 和 `performance`；Engine 为这些 job 提供一一对应的薄代理，scheduler 不直接持有 provider/store。新增可配置 `CriticalJobSpec`，按任务时区/日历检查应完成时间、最长运行、最大陈旧度；19:15 rank 必须引用当天成功的 `ingest_run_id/source_snapshot_id`，不能只靠墙钟。Heartbeat 汇总 China coverage、fallback、token/权限和 outbox backlog，并保持现有两小时静默策略。

- [ ] **Step 4: 实现生产装配和验证**

装配时 provider readiness 和 calendar bootstrap 失败只把中国功能置 unavailable，现有美股 scheduler 仍必须启动。
Run: `uv run pytest tests/test_engine.py tests/test_scheduler.py tests/test_china_runtime.py -q`  
Run: `uv run mypy src/quant_signal/china_runtime.py src/quant_signal/engine.py src/quant_signal/main.py src/quant_signal/scheduler.py`
Commit: `feat: schedule China rankings independently`

### Task 12: 飞书只读查询、备份与部署状态

**Files:**

- Modify: `src/quant_signal/feishu_bot.py`
- Modify: `tests/test_feishu_bot.py`
- Modify: `src/quant_signal/backup.py`
- Modify: `tests/test_backup.py`
- Modify: `deploy/status.ps1`
- Modify: `deploy/README.md`
- Modify: `tests/test_deploy.py`

- [ ] **Step 1: 写 RED 机器人测试**

覆盖 `A股`、`A股 半导体`、`北交所`、`ETF`、`基金 债券`、`榜单 600519`、`国内数据状态`；模糊六位代码必须返回多个带市场 ID 的选项；查询只读 ledger，不触发 provider；现有群隐私规则不变。

- [ ] **Step 2: 实现紧凑查询输出**

Top10 使用窄行文本/卡片而非 Markdown 表格，固定显示行情/NAV 日、来源、覆盖率、规则与 shadow/降级标签。旧榜仅在明确显示 `STALE` 时允许查询，不能冒充今日榜。

- [ ] **Step 3: 写 RED 备份/状态测试并实现**

备份纳入 `china.duckdb` 和已提升 schema version、带中国表的 `signals.db`。SQLite 使用现有 backup API；DuckDB 调用 Task 3 的 `ChinaMarketStore.backup_to()`，在 store 锁和一致性读事务内使用原生 export/copy，并回读行数/哈希，禁止直接复制开放文件。恢复文档校验 source snapshot/run/bundle/outbox；`status.ps1` 显示最近中国 calendar/ingest/rank/push、覆盖率和 source tier，且不泄露 token。

- [ ] **Step 4: 验证并提交**

Run: `uv run pytest tests/test_feishu_bot.py tests/test_backup.py tests/test_deploy.py -q`  
Run: `uv run mypy src/quant_signal/feishu_bot.py src/quant_signal/backup.py`  
Commit: `feat: expose and operate China rankings safely`

### Task 13: 点时回测、前向绩效与发布门

**Files:**

- Create: `src/quant_signal/china_performance.py`
- Create: `src/quant_signal/pipelines/china_backfill.py`
- Create: `src/quant_signal/pipelines/china_performance.py`
- Create: `tests/test_china_backfill.py`
- Create: `tests/test_china_performance.py`
- Create: `research/backtest_china_rankings.py`
- Modify: `src/quant_signal/datafeed/china_store.py`
- Modify: `src/quant_signal/datafeed/tushare_china.py`
- Modify: `src/quant_signal/scheduler.py`
- Modify: `README.md`
- Modify: `deploy/README.md`

- [ ] **Step 1: 写 RED 历史回填测试**

覆盖按年份/接口分块、API 限频、重试 deadline、checkpoint 断点恢复、相同输入幂等、权限缺口、年度覆盖报告和数据校验。缺历史行业/ST/费率 capability 时，回填报告必须标出不可点时回测区间；禁止当前元数据回填历史。

- [ ] **Step 2: 实现 backfill runner**

`china_backfill.py` 依次回填交易日历、点时证券池/状态、原始行情、调整因子、产品元数据、NAV 和类别基准，写 `china_backfill_checkpoints`。2018 年是目标起点；如果权限/覆盖不够，报告给出首个合格日期，回测从该日开始而非伪造全历史。

- [ ] **Step 3: 写 RED 成交与前视测试**

覆盖 A 股/ETF D 日榜、D+1 可交易开盘加保守滑点；有分钟数据时使用 09:35–09:45 VWAP；停牌/涨跌停锁死记 `UNEXECUTABLE`；历史交易单位、T+1、费用和印花税版本化；公募按截止时点后的下一可见 NAV 和实际费用确认。

- [ ] **Step 4: 实现不可复用美股假设的绩效模块**

为沪深 300/中证全指、对应 ETF 类别基准和公募官方基准建立 point-in-time benchmark mapping。任何缺少成交窗口或公告时间的样本标记不可评分，不用收盘价或后来 NAV 代替。

- [ ] **Step 5: 实现 walk-forward 与 candidate shadow**

从 coverage 合格的首日开始滚动训练/验证，冻结最后 12–18 个月；比较 `cn_a_v1_baseline` 与 `cn_a_v2_candidate`；输出 Rank IC、Top 组合相对收益、换手、成本压力、最大回撤、不可成交率和分板块/类别稳定性。

- [ ] **Step 6: 实现分资产晋级判定（不等待未来 60 日完成开发）**

代码完成后保持 `shadow_only=true`，由后续监控积累观测周期。晋级条件分别为：

- A 股：20 交易日前瞻 Rank IC >0.02，Top 组合相对中证全指/对应板块基准扣费超额为正；
- 场内基金：各类别 20/60 交易日前瞻 Rank IC >0.02，Top 组合相对类别基准扣费超额为正；
- 场外公募：6 个月前瞻类别相对 Rank IC >0.02，Top 组类别超额和同类 Top 分位命中率优于基线；
- 每类至少 4 个样本外窗口中的 3 个方向一致、双倍成本仍为正、最大回撤相对各自基准恶化不超过 5 个百分点；
- 各自至少 60 个观测周期；provider 故障并 fail closed 可以发生，但不得有任何日期、单位、覆盖率或混源不合格数据被发布。

- [ ] **Step 7: 全量开发验收**

Run:

```powershell
uv run pytest tests/test_china_backfill.py tests/test_china_performance.py tests/test_scheduler.py tests/test_deploy.py -q
uv run pytest -q
uv run mypy src/
uv lock --check
```

另外完成：5000+ 股票/大规模基金 fixture 负载测试、生产 DB 副本升级、DuckDB 一致性备份恢复、飞书 sandbox 卡片和关闭 feature flag 回滚演练。真实源 smoke 是条件式验收：有 `TUSHARE_TOKEN`/所需积分且 capability 支持时必须通过；仅对 capability 完整的资产类别执行 AKShare 整批回退 smoke。缺凭证、积分或关键接口时不能假通过，对应中国榜单保持 `UNAVAILABLE`，但不阻断离线代码开发验收和现有美股服务。

Commit: `feat: validate China rankings in shadow mode`

---

## Execution waves

1. Wave 1（顺序）：Task 1–6，先建立正确身份、数据与审计基础；
2. Wave 2：Task 7–8 交付 A 股 shadow；
3. Wave 3（顺序）：Task 9 场内基金完成后再做 Task 10 公募；二者修改相同 provider/store/card 文件，禁止并行；
4. Wave 4（顺序）：Task 11–13 生产装配、查询、备份、回测与发布门。

Task 8、9、10、13 完成后必须额外运行全套 pytest 与 mypy。任何真实源 smoke 都是只读；首版不要求或授权券商交易权限。
