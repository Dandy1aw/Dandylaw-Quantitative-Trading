# 趋势持有闸门 + 防御切换 + 推荐卖出价（阶段1：降回撤）设计

日期：2026-07-05
状态：待用户 review（第2版：加入周度检查 + 推荐卖出价）
目标读者：实现工程师（假设不了解本仓库量化背景）

## 背景与目标

动量轮动当前**永远满仓**在高 beta 半导体上，3 年回测夏普 2.5~2.9（很好），但**最大回撤 30~44%**（长历史含 2018/2022 熊市达 44%）——这是唯一痛点。

阶段1 目标：**在不显著牺牲收益的前提下压低最大回撤**，并给持仓一个**参考卖出价**做心理预期。手段是给动量选出的标的加一道**绝对趋势闸门**（趋势失效切防御 sleeve），并按策略类型输出卖出价。这不是新增选股策略，而是补一层组合层风险叠加。

> 阶段2（波动率目标仓位，提夏普）是**独立的下一轮**，本 spec 不含。

## 〇、调仓与检查频率：慢进快出（用户决策）

- **动量选股**：仍**月末**最后一个交易日触发（换手可控，现有逻辑不变）。
- **趋势闸门 / 止损检查**：改为**每周五收盘后**触发。
- 效果：**进场慢（月度）、离场快（周度）**——持仓跌破退出价当周就走，不用等到月末，回撤控制更好，换手只小幅增加。
- 组合含义：每月末动量选出候选并入场；月中每周五只做"退出检查"（可把已持仓切防御），**不月中新增动量仓**。

## 一、单标的趋势信号规则（用户 spec，已采纳）

对单一标的输出三态：ENTER / HOLD / EXIT（外加 WARN 预警），用于持有决策。

### 数据依赖
- 日线复权收盘价，至少 260 个交易日
- 无风险利率基准：**BIL**（美债 1-3 月 ETF，走 Alpaca 抓取），取其同期 12 个月收益作 rf
- 触发：趋势判定每**周五收盘后**跑（见第〇节）

### 指标定义
- `SMA200` = 最近 200 交易日收盘均值
- `price` = 当前（周五）收盘价
- `ret_12m` = price / close_252_days_ago − 1
- `rf_12m` = BIL 同期 12 个月收益
- `buffer` = 0.03（200日线缓冲带，回测可调 0.02~0.05）

### 信号条件
- `cond_ma`    = price > SMA200
- `cond_mom`   = ret_12m > rf_12m          （对比无风险利率，**不是 >0**）
- `cond_break` = price < SMA200 × (1 − buffer)   （有效跌破，带缓冲）
- `cond_rs`    = ret_12m(标的) > ret_12m(SPY)     （**仅美股个股**叠加，见第三节）

### 状态机（带迟滞，防震荡市锯齿）
`state ∈ {FLAT, LONG}`，每个标的独立维护，**每周五**推进一次：

```
if state == FLAT:
    if 入场条件成立:                     → ENTER, state = LONG
    else:                                → 维持 FLAT
if state == LONG:
    if cond_break AND (NOT cond_mom):    → EXIT, state = FLAT   # 双失效才退出
    elif cond_break OR (NOT cond_mom):   → HOLD + WARN 预警      # 单失效仅预警
    else:                                → HOLD
```

入场条件：ETF = `cond_ma AND cond_mom`；美股个股 = `cond_ma AND cond_mom AND cond_rs`。

> 注意迟滞与"慢进快出"的关系：state 的 FLAT→LONG（ENTER）只影响"该标的趋势是否健康"；
> 真正把仓位买进组合仍由**月末动量选股**决定。周五的 LONG→FLAT（EXIT）则会**当周**把已持仓切防御。

### 输出 payload（每标的）
```json
{
  "symbol": "MU", "date": "2026-07-31", "signal": "HOLD",
  "price": 123.45, "sma200": 118.20, "dist_to_sma200_pct": 4.4,
  "ret_12m_pct": 32.1, "rf_12m_pct": 4.2, "state": "LONG",
  "sell_ref": 114.65
}
```
signal ∈ {ENTER, HOLD, EXIT, WARN}；`sell_ref` = 参考卖出价（见第三节）。

