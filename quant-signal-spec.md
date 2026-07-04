# 美股半自动量化信号系统 — 实施规格说明书（Agent 版）

> 本文档面向执行开发任务的 AI Agent（Claude Code 等）。目标是从零搭建一个 7x24 运行的美股量化信号系统：**只产生信号并推送飞书，不自动下单**（用户在致富证券手动执行）。请严格按 Milestone 顺序交付，每个 Milestone 有明确验收标准。

---

## 0. 关键决策（已定，勿改）

| 决策点 | 选择 | 说明 |
|---|---|---|
| 交易模式 | 半自动 | 系统只推信号，人工在券商 App 下单。**任何情况下不得实现自动下单逻辑** |
| 策略频率 | 日级为主 + 盘中突破提醒 | 日级信号在盘前生成；盘中仅监控观察列表的突破类事件 |
| 首批策略 | 规则型 | 动量轮动（日级）+ 20日高点突破（盘中）。ML/qlib 留到后期 |
| 回测框架 | vectorbt（参数扫描）+ backtrader（事件驱动验证） | 不自研回测引擎 |
| 实时数据 | Alpaca（免费 IEX 行情，paper 账户即可） | yfinance 仅作历史数据兜底 |
| 通知 | 飞书自定义机器人 webhook（Phase 1） | 交互按钮/回调闭环放 Phase 2，本期不做 |
| 存储 | duckdb（行情/因子）+ sqlite（信号台账） | 单机部署，不引入外部数据库 |
| 部署 | docker-compose，单台 VPS（美西） | 时区统一用 `America/New_York` 处理交易日历，展示时间转 `Asia/Singapore` |
| 语言/版本 | Python 3.11+ | 类型注解全覆盖，mypy 通过 |

## 1. 非目标（明确不做）

- 不做自动下单、不接任何券商交易 API
- 不做 tick 级/秒级高频，最小粒度 1 分钟 bar
- 不做 Web UI，一切输出走飞书
- Phase 1 不做飞书交互回调、不做 UZI-Skill 深度分析集成（接口预留即可）

## 2. 系统架构

```
┌──────────────┐    ┌───────────────┐    ┌───────────────┐
│  data-feed    │ →  │ signal-engine  │ →  │ notifier       │
│  行情采集      │    │ 策略调度+信号   │    │ 飞书卡片+去重   │
└──────┬───────┘    └───────┬───────┘    └───────────────┘
       ↓                    ↓
   duckdb(bars)        sqlite(signals)
                            ↑
                    ┌───────────────┐
                    │  research      │  离线回测（vectorbt/backtrader）
                    └───────────────┘
```

四个模块同仓库（monorepo），data-feed / signal-engine / notifier 跑在同一进程内由 APScheduler 调度（单机规模不需要拆进程），research 为离线脚本集。

## 3. 仓库结构

```
quant-signal/
├── pyproject.toml            # uv 管理依赖
├── docker-compose.yml
├── Dockerfile
├── config/
│   ├── settings.yaml         # 观察列表、策略参数、时段配置
│   └── .env.example          # ALPACA_KEY, ALPACA_SECRET, FEISHU_WEBHOOK
├── src/quant_signal/
│   ├── main.py               # 入口：启动 scheduler
│   ├── scheduler.py          # 时段任务编排（见 §5）
│   ├── datafeed/
│   │   ├── alpaca_client.py  # 历史bar拉取 + ws订阅
│   │   ├── yf_fallback.py
│   │   └── store.py          # duckdb 读写
│   ├── strategies/
│   │   ├── base.py           # Strategy 抽象基类（见 §4）
│   │   ├── momentum_rotation.py
│   │   └── breakout_20d.py
│   ├── engine.py             # 加载策略、生成信号、写台账
│   ├── notifier/
│   │   ├── feishu.py         # webhook 发送 + 重试
│   │   ├── cards.py          # 消息卡片模板
│   │   └── dedup.py          # 去重规则（见 §6）
│   ├── ledger.py             # sqlite 信号台账 CRUD
│   ├── calendar.py           # 交易日/夏令时判断（用 pandas_market_calendars）
│   └── report.py             # 日报/复盘统计
├── research/
│   ├── backtest_momentum.py
│   ├── backtest_breakout.py
│   └── notebooks/
└── tests/
```

## 4. 核心接口定义

```python
# strategies/base.py
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import pandas as pd

class Direction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    REDUCE = "reduce"

@dataclass(frozen=True)
class Signal:
    ticker: str
    direction: Direction
    price: float              # 触发时参考价
    reason: str               # 人类可读，将出现在飞书卡片
    strategy_id: str
    ts: datetime              # UTC
    suggested_weight: float | None = None   # 建议仓位占比 0~1
    extra: dict | None = None

class Strategy:
    strategy_id: str
    schedule: str             # "daily_premarket" | "intraday_5min"
    universe: list[str]       # 标的池，从 settings.yaml 注入

    def generate(self, bars: pd.DataFrame) -> list[Signal]:
        """bars: MultiIndex(ticker, ts) OHLCV。纯函数，无副作用，无 IO。"""
        raise NotImplementedError
```

策略必须是**纯函数式**（数据进、信号出），便于回测和实时共用同一份逻辑。回测脚本直接 import 策略类喂历史数据，禁止在 research/ 里复制策略代码。

### 首批策略参数（写入 settings.yaml，可调）

**momentum_rotation**（日级，盘前跑）：
- universe: 用户配置的 ETF/个股池（默认给 SPY,QQQ,IWM,TLT,GLD + 用户自选 20 只个股）
- 逻辑：60 日动量排序，取 top N（默认 3），与当前虚拟持仓比较，产出 BUY/SELL 调仓信号
- 过滤：20 日均成交额 < $50M 的剔除

