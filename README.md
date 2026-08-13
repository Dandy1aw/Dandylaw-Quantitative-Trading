# 美股/港股/韩股半自动量化信号系统 (quant-signal)

只产生信号与 **PAPER 执行建议**并推送飞书，**不自动下单**——所有交易由用户在券商 App
手动决策执行。执行建议基于只读 Alpaca paper 账户计算"何时、什么限价、买多少股"，
由确定性规则生成，AI 只解释不改写。

## 目录

- [非目标（明确不做的事）](#非目标明确不做的事)
- [架构](#架构)
- [快速开始](#快速开始)
- [凭证配置](#凭证配置)
- [核心概念](#核心概念)
- [策略](#策略)
- [指数发现池与 PAPER 执行建议](#指数发现池与-paper-执行建议)
- [美股期权异动提醒（Cboe 四市场）](#美股期权异动提醒cboe-四市场)
- [持仓期权情报（期权市场当信息源）](#持仓期权情报期权市场当信息源)
- [飞书机器人交互（可选）](#飞书机器人交互可选)
- [调度：一天跑哪些任务](#调度一天跑哪些任务)
- [配置参考（settings.yaml）](#配置参考settingsyaml)
- [命令行工具](#命令行工具)
- [数据源与国际标的](#数据源与国际标的)
- [飞书卡片格式](#飞书卡片格式)
- [回测与研究](#回测与研究)
- [开发与测试](#开发与测试)
- [已知限制](#已知限制)
- [风险与合规提示](#风险与合规提示)
- [目录结构](#目录结构)

## 非目标（明确不做的事）

- 不自动下单：账户适配器只调 GET 接口，代码里不存在下单/撤单能力
- 不做 tick/秒级高频，最小颗粒度是 5 分钟 bar
- 不维护港股/韩股专属交易日历（用固定 UTC 时间窗口兜底，见"已知限制"）
- 不做 Web UI，所有输出走飞书（或本地终端 Console 模式）
- 不让 AI 生成或修改任何价格、金额和股数（prompt 硬约束 + 结构化卡片独立于 AI）
- 不推荐任何期权交易（备兑/保护性认沽/价差都不做）——期权按 100 股一张合约交易，
  当前账户规模买不起任何一张标准合约对应的正股，卖裸期权更是禁区；期权市场
  只作为**信息源**服务于股票决策（见"持仓期权情报"）
- 不用当前指数成分宣称历史回测无偏（幸存者偏差显式标注）

## 架构

```
┌──────────────┐    ┌───────────────┐    ┌───────────────┐
│  data-feed    │ →  │ signal-engine  │ →  │ notifier       │
│  行情采集      │    │ 策略调度+信号   │    │ 飞书卡片+去重   │
│ Alpaca/yfinance│    │ Engine        │    │ Console/飞书    │
└──────┬───────┘    └───────┬───────┘    └───────────────┘
       ↓                    ↓
   duckdb(bars)        sqlite(signals/holdings/plans/events)
       ↑                    ↑
┌──────┴───────┐    ┌───────┴───────┐
│ index-universe│    │ execution      │  只读 paper 账户 → 确定性
│ 纳指100+标普500│    │ 计划+状态机     │  sizing + 状态迁移提醒
└──────────────┘    └───────────────┘
                            ↑
                    ┌───────────────┐
                    │  research      │  离线回测（vectorbt + walk-forward + 回放）
                    └───────────────┘
```

单进程 monorepo，APScheduler 驱动 18 个定时任务（详见下文）。数据源
（yfinance / Alpaca）与通知器（Console / 飞书）都是可切换的抽象：

- 每个标的按 `tickers.<symbol>.currency` 自动派生数据源：非 USD 固定走 yfinance，USD 跟随
  `data_source` 走 Alpaca——美股/ETF 走 Alpaca，港股/韩股等 Alpaca
  不支持的市场固定走 yfinance，两者数据合并进同一个 duckdb
- 推送通道按优先级选择：`feishu_bot.push_receive_id` 配置齐全时走**自建应用**
  （单聊或群）→ 否则 `FEISHU_WEBHOOK`（群自定义机器人）→ 都没有则降级为
  Console 通知器（终端打印 + 写 `logs/signals.jsonl`），不影响开发和测试

## 快速开始

```bash
uv sync --all-extras
uv run python -m quant_signal.ingest --days 730   # 拉取 2 年历史日线入 duckdb
uv run quant-signal                                # 启动调度器（前台运行，Ctrl+C 退出）
```

首次运行前建议先看一遍下面的[配置参考](#配置参考settingsyaml)，把
`tickers`/`watchlist` 改成你自己关心的标的，并跑一次
[`seed_holdings`](#命令行工具) 把真实持仓写进虚拟持仓台账。

## 凭证配置

复制 `config/.env.example` 为 `config/.env`，按下面步骤获取两个凭证后填入。
**这两个凭证都是可选的**——不配置也能跑（数据源自动用 yfinance，通知器自动用
Console），但美股数据质量和飞书推送体验会打折扣。

### 1. Alpaca paper 账户（免费 IEX 行情，美股/ETF 用）

1. 访问 [alpaca.markets](https://alpaca.markets) → 右上角 **Sign Up**，用邮箱注册并验证
2. 登录后进入 Dashboard，左侧导航确认处于 **Paper Trading** 模式（默认即是，无需申请实盘权限）
3. 左侧菜单找到 **API Keys**（或 Dashboard 首页的 "View API Keys"）
4. 点击 **Generate New Key**，会显示一对 `API Key ID` 和 `Secret Key`（Secret 只显示一次，务必立即复制）
5. 把两个值填入 `config/.env`：
   ```
   ALPACA_KEY=你的API Key ID
   ALPACA_SECRET=你的Secret Key
   ```
6. 把 `config/settings.yaml` 里的 `data_source` 改成 `alpaca`（注册表中非 USD
   标的不受这个开关影响，永远走 yfinance）
7. 验证：`uv run python -m quant_signal.ingest --days 30`，duckdb 里应能看到 `source='alpaca'` 的数据
8. **如果你的网络需要代理才能访问 Alpaca**（比如国内网络），确保
   `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 环境变量已设置；若代理是
   SOCKS5，本项目已经在依赖里带了 `httpx[socks]`，无需额外安装

### 2. 飞书自定义机器人 webhook

1. 打开目标飞书群（网页版/客户端/App 均可）→ 右上角设置（齿轮图标）→ **群机器人**
2. 点击 **添加机器人** → 选择 **自定义机器人**
3. 填写机器人名称（如"量化信号"），可选头像，点击 **添加**
4. 复制生成的 **Webhook 地址**（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx`）
5. （建议）在安全设置中启用**关键词**校验，填一个所有卡片标题都会出现的词，比如 `信号`；如果本系统卡片标题不含该词会导致发送被拒，可根据 `notifier/cards.py` 里的标题格式调整关键词或改用"签名校验"方式
6. 把 webhook 填入 `config/.env`：
   ```
   FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx
   ```
7. 验证：`uv run python -m quant_signal.notifier.feishu --test`，飞书群应收到三张测试卡片（信号/早报/告警），中文和 emoji 显示正常

## 核心概念

- **Signal（信号）**：一个标的的 BUY/SELL 建议，含标的、方向、价格、原因、策略来源、
  时间戳。所有信号无论是否实际推送，都会写入 sqlite 台账（`signals` 表）。
- **虚拟持仓（holdings）**：系统自己维护的一份"当前应该持有哪些标的"记录，不等于
  你券商账户里的真实持仓——两者需要你手动同步（初始化用 `seed_holdings`，见下文）。
  `momentum_rotation` 每次跑完都会把虚拟持仓覆盖为当次计算出的 top-N 目标。
- **参考价 vs 现价**：早报卡片里"参考价"是策略计算动量用的收盘价（可能是隔夜价），
  "现价"是推送那一刻额外抓的最新 5 分钟价，两者分开展示，互不影响计算逻辑。
- **去重（dedup）**：同一 `(标的, 方向, 策略)` 在 `notify.dedup_hours`（默认 4 小时）
  内只推送一次，按**实际推送的墙钟时间**（不是信号自身的时间戳）判断，跨进程重启后
  依然有效。只有通知发送成功才记录 `pushed=true`；失败会在下轮重试。盘前、突破、偏离
  三条通道各有独立小时配额，互不挤占。

## 策略

### momentum_rotation（动量轮动，日级）

- 逻辑：对 `universe` 里每个标的算 `lookback_days`（默认 60）日动量，剔除
  20 日均成交额（换算成美元后）低于 `min_dollar_volume` 的标的，取动量最高的
  `top_n`（默认 3）个作为目标持仓
- 每个标的按**自己的有效数据**计算动量，不依赖全市场统一的"最新一行"——这样
  美股假期休市但港股/韩股照常交易时，美股标的不会被错误地整体排除
- 已持仓但跌出 top-N 的标的会产出 SELL 信号（"轮动调出"），新进入 top-N 的产出
  BUY 信号
- 非美元计价标的（`international_tickers` 里的）用**实时汇率**换算流动性门槛，
  汇率来自 yfinance 的 `{币种}=X` 报价，每次跑动量轮动时刷新一次；取不到汇率时
  该币种标的本轮直接剔除，不按 1:1 静默误算
- **按市场分组排名**（`momentum_group_top_n` 配置）：`international_tickers`
  里的币种（如 HKD/KRW）各自独立排名、独立取名额，不跟默认组（美股/其余
  标的，用 `top_n`）竞争——否则港股/韩股这类杠杆/热门标的动量经常极端
  （实测 +386.7%），会把美股标的全部挤出 top-N。默认组的名额始终是完整
  的 `top_n` 个，不会被其他组占用
- 每次盘前流程额外发送“动量全池榜单”：Top5 买入候选与 Bottom3 卖出警示；持仓、
  趋势 FLAT、历史不足会显式标注。榜单只用于展示，不改变已回测的组内选股逻辑

### breakout_20d（20日突破，盘中5分钟级）

- 只监控 `watchlist`（默认 NVDA/TSLA/AAPL/MSFT/AMD，跟 `universe` 是两个独立列表）
- 当日最新价突破前 20 日最高价（不含当日）且当日累计成交量 > 20 日均量的
  `volume_multiplier`（默认 1.5）倍，产出 BUY 信号
- yfinance 模式下卡片会标注"⚠️ 数据延迟约15分钟，仅供观察"

### rsi_reversion（RSI 均值回归，日级）

- 跟 `universe` 同一批标的、跟着 premarket 一起跑
- RSI(`period`，默认14) < `oversold`（默认30）→ 超卖，产出 BUY；RSI > `overbought`
  （默认70）→ 超买，产出 SELL
- 跟动量轮动是互补视角：动量轮动追强势排名靠前的，RSI 抓的是短期超跌反弹/
  超涨回调，两者经常对同一标的给出不同方向的信号，这是设计上预期的分歧，
  不是 bug

### macd_cross（MACD 金叉/死叉，日级）

- EMA(`fast`/`slow`，默认12/26) 的差值（MACD线）上穿/下穿其 `signal`
  （默认9）周期 EMA（信号线）分别产出 BUY（金叉）/SELL（死叉）
- 看的是绝对趋势拐点，不是相对排名，可能跟动量轮动同时对同一标的产出信号

### bollinger_breakout（布林带突破，日级）

- `period`（默认20）日均值 ± `num_std`（默认2）倍标准差，收盘价突破上轨
  产出 BUY，跌破下轨产出 SELL
- 跟 `breakout_20d` 概念相似但不同：布林带惯例是滚动窗口**含当日**，
  `breakout_20d` 明确排除当日，两者各自遵循自己指标的惯例

以上三个策略跟动量轮动共用同一个 `run_premarket` 流程和早报卡片（多了一列
"策略"标明来源），回测已用 `research/backtest_new_strategies.py` 验证：
BUY 端历史表现尚可（20日胜率52-60%，正收益），**SELL 端（超买/死叉/跌破）
预测下跌的胜率明显偏低（32%-42%）**——测试窗口以牛市为主，趋势压过均值
回归，SELL 信号可靠性存疑，如实记录不代表回测覆盖的将来一定成立。

### price_deviation（持仓偏离监控，盘中5分钟级）

- 监控范围：当前虚拟持仓（= 动量轮动最近一次算出的 top-N 目标）
- 每 5 分钟抓一次最新价，跟当天该标的最近一次动量信号的"参考价"比较，涨跌超过
  `threshold`（默认 ±2%）就推送提醒
- 用独立的 `strategy_id=price_deviation` 去重，跟动量轮动/突破策略互不干扰
- 若某标的当天还没有任何动量信号（比如刚加入 universe），本次检查会跳过它，
  不报错

### UZI-Skill 深度分析信息增强（可选，默认关闭）

- 不是量化策略，是外部工具（`stock-deep-analyzer` 插件，65位"投资大佬"
  规则化评分 + 财务建模 + 杀猪盘检测）的补充参考层，`enrichment.enabled`
  默认 `false`
- 监控范围：当前持仓 ∪ 今日全部策略的 BUY 信号，按 `max_tickers`（默认8）
  截断
- headless 子进程调用（设 `UZI_CLI_ONLY=1` 环境变量），不需要 Claude 会话，
  单只标的实测约 20-60 秒；读取其 `synthesis.json` 提取综合评分/结论/
  风险点
- 若某标的是我们的持仓/BUY 目标，但深度分析给出"看空/谨慎"或评分 <50，
  卡片上会标 ⚠️ 分歧提示——这是有意设计的交叉验证，不是矛盾
- 子进程失败/超时/解析异常一律静默跳过该标的，不影响主流程；需要用户
  本机单独装好 UZI-Skill 及其依赖（akshare/baostock/playwright 等），跟
  quant-signal 自己的 uv 环境完全隔离
- 韩股等 UZI-Skill 覆盖较弱的市场，实测会触发 `timeout_seconds`（默认60秒）
  超时，属于预期的优雅降级，不是 bug

## 指数发现池与 PAPER 执行建议

v0.5 起系统从"价格到点提醒器"升级为只读交易执行建议系统（tag
`v0.5.0-index-execution-paper`），核心链路：

1. **指数发现池**（`index_universe.py`）：每次扫描从官方源拉取纳指100（Nasdaq
   API）与标普500（State Street SPY 每日持仓 xlsx）成分股，去重并集约 520 只。
   硬校验成分数量区间、单次变更 ≤25 只，原子缓存最后一次成功快照；源站挂了用
   快照兜底，快照超过 14 天进入 STALE 状态并停发新计划（fail closed）
2. **有界扫描**（`market_scan` 指数模式）：Alpaca SIP 完整市场成交量日线，增量
   写入 BarStore；覆盖率 <98% 或超 10 分钟 deadline 一律 fail closed。三因子
   截尾百分位打分（60日动量40% / 距20日高30% / 5日量比30%）出 Top20 观察榜 +
   前 5 名带执行价位（买入区/限价/止损/止盈）
3. **只读账户**（`account.py`）：Alpaca paper 账户适配器只调 GET 接口，代码里
   不存在下单能力；凭据不进日志/repr/异常
4. **确定性 sizing**（`execution.py`）：整股数量 =
   `min(目标权重缺口, 风险预算, 可用现金, 单票上限)`，全部中间上限落库可审计；
   止损距离 <2% 或 >20%、价格次序错误、账户/行情过期、当日新仓超额都会 BLOCKED
   并写明原因，绝不静默补算
5. **买入状态机**：`CANDIDATE → ARMED → IN_ENTRY_ZONE → ACTIONABLE`，只有
   09:45–15:45 ET 内一根**完整 5 分钟 bar** 收在买入区内且未破止损才确认
   ACTIONABLE；跌破止损/趋势转平/过期立即失效。同一计划同一事件最多推送一次
   （sqlite `plan_events` 幂等表）
6. **AI 边界**：AI 早报/计划解释只能引用结构化数据，prompt 硬约束禁止改写
   limit/qty/stop/take-profit、缺失字段只能写"不可用"、PAPER 不得描述为实盘；
   AI 失败不影响结构化卡片（生产已验证优雅降级）

一句话：**每天 08:15 ET 一张 PAPER 执行计划卡**（账户/持仓/未成交/候选计划+
阻断原因），**盘中只在状态迁移时提醒**（ACTIONABLE / INVALIDATED / EXPIRED /
STOP_BREACH / TAKE_PROFIT），旧的 ±2% 价格偏离噪音提醒已下线（feature flag
`legacy_price_deviation.enabled` 可回滚）。

## 真实持仓股价异动

`holding_price_alert.enabled` 打开后，系统在美股正常交易时段每分钟检查
最新一份**完整券商截图**里的真实持仓。它同时比较 1/5/15 分钟和当日涨跌，
个股最低门槛为 **2% / 4% / 6% / 10%**，ETF 为
**1.2% / 2.5% / 4% / 6%**，并用近 30 根分钟收益波动率自适应抬高门槛。
末根分钟成交量达到近 20 根中位数的 4 倍、且 1 分钟涨跌至少 1% 时，也可触发
放量异动。实时价格优先使用 Alpaca IEX（部分市场成交量）；
SIP 在当前账户下延迟约 15 分钟，不作为实时告警主源。

频控按 `America/New_York` 自然日重置，并从 SQLite 成功推送历史恢复，重启不会
清空额度。每个标的每天最多成功推送 2 次：首次越过门槛可提醒；同方向第二次必须
强度至少达到门槛的 1.5 倍且严重等级高于当日历史最高等级，反方向重新越过门槛则
作为方向反转提醒。全局每天最多 5 次，前 4 个名额可用于首次/升级/反转，第 5 个
只留给升级或反转。窗口变化本身不构成升级。抑制、发送失败和历史不确定事件都会
以 `pushed=false` 及结构化原因写入台账；只有成功发送记录消耗额度。

确定要发送的异动会先调用 Codex 联网搜索异动原因：优先公司官网、SEC
和可核验财经媒体，输出事件分类、置信度、简要结论与最多 3 条来源。
只有时间接近而没有证据时必须写“原因未确认”。Codex 超时或失败不会吞掉告警，
卡片会带失败状态继续发出。单轮查因最多两批；发送失败后继续尝试后续候选，查因
预算耗尽的新递补提醒仍会发送，并记录 `research_skipped_reason`。

## 美股单日 ±10% 异动累计榜

`extreme_movers.enabled` 打开后，系统扫描 Alpaca 当前 active、tradable、非 OTC
美股代码。每个完整交易日只记录一次收盘对前收盘涨跌幅达到 `+10%` 或 `-10%`
的事件。主榜进一步要求 `quote_type=EQUITY`、收盘价至少 $5、近 20 日平均成交额
至少 $20M；ETF、权证、资料不可用和低流动性标的保留审计状态但不进入主榜。

榜单按上涨/下跌分开，提供 20、60、252 个已统计交易日窗口：个股显示累计入榜
天数和“只复合入榜日收益”的事件日复合涨跌幅；板块 Top5 按累计合格事件天数、
重复强度、涉及个股数排序。板块榜是异动活跃度，不是市场宽度，也不等于连续
持有收益。收盘后 16:30 ET 推当天新事件，下一交易日 08:00 ET 只读台账推累计榜。
覆盖率不足时 fail closed，不保存残缺快照。`feed` 可选 `hybrid`/`sip`；生产账户
当前无 SIP 权限，采用 IEX 全市场 8% 初筛，再用 Yahoo adjusted 日线确认 ±10%
并计算完整市场口径的 20 日平均成交额。候选确认覆盖必须至少 95%，未确认候选
保留为不可用审计事件且绝不进入主榜；不足 20 个有效确认交易日的标的也不会进入主榜。
混合模式可能漏掉 IEX 初筛未达到 8% 的极端异动，因此卡片标为 best-effort；升级
SIP 权限后可切换到严格的单源模式。

历史初始化可运行 `research/backfill_extreme_movers.py`。回填使用当前仍活跃标的，
因此明确标记 `current_active_symbols` 幸存者偏差；默认不发送通知。

## 美股期权异动提醒（Cboe 四市场）

只观察、不下单的期权热度频道（`option_flow.enabled` 开关），回答三个问题：
当前哪些标准美股期权合约进入 Call/Put 成交量 Top10、哪些合约相较上一轮出现
实质变化、数据覆盖与延迟边界是什么。

1. **发现源**（`datafeed/cboe_options.py`）：聚合 Cboe 四个期权交易所
   （C1/C2/BZX/EDGX）官网榜单的可见合约成交量，按
   `(root, expiry, strike, call/put)` 合并后分别取 Call/Put Top10。四市场
   缺一、解析不完整或任一侧候选不足 10 只一律 fail closed，不推残缺榜
2. **范围过滤**：只保留标准个股/ETF 期权——指数根（SPX/VIX/NDX 等）与
   公司行动调整合约（根代码含数字）不进榜；到期早于扫描交易日的剔除；
   0DTE 保留但显式标记
3. **可选补全**（`datafeed/alpaca_options.py`）：Alpaca Indicative feed
   （约 15 分钟延迟，非 OPRA NBBO）补充最新成交价/买卖价/IV/Greeks/OI，
   只用于解释，**不能反向改变 Cboe 原始排名**；补全失败不影响榜单
4. **异动硬门槛**（`options_flow.py`）：每 15 分钟与上一轮 50 名候选比较，
   新进 Top10 / 排名跳升 ≥3 / 15 分钟增量 ≥1 万张（ETF 与 0DTE 要 ≥2 万张）
   才算实质变化；Volume/OI 高换手只做辅助排序，永不单独触发。异动聚焦最多
   5 张合约、同一标的最多 2 张。上一轮候选中不存在的合约，其全天累计量
   **不会**被当作 15 分钟增量（显示"首次可见"，不触发成交激增）
5. **降噪**：当日第一份有效扫描发基线榜；盘中只有实质变化才发（冷却 60
   分钟）；北京 08:00（美东晚间）固定发收盘榜；每日最多 4 张。盘中卡 45 分钟未发出
   即过期取消，不在下午补发上午旧排名
6. **专用 durable outbox**（sqlite `option_flow_scans/rows/outbox`）：扫描
   与待发卡同一事务原子落库，时间槽幂等（任务重跑不重复保存/推送），
   发送失败按退避重试；与执行计划的 plan outbox 完全隔离，账户截图更新
   不会误取消期权榜

卡片明确标注"Cboe 四市场可见量合计（下限近似）"，不声称全市场
consolidated volume；解释边界写明 Call 成交 ≠ 看涨、Put 成交 ≠ 看跌
（可能是平仓/备兑/保护/价差/做市对冲），不构成交易建议。

## 持仓期权情报（期权市场当信息源）

`option_intel.enabled` 开关。**不推荐任何期权交易**（原因见"非目标"），而是把
持仓标的的期权链当作前瞻信息源，回答"期权市场怎么看我手里的股票"：

- **预期波动**：ATM straddle 中间价 ÷ 现价 ≈ 市场定价的到期前波动幅度
  （取 DTE≥7 的最近到期）；若 30 天内有财报，另取跨财报到期算一份
  "跨财报预期波动"——比如"财报周 ±8%"能提示你财报前是否该减仓/挂宽止损
- **IV vs 实际波动**：ATM 隐含波动率对比 20 日实现波动率，比值超过
  `iv_rv_warn_ratio`（默认 1.5x）标注 ⚠IV偏高（事件定价），说明期权市场
  在为某个事件付溢价
- **Put/Call 量比与 OI 比**：当日成交与持仓的方向倾斜（只做参考，
  Put 多 ≠ 看跌，可能是保护盘）
- **大 OI 执行价**：未平仓量最大的 3 个 strike，常构成支撑/压力磁吸位
- **数据落库**：每日快照进 sqlite `option_intel_daily`（保留 400 天），
  攒够历史后可算 IV 分位数

产出三处：① 每交易日北京时间次日 08:10（美东晚间，收盘数据已完整）推「🧭 持仓期权
情报」卡片；② 期权异动榜卡片上你的持仓标的带 📌 标记；③ 机器人指令
`期权 <代码>`（如 `期权 MU`）现场拉取单标的情报。数据源为 Alpaca Indicative
feed（约 15 分钟延迟），单标的拉取失败只影响该标的（显示 `-`），不编造数据。

## 恐慌指数定投提醒

`fear_dca.enabled` 打开后，系统在北京时间周一至周五 09:30 使用 Yahoo Finance
的 `^VIX`、`^VXN`、`SPY`、`QQQM` adjusted 日线，对齐最近一个已完成的美股
交易日，生成 60 交易日图表和 SPY/QQQM 定投倍数卡片。同一美股交易日只正式
生成一次；`0×` 仅表示本次不额外加仓，不会取消原有定投，也不会自动下单。

SPY 的 VIX 基础档位为 `<25: 0×`、`25/30/40/50: 1/1.5/2/3×`；QQQM 的
VXN 基础档位为 `<35: 0×`、`35/40/50/60: 1/1.5/2/3×`。达到首档后，SPY
近 5/20 日跌幅达到 `-3%/-5%`、或 QQQM 达到 `-4%/-7%`，只加一次 `0.5×`，
最终封顶 `3×`。机器人指令 `定投` 只重发最新 COMPLETE 卡片、不重新抓行情；
`定投规则` 返回上述静态规则。

## 飞书机器人交互（可选）

`feishu_bot.enabled` 打开后，系统通过**企业自建应用**机器人的长连接（WebSocket，
无需公网 IP）接收你的**单聊**消息，实现双向交互；现有群 webhook 推送通道不受
影响，两者独立共存。

**能做什么**：

- 发送券商账户资产页原图（需清晰包含总资产、持仓市值、现金/购买力及完整持仓）
  → Codex 解析 → 对账校验 → 更新账户快照（复用
  `portfolio_import` 全部安全门槛）。`VALIDATED` 自动应用；`PARTIAL` 列出校验
  错误并等待 15 分钟内回复「确认导入」；`REJECTED` 绝不应用。交易日
  08:15–15:45 ET 导入成功后会立即按新持仓重算并重推今日行动计划；其他时段
  只安全更新账户，由下一次定时任务重算
- 文本指令：`状态`（系统概况）/ `持仓`（截图账户+持仓）/ `计划`（活跃执行
  计划）/ `期权`（最新期权榜，读台账不新抓）/ `期权 <代码>`（单标的期权
  情报，现场拉取，如 `期权 MU`）/ `异动榜 [20|60|252]` / `异动板块 [板块]` /
  `异动 <代码>` / `定投`（重发最新报告）/ `定投规则` / `监控 [代码]` /
  `取消监控 <代码>` / `重推` /
  `重推 异动榜` / `帮助`。持仓强制纳入实时价格监控，无法取消；手动监控标的
  没有持仓成本时不会显示伪造盈亏
- **接管全部推送**：`push_receive_id` 填 `ou_xxx`（单聊）或 `oc_xxx`（群，需
  先把机器人拉进群）后，早报/信号/行动卡/期权榜/告警等所有卡片都改由自建
  应用发送，webhook 群通道自动闲置；清空该项即回退 webhook，零迁移风险

**一次性配置**（代码无法代劳）：

1. [飞书开放平台](https://open.feishu.cn)创建**企业自建应用**，启用机器人能力
2. 权限：`im:message`、`im:message.p2p_msg`、`im:resource`
3. 事件订阅选**长连接**模式，订阅 `im.message.receive_v1`，发布应用
4. `config/.env` 填 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
5. `settings.yaml` 设 `feishu_bot.enabled: true` 并重启；先随便发一条消息，
   机器人会回你的 open_id，把它填进 `feishu_bot.allowed_open_ids` 再重启

**群里 @ 机器人**（需开通 `im:message.group_at_msg:readonly` 权限并重新发布
版本）：`@机器人 状态`/`计划`/`期权`/`帮助` 直接在群里回复；`@机器人 持仓`
因含权益与持仓明细，详情**私发单聊**、群里只提示"已私发"；截图导入与
「确认导入」、监控增删和重推只能在单聊进行，群里会被引导回单聊。

**安全边界**：没 @ 机器人的群聊天一概收不到（权限 scope 决定）；白名单外的
发送者在单聊只会收到自己的 open_id 回显、在群里被静默忽略，任何操作都不
执行；事件按 message_id 幂等去重（飞书 at-least-once 投递）；图片临时文件
用完即删、凭据与图片字节不进日志；机器人线程崩溃不影响调度器。

## 调度：一天跑哪些任务

| Job ID | 时间 | 门控 | 做什么 |
|---|---|---|---|
| `market_scan` | 07:00 ET | NYSE 交易日历 | 指数发现池扫描 → Top20 候选榜 + Top1 入台账受检验 |
| `premarket` | 08:00 ET | NYSE 交易日历 | 补历史数据 → 跑动量轮动+技术策略 → 推早报（+可选 AI 观点） |
| `execution_brief` | 08:15 ET | NYSE 交易日历 | 聚合指数候选+核心信号 → 只读账户风控 → PAPER 执行计划卡 |
| `rotation_asia_open` | 08:00 北京时间 (00:00 UTC) | 工作日 | 早报逻辑，服务亚洲盘前（不依赖 NYSE 日历） |
| `rotation_asia_close` | 15:30 北京时间 (07:30 UTC) | 工作日 | 同上，港股/韩股收盘前后 |
| `fear_dca_reminder` | 09:30 北京时间 | 周一至周五 + 美股交易日幂等 | Yahoo VIX/VXN + SPY/QQQM 定投倍数与 60 日图表 |
| `enrichment` | 08:45 ET | NYSE 交易日历 | UZI-Skill 深度分析（`enrichment.enabled=false` 时空跑） |
| `intraday` | 09:30–15:55 ET 每5分钟 | NYSE 交易日历 + 已开盘 | 20日突破策略，监控 `watchlist` |
| `execution_watch` | 09:00–15:55 ET 每5分钟 | NYSE 交易日历 | 推进执行计划状态机，只推状态迁移事件 |
| `holding_price_alert` | 09:30–15:59 ET 每分钟 | NYSE 交易日历 + 实际收市时间 | 真实持仓 1/5/15 分钟与当日异动，自适应门槛+每股2次+全局4+1额度 |
| `extreme_movers_premarket` | 08:00 ET | NYSE 交易日历 | 只读最新完整事件台账，推 60 日个股/板块累计榜 |
| `extreme_movers_close` | 16:30 ET | NYSE 交易日历 | 全市场 configured-feed adjusted 日线扫描，记录并推送单日 ±10% 个股 |
| `option_flow` | 10:00–15:45 ET 每15分钟 | NYSE 交易日历 | Cboe 四市场期权 Call/Put Top10 扫描，基线/实质变化才推送 |
| `option_flow_close` | 北京 08:00 (UTC 00:00) | NYSE 交易日历(美东日期) | 期权收盘榜（force_summary；美东晚间抓当日最终数据，随早间统一阅读） |
| `option_flow_drain` | 16:35–21:35 ET 每小时 | NYSE 交易日历 | 只重试期权 outbox 未发出的卡（不抓数据），保证收盘榜在过期窗口内有真实重试 |
| `option_intel` | 北京 08:10 (UTC 00:10) | NYSE 交易日历(美东日期) | 持仓标的期权情报卡（预期波动/IV vs RV/PC比/大OI） |
| `postmarket` | 北京 08:05 (UTC 00:05) | NYSE 交易日历(美东日期) | 收盘复盘卡：截图持仓逐标的当日涨跌/持仓盈亏/市值 + 组合加权小结 + 当日信号计数 |
| `negative_overreaction` | 16:45 ET | NYSE 交易日历 | 利空错杀观察（新闻分类+企稳确认，仅观察不建仓） |
| `maintenance` | 03:00 ET | 无 | 近 10 日缺 bar 重拉 + 台账/行情备份（保留14天） |
| `data_qa` | 03:30 ET | 无 | 两源收盘价偏差体检 |
| `performance` | 周六 09:00 ET | 无 | 近 90 天信号虚拟盘复盘周报 |
| `heartbeat` | 每 15 分钟 | 无 | 任务失败/MISSED/超时汇总告警 + market_scan 停摆检测 |

三个"推早报"的 job（`premarket`/`rotation_asia_open`/`rotation_asia_close`）
跑的是同一套逻辑（`Engine.run_premarket`），只是时间点和门控条件不同。去重机制
保证同一标的的同一个信号 4 小时内不会重复推送。所有 job 都被 `JobRuntime` 包装，
记录开始/时长/最近成功时间——`market_scan` 在应跑时点后 30 分钟仍无成功、或任一
job 运行超过 10 分钟，心跳会持续告警（健康检查不恒为 True）。

## 配置参考（settings.yaml）

```yaml
data_source: alpaca            # yfinance | alpaca，USD 标的用这个
db_dir: data                   # duckdb/sqlite 存放目录

tickers:                       # 单一来源：自动派生 universe/asset_type/国际标的
  SPY: {asset_type: ETF, currency: USD}
  AAPL: {asset_type: STOCK, currency: USD}
  "7709.HK": {asset_type: STOCK, currency: HKD}

momentum_group_top_n:          # 动量轮动按币种分组的独立名额，不配置的币种(含美股)用 top_n
  HKD: 1
  KRW: 1

watchlist:                     # 20日突破策略监控的标的（跟 universe 独立）
  - NVDA
  - ...

strategies:
  momentum_rotation:
    lookback_days: 60
    top_n: 3
    min_dollar_volume: 50000000    # 美元
  breakout_20d:
    high_lookback_days: 20
    volume_multiplier: 1.5
  price_deviation:
    threshold: 0.02                # ±2%
  rsi_reversion:
    period: 14
    oversold: 30
    overbought: 70
  macd_cross:
    fast: 12
    slow: 26
    signal: 9
  bollinger_breakout:
    period: 20
    num_std: 2.0

notify:
  dedup_hours: 4
  premarket_hourly_limit: 10
  intraday_hourly_limit: 10
  deviation_hourly_limit: 10

enrichment:                      # UZI-Skill 深度分析，可选，默认关闭
  enabled: false
  uzi_run_py: ""                  # 你的 UZI-Skill run.py 绝对路径
  python_exe: "python"
  depth: lite
  timeout_seconds: 60
  max_tickers: 8

index_universe:                  # 指数发现池（v0.5）
  enabled: true
  indices: [sp500, nasdaq100]
  cache_path: data/index_universe.json
  refresh_days: 7                # 成分快照刷新周期
  max_stale_days: 14             # 快照超龄 → STALE, 停发新计划
  scan_top_n: 20                 # 观察榜数量
  execution_top_n: 5             # 进入执行风控的候选数
  min_coverage: 0.98             # 行情覆盖率下限, 不足 fail closed
  min_dollar_volume: 50000000

execution_plan:                  # PAPER 执行建议（v0.5）
  enabled: true
  account_provider: alpaca_paper # 只读, 仅 GET
  risk_per_trade: 0.005          # 单笔风险占权益
  max_daily_new_risk: 0.01       # 当日新增风险上限
  max_position_weight: 0.12      # 单票市值上限
  cash_reserve: 0.20             # 现金保留比例
  max_new_positions_per_day: 2
  min_stop_distance: 0.02        # 止损距离 <2% → STOP_TOO_TIGHT
  max_stop_distance: 0.20        # >20% → STOP_TOO_WIDE
  account_max_age_seconds: 60    # 账户快照超龄不出股数

option_flow:                     # 期权异动提醒（Cboe 四市场，只观察不下单）
  enabled: true
  feed: indicative               # 生产只允许 indicative；OPRA 开通后显式切换，不做静默降级
  top_n: 10
  discovery_limit: 50            # 每市场每侧抓前 50 做候选
  venues: [cone, ctwo, opt, exo] # C1 / C2 / BZX / EDGX
  excluded_index_roots: [SPX, SPXW, VIX, RUT, RUTW, NDX, XSP, OEX]
  etf_roots: [SPY, QQQ, IWM, TQQQ, SQQQ, SOXL, SOXS, TLT, GLD, HYG, XLF]
                                 # 这些根用 0DTE/ETF 更高的激增阈值
  min_volume: 5000               # 进榜最低可见张数
  surge_volume: 10000            # 15 分钟增量阈值（普通合约）
  zero_dte_surge_volume: 20000   # 0DTE/ETF 阈值
  rank_jump: 3                   # 排名跳升阈值
  cooldown_minutes: 60           # 盘中变化卡冷却
  max_alerts_per_day: 4          # 含基线与收盘榜
  intraday_expiry_minutes: 45    # 盘中卡过期取消
  closing_expiry_hours: 12       # 收盘榜过期时间
  min_venue_coverage: 0.75       # 至少三市场成功；缺失市场会标注覆盖率
  circuit_breaker_failures: 2    # 单市场连续失败后开启熔断
  circuit_breaker_cooldown_minutes: 10

option_intel:                    # 持仓期权情报（只观察，不推荐任何期权交易）
  enabled: false
  max_expiry_days: 60            # 只看 60 天内到期的合约
  min_expected_move_dte: 7       # 预期波动取 DTE>=7 的最近到期
  top_oi_strikes: 3              # 展示 OI 最大的 N 个 strike
  iv_rv_warn_ratio: 1.5          # ATM IV / 20日RV 超过此值提示"事件定价"
  retention_days: 400            # 日度快照保留天数（留足一年做 IV 分位数）
  max_tickers: 12                # 每日最多处理的持仓标的数

holding_price_alert:             # 真实持仓分钟级股价异动
  enabled: true
  stock_1m_pct: 0.020            # 个股：1/5/15分钟/当日 = 2/4/6/10%
  stock_5m_pct: 0.040
  stock_15m_pct: 0.060
  stock_session_pct: 0.100
  etf_1m_pct: 0.012              # ETF：1/5/15分钟/当日 = 1.2/2.5/4/6%
  etf_5m_pct: 0.025
  etf_15m_pct: 0.040
  etf_session_pct: 0.060
  volume_spike_multiple: 4.0     # 末根量 / 近 20 根中位数
  min_volume_spike_move_pct: 0.010
  cooldown_minutes: 30           # 兼容旧配置；每日状态机不再用窗口冷却限频
  max_alerts_per_day: 5
  regular_alert_slots: 4         # 第 5 个名额只用于升级/反转
  max_alerts_per_ticker_per_day: 2
  meaningful_upgrade_score: 1.5
  cause_search:
    enabled: true
    command: codex
    model: gpt-5.6-terra         # 盘中低延迟查因专用
    reasoning_effort: low
    timeout_seconds: 60          # 实测联网查因约49秒；超时仍发告警
    lookback_hours: 24
    max_sources: 3
    max_alerts_per_batch: 8
    max_summary_chars: 220

legacy_price_deviation:          # 旧 ±2% 偏离提醒, 默认下线, 可回滚
  enabled: false

feishu_bot:                      # 自建应用机器人交互(需 FEISHU_APP_ID/SECRET)
  enabled: false
  allowed_open_ids: []           # 白名单为空时只回显 open_id，不执行任何操作
  push_receive_id: ""            # 填 ou_xxx(单聊)/oc_xxx(群) 后所有推送改走自建应用；留空走 webhook
  capital_limit: 6000
  max_financing_ratio: 0.20
  confirm_window_minutes: 15     # PARTIAL 导入确认窗口
  codex_timeout_seconds: 180
```

## 命令行工具

```bash
# 拉取历史日线入库（首次用 --days 730，之后日常用 maintenance job 自动补）
uv run python -m quant_signal.ingest --days 730
uv run python -m quant_signal.ingest --check          # 只检查缺失交易日，不拉数据

# 把真实持仓写入虚拟持仓台账（避免系统对已持有标的重复提醒买入）
uv run python -m quant_signal.seed_holdings --strategy momentum_rotation \
    --tickers MU,DRAM,RAM,MRVL,SNDK,SNXX,SMH,QQQM,SPY

# 测试飞书/Console 通知链路（发三种测试卡片：信号/早报/告警）
uv run python -m quant_signal.notifier.feishu --test

# 恐慌定投 E2E：默认只抓真实 Yahoo 数据，使用内存台账/记录通知器，绝不发飞书
.venv/Scripts/python.exe scripts/live_fear_dca_e2e.py

# ⚠️ 真实发送：写 data/signals.db，最多发 1 张；已有 COMPLETE 时幂等跳过
.venv/Scripts/python.exe scripts/live_fear_dca_e2e.py --send

# ⚠️ 明确再多发 2 张：最新 COMPLETE 重放 1 张 + 静态规则 1 张
.venv/Scripts/python.exe scripts/live_fear_dca_e2e.py --send --replay --rules

# 初始化最近 252 个交易日的 ±10% 事件（默认不推送，支持中断检查点）
.venv/Scripts/python.exe research/backfill_extreme_movers.py --sessions 252 \
    --no-notify --checkpoint data/extreme-movers-backfill.json

# 启动调度器（前台运行，挂机跑一整天的任务）
uv run quant-signal
```

## 数据源与国际标的

- `DataSource` 协议只有两个方法：`fetch_daily_bars` / `fetch_intraday_bars`，
  `YFinanceSource` 和 `AlpacaSource` 都实现它
- 每次拉数据时，派生出的 `universe`/`watchlist` 会按注册表币种
  是否包含它来分流：包含则固定走 `YFinanceSource`，否则走 `data_source`
  配置指定的源——这样你可以美股用 Alpaca（更稳定、无延迟）、港股韩股用
  yfinance（Alpaca 完全不支持这些市场）
- 想加新的港股/韩股标的：先用 yfinance 确认准确代码格式（港股是 4-5 位数字
  + `.HK`，如 `7709.HK`；韩股是 6 位数字 + `.KS`，如 `000660.KS`），加进
  `tickers` 并填写 `asset_type` 与 `currency`；无需再同步多个列表

## 飞书卡片格式

三种卡片模板（`notifier/cards.py`）：

1. **信号卡片**：`{emoji} {ticker} {方向}`，含现价/触发原因/建议仓位/策略/时间(SGT)
2. **早报/复盘卡片**：markdown 表格，早报含"标的/方向/参考价/现价/原因"，最多
   展示 5 条（BUY 按动量排名优先，超出的在末尾注明"还有 N 条见台账"）；
   收盘复盘卡含"标的/收盘/当日/持仓盈亏/市值"与组合加权小结，缺数据显示 `-`
3. **告警卡片**：红色 header，用于心跳失败和限流汇总
4. **期权热度卡片**：标题"🔥 美股期权热度 · Cboe四市场"，纯结构化分段
   （数据身份/异动聚焦/CALL Top10/PUT Top10/解释边界），不用 markdown
   表格以保证手机端可读；标题措辞避免"主力买入/扫货/强烈看涨"等当前
   数据无法证明的说法

飞书 lark_md 卡片在手机端排版有限——列数建议不超过 4-5 列，标的名称建议只显示
ticker（全名太长会挤压其他列）。

## 回测与研究

```bash
uv run python research/backtest_momentum.py       # vectorbt 参数扫描（lookback×top_n）
uv run python research/backtest_breakout.py       # 突破策略回测（日线近似，5min历史数据不可得）
uv run python research/backtest_new_strategies.py # RSI/MACD/布林带 事件驱动回测
uv run python research/backtest_my_holdings.py    # 只针对真实持仓标的的专属回测(复用上面几个脚本的函数)
uv run python research/walkforward.py             # 逐日喂数据 vs 全量回放，验证无未来函数（覆盖全部5个策略）
uv run python research/oos_validation.py          # 滚动样本外验证（选参与检验分窗）
uv run python research/replay_execution.py        # 新旧口径回放：target-hit 提醒 vs ACTIONABLE 确认（幸存者偏差显式标注）
```

回测脚本 import 的是 `src/quant_signal/strategies/` 下同一份策略代码，不重复实现。
报告输出到 `research/reports/*.md`。**回测结果仅供评估，Agent 不会根据回测结果
自行修改策略参数。**

## 开发与测试

```bash
uv run pytest -q          # 全量单测
uv run mypy src/          # 类型检查（strict 模式，research/ 目录不含在内）
```

开发遵循 TDD：先写失败的测试，确认失败原因符合预期，再写实现让测试通过。
策略层（`strategies/`）是纯函数（`generate(bars) -> list[Signal]`），保证回测
和实盘共用同一份逻辑。

## 已知限制

- **国际标的没有专属交易日历**：`watch_deviation` 用固定 UTC 时间窗口
  （工作日 00:00–21:55 UTC）兜底覆盖亚洲+美股交易时段，不精确感知港交所/
  韩交所具体的假期安排——某些假期可能会有一两次无意义的空跑（不影响正确性，
  只是浪费几次请求）
- **yfinance 免费接口**盘中数据延迟约 15 分钟，且可能对某个标的/时段完全
  没有数据（已做容错，不会导致崩溃，只是那次检查会跳过该标的）
- **Alpaca 免费行情**是 IEX 单一交易所数据，跟全市场 NBBO 报价存在差异，
  仅供参考
- **"现价"字段**是尽力而为的补充展示——抓取失败时显示 `-`，不影响信号本身
  的生成逻辑
- **虚拟持仓需要手动初始化同步**：系统不会自动读取你券商账户的真实持仓，
  首次使用务必跑一次 `seed_holdings`，否则动量轮动会对你已持有的标的重复
  提示"买入"
- **PAPER 账户 ≠ 你的券商账户**：执行建议的股数按 Alpaca paper 账户的权益/现金
  计算；`OPEN`（成交检测）与止损/止盈提醒也只看 paper 持仓——如果你不在 paper
  账户镜像下单，计划最多走到 ACTIONABLE，不会有后续持仓事件
- **指数成分快照没有历史时点库**：缓存只存最新一次成功快照，用当前成分回放
  历史必然带幸存者偏差，回放报告已显式标注，不作为正式 alpha 证据
- **期权榜只覆盖 Cboe 四市场可见量**：不含 Nasdaq/NYSE/MIAX/BOX/MEMX 等
  期权交易所成交，且每市场只暴露前 50，属于下限近似；Alpaca Indicative
  补全约延迟 15 分钟且非 OPRA NBBO。Cboe 页面 JSON 是网站接口而非承诺
  稳定的商业 API，接口变更时该频道会 fail closed 停发而不是发错数据

## 风险与合规提示

- 本系统仅生成参考信号与 PAPER 执行建议，**不构成投资建议**；所有交易由用户
  人工决策并在券商 App 执行
- 系统在任何情况下都不实现自动下单逻辑：账户适配器只有 GET 方法，卡片与日志
  全部标注 PAPER
- 回测结果（`research/reports/`）仅供策略评估参考，不保证未来表现

## 目录结构

```
quant-signal/
├── config/
│   ├── settings.yaml           # 标的池、策略参数、去重限流配置
│   └── .env                    # 凭证（不提交 git）
├── src/quant_signal/
│   ├── main.py                 # 入口：uv run quant-signal
│   ├── scheduler.py            # 18 个定时任务的注册与门控逻辑
│   ├── engine.py                # 依赖装配与兼容入口
│   ├── pipelines/               # premarket/intraday/execution_plan/option_flow 等工作流
│   ├── config.py                # 配置加载（yaml + .env）
│   ├── calendar.py               # NYSE 交易日历
│   ├── ledger.py                 # sqlite 信号台账 + 虚拟持仓 + 参考价查询
│   ├── ingest.py                  # 历史数据入库 CLI，按标的分流数据源
│   ├── seed_holdings.py           # 初始化虚拟持仓 CLI
│   ├── report.py                  # 收盘复盘卡（持仓表现+信号计数）
│   ├── watch_monitor.py           # 持仓偏离检测（纯函数）
│   ├── options_flow.py            # 期权榜领域模型：OCC symbol/聚合排名/异动判定
│   ├── options_intel.py           # 持仓期权情报领域模型：预期波动/IV vs RV/PC比/大OI
│   ├── feishu_bot.py              # 自建应用机器人：长连接指令交互+截图导入
│   ├── portfolio_import.py        # 券商截图解析(Codex)+对账校验+导入
│   ├── index_universe.py           # 指数发现池（纳指100+标普500 成分）
│   ├── account.py                  # 只读 Alpaca paper 账户适配器（仅 GET）
│   ├── execution.py                # 确定性 sizing + 执行计划状态机
│   ├── enrichment.py              # UZI-Skill headless 调用（尽力而为）
│   ├── logging_setup.py           # structlog 初始化
│   ├── datafeed/
│   │   ├── base.py                # DataSource 协议 + 工厂函数
│   │   ├── yf_source.py           # yfinance 实现
│   │   ├── alpaca_source.py       # Alpaca REST 实现
│   │   ├── cboe_options.py        # Cboe 四市场期权榜发现（网站 JSON 接口）
│   │   ├── alpaca_options.py      # Alpaca Indicative 期权补全（OI/报价/Greeks）+ 期权链快照
│   │   ├── fx.py                  # 实时汇率查询
│   │   └── store.py               # duckdb 行情读写
│   ├── strategies/
│   │   ├── base.py                # Signal/Direction/Strategy 基类
│   │   ├── momentum_rotation.py   # 动量轮动策略
│   │   ├── breakout_20d.py        # 20日突破策略
│   │   ├── rsi_reversion.py       # RSI 均值回归
│   │   ├── macd_cross.py          # MACD 金叉/死叉
│   │   └── bollinger_breakout.py  # 布林带突破
│   └── notifier/
│       ├── base.py                 # Card + Notifier 协议 + Console 实现
│       ├── cards.py                # 三种卡片模板
│       ├── feishu.py               # 飞书 webhook 发送 + 重试
│       └── dedup.py                # 去重限流规则
├── research/                    # 离线回测脚本（import src/ 下同一份策略代码）
├── tests/                       # 单测（TDD，覆盖每个模块）
├── data/                        # duckdb/sqlite 数据文件（不提交 git）
└── logs/                        # structlog JSON 日志 + Console 通知器的 signals.jsonl（不提交 git）
```
