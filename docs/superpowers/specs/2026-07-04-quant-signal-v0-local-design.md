# 量化信号系统 v0（本机初期版）设计文档

> 本文档是对 [`quant-signal-spec.md`](../../../quant-signal-spec.md) 的**增量适配**，两者共同构成实施依据。凡本文档未提及的部分（架构、仓库结构、Strategy/Signal 接口、sqlite schema、去重规则、调度时刻表、飞书卡片规格、工程规约），一律按原 spec 执行。冲突处以本文档为准。

## 1. 背景与决策记录

原 spec 面向 VPS + Docker 的 7x24 部署。v0 阶段在用户 Windows 11 本机运行验证效果，且 Alpaca key 与飞书 webhook 尚未注册（用户将在本版期间自行注册）。据此与用户确认了以下决策：

| 决策点 | 结论 | 原 spec 对应 |
|---|---|---|
| 推进方式 | 回测线与通知链路线两条腿并行 | Milestone 顺序重排（见 §5） |
| Docker/M5 | **延后**，本机 uv 裸跑 Python 进程；将来上 VPS 时补 Dockerfile | 修改 spec §0"部署"决策 |
| backtrader | **移除**，以自写 walk-forward 验证器替代（见 §4） | 修改 spec §0"回测框架"决策 |
| 数据源 | yfinance 升格为平级可切换数据源，Alpaca 注册后一行配置切换 | 扩展 spec §9 |
| 通知 | 新增 ConsoleNotifier，飞书代码完整实现，webhook 填入后自动切换 | 扩展 spec §8 |
| 凭证 | 用户在本版期间注册 Alpaca paper 与飞书机器人；代码只留配置位 | — |

## 2. 数据源抽象（datafeed/base.py）

```python
class DataSource(Protocol):
    def fetch_daily_bars(self, tickers: list[str], start: date, end: date) -> pd.DataFrame: ...
    def fetch_intraday_bars(self, tickers: list[str], interval: str = "5min") -> pd.DataFrame: ...
```

- 返回统一为 MultiIndex(ticker, ts) OHLCV DataFrame，ts 为 UTC
- 实现类：`YFinanceSource`（v0 默认）、`AlpacaSource`（同步实现，等 key）
- `settings.yaml` 中 `data_source: yfinance | alpaca` 选择实现；duckdb 存储层不感知来源，bars 表带 `source` 列（原 spec §9 已要求）
- **已知限制**：yfinance 盘中数据延迟约 15 分钟。日线/盘前动量策略不受影响；盘中突破信号在 yfinance 模式下卡片标注"延迟数据，仅供观察"，正式盘中信号以 Alpaca 接入为准

## 3. 通知抽象（notifier/base.py）

```python
class Notifier(Protocol):
    def send(self, card: Card) -> bool: ...
```

- `ConsoleNotifier`：终端渲染 + 追加写 `logs/signals.jsonl`，v0 开发验收用
- `FeishuNotifier`：按原 spec §8 完整实现（三种卡片模板、3 次指数退避重试、URL 从环境变量读取）
- 选择逻辑：`.env` 中存在非空 `FEISHU_WEBHOOK` 则用飞书，否则回落 Console；无需改代码
- dedup 与 ledger 位于 notifier 之外，与具体实现无关；heartbeat 告警在 Console 模式下降级为日志 + 终端输出

## 4. walk-forward 验证器（research/walkforward.py，替代 backtrader）

目的：对 vectorbt 全量向量化回测做事件驱动交叉验证，证明策略无未来函数。

- 原理：利用策略是纯函数这一约束，逐交易日循环，每次只喂"截至当日"的 bars 切片调用 `strategy.generate()`，收集信号序列并模拟持仓计算收益
- 对账断言：逐日喂数产生的信号序列 == 对全量历史一次性回放提取的信号序列；绩效指标与 vectorbt 结果在容差内一致
- 约 150 行、零新增依赖；它是测试工具而非通用回测引擎，不违背原 spec"不自研回测引擎"的本意
- 另在 tests/ 中为每个策略增加防未来函数单测：截断尾部数据后重跑，断言历史信号不变

## 5. Milestone 重排

| 阶段 | 内容 | 对应原 spec | 验收标准 |
|---|---|---|---|
| **A1** | 数据层：yfinance 拉 2 年日线入 duckdb、5min bar 轮询、数据校验 | M2 | `pytest tests/test_datafeed.py` 通过；duckdb 中 SPY 日线 ≥ 490 根 |
| **A2** | 两个策略 + 单测（含防未来函数测试） | M3 前半 | 策略 fixture 单测通过 |
| **B1** | notifier：Console + 飞书代码 + 卡片模板 + 去重 | M1 | `--test` 命令输出三种卡片（Console 渲染）；去重单测通过 |
| **B2** | engine + 台账 + scheduler，mock 时间跑通三个 job | M3 后半 | premarket/intraday/postmarket 三 job 本地跑通，信号落台账，4h 去重生效 |
| **A3** | vectorbt 参数扫描 + walk-forward 交叉验证，markdown 回测报告 | M4 | 报告生成于 research/reports/；回测 import src 下策略；对账断言通过 |
| **C1** | 凭证接入：指导用户注册 Alpaca paper + 飞书机器人，切换真实数据源与推送，端到端验证 | — | 飞书群收到真实卡片；Alpaca 数据入库 `source='alpaca'` |
| **C2**（延后） | Dockerfile + docker-compose + VPS 部署 | M5 | 本版不做 |

执行顺序：A1 → A2 → B1 → B2 → A3 → C1（A3 与 B2 无依赖可互换/穿插）。每阶段完成后停下向用户汇报，确认后继续（原 spec §11 要求保留）。

原 spec M4 的约束继续有效：**回测结果仅供用户评估，Agent 不得根据回测结果自行修改策略参数后宣称"已优化"。**

## 6. 本机运行方式

- uv 管理依赖、固定版本；`uv run quant-signal` 前台启动 scheduler，终端保持打开即持续运行
- 时间处理照原 spec：内部全 UTC + pandas_market_calendars 的 NYSE 日历；本机时区不影响正确性
- 日志 structlog JSON 落 `logs/`，按天轮转；`.gitignore` 覆盖 `.env`、`*.db`、`*.duckdb`、`logs/`

## 7. 测试范围

- 单测：dedup、calendar、两个策略（固定 OHLCV fixture）、防未来函数回归（原 spec §11 + 本文档 §4）
- 集成：mock 时间跑三个调度 job（B2 验收）
- 回测对账：walk-forward vs vectorbt（A3 验收）

## 8. 风险与限制

- yfinance 为非官方接口，可能被 Yahoo 限流或改版；仅作 v0 过渡与后续兜底，正式数据源为 Alpaca
- 本机运行意味着关机/休眠即停摆，v0 阶段可接受；7x24 需求等 C2 上 VPS 解决
- 原 spec §12 合规提示不变：系统仅生成参考信号，不构成投资建议，交易由用户人工决策
