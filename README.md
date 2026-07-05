# 美股/港股/韩股半自动量化信号系统 (quant-signal)

只产生信号并推送飞书，**不自动下单**——所有交易由用户在券商 App 手动决策执行。

## 目录

- [非目标（明确不做的事）](#非目标明确不做的事)
- [架构](#架构)
- [快速开始](#快速开始)
- [凭证配置](#凭证配置)
- [核心概念](#核心概念)
- [策略](#策略)
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

- 不自动下单，不接入任何券商交易 API
- 不做 tick/秒级高频，最小颗粒度是 5 分钟 bar
- 不维护港股/韩股专属交易日历（用固定 UTC 时间窗口兜底，见"已知限制"）
- 不做 Web UI，所有输出走飞书（或本地终端 Console 模式）

## 架构

```
┌──────────────┐    ┌───────────────┐    ┌───────────────┐
│  data-feed    │ →  │ signal-engine  │ →  │ notifier       │
│  行情采集      │    │ 策略调度+信号   │    │ 飞书卡片+去重   │
│ Alpaca/yfinance│    │ Engine        │    │ Console/飞书    │
└──────┬───────┘    └───────┬───────┘    └───────────────┘
       ↓                    ↓
   duckdb(bars)        sqlite(signals+holdings)
                            ↑
                    ┌───────────────┐
                    │  research      │  离线回测（vectorbt + walk-forward）
                    └───────────────┘
```

单进程 monorepo，APScheduler 驱动 9 个定时任务（详见下文）。数据源
（yfinance / Alpaca）与通知器（Console / 飞书）都是可切换的抽象：

- 每个标的按 `international_tickers` 配置固定走 yfinance 或跟随
  `data_source` 走 Alpaca——美股/ETF 走 Alpaca，港股/韩股等 Alpaca
  不支持的市场固定走 yfinance，两者数据合并进同一个 duckdb
- `FEISHU_WEBHOOK` 未配置时自动降级为 Console 通知器（终端打印 +
  写 `logs/signals.jsonl`），不影响开发和测试

## 快速开始

```bash
uv sync --all-extras
uv run python -m quant_signal.ingest --days 730   # 拉取 2 年历史日线入 duckdb
uv run quant-signal                                # 启动调度器（前台运行，Ctrl+C 退出）
```

首次运行前建议先看一遍下面的[配置参考](#配置参考settingsyaml)，把
`universe`/`watchlist` 改成你自己关心的标的，并跑一次
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
6. 把 `config/settings.yaml` 里的 `data_source` 改成 `alpaca`（`international_tickers`
   里列出的标的不受这个开关影响，永远走 yfinance）
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
  依然有效。超过 `notify.hourly_limit`（默认每小时 10 条）的部分会合并成一条汇总告警。

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
  汇率来自 yfinance 的 `{币种}=X` 报价，每次跑动量轮动时刷新一次
- **按市场分组排名**（`momentum_group_top_n` 配置）：`international_tickers`
  里的币种（如 HKD/KRW）各自独立排名、独立取名额，不跟默认组（美股/其余
  标的，用 `top_n`）竞争——否则港股/韩股这类杠杆/热门标的动量经常极端
  （实测 +386.7%），会把美股标的全部挤出 top-N。默认组的名额始终是完整
  的 `top_n` 个，不会被其他组占用

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
- 韩股等 UZI-Skill 覆盖较弱的市场，实测会触发 `timeout_seconds`（默认120秒）
  超时，属于预期的优雅降级，不是 bug

## 调度：一天跑哪些任务

| Job ID | 时间 | 门控 | 做什么 |
|---|---|---|---|
| `premarket` | 08:00 ET | NYSE 交易日历 | 补历史数据 → 跑动量轮动 → 推早报 |
| `rotation_asia_open` | 08:00 北京时间 (00:00 UTC) | 工作日 | 同上，服务亚洲盘前（不依赖 NYSE 日历，美股假期照跑） |
| `rotation_asia_close` | 15:30 北京时间 (07:30 UTC) | 工作日 | 同上，港股/韩股收盘前后 |
| `intraday` | 09:30–15:55 ET 每5分钟 | NYSE 交易日历 + 已过开盘时间 | 跑 20日突破策略，监控 `watchlist` |
| `watch_deviation` | 00:00–21:55 UTC 每5分钟 | 工作日 | 持仓偏离监控（覆盖亚洲+美股交易时段，含冬令时缓冲） |
| `enrichment` | 08:45 ET | NYSE 交易日历 | UZI-Skill 深度分析（`enrichment.enabled=false` 时空跑） |
| `postmarket` | 16:30 ET | NYSE 交易日历 | 推送当日信号日报（数量、理论收益） |
| `maintenance` | 03:00 ET | 无 | 近 10 日缺 bar 重拉（美股走配置的数据源，国际标的固定走 yfinance） |
| `heartbeat` | 每 15 分钟 | 无 | 进程自检，连续 2 次失败推告警卡片 |

三个"推早报"的 job（`premarket`/`rotation_asia_open`/`rotation_asia_close`）
跑的是同一套逻辑（`Engine.run_premarket`），只是时间点和门控条件不同——每次都
用当时能拿到的最新收盘价重新计算一遍动量排名。去重机制保证同一标的的同一个
信号 4 小时内不会重复推送三次。

## 配置参考（settings.yaml）

```yaml
data_source: alpaca            # yfinance | alpaca，非 international_tickers 标的用这个
db_dir: data                   # duckdb/sqlite 存放目录

universe:                      # 动量轮动的标的池
  - SPY
  - ...

international_tickers:         # 固定走 yfinance 的标的 -> 币种（Alpaca 不支持这些市场）
  "7709.HK": HKD
  "000660.KS": KRW

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
  hourly_limit: 10

enrichment:                      # UZI-Skill 深度分析，可选，默认关闭
  enabled: false
  uzi_run_py: ""                  # 你的 UZI-Skill run.py 绝对路径
  python_exe: "python"
  depth: lite
  timeout_seconds: 120
  max_tickers: 8
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

# 启动调度器（前台运行，挂机跑一整天的任务）
uv run quant-signal
```

## 数据源与国际标的

- `DataSource` 协议只有两个方法：`fetch_daily_bars` / `fetch_intraday_bars`，
  `YFinanceSource` 和 `AlpacaSource` 都实现它
- 每次拉数据时，`universe`/`watchlist` 里的标的会按 `international_tickers`
  是否包含它来分流：包含则固定走 `YFinanceSource`，否则走 `data_source`
  配置指定的源——这样你可以美股用 Alpaca（更稳定、无延迟）、港股韩股用
  yfinance（Alpaca 完全不支持这些市场）
- 想加新的港股/韩股标的：先用 yfinance 确认准确代码格式（港股是 4-5 位数字
  + `.HK`，如 `7709.HK`；韩股是 6 位数字 + `.KS`，如 `000660.KS`），加进
  `universe`，同时在 `international_tickers` 里补上对应币种

## 飞书卡片格式

三种卡片模板（`notifier/cards.py`）：

1. **信号卡片**：`{emoji} {ticker} {方向}`，含现价/触发原因/建议仓位/策略/时间(SGT)
2. **早报/日报卡片**：markdown 表格，早报含"标的/方向/参考价/现价/原因"，最多
   展示 5 条（BUY 按动量排名优先，超出的在末尾注明"还有 N 条见台账"）
3. **告警卡片**：红色 header，用于心跳失败和限流汇总

飞书 lark_md 卡片在手机端排版有限——列数建议不超过 4-5 列，标的名称建议只显示
ticker（全名太长会挤压其他列）。

## 回测与研究

```bash
uv run python research/backtest_momentum.py       # vectorbt 参数扫描（lookback×top_n）
uv run python research/backtest_breakout.py       # 突破策略回测（日线近似，5min历史数据不可得）
uv run python research/backtest_new_strategies.py # RSI/MACD/布林带 事件驱动回测
uv run python research/backtest_my_holdings.py    # 只针对真实持仓标的的专属回测(复用上面几个脚本的函数)
uv run python research/walkforward.py             # 逐日喂数据 vs 全量回放，验证无未来函数（覆盖全部5个策略）
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

## 风险与合规提示

- 本系统仅生成参考信号，**不构成投资建议**；所有交易由用户人工决策并在券商 App 执行
- 系统在任何情况下都不实现自动下单逻辑，不接入任何券商交易 API
- 回测结果（`research/reports/`）仅供策略评估参考，不保证未来表现

## 目录结构

```
quant-signal/
├── config/
│   ├── settings.yaml           # 标的池、策略参数、去重限流配置
│   └── .env                    # 凭证（不提交 git）
├── src/quant_signal/
│   ├── main.py                 # 入口：uv run quant-signal
│   ├── scheduler.py            # 8 个定时任务的注册与门控逻辑
│   ├── engine.py                # 串联策略→去重→台账→推送的核心编排
│   ├── config.py                # 配置加载（yaml + .env）
│   ├── calendar.py               # NYSE 交易日历
│   ├── ledger.py                 # sqlite 信号台账 + 虚拟持仓 + 参考价查询
│   ├── ingest.py                  # 历史数据入库 CLI，按标的分流数据源
│   ├── seed_holdings.py           # 初始化虚拟持仓 CLI
│   ├── report.py                  # 日报统计
│   ├── watch_monitor.py           # 持仓偏离检测（纯函数）
│   ├── enrichment.py              # UZI-Skill headless 调用（尽力而为）
│   ├── logging_setup.py           # structlog 初始化
│   ├── datafeed/
│   │   ├── base.py                # DataSource 协议 + 工厂函数
│   │   ├── yf_source.py           # yfinance 实现
│   │   ├── alpaca_source.py       # Alpaca REST 实现
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
