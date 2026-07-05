# 盘前早报按市场拆卡 + 多策略共振【重要】卡 设计

日期：2026-07-05
状态：已与用户确认，待实现

## 问题

当前 `Engine.run_premarket` 把所有策略当天推送的信号塞进**单张**"📋 盘前早报"卡片，
且有一个全局 `PREMARKET_REPORT_LIMIT = 5` 行上限，BUY 按动量排名优先排序。

真实数据上暴露两个问题：

1. **其他策略被挤没**：动量轮动每次调仓产出 7 条 BUY，直接占满 5 行名额，
   MACD/RSI/布林的信号虽然照常推送、照常进台账，却被挤进"还有 N 条未展示"，
   用户在卡片上看不到，误以为"只有动量轮动在跑"。
2. **混市场**：美股、港股、韩股信号混在一张卡里，用户希望分市场单独推送。

## 设计

`run_premarket` 里原来的"单张限行早报卡"换成**一组卡片**，按固定顺序依次推送，
跳过无信号的卡。

### 卡片顺序（跳过空卡）

1. 🔥 **【重要】多策略共振** —— 排最前，跨市场汇总
2. 📋 **早报·美股组** —— 美股优先
3. 📋 **早报·港股组**
4. 📋 **早报·韩股组**

### 市场归类

用 `settings.international_tickers`（ticker → 币种）判断：

- 币种 `HKD` → 港股组
- 币种 `KRW` → 韩股组
- 不在该表中（默认美元）→ 美股组

动量 SELL（"动量排名跌出前列，轮动调出"）按**被调出标的自己所属市场**归卡，
方向 SELL。

RSI/MACD/布林的 universe 含港股/韩股标的，因此这些技术信号也会按需出现在
港股/韩股卡里——不是只有动量。某天若 MACD 命中 7709.HK，港股卡就同时有
【动量轮动】和【MACD】两个小节。

### 每张市场卡内部结构

按策略分小节，固定顺序：【动量轮动】【MACD】【RSI回归】【布林带】。
每小节一个 Markdown 表格，空小节跳过。

- **不限行**，该市场当天全部信号都显示（删除 `PREMARKET_REPORT_LIMIT` 及
  `_select_report_rows` 的截断逻辑）。
- 列：`标的 | 方向 | 参考价 | 现价 | 原因`（小节头即策略名，不再单列"策略"列）。

### 【重要】多策略共振卡

把本次实际推送的信号（`result.to_push`）按 `(标的, 方向)` 分组：
若同一 `(标的, 方向)` 被 **≥2 个不同 strategy_id** 命中，则该标的进【重要】卡。

- 方向必须一致才算共振：都 BUY 或都 SELL；一买一卖不算共振。
- 列：`标的 | 方向 | 命中策略 | 参考价`，命中策略按固定策略顺序 join，如 `动量+MACD`。
- **参考价**取该标的在这些信号里的任一价（同一根 bar，价一致）。
- 共振标的**仍保留**在下面各自市场卡的每个策略小节里（用户选"保留重复"），
  各策略小节保持完整，能看到该策略的全部信号。
- 若无任何共振 → 不发【重要】卡。

## 代码结构

新增纯函数（放 `src/quant_signal/notifier/cards.py`，该模块已 import Signal）：

```python
def premarket_cards(
    signals: list[Signal],
    international_tickers: dict[str, str],
    live_prices: dict[str, float | None],
) -> list[Card]:
    """把当天推送的盘前信号渲染成有序的一组卡片：
    [可选的【重要】共振卡] + [美股组] + [港股组] + [韩股组]（跳过空卡）。"""
```

- 纯函数，无 IO，可独立单测：给定一组信号 + 归类表 + 现价表，断言产出哪些卡、
  每卡标题与 body 内容、顺序、共振判定。
- 策略标签与展示顺序（动量轮动/MACD/RSI回归/布林带）在 cards.py 内定义，
  不依赖 engine。

`Engine.run_premarket` 改为：

1. 照旧算 `all_signals`、`_dedup`、写台账、`set_holdings`。
2. 若 `result.to_push` 非空：按去重后的**唯一标的**抓现价（避免共振标的重复抓网络），
   得到 `live_prices: dict[str, float | None]`。
3. `for card in premarket_cards(result.to_push, settings.international_tickers, live_prices): notifier.send(card)`。
4. 删除 `_select_report_rows`、`PREMARKET_REPORT_LIMIT` 及内联的单卡拼装。

## 不受影响

- 盘中 `breakout_20d`（`run_intraday`）仍是每条信号单独 `signal_card`，不动。
- 去重（`apply_dedup`）、台账（`ledger.insert/set_holdings`）逻辑不动；卡片只渲染
  `result.to_push`。
- `run_watch_deviation`、`run_enrichment` 不动。

## 边界

- 本次完全无推送信号（`result.to_push` 为空）→ 一张卡都不发（同现状）。
- 现价抓取失败 → 该标的现价列显示 `-`（同现状 `_fetch_live_price` 返回 None）。

## 测试范围

`tests/test_cards.py`（新增，纯函数单测）：

1. 混合信号 → 产出【重要】+ 美股 + 港股 + 韩股，顺序正确。
2. 无共振 → 不产出【重要】卡。
3. 同标的同方向被 2 策略命中 → 进【重要】，命中策略拼接正确，且仍在各策略小节。
4. 同标的一买一卖（动量买 + 假设某策略卖）→ 不算共振。
5. 某市场无信号 → 不产出该市场卡。
6. 美股卡内部按策略分小节，空小节跳过。
7. 动量 SELL 归到被调出标的所属市场卡的动量小节。
8. 现价缺失 → 显示 `-`。

`tests/test_engine.py`：更新 `test_premarket_generates_rotation_and_report`，
断言从"单张卡 + 5 行上限"改为"多张市场卡、无行数上限"。

## 明确排除（YAGNI）

- 不做卡片内 ETF/个股再分小节（动量 reason 里已有"美股ETF组/个股组"标注，够用）。
- 不做共振的加权/评分，只做"≥2 策略同向"的布尔判定。
- 不改盘中/偏离/增强流程的卡片形态。
