# 持仓期权情报层设计（期权市场作为持股决策的情报源）

## 0. 定位与为什么不做"期权交易推荐"

结论先行：**不做备兑开仓/现金担保 put/保护性 put 等期权交易策略推荐**，写进 README 非目标。

依据（台账实测，2026-07-11）：真实截图账户权益 ≈ $5,995，持仓市值 ≈ $4,244，
最大单票 SMH ≈ $1,227。期权按 100 股一张交易：

- 备兑 call 需要整 100 股（MU 100 股 ≈ $10,000+，实际仓位 ≈ $991）；
- 现金担保 put 一张需要 ~$10,000 抵押；
- 保护性 put 一张对应 100 股，对几股的持仓是数量级的过度对冲。

小账户唯一可执行的期权操作是裸买 call/put 投机（加杠杆赌方向），系统不应引导。

**要做的**：把期权市场当情报源——期权价格里的预期波动、IV 水平、Put/Call
仓位结构，对"财报前要不要减仓、持仓风险有没有异常定价"是真实输入，且不需要
用户交易任何期权。

## 1. 功能范围

### A. 每日"持仓期权情报卡"（新 job `option_intel`，16:40 ET）

覆盖标的 = 截图账户持仓（`observed_positions`）∪ 虚拟持仓（`holdings` 全部
strategy）中的 **USD 标的**（非 USD 如 `000660.KS` 无美股期权，直接排除）。

每个标的一段，字段（任一字段数据缺失则显示 `-`，不猜）：

1. **预期波动**：`±X% 到 YYYY-MM-DD`——ATM straddle 中间价 ÷ 现价。
   到期日选**最近的 DTE ≥ 7 天**的到期（跳过 0DTE/周内噪音）；若 30 天内有
   财报且该到期在财报**之前**，额外给一行"跨财报 ±X%（到财报后首个到期日）"。
2. **IV vs 已实现波动**：ATM IV（call/put 平均，年化）对比 20 日已实现波动率
   （duckdb 日线收益率 std × √252）。比值 ≥ 1.5 标注 `⚠IV偏高(事件定价)`。
3. **Put/Call 比**：60 天内全部合约的成交量比（当日）与未平仓比（OI）。
4. **大 OI 行权价**：60 天内 OI 最大的 3 个行权价（带 call/put 与到期日），
   作为市场仓位参考位。
5. **财报标注**：复用现有 `datafeed/earnings.py`，`N 天后财报` 徽标。

降级规则（逐标的 fail-open，与全市场榜单频道的 fail closed 定位不同——这是
展示层参考，不是数据承诺频道；但**缺数据只能显示缺失，绝不能编造**）：

- 无挂牌合约/接口 404 → 该标的显示"无可用期权数据"；
- 现价取不到 → 跳过该标的并注明；
- 单个指标算不出 → 该行显示 `-`；
- 整卡至少 1 个标的有数据才发送，否则只记日志不发卡。

### B. 全市场期权榜 × 持仓交叉标记

`option_flow_card` 的 CALL/PUT Top10 与异动聚焦里，underlying ∈ 持仓集合的行
前缀 `📌`。纯展示，不改变排名/异动/落库任何口径。持仓集合取法与 A 相同，
由 pipeline 注入（cards.py 保持纯函数）。

### C. 飞书 bot 指令 `期权 <ticker>`

- `期权`（无参数）：保持现状（读台账最新榜单）；
- `期权 MU` / `options MU`：**现场拉取**该标的期权链，回复单标的情报卡
  （字段同 A）。任意合法根代码（1-6 个 A-Z）都可查，不限于持仓；拉取失败
  回复错误文案。群里同样可用（不含账户信息，无需转私聊）。

### D. IV 历史落库（为将来的 IV 分位做数据积累）

新表 `option_intel_daily(session, symbol, atm_iv, rv20, expected_move_pct,
expected_move_expiry, pc_volume_ratio, pc_oi_ratio, spot, captured_at)`，
`UNIQUE(session, symbol)`。每日 job 写入（bot 现场查询不写）。maintenance 按
`option_intel.retention_days`（默认 400，ge=90）清理。本期**不做** IV 分位
展示（数据不足一年会误导），表里攒着。

## 2. 数据层（datafeed/alpaca_options.py 扩展）

复用现有 `AlpacaOptionEnricher` 的 httpx client/重试/凭据模式，新增：

1. **链快照**：`GET /v1beta1/options/snapshots/{underlying}`，参数
   `feed=indicative`、`expiration_date_lte=today+max_expiry_days`、
   `limit=1000` + `next_page_token` 分页。产出每合约：OCC symbol、
   latestQuote(bid/ask)、impliedVolatility、dailyBar volume。
2. **合约 OI**：`GET /v2/options/contracts?underlying_symbols=X&expiration_date_lte=...&limit=10000`
   分页，产出每合约 open_interest。
3. 现价：复用引擎现有 datafeed（最新日线 close 或 5 分钟价，取得到哪个用哪个）。

延迟边界照实标注：indicative feed（非 OPRA NBBO），约 15 分钟延迟。
分页上限保护：每标的最多 30 页（异常大链保护），超限如实降级为"数据不完整"。

## 3. 领域层（新模块 options_intel.py，纯函数）