## 二、组合层：防御切换（决策1 = B）

趋势信号只决定"是否持有"，本节决定"不持有时拿什么"。

**合成规则：**
1. **月末**：动量轮动照常按分组独立排名选出 top-N（现有逻辑不变）。
2. 对每个被选中标的查其当前趋势 state：
   - `LONG`（ENTER 后未 EXIT）→ **按动量给的权重持有**。
   - `FLAT`（未入场或已 EXIT）→ 该仓位**释放**，权重转入防御 sleeve。
3. **月中每周五**：只对已持仓做退出检查，state 变 FLAT 的持仓当周切防御；不新增动量仓。
4. **防御 sleeve** = {BIL, TLT, GLD} 中按各自 `ret_12m − rf_12m` 最大者（绝对动量最强）。所有释放权重合并投入这**单一最强防御标的**。BIL 即现金地板：TLT/GLD 绝对动量都为负时 BIL 胜出 ≈ 持现金/T-bill。
5. WARN 不改仓位，只推预警；EXIT 才真正切防御。

**组合关系**：只有"动量选中 **AND** 趋势 LONG"才真正持有。防御 sleeve 自身不再叠趋势闸门。

## 三、推荐卖出价（新增输出，按策略类型分类，用户决策）

业界共识：趋势单不设固定止盈（封顶利润），只给移动止损；均值回归单才给止盈目标。故按策略类型分两类：

### 趋势型策略（momentum_rotation, macd_cross）——只给"离场下沿"
- `sell_ref` = `SMA200 × (1 − buffer)`（= 趋势闸门 `cond_break` 触发价，已在算）。
- 含义：跌破此价趋势失效、该离场。随价格/均线上移而**动态抬升**（移动止损效果）。
- 不给止盈目标——让利润奔跑，退出交给趋势闸门。

### 均值回归型策略（rsi_reversion, bollinger_breakout）——给"止盈目标 + 止损"
- `take_profit` = **中轨**（20 日均线 SMA20）；布林可选上轨。回归均值即止盈。
- `stop_loss` = `entry − 1.5 × ATR(14)`（止损放极值之外，防正常波动误杀）。
- 需新增 **ATR(14)** 计算（日线真实波幅均值）。

> breakout_20d（盘中）归趋势型，`sell_ref = SMA200×(1−buffer)`；本阶段先不改盘中卡，留待接入时统一。

## 四、个股相对强弱与非美元标的（决策2 = A + v1 币种处理）

- **美股 ETF**：入场 = `cond_ma AND cond_mom`。
- **美股个股**：入场 = `cond_ma AND cond_mom AND cond_rs`，`cond_rs` 基准统一用 **SPY**（v1；分板块基准如 SMH 留待后续，回测可另跑对照）。ETF/个股用现有 `asset_type` 区分。
- **港股/韩股（非美元）**：`cond_mom`（比美元 rf）与 `cond_rs`（比 SPY）是**跨币种比较**，不干净。v1 **只用 `cond_ma`**（自身站上 200 日线，币种中性）：`price > SMA200` 持有、有效跌破退出；`sell_ref` 同样用 `SMA200×(1−buffer)`。绝对动量/相对强弱对非美元标的暂不适用。后续可改为统一换算成美元再比较。

## 五、架构与实现约束

