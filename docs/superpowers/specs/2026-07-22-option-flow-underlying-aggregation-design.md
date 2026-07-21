# 期权热度按标的聚合展示设计

## 1. 目标与范围

将“美股期权热度”卡片中当前分开的 `CALL Top10` 和 `PUT Top10` 合并成一个“标的热度 Top10”。同一只股票或 ETF 的可见 Call/Put 合约聚合为一行，同时展示 Call/Put 可见量、比值、结构强度和 15 分钟可比增量。

本次只改造全市场期权热度卡。已按持仓标的计算 Put/Call 成交量比和 OI 比的“持仓期权情报”保持不变。

## 2. 数据边界

- 数据仅包含 Cboe C1/C2/BZX/EDGX 四市场榜单接口当前可见的合约。
- 每个市场每侧只暴露前 50 合约；聚合量是榜单可见量的近似下限，不是全 OPRA 总成交量。
- 用户可见文案统一使用“Call”、“Put”和“Call/Put 比”，不使用单字母 `C`、`P` 或 `C/P`。
- Call 成交不等于看涨，Put 成交不等于看跌。“占优”只描述可见量结构，不推断开平仓、主动买卖或最终方向。

## 3. 备选方案与决策

### 方案 A：只在展示层按标的聚合（采用）

- 保留逐合约台账、排名、数据质量门槛和异动告警。
- 卡片榜单使用当前快照的逐标的汇总视图。
- 风险最小，不会破坏已经积累的逐合约历史。

### 方案 B：告警也改为标的级

用户更容易理解，但会改变新进榜、排名跃升和成交加速的历史语义，需要重新收集数据并校准阈值，本次不采用。

### 方案 C：对每个标的拉取完整期权链

比值覆盖更完整，但全市场请求量大、延迟高，当前 Alpaca Indicative 也不能代表完整低延迟 OPRA。该方案只适合持仓或候选池情报，不用于本热度榜。

## 4. 领域模型

新增不落库的纯展示模型 `UnderlyingOptionFlow`：

- `underlying`
- `call_volume`
- `put_volume`
- `total_volume`
- `call_contract_count`
- `put_contract_count`
- `call_put_ratio`
- `dominance`
- `structure_label`
- `known_call_delta`
- `known_put_delta`
- `call_delta_partial`
- `put_delta_partial`

现有 `OptionFlowSnapshot.rows` 仍是唯一原始数据。新增纯函数根据快照生成聚合视图，不新增 SQLite 聚合表，不回写历史快照。

ETF 标记只能用 `option_flow.etf_roots` 中已知名单。名单外标的不强行标记为“股票”，避免将未收录 ETF 误分类。

## 5. 聚合与排名算法

1. 对当前快照按 `(underlying, side)` 分组，汇总已在四市场去重后的合约成交量。
2. `total_volume = call_volume + put_volume`。
3. 按 `(total_volume 降序, underlying 字典序)` 确定标的热度排名，取前 `option_flow.top_n`。
4. 不再先按到期日重排聚合榜；榜单是标的热度榜，不是到期日榜。
5. 当 Call 和 Put 均大于零时：

   `call_put_ratio = call_volume / put_volume`

   `dominance = abs(call_volume - put_volume) / total_volume`

6. 任一侧为零时，比值返回 `None`，不生成无穷大或零比值。

## 6. 结构标签

只有两侧均有可见量时才按 Call/Put 比生成结构标签：

| Call/Put 比 | 用户可见标签 |
|---|---|
| `>= 3.00` | Call 显著占优 |
| `>= 1.50 and < 3.00` | Call 占优 |
| `> 0.67 and < 1.50` | Call/Put 相对均衡 |
| `> 0.33 and <= 0.67` | Put 占优 |
| `<= 0.33` | Put 显著占优 |

只有 Call 可见时显示“仅 Call 可见”；只有 Put 可见时显示“仅 Put 可见”。两种情况都不显示结构强度百分比。

## 7. 15 分钟增量语义

由于上一轮和当前轮的前 50 可见合约集合可能变化，不能直接用“当前标的总量 - 上一轮标的总量”作为完整增量。

增量只对两轮都可见的同一 OCC 合约计算：

- `known_delta = sum(max(current.volume - previous.volume, 0))`。
- 当前轮存在上一轮不可见的新合约时，该侧标记为“部分可比”。
- 首次快照没有历史时显示“首次可见，无 15 分钟可比增量”。
- 某侧没有任何可比合约时显示“不可比”，不将新进榜合约的累计成交量当作 15 分钟增量。

## 8. 飞书卡片

保留“数据身份”、“异动聚焦”和“解释边界”。用单个“标的热度 Top10”替换原 `CALL Top10` 和 `PUT Top10` 两段。

用户确认的完整文案样例：

```text
#1 SPY · ETF
Call 1,250,000 / Put 820,000 · 总量 2,070,000
可见量 Call/Put 比 1.52 · Call 占优 21%
15 分钟增量 Call +85,000 / Put +32,000
```

存在新进榜合约的边界样例：

```text
15 分钟已知增量 Call +85,000（部分可比） / Put +32,000
```

单侧数据样例：

```text
Call 120,000 / Put 0 · 总量 120,000
可见量 Call/Put 比 - · 仅 Call 可见
```

卡片边界说明必须包含：

> 可见量来自 Cboe 四市场榜单样本，不是全 OPRA 总量。Call/Put 占优只表示成交量结构，不等于看涨/看跌，也不能证明新开仓或主动买卖。

## 9. 与现有告警的关系

- `detect_material_changes()` 继续按 OCC 合约比较新进榜、排名跃升、成交加速和 Volume/OI。
- “异动聚焦”继续显示具体合约，但将用户可见的 Call/Put 文案写全。
- 冷却时间、每日上限、outbox、过期取消和定时调度不变。
- 数据质量仍以每侧原始合约数和四市场覆盖为门槛，不因聚合后标的数不足 10 而误判缺数据。

## 10. 修改位置

- `src/quant_signal/options_flow.py`：新增标的级聚合模型和纯函数。
- `src/quant_signal/notifier/cards.py`：将双榜渲染改为单个标的榜，用户文案写全 Call/Put。
- `src/quant_signal/pipelines/option_flow.py`：向卡片传递 ETF 名单；保留现有逐合约补全与告警流程。
- `tests/test_options_flow.py`：覆盖聚合、排名、比值、强度和部分可比增量。
- `tests/test_option_flow_card.py`：覆盖新卡片结构、完整 Call/Put 文案、ETF 标记和数据边界。
- 现有 SQLite schema 和历史数据无需迁移。

## 11. 验收标准

1. 同一标的所有当前可见 Call/Put 合约只生成一条榜单记录。
2. 榜单按 Call+Put 总可见量降序排名，并使用标的代码稳定打破平局。
3. 比值和强度按本文公式计算；单侧为零时不生成虚假比值。
4. 15 分钟增量不把新进榜合约的累计量误当成增量。
5. 卡片中不再出现单字母 `C`、`P` 或 `C/P` 作为用户文案，统一写全为 Call、Put 和 Call/Put 比。
6. 异动告警、台账、数据质量、冷却和 outbox 现有测试继续通过。
7. 新增单元测试、全量 pytest、mypy 和一次真实只读 Cboe 卡片烟雾验证全部通过后才可发布。
