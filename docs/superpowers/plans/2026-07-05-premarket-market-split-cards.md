# 盘前早报按市场拆卡 + 多策略共振【重要】卡 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把盘前早报从"单张5行上限卡"改为"按市场拆分的多张卡（美股/港股/韩股，美股优先）+ 一张排最前的【重要】多策略共振卡"，不限行。

**Architecture:** 新增纯函数 `premarket_cards()` 到 `notifier/cards.py`，输入去重后待推送信号 + 国际标的币种表 + 现价表，输出有序 `list[Card]`。`Engine.run_premarket` 改为按唯一标的抓现价后调用它并依次 `notifier.send`，删除旧的 `PREMARKET_REPORT_LIMIT` / `_select_report_rows`。

**Tech Stack:** Python 3.12, pytest, pydantic Settings, 飞书 interactive card。

## Global Constraints

- 所有 `src/` 改动走 TDD：先写失败测试→确认红→最小实现→确认绿→`uv run mypy src/`→提交。
- 测试命令用 `uv run pytest`，类型检查用 `uv run mypy src/`。
- 提交信息结尾附：
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn
  ```
- 市场归类：`international_tickers[ticker]` 为 `HKD`→港股组、`KRW`→韩股组，其余→美股组。
- 卡片顺序：`[🔥【重要】共振]`（有共振才发）→ 美股组 → 港股组 → 韩股组，跳过无信号的卡。
- 策略展示顺序固定：动量轮动 → MACD → RSI回归 → 布林带。
- 共振判定：同一 `(ticker, direction)` 被 ≥2 个不同 `strategy_id` 命中；方向必须一致。
- 共振标的仍保留在各自策略小节（不去重）。
- 卡片不限行，全部显示。

---

### Task 1: `premarket_cards` 纯函数 + 单元测试

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`（新增常量与 `premarket_cards` 及其私有辅助）
- Test: `tests/test_cards.py`（新建）

**Interfaces:**
- Consumes: `Signal`（`quant_signal.strategies.base`），`Card`/`CardKind`/`report_card`（同模块）。
- Produces:
  ```python
  def premarket_cards(
      signals: list[Signal],
      international_tickers: dict[str, str],
      live_prices: dict[str, float | None],
  ) -> list[Card]
  ```
  返回有序卡片列表：可选的【重要】共振卡在最前，随后美股/港股/韩股组卡（跳过空卡）。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cards.py`：

```python
from datetime import datetime, timezone

from quant_signal.notifier.cards import premarket_cards
from quant_signal.strategies.base import Direction, Signal

TS = datetime(2026, 1, 2, tzinfo=timezone.utc)
INTL = {"7709.HK": "HKD", "000660.KS": "KRW"}


def _sig(ticker, direction, strategy_id, price=10.0, reason="r", rank=None):
    extra = {"rank": rank} if rank is not None else None
    return Signal(
        ticker=ticker, direction=direction, price=price, reason=reason,
        strategy_id=strategy_id, ts=TS, extra=extra,
    )


def _card_by(cards, needle):
    return next(c for c in cards if needle in c.title)


def test_splits_markets_us_first_and_skips_empty():
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("7709.HK", Direction.BUY, "momentum_rotation", rank=2),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None, "7709.HK": None})
    titles = [c.title for c in cards]
    assert titles == ["📋 盘前早报 · 美股组", "📋 盘前早报 · 港股组"]  # 韩股无信号被跳过，美股在前


def test_confluence_card_first_when_two_strategies_same_direction():
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("MU", Direction.BUY, "macd_cross"),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert "【重要】" in cards[0].title            # 共振卡排最前
    assert "MU" in cards[0].body_md and "动量轮动+MACD" in cards[0].body_md
    # 仍保留在美股组各策略小节
    us = _card_by(cards, "美股组")
    assert "【动量轮动】" in us.body_md and "【MACD】" in us.body_md


def test_no_confluence_card_when_single_strategy():
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", rank=1)]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert all("【重要】" not in c.title for c in cards)


