# 趋势持有闸门 + 防御切换（阶段1：降回撤）设计

日期：2026-07-05
状态：待用户 review
目标读者：实现工程师（假设不了解本仓库量化背景）

## 背景与目标

动量轮动当前**永远满仓**在高 beta 半导体上，3 年回测夏普 2.5~2.9（很好），但**最大回撤 30~44%**（长历史含 2018/2022 熊市达 44%）——这是唯一痛点。

阶段1 目标：**在不显著牺牲收益的前提下压低最大回撤**。手段是给动量选出的标的加一道**绝对趋势闸门**，趋势失效的标的把仓位切到**防御 sleeve**（债/金/现金）。这不是新增选股策略，而是补一层组合层风险叠加。

> 阶段2（波动率目标仓位，提夏普）是**独立的下一轮**，本 spec 不含。

## 一、单标的趋势信号规则（用户 spec，已采纳）

对单一标的输出三态：ENTER / HOLD / EXIT（外加 WARN 预警），用于持有决策。

### 数据依赖
- 日线复权收盘价，至少 260 个交易日
- 无风险利率基准：**BIL**（美债 1-3 月 ETF，走 Alpaca 抓取），取其同期 12 个月收益作 rf
- 判定频率：**月末最后一个交易日收盘后**触发（与动量轮动同频，非每日）

### 指标定义
- `SMA200` = 最近 200 交易日收盘均值
- `price` = 当前月末收盘价
- `ret_12m` = price / close_252_days_ago − 1
- `rf_12m` = BIL 同期 12 个月收益
- `buffer` = 0.03（200日线缓冲带，回测可调 0.02~0.05）

### 信号条件
- `cond_ma`    = price > SMA200
- `cond_mom`   = ret_12m > rf_12m          （对比无风险利率，**不是 >0**）
- `cond_break` = price < SMA200 × (1 − buffer)   （有效跌破，带缓冲）
- `cond_rs`    = ret_12m(标的) > ret_12m(SPY)     （**仅个股**叠加，见第三节）

### 状态机（带迟滞，防震荡市锯齿）
`state ∈ {FLAT, LONG}`，每个标的独立维护：

```
if state == FLAT:
    if 入场条件成立:                     → ENTER, state = LONG
    else:                                → 维持 FLAT
if state == LONG:
    if cond_break AND (NOT cond_mom):    → EXIT, state = FLAT   # 双失效才退出
    elif cond_break OR (NOT cond_mom):   → HOLD + WARN 预警      # 单失效仅预警
    else:                                → HOLD
```

入场条件：ETF = `cond_ma AND cond_mom`；个股 = `cond_ma AND cond_mom AND cond_rs`。

### 输出 payload（每标的）
```json
{
  "symbol": "MU", "date": "2026-07-31", "signal": "HOLD",
  "price": 123.45, "sma200": 118.20, "dist_to_sma200_pct": 4.4,
  "ret_12m_pct": 32.1, "rf_12m_pct": 4.2, "state": "LONG"
}
```
signal ∈ {ENTER, HOLD, EXIT, WARN}。

## 二、组合层：防御切换（决策1 = B）

趋势信号只决定"是否持有"，本节决定"不持有时拿什么"。

**合成规则（每月末）：**
1. 动量轮动照常按分组独立排名选出 top-N（现有逻辑不变）。
2. 对每个被选中标的查其当前趋势 state：
   - `LONG`（ENTER 后未 EXIT）→ **按动量给的权重持有**。
   - `FLAT`（未入场或已 EXIT）→ 该仓位**释放**，权重转入防御 sleeve。
3. **防御 sleeve** = {BIL, TLT, GLD} 中按各自 `ret_12m − rf_12m` 最大的那个（绝对动量最强者）。所有释放出来的权重合并投入这**单一最强防御标的**。BIL 即现金地板：当 TLT/GLD 绝对动量都为负时 BIL 胜出 ≈ 持有现金/T-bill。
4. WARN 不改仓位，只推预警；EXIT 才真正把仓位切到防御。

**组合关系**：只有"动量选中 **AND** 趋势 LONG"才真正持有；两者任一不满足都不持有。防御 sleeve 自身不再叠趋势闸门（它已是按绝对动量选出的）。

## 三、个股相对强弱与非美元标的（决策2 = A + v1 币种处理）