**breakout_20d**（盘中，每 5 分钟）：
- universe: settings.yaml 中 watchlist
- 逻辑：最新价突破 20 日最高价（不含当日）且当日成交量 > 20 日均量 1.5 倍 → BUY 信号
- 仅在常规盘时段运行

## 5. 调度时刻表（America/New_York）

| ET 时间 | 任务 | 说明 |
|---|---|---|
| 08:00 | premarket_job | 补齐日线数据 → 跑 momentum_rotation → 推"盘前早报"卡片（信号 + 盘前涨跌幅 top5） |
| 09:30–16:00 每 5min | intraday_job | 拉最新 5min bar → 跑 breakout_20d → 触发即推 |
| 16:30 | postmarket_job | 当日信号复盘（数量、理论收益）→ 推日报 |
| 03:00 | maintenance_job | 数据完整性校验、duckdb compact、失败重拉 |
| 每 15min | heartbeat_job | 进程自检；连续 2 次失败推飞书告警卡片 |

非交易日（用 pandas_market_calendars 的 NYSE 日历判断）跳过 premarket/intraday/postmarket，保留 maintenance 和 heartbeat。

## 6. 去重与限流规则（notifier/dedup.py）

1. 同 (ticker, direction, strategy_id) 在 4 小时内只推一次
2. 盘中单小时推送上限 10 条，超出合并为一条汇总卡片
3. 所有信号无论是否推送都写入台账，`pushed` 字段标记

## 7. sqlite 台账 schema

```sql
CREATE TABLE signals (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,             -- UTC ISO8601
    ticker TEXT NOT NULL,
    direction TEXT NOT NULL,
    price REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    reason TEXT,
    suggested_weight REAL,
    pushed INTEGER DEFAULT 0,
    dedup_key TEXT,               -- ticker|direction|strategy_id
    extra_json TEXT
);
CREATE INDEX idx_signals_dedup ON signals(dedup_key, ts);
```

## 8. 飞书卡片规格（notifier/cards.py）

使用飞书 interactive card（schema 2.0）。三种模板：

1. **信号卡片**：标题 `📈 {ticker} {direction}`，字段：现价、触发原因、建议仓位、策略ID、时间（SGT）；底部按钮为跳转链接 `https://www.tradingview.com/chart/?symbol={ticker}`
2. **早报/日报卡片**：markdown 表格列出信号汇总与统计
3. **告警卡片**：红色 header，用于 heartbeat 失败和数据异常

发送失败重试 3 次（指数退避），仍失败写本地日志。webhook URL 从环境变量读取，**禁止硬编码到代码或提交到 git**。

## 9. 数据层要求

- 历史日线：Alpaca REST 拉取，起始回溯 2 年，落 duckdb 表 `bars_1d`
- 盘中：Alpaca 5min bar 轮询（免费档不用 ws 也够；若实现 ws 需处理断线重连 + 指数退避）
- 复权：使用 Alpaca `adjustment=all`
- 数据校验：每日 maintenance 检查缺 bar，缺失用 yfinance 补并打标记 `source='yf'`

## 10. Milestones 与验收标准

### M1 — 通知链路（先做，半天）
- feishu.py + cards.py 完成，`python -m quant_signal.notifier.feishu --test` 能向配置的 webhook 发出三种测试卡片
- 验收：飞书群收到卡片，格式正确，中文无乱码

### M2 — 数据层（1 天）
- Alpaca 拉取 2 年日线入 duckdb；5min bar 轮询可用
- 验收：`pytest tests/test_datafeed.py` 通过；duckdb 中 SPY 日线 bar 数 ≥ 490

### M3 — 策略 + 引擎 + 调度（2 天）
- 两个策略实现 + engine + scheduler 全链路；台账落库；去重生效
- 验收：本地以 mock 时间跑通 premarket/intraday/postmarket 三个 job，飞书收到对应卡片；同信号 4h 内重复触发不重复推送（单测覆盖）

### M4 — 回测验证（1–2 天）
- research/ 下用 vectorbt 对两个策略跑 2016 至今回测，输出年化、Sharpe、最大回撤、换手率到 markdown 报告
- 验收：回测脚本 import 的是 src 下同一份策略代码；报告生成于 research/reports/
- **注意：回测结果仅供用户评估，Agent 不得根据回测结果自行修改策略参数后宣称"已优化"**

### M5 — 部署（半天）
- Dockerfile + docker-compose.yml + 启动文档 README
- 验收：`docker compose up -d` 后 heartbeat 卡片按时到达；容器重启后调度恢复

### Phase 2 预留（本期不实现，仅留接口）
- 飞书企业自建应用交互回调（"已下单/忽略"按钮 → 虚拟持仓跟踪）
- UZI-Skill 深度分析 enrichment：engine 中预留 `enrichers: list[Enricher]` 钩子
- qlib ML 选股策略接入

## 11. 工程规约

- 依赖用 uv 管理；固定版本号
- 全部时间内部统一 UTC，仅展示层转 SGT；禁止 naive datetime
- 日志用 structlog，JSON 格式，按天轮转
- 单测覆盖：dedup、calendar、两个策略的信号生成（用构造的固定 OHLCV fixture）
- 密钥只走 .env，提供 .env.example；确认 .gitignore 覆盖 .env 与 *.db
- 每个 Milestone 完成后停下来向用户汇报，等确认再继续

## 12. 风险与合规提示（写入 README）

- 本系统仅生成参考信号，不构成投资建议；所有交易由用户人工决策执行
- Alpaca 免费行情为 IEX 单一交易所数据，与全市场 NBBO 存在差异，信号价格仅供参考