def test_opposite_directions_are_not_confluence():
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("MU", Direction.SELL, "macd_cross"),
    ]
    cards = premarket_cards(signals, INTL, {"MU": None})
    assert all("【重要】" not in c.title for c in cards)


def test_market_card_sections_by_strategy_and_skip_empty():
    signals = [
        _sig("MU", Direction.BUY, "momentum_rotation", rank=1),
        _sig("GLD", Direction.BUY, "macd_cross"),
    ]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None, "GLD": None}), "美股组")
    assert "【动量轮动】" in us.body_md and "【MACD】" in us.body_md
    assert "【RSI回归】" not in us.body_md and "【布林带】" not in us.body_md  # 空小节跳过


def test_momentum_sell_routed_to_ticker_market():
    signals = [_sig("000660.KS", Direction.SELL, "momentum_rotation")]
    cards = premarket_cards(signals, INTL, {"000660.KS": None})
    assert [c.title for c in cards] == ["📋 盘前早报 · 韩股组"]
    assert "SELL" in cards[0].body_md


def test_live_price_missing_shows_dash():
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    us = _card_by(premarket_cards(signals, INTL, {"MU": None}), "美股组")
    assert "| 100.00 | - |" in us.body_md   # 参考价100，现价缺失显示 -


def test_live_price_present_formatted():
    signals = [_sig("MU", Direction.BUY, "momentum_rotation", price=100.0, rank=1)]
    us = _card_by(premarket_cards(signals, INTL, {"MU": 123.456}), "美股组")
    assert "123.46" in us.body_md
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_cards.py -q`
Expected: FAIL（`ImportError: cannot import name 'premarket_cards'`）

- [ ] **Step 3: 最小实现**

在 `src/quant_signal/notifier/cards.py` 顶部 import 处补 `Direction`：

```python
from quant_signal.strategies.base import Direction, Signal
```

在文件末尾追加：

```python
_STRATEGY_LABELS_ORDERED: list[tuple[str, str]] = [
    ("momentum_rotation", "动量轮动"),
    ("macd_cross", "MACD"),
    ("rsi_reversion", "RSI回归"),
    ("bollinger_breakout", "布林带"),
]
_STRATEGY_LABEL = dict(_STRATEGY_LABELS_ORDERED)
_STRATEGY_ORDER = [sid for sid, _ in _STRATEGY_LABELS_ORDERED]

_MARKET_BY_CCY = {"HKD": "港股组", "KRW": "韩股组"}
_MARKET_ORDER = ["美股组", "港股组", "韩股组"]   # 美股优先


def _market_of(ticker: str, international_tickers: dict[str, str]) -> str:
    return _MARKET_BY_CCY.get(international_tickers.get(ticker, ""), "美股组")


def _sig_sort_key(s: Signal) -> tuple[int, int, str]:
    rank = (s.extra or {}).get("rank", 999)
    rank_i = int(rank) if isinstance(rank, int) else 999
    dir_rank = 0 if s.direction == Direction.BUY else 1
    return (dir_rank, rank_i, s.ticker)


def _confluence_rows(
    signals: list[Signal], international_tickers: dict[str, str]
) -> list[dict[str, object]]:
    groups: dict[tuple[str, str], list[Signal]] = {}
    for s in signals:
        groups.setdefault((s.ticker, s.direction.value), []).append(s)
    rows: list[dict[str, object]] = []
    for (ticker, direction), sigs in groups.items():
        strat_ids = {s.strategy_id for s in sigs}
        if len(strat_ids) < 2:
            continue
        hit = [_STRATEGY_LABEL[sid] for sid in _STRATEGY_ORDER if sid in strat_ids]
        rows.append(
            {"ticker": ticker, "direction": direction, "hit": "+".join(hit),
             "price": sigs[0].price}
        )
    rows.sort(key=lambda r: (_MARKET_ORDER.index(
        _market_of(str(r["ticker"]), international_tickers)), str(r["ticker"])))
    return rows


def _confluence_card(rows: list[dict[str, object]]) -> Card:
    lines = ["| 标的 | 方向 | 命中策略 | 参考价 |", "|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {str(r['direction']).upper()} | {r['hit']} |"
            f" {float(r['price']):.2f} |"  # type: ignore[arg-type]
        )
    return Card(kind=CardKind.REPORT, title="🔥 【重要】多策略共振", body_md="\n".join(lines))


