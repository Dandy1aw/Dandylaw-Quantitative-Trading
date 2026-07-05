# 动量轮动按市场分组排名设计

> 本文档是对 [`quant-signal-spec.md`](../../../quant-signal-spec.md) 的增量扩展。
> 背景：momentum_rotation 目前把 universe 里所有标的（18只美股/ETF + 港股
> 7709.HK + 韩股 000660.KS）混在一起按 60 日动量统一排名取 top_n。实测
> 发现港股/韩股这类杠杆/热门标的动量经常极端（如 +386.7%/+173.7%），
> 会把美股标的全部挤出 top-N，不是我们想要的行为。

## 1. 问题

单一全局排名下，标的之间是"零和竞争"关系——只要有一个标的动量特别夸张，
无论是不是真的更值得买入，都会占掉其他市场的名额。港股/韩股目前只有各
1 只候选，一旦入选前列，等于把美股标的挤出去。

## 2. 设计

`MomentumRotation` 新增 `group_top_n: dict[str, int]` 构造参数，按**币种**
分组（复用已有的 `ticker_currency: dict[ticker, 币种]` 映射，不引入新概念）：

- 币种出现在 `group_top_n` 里的标的（如 `HKD`, `KRW`），各自独立成组，
  组内单独按动量排名，取该组配置的名额
- 其余标的（含全部美股/USD 计价标的，以及任何未在 `group_top_n` 里显式
  配置币种的标的）归入**默认组**，用现有的 `top_n` 参数
- 三组的候选池、排名、名额完全独立——港股组动量再夸张，也只影响港股
  自己的 1 个名额，不会挤占美股组的 3 个名额
- 若某组没有标的通过流动性门槛（`min_dollar_volume`），该组产出的 BUY
  信号数量就少于配置名额，**不会**把空出来的名额让给其他组

## 3. 配置（settings.yaml）

```yaml
strategies:
  momentum_rotation:
    lookback_days: 60
    top_n: 3              # 默认组（美股/其余标的）名额
    min_dollar_volume: 50000000
    group_top_n:
      HKD: 1               # 港股组名额
      KRW: 1                # 韩股组名额
```

`group_top_n` 默认为空字典——不配置就是原来的全局统一排名行为，对没有
国际标的的场景完全没有影响，现有测试不需要改动。

## 4. 仓位与展示

- **建议仓位**：均匀 `1/len(全部选中标的)`，不分市场（比如美股3+港股1+
  韩股1 共选中5只时，每只 20%）。选择均匀而不是按市场分配固定总仓位比例，
  是因为更简单、跟现有"1/top_n"逻辑保持一致，只是把分母从"配置的 top_n"
  换成"实际选中总数"
- **信号 reason 文案**：注明分组，如 `"60日动量 +386.7%，港股组第1"`，
  方便一眼看出这是哪个组选出来的，跟原来的 `"排名第N"` 区分开
- **Signal.extra["rank"]**：改成`全局按动量降序排的名次`（仅用于早报
  卡片 `_select_report_rows` 的展示排序，不影响谁入选——入选逻辑完全
  由分组决定）

## 5. 实现要点

```python
def generate(self, bars: pd.DataFrame) -> list[Signal]:
    # 动量/流动性计算部分不变（仍是逐标的用自身有效数据计算）
    ...
    eligible = {t: m for t, m in momentum.items() if dollar_vol_usd.get(t, 0.0) >= self.min_dollar_volume}

    groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for t, m in eligible.items():
        ccy = self.ticker_currency.get(t)
        key = ccy if ccy in self.group_top_n else "_default"
        groups[key].append((t, m))

    selected: list[tuple[str, float, str]] = []  # (ticker, momentum, group_label)
    for key, items in groups.items():
        n = self.group_top_n[key] if key != "_default" else self.top_n
        label = {"HKD": "港股组", "KRW": "韩股组"}.get(key, "美股组")
        top_in_group = sorted(items, key=lambda kv: kv[1], reverse=True)[:n]
        selected += [(t, m, label) for t, m in top_in_group]

    selected.sort(key=lambda x: x[1], reverse=True)  # 仅用于展示排名
    weight = round(1.0 / len(selected), 4) if selected else None
    return [
        Signal(..., reason=f"{lookback}日动量 {mom:+.1%}，{label}第{i}", ...,
               suggested_weight=weight, extra={"momentum_60d": mom, "rank": i})
        for i, (t, mom, label) in enumerate(selected, start=1)
    ]
```

## 6. 测试范围

- 分组隔离：构造一个 KRW 标的动量远超其他所有标的，验证它只占 KRW 组
  的名额，不影响默认组（美股）选出的标的
- 默认组回退：不传 `group_top_n`（或传空字典）时，行为与修改前完全一致
- 某组无合格候选：该组标的因流动性不达标被过滤时，总选中数相应减少，
  不从其他组补位
- 仓位/rank 计算：多组混合选中时，`suggested_weight` 正确按总数均分，
  `rank` 按全局动量降序编号

## 7. 明确排除的范围

- 不改变默认组内部的排名逻辑（仍是原来的"全体按动量取 top_n"）
- 不为分组仓位分配提供固定比例配置（如"美股60%+港股20%+韩股20%"）——
  用户已确认用均匀 1/总数，更复杂的固定比例分配留待后续需要时再做
- 不影响 RSI/MACD/布林带/breakout_20d 等其他策略——这些策略里每个标的
  独立判断信号，没有"排名竞争"的问题，本次改动范围只限 momentum_rotation