```python
@dataclass(frozen=True)
class OptionChainContract:  # 数据层产出
    contract_symbol: str; underlying: str; side: OptionSide
    expiration: date; strike: Decimal
    bid: Decimal | None; ask: Decimal | None
    implied_volatility: float | None
    day_volume: int; open_interest: int | None

@dataclass(frozen=True)
class OptionIntel:  # 计算结果，卡片直接渲染
    symbol: str; spot: Decimal; session: date
    expected_move_pct: float | None; expected_move_expiry: date | None
    earnings_move_pct: float | None; earnings_move_expiry: date | None
    atm_iv: float | None; realized_vol_20d: float | None
    pc_volume_ratio: float | None; pc_oi_ratio: float | None
    top_oi_strikes: tuple[TopOIStrike, ...]
    earnings_date: date | None
    data_note: str | None  # 降级说明

def compute_intel(contracts, spot, session, bars_close, earnings_date, policy) -> OptionIntel
```

计算口径（全部确定性）：

- **ATM 对**：目标到期日内 |strike − spot| 最小的 call+put 各一张；straddle
  中间价 = call mid + put mid（mid = (bid+ask)/2，bid/ask 任一缺失该指标为 None；
  bid=0 且 ask>0 视为无效报价）。expected_move_pct = straddle_mid / spot。
- **到期日选择**：`expected_move` 取最近 DTE ≥ min_dte(7) 的到期；
  `earnings_move` 取财报日后（含当日）首个到期，仅当财报在 30 天内且
  两个到期不同才输出。
- **ATM IV** = ATM call/put 的 impliedVolatility 平均（一侧缺失用另一侧）。
- **RV20** = 最近 21 根日线收盘的 20 个对数收益 std × √252；不足 21 根为 None。
- **P/C 量比** = Σput day_volume / Σcall day_volume（call 侧为 0 则 None）；
  OI 比同理。范围限 `max_expiry_days`(60) 内合约。
- **大 OI 行权价**：按 open_interest 降序取前 top_oi_strikes(3)，
  tie-break OCC symbol 字典序。

## 4. 管道与接线

- `pipelines/option_intel.py`：`run(engine, now)`——取持仓集合 → 逐标的
  拉链+OI+现价 → `compute_intel` → 落库 `option_intel_daily` → 渲染
  `option_intel_card` → `engine.notifier.send`（直接发送，卡片自带日期，
  失败记日志次日再来；不复用 durable outbox——每日情报卡时效一天，YAGNI）。
- scheduler：`option_intel` job，16:40 ET（收盘榜 16:20 之后），NYSE 交易日
  门控 + 半日市用 `session_close_utc + 40min` 对齐（同 option_flow_close 模式，
  窗口 close+25min ~ close+70min，双时点 13:40/16:40 触发）。
- engine：新增 `option_chain_source`（数据层实例）与 `run_option_intel`。
- 配置 `option_intel`: `enabled`(默认 false)、`max_expiry_days`(60, 7-120)、
  `min_expected_move_dte`(7, 1-30)、`top_oi_strikes`(3, 1-5)、
  `iv_rv_warn_ratio`(1.5, >1)、`retention_days`(400, ge=90)、
  `max_tickers`(12, 1-30，持仓集合截断保护)。
- 依赖 `alpaca_key/secret`：未配置时 job 空跑并 log（enabled 但无凭据属配置
  错误，心跳可见）。

## 5. 卡片（notifier/cards.py 新增 option_intel_card）

标题 `🧭 持仓期权情报 · YYYY-MM-DD`，分段每标的：

```
**MU** $102.34 · 📅 18天后财报(08-01)
预期波动: ±6.2% 到 07-25 | 跨财报 ±11.4% 到 08-08
ATM IV 58% vs 20日实际波动 41% (1.4x)
Put/Call: 量比 0.71 · OI比 0.85
大OI: 100C(07-18) 95P(07-25) 110C(08-15)
```

尾注（诚实边界，风格与期权榜一致）：数据为 Alpaca indicative feed（约 15
分钟延迟，非 OPRA NBBO）；预期波动是期权市场定价的隐含区间，不是预测；
Put/Call 高低不等于看空/看多（可能是对冲/平仓/价差）；仅供观察，不构成建议。

bot 单标的卡：同一渲染函数，单段 + 尾注。

## 6. 测试

- 领域层：ATM 选择（等距 tie、无效报价、单侧缺失）、expected move、跨财报
  到期选择、RV20（不足 21 根）、P/C 比（零分母）、大 OI tie-break、全缺失
  时各字段 None。
- 数据层：分页拼接、过滤参数、404 → 空链、页数上限降级（respx/mock client，
  沿用现有 alpaca_options 测试模式）。
- 管道：持仓集合（观察∪虚拟、剔非 USD、截断 max_tickers）、单标的失败不拖累
  其余、全失败不发卡、落库幂等（UNIQUE 冲突覆盖）、无凭据空跑。
- 卡片：字段渲染、`-` 缺失、📌 交叉标记（flow 卡）。
- bot：`期权 MU` 路由带参、非法 ticker 文案、无参数走旧路径。
- 收尾：全量 pytest + mypy strict + 真实冒烟（拉一次 MU 渲染卡片）。

## 7. 非目标

- 不推荐任何期权交易（含备兑/保护/价差），README 非目标补充此条及原因；
- 不做 IV 历史分位展示（数据积累 ≥ 半年后另立方案）;
- 不做希腊字母组合风险（持仓里没有期权仓位，无意义）；
- 不改动现有期权榜频道的排名/异动/fail closed 语义。