def _market_card(
    market: str, signals: list[Signal], live_prices: dict[str, float | None]
) -> Card:
    parts: list[str] = []
    for sid in _STRATEGY_ORDER:
        ssigs = sorted((s for s in signals if s.strategy_id == sid), key=_sig_sort_key)
        if not ssigs:
            continue
        lines = [
            f"**【{_STRATEGY_LABEL[sid]}】**",
            "| 标的 | 方向 | 参考价 | 现价 | 原因 |", "|---|---|---|---|---|",
        ]
        for s in ssigs:
            live = live_prices.get(s.ticker)
            live_str = f"{live:.2f}" if live is not None else "-"
            lines.append(
                f"| {s.ticker} | {s.direction.value.upper()} | {s.price:.2f} |"
                f" {live_str} | {s.reason} |"
            )
        parts.append("\n".join(lines))
    return report_card(f"📋 盘前早报 · {market}", "\n\n".join(parts))


def premarket_cards(
    signals: list[Signal],
    international_tickers: dict[str, str],
    live_prices: dict[str, float | None],
) -> list[Card]:
    """把当天推送的盘前信号渲染成有序卡片：
    [可选【重要】共振卡] + 美股组 + 港股组 + 韩股组（跳过空卡，美股优先）。"""
    cards: list[Card] = []
    confl = _confluence_rows(signals, international_tickers)
    if confl:
        cards.append(_confluence_card(confl))
    for market in _MARKET_ORDER:
        msigs = [s for s in signals if _market_of(s.ticker, international_tickers) == market]
        if msigs:
            cards.append(_market_card(market, msigs, live_prices))
    return cards
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_cards.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 类型检查**

Run: `uv run mypy src/`
Expected: Success

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/notifier/cards.py tests/test_cards.py
git commit -m "feat: premarket_cards 纯函数——按市场拆卡+多策略共振【重要】卡

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn"
```

---

### Task 2: engine 接入 + 删除旧限行逻辑 + 更新 engine 测试

**Files:**
- Modify: `src/quant_signal/engine.py`（`run_premarket` 改用 `premarket_cards`；删除 `PREMARKET_REPORT_LIMIT`、`_STRATEGY_LABELS`、`_select_report_rows`）
- Modify: `tests/test_engine.py`（更新 3 个 premarket 断言；删除 2 个 `_select_report_rows` 测试及 `_buy`/`_sell` 辅助与相关 import）

**Interfaces:**
- Consumes: `premarket_cards`（Task 1）。

- [ ] **Step 1: 更新 engine 测试（写期望）**

在 `tests/test_engine.py`：

1. 第 9 行 import 去掉 `_select_report_rows`：
   ```python
   from quant_signal.engine import Engine, _intraday_snapshot
   ```

2. `test_premarket_generates_rotation_and_report` 末尾两行（`assert len(notifier.cards) == 1` 与标题断言）替换为：
   ```python
       titles = [c.title for c in notifier.cards]
       assert any("美股组" in t for t in titles)        # 合成标的均为美股，出美股组卡
       assert all("盘前早报" in t or "【重要】" in t for t in titles)
   ```

3. `test_premarket_report_shows_strategy_column_for_multiple_strategies` 里
   `body = notifier.cards[0].body_md` 改为跨所有卡搜索：
   ```python
       body = "\n".join(c.body_md for c in notifier.cards)
       assert "动量轮动" in body
       assert "RSI回归" in body
   ```

4. `test_premarket_report_includes_live_price` 里
   `body = notifier.cards[0].body_md` 改为：
   ```python
       body = "\n".join(c.body_md for c in notifier.cards)
   ```

5. 删除 `_buy`、`_sell` 两个辅助函数（第 207-219 行）与
   `test_select_report_rows_prioritizes_buy_by_rank_then_caps`、
   `test_select_report_rows_no_truncation_when_under_limit`（第 222-232 行）整段。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_engine.py -q`