- **纯函数、无隐藏状态、无未来函数**：state 不落库。趋势判定函数内部**从头把每个周五重放一遍状态机**推出当前 state（每个周五只用截至当日数据算 cond_*）。回测与实盘走同一份代码、天然一致，满足现有 `test_no_lookahead_truncation` 契约。
- **模块**：新增 `src/quant_signal/strategies/trend_gate.py`，核心纯函数
  ```python
  def apply_trend_gate(
      picks: list[Signal],            # 动量轮动当月选出的标的
      bars: pd.DataFrame,             # 全历史日线（含 SPY/BIL/TLT/GLD 及所有 picks）
      asset_type: dict[str, str],
      international_tickers: dict[str, str],
      cfg: TrendGateConfig,
  ) -> tuple[list[Signal], list[TrendInfo]]:
      """返回 (最终持有信号[含防御替换], 每标的趋势详情[state/sell_ref，供卡片/预警])。"""
  ```
  `TrendGateConfig`：`ma_days=200, mom_days=252, buffer=0.03, benchmark="SPY", defensive=("BIL","TLT","GLD")`。
  卖出价（第三节）由 `trend_gate` 出趋势型 `sell_ref`；均值回归的 `take_profit/stop_loss` 由各自策略（rsi/bollinger）用新增 ATR 工具算，附在 Signal 上。
- **数据接入**：BIL 作为**只取数、不参与动量选股**的标的加入抓取集（Alpaca）。SPY/TLT/GLD 已在 universe。
- **配置**：`config.py` 增 `trend_gate: TrendGateSettings`（上述参数 + `enabled: bool`）；`settings.yaml` 增对应段，`enabled` 初始 false（回测通过前不影响实盘）。
- **调度**：`scheduler` 增每周五收盘后的"趋势/退出检查"job（月末那次由既有盘前 job 承载动量选股 + 首次入场）。

## 六、回测（上线前必做，go/no-go 闸门）

**先回测、达标才上线**。新增 `research/backtest_trend_gate.py`，复用 `backtest_momentum.py` 的 vectorbt 框架：**月末动量选股 + 每周五评估趋势闸门退出**（非纯月频，需按周评估退出）。

- **区间**：2016-01-01 至今（覆盖 2018Q4、2022 两轮大回撤）。
- **对照组**（同 universe、同动量选股，只换叠加层）：
  1. 基线：动量轮动原样（无闸门）
  2. 仅 200 日线闸门
  3. 仅绝对动量闸门
  4. 完整闸门 + 防御切换（本设计，周度退出）
  5. 参考：buy&hold SPY
- **参数扫描**：buffer ∈ {0.02, 0.03, 0.05}；退出频率对照（月度退出 vs 周度退出，量化"快出"到底值不值那点换手）。
- **关注指标**：最大回撤、年化、夏普、年换手、震荡市（2015/2018）被"锯齿"打出的次数。
- **go 判据**：完整闸门相比基线**最大回撤明显下降**（目标 ~44% → 30% 以下量级）**且年化不塌**（幅度可接受，用户看数据拍板）。不达标不上线，只留研究结论。

## 七、上线接入（回测达标后才做）

- `engine`：月末盘前 job 里，动量 `generate()` 后调 `apply_trend_gate` 得最终持有（含防御替换）；新增每周五 job 复用同一 `apply_trend_gate` 对当前持仓做退出检查。最终信号进去重/台账/推送；防御标的作普通 BUY（reason 标"趋势闸门·防御切换"）。
- **卡片**：
  - 动量/趋势型 BUY 行加"参考卖出价"列（`sell_ref`）。
  - RSI/布林 BUY 行加"止盈目标 / 止损"。
  - 加【趋势闸门】小节：列 WARN（持有转弱，附 dist_to_sma200/ret_12m）与 EXIT→防御切换记录；防御 sleeve 持仓单独一行。
  - 沿用现有 `premarket_cards` 结构，不新造推送流程。
- `enabled` 置 true。

## 八、明确排除（YAGNI）

- 阶段2 波动率目标仓位、多因子（缺基本面）——不在本 spec。
- 财报 gap 风险不做兜底（已知不防护）。
- 分板块 RS 基准、非美元标的换算成美元、盘中卡改造——留待 v2。
- 已知失效场景（V 型反转两头挨打、长期横盘鞭打）——接受，不额外处理。

## 九、已知失效场景（诚实记录）
- V 型反转：跌破退出后又急拉，两头挨打。
- 长期横盘：反复触边 buffer，鞭打损耗（迟滞状态机缓解非根治）。
- 财报跳空：规则基于收盘价，防不了盘中/隔夜 gap。
