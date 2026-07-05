# 趋势闸门 阶段A+B 实现计划（构建 + 回测 go/no-go）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现纯函数趋势闸门（绝对趋势状态机 + 防御切换 + 趋势型卖出价），并用带对照组的 vectorbt 回测证明它能降低动量组合最大回撤。

**Architecture:** 新增 `strategies/trend_gate.py`：`trend_state()` 逐周五重放迟滞状态机推导单标的 state/sell_ref（无未来函数），`apply_trend_gate()` 把动量选出的 picks 中趋势失效的仓位切到防御 sleeve。回测脚本复用 `backtest_momentum.py` 的框架，月末选股 + 每周五评估退出，跑 5 组对照。**阶段B 回测是人工 go/no-go 闸门：达标才写阶段C（实盘接入）计划。**

**Tech Stack:** Python 3.12, pandas, vectorbt, pytest, mypy。

## Global Constraints

- 参考 spec：`docs/superpowers/specs/2026-07-05-trend-hold-gate-design.md`
- `src/` 全程 TDD：先失败测试→确认红→最小实现→确认绿→`uv run mypy src/`→提交。
- 测试 `uv run pytest`，类型 `uv run mypy src/`。`research/` 脚本不强制 TDD，靠真实数据跑通验证。
- 提交信息结尾附：
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn
  ```
- 状态机迟滞：入场 `cond_ma AND cond_mom (AND cond_rs 仅美股个股)`；LONG 下 `cond_break AND NOT cond_mom`→EXIT、`cond_break OR NOT cond_mom`→WARN、否则 HOLD。
- `cond_mom = ret_12m > rf_12m`（比 BIL 的 rf，非 >0）；`buffer=0.03`；`ma_days=200`；`mom_days=252`。
- 非美元标的（在 `international_tickers` 里）**只用 `cond_ma`**（跨币种不比 mom/rs）。
- 趋势型 `sell_ref = SMA200 × (1 − buffer)`。
- 防御 sleeve = {BIL,TLT,GLD} 按 `ret_12m − rf_12m` 最大者；释放权重全投该单一标的。
- **无未来函数**：所有指标 rolling 只用过去数据；state 由重放推导、不落库。

---

### Task 1: `trend_state` 单标的趋势状态机（纯函数）

**Files:**
- Create: `src/quant_signal/strategies/trend_gate.py`
- Test: `tests/test_trend_gate.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class TrendGateConfig:
      ma_days: int = 200
      mom_days: int = 252
      buffer: float = 0.03
      benchmark: str = "SPY"
      defensive: tuple[str, ...] = ("BIL", "TLT", "GLD")

  @dataclass(frozen=True)
  class TrendInfo:
      ticker: str
      state: str      # "LONG" | "FLAT"
      signal: str     # "ENTER" | "HOLD" | "EXIT" | "WARN"
      price: float
      sma200: float
      sell_ref: float
      ret_12m: float
      rf_12m: float

  def trend_state(
      ticker: str,
      close: pd.Series,          # 单标的日线收盘，DatetimeIndex 升序
      benchmark_close: pd.Series,
      rf_close: pd.Series,       # BIL 日线收盘
      is_stock: bool,            # True 则叠 cond_rs
      is_usd: bool,              # False 则只用 cond_ma
      cfg: TrendGateConfig,
      as_of: pd.Timestamp,
  ) -> TrendInfo | None          # 数据不足返回 None
  ```

- [ ] **Step 1: 写失败测试**

创建 `tests/test_trend_gate.py`：

```python
import numpy as np
import pandas as pd

from quant_signal.strategies.trend_gate import TrendGateConfig, trend_state

CFG = TrendGateConfig()


def _series(vals, start="2022-01-03"):
    idx = pd.bdate_range(start, periods=len(vals), tz="UTC")
    return pd.Series(vals, index=idx)


def _flat_rf(n, start="2022-01-03"):
    # BIL 近似常数（rf≈0），价格微涨保证 rf_12m 很小
    return _series(np.linspace(100.0, 101.0, n), start)