Expected: FAIL（`ImportError`：engine 尚未导出/删除对应符号，`premarket_cards` 未接入导致 `notifier.cards` 仍是旧单卡结构）

- [ ] **Step 3: 改 engine 实现**

在 `src/quant_signal/engine.py`：

1. import 段把 cards 那行改为包含 `premarket_cards`、去掉 `report_card` 之外无用的（保留 `alert_card, build_enrichment_card, signal_card`；`report_card` 仍被别处用，保留）：
   ```python
   from quant_signal.notifier.cards import (
       alert_card, build_enrichment_card, premarket_cards, report_card, signal_card,
   )
   ```

2. 删除模块级 `PREMARKET_REPORT_LIMIT = 5` 与 `_STRATEGY_LABELS = {...}` 常量块。

3. 删除 `_select_report_rows` 整个函数。

4. `run_premarket` 里从 `if result.to_push:` 到该方法内 `self.notifier.send(report_card(...))` 之间的整段卡片拼装，替换为：
   ```python
           if result.to_push:
               unique = {s.ticker for s in result.to_push}
               live_prices = {t: self._fetch_live_price(t) for t in unique}
               for card in premarket_cards(
                   result.to_push, self.settings.international_tickers, live_prices
               ):
                   self.notifier.send(card)
   ```
   （`log.info("premarket.done", ...)` 那行保留在方法末尾不变。）

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_engine.py tests/test_cards.py -q`
Expected: PASS

- [ ] **Step 5: 全量回归 + 类型检查**

Run: `uv run pytest -q && uv run mypy src/`
Expected: 全绿，mypy Success

- [ ] **Step 6: 提交**

```bash
git add src/quant_signal/engine.py tests/test_engine.py
git commit -m "feat: run_premarket 改用 premarket_cards 分市场推送，删除5行上限逻辑

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01DhyGzjiCv64ce6uXXBz6rn"
```

---

### Task 3: 真实数据 + 真实飞书端到端验证

**Files:** 无代码改动（复用 scratchpad 的 `run_once.py` 一次性触发器）。

- [ ] **Step 1: 触发一次真实盘前流程**

Run: `uv run python <scratchpad>/run_once.py`
Expected: 日志出现多条 `POST .../feishu... 200 OK`（分市场若干张卡 + 可能的【重要】卡），末尾 `premarket.done`。

- [ ] **Step 2: 人工确认飞书群**

预期收到：若有共振先一张 `🔥【重要】多策略共振`，随后 `📋 盘前早报 · 美股组`（内部按【动量轮动】【MACD】等分节，MACD 的 GLD/TLT/TSM 不再被挤没），港股/韩股组若有信号各一张。向用户报告实际收到的卡片数与内容，等用户确认。

---

## Self-Review

**Spec coverage:**
- 卡片顺序/美股优先/跳过空卡 → Task1 `premarket_cards` + `test_splits_markets_us_first_and_skips_empty`。
- 市场归类（含 SELL 按标的市场）→ `_market_of` + `test_momentum_sell_routed_to_ticker_market`。
- 每卡按策略分节、空节跳过、不限行 → `_market_card` + `test_market_card_sections_by_strategy_and_skip_empty`。
- 共振检测（≥2策略同向、方向一致、保留重复、命中策略拼接）→ `_confluence_rows` + 3 个 confluence 测试。
- 现价缺失显示 `-` / 正常格式化 → 2 个 live_price 测试。
- 删除 `PREMARKET_REPORT_LIMIT`/`_select_report_rows` → Task2。
- 盘中/去重/台账不受影响 → Task2 未触碰 `run_intraday`/dedup/ledger，全量回归覆盖。
- 完全无信号不发卡 → `run_premarket` 的 `if result.to_push:` 守卫（保留），engine 现有 dedup 测试路径覆盖。

**Placeholder scan:** 无 TODO/TBD，每个代码步骤均含完整代码。

**Type consistency:** `premarket_cards(signals, international_tickers, live_prices)` 三处签名一致（spec/Task1 Produces/Task2 调用）；`_market_of`、`_confluence_rows`、`_market_card` 命名前后一致；`live_prices: dict[str, float | None]` 贯穿一致。
