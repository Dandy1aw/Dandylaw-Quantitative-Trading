# 多市场实时监控设计

> 本文档是对 [`quant-signal-spec.md`](../../../quant-signal-spec.md) 与
> [`2026-07-04-quant-signal-v0-local-design.md`](2026-07-04-quant-signal-v0-local-design.md)
> 的增量扩展。背景：v0 上线后接入了 Alpaca 真实凭证和用户的 9 只真实持仓
> （含港股 7709.HK、韩股 000660.KS），momentum_rotation 的 universe 从原始
> 5 只 ETF 扩容到 18 个美股/ETF + 2 个国际标的。用户反馈两点：

1. 现有调度只在 `08:00 ET`（约北京时间 20-21 点）跑一次盘前推荐，这个
   时间点对港股/韩股（亚洲时段交易）很不友好——用户想要的"结合最新股价
   给推荐买入价"在这个时间点用的是隔夜的收盘价，参考意义有限。
2. 持仓标的目前完全没有盘中监控——现有的 `breakout_20d` 5 分钟级策略
   只覆盖 `watchlist`（NVDA/TSLA/AAPL/MSFT/AMD），跟用户的真实持仓
   （momentum_rotation universe）无关。

## 1. 三次每日推送

在现有 `08:00 ET` premarket job（NYSE 日历门控，保留不变）之外，新增两个
调度时间点，同样调用 `Engine.run_premarket()` 重新计算动量排名并推送早报：

| 时间 | UTC | 说明 |
|---|---|---|
| 08:00 ET（保留） | 视夏令时 12:00/13:00 UTC | 现有 job，NYSE 日历门控 |
| 08:00 北京时间（新增） | 00:00 UTC | 亚洲盘前，工作日门控 |
| 15:30 北京时间（新增） | 07:30 UTC | 港股/韩股收盘前后，工作日门控 |

**门控差异是关键**：两个新 job **不用 NYSE 交易日历**门控（`calendar.is_trading_day`
只覆盖 NYSE），而是简单判断"是否工作日"（`now.weekday() < 5`）。原因：
这两个 job 存在的意义就是服务美股休市但港股/韩股照常交易的场景（如美股
独立日调休），如果沿用 NYSE 日历门控会在这类日子被错误跳过，正好抵消
新增它们的目的。

三次推送共用同一套 `run_premarket` 逻辑，动量计算天然按各标的自身有效
数据取值（已在此前的 bugfix 中修复），能正确处理"美股休市、亚洲市场
正常交易"这类日历错位场景，无需额外改造策略层。

## 2. 推送时结合最新股价

`run_premarket` 生成的报告卡片目前展示的"价格"是策略计算用的参考收盘价
（可能是隔夜收盘）。现在追加一步：报告卡片渲染前，对卡片里出现的每个
标的，用已有的 `DataSource.fetch_intraday_bars`（5 分钟 bar，US 走
Alpaca、国际标的走 yfinance，复用现有分流逻辑）额外抓一次最新 5 分钟
收盘价，作为"现价"单独展示，与策略参考价并列，不覆盖参考价本身（参考
价仍用于动量计算和台账记录，现价只是展示层的补充信息）。

不新增任何数据源接口——这一步复用的正是板块三（5 分钟监控）本来就要
调用的同一段抓取逻辑，两者共享实现。

## 3. 持仓标的 5 分钟级偏离监控

新增一个轻量级监控模块，**不是** `Strategy` 子类（这个功能是纯粹的实时
展示型提醒，不参与回测，没有必要满足"数据进信号出"的策略契约），放在
`src/quant_signal/watch_monitor.py`：

```python
def check_deviations(
    ref_prices: dict[str, float],   # ticker -> 参考价（当天最近一次动量信号价）
    live_prices: dict[str, float],  # ticker -> 最新5分钟收盘价
    threshold: float = 0.02,        # ±2%
) -> list[Signal]
```

纯函数，输入两个价格字典和阈值，输出偏离超阈值的 `Signal` 列表（复用
`strategies/base.py` 的 `Signal`/`Direction`：向上偏离用 `Direction.BUY`，
向下用 `Direction.SELL`，`strategy_id="price_deviation"`，`reason` 里写明
参考价、现价、偏离幅度）。

**监控范围**：当前持仓（`ledger.get_holdings("momentum_rotation")`）∪ 今日
BUY 目标（当天最近一次 `run_premarket` 产生的 BUY 信号标的），通常
2-5 只。范围之外的候选标的（WATCH 状态）不监控——它们没有"当天推荐价"
可比较，纳入需要另定基准，本期不做。

**参考价来源**：`SignalLedger` 新增一个只读查询
`latest_signal_price(strategy_id: str, ticker: str, since: datetime) -> float | None`，
取当天该标的最近一次 `momentum_rotation` 信号的 `price` 字段。若当天该
标的还没有任何信号（返回 `None`），`Engine.run_watch_deviation` 直接跳过
这只标的本次检查，不产生提醒也不报错——等下一次 `run_premarket` 产生
参考价后再纳入监控。

**去重**：复用现有 `apply_dedup`/`dedup_hours`（4 小时），因为
`dedup_key = (ticker, direction, strategy_id)` 里 `strategy_id="price_deviation"`
与 `momentum_rotation`、`breakout_20d` 天然不冲突，同一标的的偏离提醒和
动量轮动信号互不干扰、各自独立去重。

**调度**：新开一个 `watch_deviation` job，工作日 `00:00–21:00 UTC` 每 5
分钟跑一次（覆盖亚洲 01:00-08:00 UTC 和美股 13:30-21:00 UTC 两个交易
时段，中间留一段无谓请求换取不用维护 HKEX/KRX 交易日历的复杂度）。
`Engine` 新增方法 `run_watch_deviation(now)`：取监控范围 → 抓最新价 →
调用 `check_deviations` → 走 dedup → 落台账 → 推送。

## 4. 不做的事（明确排除）

- 不新增真正的"实时报价流"（WebSocket/tick级），仍是 5 分钟轮询颗粒度，
  符合原 spec"不做 tick/秒级高频"的约束
- 不监控 WATCH 状态的候选标的
- 不新建港股/韩股专属交易日历，偏离监控用固定时间窗口兜底
- 不改变 momentum_rotation/breakout_20d 两个既有策略的核心逻辑

## 5. 测试范围

- `watch_monitor.check_deviations`：固定价格字典的单测，覆盖正偏离/负
  偏离/未超阈值/边界值
- `SignalLedger.latest_signal_price`：单测覆盖有信号/无信号/多次信号取
  最近一次
- `scheduler.py`：新增两个 job 的注册断言，门控函数（工作日 vs NYSE
  日历）的单测
- `Engine.run_watch_deviation`：mock 数据源，验证监控范围计算、去重、
  推送链路