def test_uptrend_enters_long():
    n = 320
    close = _series(100.0 * np.cumprod(np.full(n, 1.004)))   # 稳定上涨
    bench = _series(100.0 * np.cumprod(np.full(n, 1.001)))   # 大盘涨得慢 → cond_rs 成立
    rf = _flat_rf(n)
    info = trend_state("MU", close, bench, rf, is_stock=True, is_usd=True,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "LONG"
    assert info.signal in ("ENTER", "HOLD")
    assert info.sell_ref == info.sma200 * (1 - CFG.buffer)


def test_break_and_mom_fail_exits():
    n = 320
    up = 100.0 * np.cumprod(np.full(220, 1.004))
    down = up[-1] * np.cumprod(np.full(100, 0.985))          # 后段大跌，跌破200线且12m转负
    close = _series(np.concatenate([up, down]))
    bench = _series(100.0 * np.cumprod(np.full(n, 1.0005)))
    rf = _flat_rf(n)
    info = trend_state("MU", close, bench, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "FLAT"          # 双失效已退出


def test_non_usd_uses_only_ma():
    n = 320
    close = _series(100.0 * np.cumprod(np.full(n, 1.004)))
    # benchmark/rf 传入但非美元标的应被忽略：给个会让 cond_rs 失败的强基准也不影响
    bench = _series(100.0 * np.cumprod(np.full(n, 1.01)))
    rf = _flat_rf(n)
    info = trend_state("7709.HK", close, bench, rf, is_stock=True, is_usd=False,
                       cfg=CFG, as_of=close.index[-1])
    assert info is not None
    assert info.state == "LONG"          # 只看站上200线，cond_rs 被强基准打败也不影响


def test_insufficient_history_returns_none():
    close = _series(100.0 * np.cumprod(np.full(50, 1.004)))
    rf = _flat_rf(50)
    assert trend_state("MU", close, close, rf, is_stock=False, is_usd=True,
                       cfg=CFG, as_of=close.index[-1]) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_trend_gate.py -q`
Expected: FAIL（`ImportError: cannot import name 'trend_state'`）

- [ ] **Step 3: 最小实现**

创建 `src/quant_signal/strategies/trend_gate.py`：

```python
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TrendGateConfig:
    ma_days: int = 200
    mom_days: int = 252
    buffer: float = 0.03
    benchmark: str = "SPY"
    defensive: tuple[str, ...] = ("BIL", "TLT", "GLD")


@dataclass(frozen=True)
class TrendInfo:
    ticker: str
    state: str
    signal: str
    price: float
    sma200: float
    sell_ref: float
    ret_12m: float
    rf_12m: float


def _weekly_marks(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """每个自然周最后一个交易日（近似周五收盘检查点）。"""
    s = pd.Series(index, index=index)
    return list(s.groupby(index.to_period("W")).last())


def trend_state(
    ticker: str,
    close: pd.Series,
    benchmark_close: pd.Series,
    rf_close: pd.Series,
    is_stock: bool,
    is_usd: bool,
    cfg: TrendGateConfig,
    as_of: pd.Timestamp,
) -> TrendInfo | None:
    close = close.dropna().sort_index()
    close = close[close.index <= as_of]
    if len(close) < cfg.ma_days + 1:
        return None

    sma = close.rolling(cfg.ma_days).mean()
    ret = close / close.shift(cfg.mom_days) - 1.0
    bench_ret = (benchmark_close / benchmark_close.shift(cfg.mom_days) - 1.0).reindex(
        close.index
    ).ffill()
    rf_ret = (rf_close / rf_close.shift(cfg.mom_days) - 1.0).reindex(close.index).ffill()

    state = "FLAT"
    signal = "HOLD"
    last: tuple[float, float, float, float] | None = None
    for ts in _weekly_marks(close.index):
        price = float(close.loc[ts])
        s = float(sma.loc[ts]) if not pd.isna(sma.loc[ts]) else float("nan")
        r = float(ret.loc[ts]) if not pd.isna(ret.loc[ts]) else float("nan")
        if pd.isna(s) or pd.isna(r):
            continue
        rf = float(rf_ret.loc[ts]) if not pd.isna(rf_ret.loc[ts]) else 0.0
        cond_ma = price > s
        cond_break = price < s * (1 - cfg.buffer)
        if is_usd:
            cond_mom = r > rf
            b = float(bench_ret.loc[ts]) if not pd.isna(bench_ret.loc[ts]) else 0.0
            cond_rs = (r > b) if is_stock else True
            enter = cond_ma and cond_mom and cond_rs
            do_exit = cond_break and (not cond_mom)
            warn = cond_break or (not cond_mom)
        else:
            cond_mom = True
            enter = cond_ma
            do_exit = cond_break
            warn = cond_break

        if state == "FLAT":
            if enter:
                state, signal = "LONG", "ENTER"
            else:
                signal = "HOLD"
        else:  # LONG
            if do_exit:
                state, signal = "FLAT", "EXIT"
            elif warn:
                signal = "WARN"
            else:
                signal = "HOLD"
        last = (price, s, r, rf)

    if last is None:
        return None
    price, s, r, rf = last
    return TrendInfo(
        ticker=ticker, state=state, signal=signal, price=price, sma200=s,
        sell_ref=s * (1 - cfg.buffer), ret_12m=r, rf_12m=rf,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_trend_gate.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 类型检查**

Run: `uv run mypy src/`
Expected: Success

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/strategies/trend_gate.py tests/test_trend_gate.py
git commit -m "feat: trend_gate.trend_state 单标的趋势状态机(周度重放迟滞,无未来函数)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn"
```

---

### Task 2: `apply_trend_gate` 组合层防御切换

**Files:**
- Modify: `src/quant_signal/strategies/trend_gate.py`
- Test: `tests/test_trend_gate.py`

**Interfaces:**
- Consumes: `trend_state`, `TrendGateConfig`, `TrendInfo`（Task 1）；`Signal`/`Direction`（`quant_signal.strategies.base`）。
- Produces:
  ```python
  def apply_trend_gate(
      picks: list[Signal],
      bars: pd.DataFrame,                    # MultiIndex(ticker, ts) OHLCV，全历史
      asset_type: dict[str, str],            # ticker -> "ETF"|"STOCK"
      international_tickers: dict[str, str],  # ticker -> 币种
      cfg: TrendGateConfig,
      as_of: pd.Timestamp | None = None,     # None=用 bars 最新 ts
  ) -> tuple[list[Signal], list[TrendInfo]]
  ```
  返回 (最终持有信号[趋势 LONG 的原 picks + 防御替换], 每 pick 的 TrendInfo)。LONG 的 pick 原样保留（保留其 price/reason/weight）；FLAT 的 pick 被丢弃，其权重合并投入单一最强防御标的（新造一条 BUY Signal，reason="趋势闸门·防御切换"）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_trend_gate.py` 追加：

```python
from datetime import timezone

from quant_signal.strategies.base import Direction, Signal
from quant_signal.strategies.trend_gate import apply_trend_gate


def _bars_from(series_map: dict[str, pd.Series]) -> pd.DataFrame:
    frames = []
    for t, s in series_map.items():
        df = pd.DataFrame(
            {"open": s, "high": s, "low": s, "close": s, "volume": 1_000_000.0}
        )
        df.index = pd.MultiIndex.from_product([[t], s.index], names=["ticker", "ts"])
        frames.append(df)
    return pd.concat(frames).sort_index()


def _pick(ticker: str, price: float) -> Signal:
    return Signal(
        ticker=ticker, direction=Direction.BUY, price=price, reason="动量",
        strategy_id="momentum_rotation", ts=pd.Timestamp("2022-01-03", tz="UTC"),
        suggested_weight=0.5,
    )


def test_long_pick_kept_flat_pick_routed_to_defensive():
    n = 320
    up = _series(100.0 * np.cumprod(np.full(n, 1.004)))          # MU 强趋势 → LONG
    down_tail = np.concatenate([100.0 * np.cumprod(np.full(220, 1.004)),
                                (100.0 * 1.004 ** 220) * np.cumprod(np.full(100, 0.985))])
    weak = _series(down_tail)                                    # BAD 跌破 → FLAT
    tlt = _series(100.0 * np.cumprod(np.full(n, 1.003)))         # 防御里 TLT 最强
    gld = _series(100.0 * np.cumprod(np.full(n, 1.001)))
    bil = _flat_rf(n)
    spy = _series(100.0 * np.cumprod(np.full(n, 1.0005)))
    bars = _bars_from({"MU": up, "BAD": weak, "TLT": tlt, "GLD": gld, "BIL": bil, "SPY": spy})

    picks = [_pick("MU", float(up.iloc[-1])), _pick("BAD", float(weak.iloc[-1]))]
    final, infos = apply_trend_gate(
        picks, bars, asset_type={"MU": "STOCK", "BAD": "STOCK"},
        international_tickers={}, cfg=TrendGateConfig(),
    )
    tickers = {s.ticker for s in final}
    assert "MU" in tickers                 # 趋势健康，保留
    assert "BAD" not in tickers            # 趋势失效，被切
    assert "TLT" in tickers                # 释放权重投最强防御
    tlt_sig = next(s for s in final if s.ticker == "TLT")
    assert "防御切换" in tlt_sig.reason
    assert abs(tlt_sig.suggested_weight - 0.5) < 1e-9   # 继承 BAD 释放的 0.5


def test_all_long_no_defensive():
    n = 320
    up = _series(100.0 * np.cumprod(np.full(n, 1.004)))
    bil = _flat_rf(n); spy = _series(100.0 * np.cumprod(np.full(n, 1.0005)))
    bars = _bars_from({"MU": up, "TLT": up, "GLD": up, "BIL": bil, "SPY": spy})
    final, _ = apply_trend_gate([_pick("MU", float(up.iloc[-1]))], bars,
                                asset_type={"MU": "STOCK"}, international_tickers={},
                                cfg=TrendGateConfig())
    assert [s.ticker for s in final] == ["MU"]   # 无 FLAT，无防御替换
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_trend_gate.py -q`
Expected: FAIL（`ImportError: cannot import name 'apply_trend_gate'`）

- [ ] **Step 3: 最小实现**

在 `trend_gate.py` 追加：

```python
from quant_signal.strategies.base import Direction, Signal


def _defensive_pick(
    bars_close: pd.DataFrame, cfg: TrendGateConfig, as_of: pd.Timestamp,
    rf_close: pd.Series,
) -> str | None:
    """{BIL,TLT,GLD} 里按 ret_12m − rf_12m 最强者。"""
    best, best_score = None, float("-inf")
    for t in cfg.defensive:
        if t not in bars_close.columns:
            continue
        s = bars_close[t].dropna()
        s = s[s.index <= as_of]
        if len(s) < cfg.mom_days + 1:
            continue
        ret = float(s.iloc[-1] / s.iloc[-1 - cfg.mom_days] - 1.0)
        rf = rf_close.reindex(s.index).ffill()
        rf12 = float(rf.iloc[-1] / rf.iloc[-1 - cfg.mom_days] - 1.0)
        score = ret - rf12
        if score > best_score:
            best, best_score = t, score
    return best


def apply_trend_gate(
    picks: list[Signal],
    bars: pd.DataFrame,
    asset_type: dict[str, str],
    international_tickers: dict[str, str],
    cfg: TrendGateConfig,
    as_of: pd.Timestamp | None = None,
) -> tuple[list[Signal], list[TrendInfo]]:
    close = bars["close"].unstack("ticker").sort_index()
    if as_of is None:
        as_of = close.index[-1]
    bench = close[cfg.benchmark] if cfg.benchmark in close.columns else pd.Series(dtype=float)
    rf = close["BIL"] if "BIL" in close.columns else pd.Series(dtype=float)

    kept: list[Signal] = []
    infos: list[TrendInfo] = []
    freed = 0.0
    for p in picks:
        if p.ticker not in close.columns:
            kept.append(p)
            continue
        info = trend_state(
            p.ticker, close[p.ticker], bench, rf,
            is_stock=asset_type.get(p.ticker, "STOCK") == "STOCK",
            is_usd=p.ticker not in international_tickers,
            cfg=cfg, as_of=as_of,
        )
        if info is not None:
            infos.append(info)
        if info is None or info.state == "LONG":
            kept.append(p)
        else:
            freed += p.suggested_weight or 0.0

    if freed > 0:
        d = _defensive_pick(close, cfg, as_of, rf)
        if d is not None:
            kept.append(
                Signal(
                    ticker=d, direction=Direction.BUY,
                    price=float(close[d].dropna().iloc[-1]),
                    reason="趋势闸门·防御切换", strategy_id="momentum_rotation",
                    ts=as_of.to_pydatetime(), suggested_weight=round(freed, 4),
                )
            )
    return kept, infos
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_trend_gate.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 类型检查**

Run: `uv run mypy src/`
Expected: Success

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/strategies/trend_gate.py tests/test_trend_gate.py
git commit -m "feat: apply_trend_gate 组合层防御切换(FLAT仓位切最强防御sleeve)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn"
```

---

### Task 3: 阶段B 回测（5 组对照 + 参数扫描）——研究脚本

**Files:**
- Create: `research/backtest_trend_gate.py`
- Output: `research/reports/trend_gate_from20160101_run<YYYYMMDD>.md`

**说明:** research 脚本不走 TDD，靠真实缓存数据跑通验证。复用 `backtest_momentum.py` 的 `load_bars`/vectorbt 框架与 `run_backtest` 的 universe 参数。回测网格：月末用 `MomentumRotation.generate()` 选股，然后**每周五**用 `apply_trend_gate` 评估退出，构建持仓矩阵喂 vectorbt。

- [ ] **Step 1: 写脚本**

创建 `research/backtest_trend_gate.py`：

```python
"""趋势闸门阶段B回测：证明"动量+趋势闸门+防御切换"能降最大回撤。
5组对照：基线动量 / 仅200线 / 仅绝对动量 / 完整闸门+防御 / buy&hold SPY。
buffer 扫描 {0.02,0.03,0.05}；月度退出 vs 周度退出对照。
研究脚本，靠真实缓存数据跑通，不走 TDD。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import vectorbt as vbt

from quant_signal.config import load_settings
from quant_signal.datafeed.store import BarStore
from quant_signal.datafeed.yf_source import YFinanceSource
from quant_signal.strategies.momentum_rotation import MomentumRotation
from quant_signal.strategies.trend_gate import TrendGateConfig, apply_trend_gate

REPORTS = Path(__file__).parent / "reports"
START = date(2016, 1, 1)
# 动量 universe + 防御/基准所需的数据标的
EXTRA = ["BIL", "TLT", "GLD", "SPY"]


def load_bars() -> pd.DataFrame:
    settings = load_settings()
    store = BarStore(settings.db_path / "bars_research.duckdb")
    tickers = sorted(set(settings.universe) | set(EXTRA))
    have = min((store.daily_bar_count(t) for t in tickers), default=0)
    if have < 100:
        df = YFinanceSource().fetch_daily_bars(tickers, START, datetime.now(timezone.utc).date())
        store.write_daily_bars(df, source="yfinance")
    start_dt = datetime(START.year, START.month, START.day, tzinfo=timezone.utc)
    return store.read_daily_bars(tickers, start=start_dt)


def _weekly_exit_marks(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(index, index=index)
    return list(s.groupby(index.tz_localize(None).to_period("W")).last())


def run_variant(
    bars: pd.DataFrame, settings, gate: str, buffer: float, weekly_exit: bool
) -> dict[str, float]:
    """gate ∈ {'none','ma_only','mom_only','full'}。返回绩效指标。"""
    mp = settings.strategies["momentum_rotation"]
    strat = MomentumRotation(
        universe=settings.universe, lookback_days=int(mp["lookback_days"]),
        top_n=int(mp["top_n"]), min_dollar_volume=float(mp["min_dollar_volume"]),
        ticker_currency=settings.international_tickers,
        group_top_n=settings.momentum_group_top_n, asset_type=settings.asset_type,
        default_group_top_n=settings.momentum_default_group_top_n,
    )
    close = bars["close"].unstack("ticker").sort_index()
    month_ends = close.groupby(close.index.tz_localize(None).to_period("M")).tail(1).index
    exit_marks = set(_weekly_exit_marks(close.index)) if weekly_exit else set(month_ends)

    cfg = TrendGateConfig(buffer=buffer)
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    current: list = []   # 当前持仓 Signal 列表
    changes = 0
    prev_tickers: set[str] = set()
    for ts in close.index:
        rebalanced = False
        if ts in set(month_ends):
            window = bars[bars.index.get_level_values("ts") <= ts]
            current = strat.generate(window)
            rebalanced = True
        if (rebalanced or ts in exit_marks) and current:
            picks = current
            if gate != "none":
                # 用对照 gate 调整 cfg：ma_only 关掉 mom（把 mom_days 效果绕过）、
                # mom_only 关掉 ma。这里用简单开关，见 _variant_cfg。
                picks, _ = apply_trend_gate(
                    _apply_variant(current, gate), bars, settings.asset_type,
                    settings.international_tickers, cfg, as_of=ts,
                ) if gate == "full" else (_gate_variant(current, bars, settings, cfg, ts, gate), [])
            names = {s.ticker for s in picks}
            if names:
                w = 1.0 / len(names)
                weights.loc[ts:, :] = 0.0
                for s in picks:
                    weights.loc[ts:, s.ticker] = s.suggested_weight or w
                changes += len(names - prev_tickers)
                prev_tickers = names
    pf = vbt.Portfolio.from_orders(
        close=close, size=weights, size_type="targetpercent", freq="1D",
        cash_sharing=True, call_seq="auto",
    )
    stats = pf.stats()
    years = (close.index[-1] - close.index[0]).days / 365.25
    total = float(stats["Total Return [%]"]) / 100
    return {
        "annual": ((1 + total) ** (1 / years) - 1) * 100,
        "sharpe": float(stats["Sharpe Ratio"]),
        "maxdd": float(stats["Max Drawdown [%]"]),
        "turnover": changes / years,
    }
```

> **实现提示（执行者补全）：** `_gate_variant(current, bars, settings, cfg, ts, gate)` 需实现 `ma_only`（只用 `cond_ma`，即构造 `is_usd=False` 等价效果或改写 trend_state 支持 flag）与 `mom_only`（只用 `cond_mom`）两个对照。最简做法：给 `trend_state` 加两个可选布尔 `use_ma=True,use_mom=True`，`apply_trend_gate` 透传；对照组传不同组合，`full` 两者都开。若这样改 `trend_state` 签名，回到 Task 1 补测试保持 TDD。buy&hold SPY 用 `vbt.Portfolio.from_holding(close["SPY"])` 单独算。主函数遍历 gate∈{none,ma_only,mom_only,full}×buffer×{weekly,monthly}，把结果拼成 Markdown 表输出到 `REPORTS`。

- [ ] **Step 2: 跑脚本**

Run: `uv run python research/backtest_trend_gate.py`
Expected: 生成 `research/reports/trend_gate_from20160101_run<今日>.md`，含各对照组 MaxDD/年化/Sharpe/换手 对比表。

- [ ] **Step 3: 人工核对 + 提交报告**

阅读报告，确认"完整闸门+防御"相比"基线"最大回撤是否明显下降、年化是否可接受。

```bash
git add research/backtest_trend_gate.py research/reports/trend_gate_from*.md
git commit -m "feat: 趋势闸门阶段B回测(5组对照+buffer/退出频率扫描)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn"
```

---

## 🚦 人工 GO/NO-GO 闸门（阶段B 之后，必须停）

**执行者到此停止，把回测报告呈给用户。** 由用户判断：
- 完整闸门相比基线，**最大回撤是否明显下降**（目标 ~44%→30% 以下量级）？
- **年化是否没塌**（下降幅度可接受）？
- 哪个 buffer / 退出频率组合最优？

**GO** → 才据结果写"阶段C 实盘接入"计划（engine 月末+每周五 job、卖出价上卡、enabled=true）。
**NO-GO** → 保留研究结论，不接入实盘；可回到设计讨论调参或换思路。

> **卖出价分类修正（写入阶段C 计划时注意）：** `bollinger_breakout` 实为**突破策略**（价>上轨才买），非均值回归。卖出价分类应为：**均值回归={rsi_reversion}**（止盈=中轨SMA20 + 止损=entry−1.5×ATR(14)）；**趋势/突破={momentum_rotation, macd_cross, bollinger_breakout}**（sell_ref=SMA200×(1−buffer)）。原 spec 第三节把布林归为均值回归有误，阶段C 按此修正。

---

## Self-Review

**Spec coverage（阶段A+B 部分）：**
- 状态机迟滞/周度重放/无未来函数 → Task1 `trend_state` + 4 测试。
- cond_ma/cond_mom(比rf)/cond_break/cond_rs(仅美股个股)/非美元只用cond_ma → Task1 实现 + `test_non_usd_uses_only_ma`。
- 防御 sleeve 选最强 + 释放权重路由 → Task2 `apply_trend_gate` + `_defensive_pick` + 2 测试。
- 趋势型 sell_ref=SMA200×(1−buffer) → Task1 `TrendInfo.sell_ref` + `test_uptrend_enters_long` 断言。
- 回测 5 组对照 + buffer 扫描 + 月度vs周度 → Task3。
- go/no-go 人工闸门 → 显式 🚦 段落。
- 阶段C（engine/scheduler/卖出价上卡/enabled）→ **不在本计划**，达标后另出，已在闸门标注 + 布林分类修正。

**Placeholder scan:** Task3 含一处显式"执行者补全"提示（对照组 gate 变体与主循环），因回测脚本细节依赖真实数据形态，属研究性质、非 src TDD；已用提示框明确边界与最简实现路径，非隐性 TODO。其余 Task1/2 代码完整。

**Type consistency:** `TrendGateConfig`/`TrendInfo`/`trend_state`/`apply_trend_gate` 签名在 Task1/2/3 一致；`is_usd`/`is_stock`/`as_of` 命名贯穿一致。若 Task3 需要给 `trend_state` 加 `use_ma/use_mom` flag 做对照组，提示已要求回 Task1 补测试维持 TDD。