- **美股 ETF**：入场 = `cond_ma AND cond_mom`。
- **美股个股**：入场 = `cond_ma AND cond_mom AND cond_rs`，`cond_rs` 基准统一用 **SPY**（v1；分板块基准如 SMH 留待后续，回测时可另跑对照）。ETF/个股用现有 `asset_type` 配置区分。
- **港股/韩股（非美元）**：`cond_mom`（比美元 rf）和 `cond_rs`（比 SPY）是**跨币种比较**，不干净。v1 **只用 `cond_ma`**（自身站上 200 日线，币种中性）作为其趋势闸门：`price > SMA200` 持有、有效跌破 `cond_break` 退出。绝对动量/相对强弱对非美元标的暂不适用。后续可改为统一换算成美元再比较。

## 四、架构与实现约束

- **纯函数、无隐藏状态、无未来函数**：state 不落库。趋势判定函数内部**从头把每个月末重放一遍状态机**推出当前 state（每个月末只用截至当日的数据算 cond_*）。这样回测与实盘走同一份代码、天然一致，且满足现有 `test_no_lookahead_truncation` 契约。
- **模块**：新增 `src/quant_signal/strategies/trend_gate.py`，核心是纯函数
  ```python
  def apply_trend_gate(
      picks: list[Signal],            # 动量轮动当月选出的标的
      bars: pd.DataFrame,             # 全历史日线（含 SPY/BIL/TLT/GLD 及所有 picks）
      asset_type: dict[str, str],
      international_tickers: dict[str, str],
      cfg: TrendGateConfig,
  ) -> tuple[list[Signal], list[TrendInfo]]:
      """返回 (最终持有信号[含防御替换], 每标的趋势详情[供卡片/预警])。"""
  ```
  `TrendGateConfig`：`ma_days=200, mom_days=252, buffer=0.03, benchmark="SPY", defensive=("BIL","TLT","GLD")`。
- **数据接入**：BIL 作为**只取数、不参与动量选股**的标的加入抓取集（Alpaca）。SPY/TLT/GLD 已在 universe。
- **配置**：`config.py` 增 `trend_gate: TrendGateSettings`（含上述参数 + `enabled: bool`）；`settings.yaml` 增对应段，`enabled` 初始 false（回测通过前不影响实盘）。

## 五、回测（上线前必做，go/no-go 闸门）

**先回测、达标才上线**。新增 `research/backtest_trend_gate.py`，复用 `backtest_momentum.py` 的 vectorbt 月频框架，把 `apply_trend_gate` 接到动量选股之后。

- **区间**：2016-01-01 至今（覆盖 2018Q4、2022 两轮大回撤，才测得出降回撤效果）。
- **对照组**（同一 universe、同一动量选股，只换叠加层）：
  1. 基线：动量轮动原样（无闸门）
  2. 仅 200 日线闸门（去掉 cond_mom/cond_rs）
  3. 仅绝对动量闸门（去掉 cond_ma）
  4. 完整闸门 + 防御切换（本设计）
  5. 参考：buy&hold SPY
- **参数扫描**：buffer ∈ {0.02, 0.03, 0.05}；口径确认（月末快照 vs 连续 N 日跌破）。
- **关注指标**：最大回撤、年化、夏普、年换手、震荡市（2015/2018）被"锯齿"打出的次数。
- **go 判据**：完整闸门相比基线**最大回撤明显下降**（目标从 ~44% 降到 30% 以下量级）**且年化不塌**（下降幅度可接受，由用户看数据拍板）。不达标就不上线，只保留研究结论。

## 六、上线接入（回测达标后才做）

- `engine.run_premarket`：动量 `generate()` 后调用 `apply_trend_gate`，用返回的最终持有信号替代原 picks 进入去重/台账/推送；防御标的作为普通 BUY 信号（reason 标"趋势闸门·防御切换"）。
- **卡片**：在分市场卡里加【趋势闸门】小节——列 WARN（持有但转弱，附 dist_to_sma200/ret_12m）与 EXIT→防御的切换记录；防御 sleeve 持仓单独一行显示。沿用现有 `premarket_cards` 结构，不新造推送流程。
- `enabled` 置 true。

## 七、明确排除（YAGNI）

- 阶段2 波动率目标仓位、多因子（价值/质量，缺基本面数据做不了）——不在本 spec。
- 财报 gap 风险不做兜底（已知不防护）。
- 分板块 RS 基准、非美元标的换算成美元比较——留待 v2。
- 已知失效场景（V 型反转两头挨打、长期横盘鞭打损耗）——接受，不额外处理。

## 八、已知失效场景（诚实记录）
- V 型反转：跌破退出后又急拉，两头挨打。
- 长期横盘：反复触发 buffer 边缘，鞭打损耗（迟滞状态机已尽量缓解，非根治）。
- 财报跳空：规则基于月末价，防不了盘中/隔夜 gap。
