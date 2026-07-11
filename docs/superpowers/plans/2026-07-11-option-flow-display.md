# Option Flow Display (Expiry Sort + Underlying Dedupe) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feishu 期权卡 CALL/PUT Top10 每侧同标的只显示最高量合约，并按到期日升序展示，全部只动展示层。

**Architecture:** 新增纯函数 `display_top_by_side()`（去重→按量选取→按到期日排序），卡片和 enrichment 目标改用它；排名、异动判定、落库、质量门槛保持逐合约原语义。

**Tech Stack:** Python 3.12, dataclasses, pytest, mypy strict.

## Global Constraints

- 排名/异动/落库/fail-closed 门槛不得改变口径（spec §2）
- 选取仍按成交量（热度榜），到期日只影响顺序不影响入选（spec §3）
- 两个开关 `display_dedupe_underlying` / `display_sort_by_expiry` 默认 true、可独立关闭
- mypy strict 全绿；全量 pytest 全绿

---

### Task 1: `display_top_by_side` 纯函数

**Files:**
- Modify: `src/quant_signal/options_flow.py`
- Test: `tests/test_options_flow.py`

**Interfaces:**
- Produces: `display_top_by_side(rows: OptionFlowSnapshot | Sequence[OptionContractVolume], side: str, n: int, *, dedupe: bool = True, sort_by_expiry: bool = True) -> tuple[OptionContractVolume, ...]`
- Produces: `folded_count(rows, side, item, top_n) -> int`（同标的在原始每侧前 top_n 中被折叠的其他合约数）

- [x] **Step 1: 写失败测试**

```python
def test_display_dedupes_underlying_and_sorts_by_expiry() -> None:
    rows = (
        contract("SPY260713C00750000", underlying="SPY", strike="750",
                 expiration=date(2026, 7, 13), volume=30_000, rank=1),
        contract("SPY260717C00755000", underlying="SPY", strike="755",
                 expiration=date(2026, 7, 17), volume=25_000, rank=2),
        contract("NVDA260717C00210000", volume=20_000, rank=3),
        contract("MSFT260714C00500000", underlying="MSFT", strike="500",
                 expiration=date(2026, 7, 14), volume=15_000, rank=4),
    )
    shown = display_top_by_side(rows, "call", 3)
    assert [item.underlying for item in shown] == ["SPY", "MSFT", "NVDA"]
    assert shown[0].contract_symbol == "SPY260713C00750000"  # SPY 只留最高量那张


def test_display_switches_can_be_disabled_independently() -> None:
    rows = (
        contract("SPY260717C00755000", underlying="SPY", strike="755", volume=25_000, rank=1),
        contract("SPY260713C00750000", underlying="SPY", strike="750",
                 expiration=date(2026, 7, 13), volume=30_000, rank=2),
    )
    raw = display_top_by_side(rows, "call", 2, dedupe=False, sort_by_expiry=False)
    assert [item.rank for item in raw] == [1, 2]  # 完全回退旧展示
    deduped = display_top_by_side(rows, "call", 2, sort_by_expiry=False)
    assert len(deduped) == 1 and deduped[0].volume == 30_000
```

- [x] **Step 2: 跑测试确认 RED**（`display_top_by_side` 未定义）
- [x] **Step 3: 实现**

```python
def display_top_by_side(
    rows: "OptionFlowSnapshot | Sequence[OptionContractVolume]",
    side: str,
    n: int,
    *,
    dedupe: bool = True,
    sort_by_expiry: bool = True,
) -> tuple[OptionContractVolume, ...]:
    """展示层选取：去重→按量选 n 个→按到期日排序。不改变任何排名语义。"""
    candidates = list(top_by_side(rows, side, 10**6))
    if dedupe:
        best: dict[str, OptionContractVolume] = {}
        for item in candidates:
            kept = best.get(item.underlying)
            if kept is None or (item.volume, kept.contract_symbol) > (kept.volume, item.contract_symbol):
                best[item.underlying] = item
        candidates = sorted(best.values(), key=lambda i: (-i.volume, i.contract_symbol))
    selected = candidates[:n]
    if sort_by_expiry:
        selected.sort(key=lambda i: (i.expiration, -i.volume, i.contract_symbol))
    return tuple(selected)
```

（注意 dedupe 比较用 `(volume, symbol)` 保证量相同取字典序小的确定性；tie-break 写测试覆盖。）

- [x] **Step 4: 跑测试 GREEN；commit** `feat: add option flow display selector`

### Task 2: 卡片改用展示选择 + 折叠标注

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`（`rank_section`）
- Modify: `src/quant_signal/config.py`（`OptionFlowSettings` 加 `display_dedupe_underlying: bool = True`、`display_sort_by_expiry: bool = True`）
- Modify: `config/settings.yaml`
- Test: `tests/test_option_flow_card.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 1 的 `display_top_by_side`
- Produces: `option_flow_card(..., display_dedupe: bool = True, display_sort_by_expiry: bool = True)` 新关键字参数

- [x] **Step 1: 失败测试**——同标的只出现一次、行首 `#原始排名` 非连续、`(+N)` 折叠标注、近月排前、开关关闭回退旧顺序
- [x] **Step 2: RED**
- [x] **Step 3: 实现**：`rank_section` 用 `display_top_by_side(snapshot, side, 10, dedupe=..., sort_by_expiry=...)`；行格式 `#{item.rank} {UL} {expiry} {strike}{C/P} · {vol}张 · {delta} · {DTE}{折叠}`，折叠= 同标的另有合约在原始前10时 ` (+N)`
- [x] **Step 4: GREEN；commit** `feat: dedupe and expiry-sort option board display`

### Task 3: pipeline enrichment 目标集合 + 全量验证

**Files:**
- Modify: `src/quant_signal/pipelines/option_flow.py`（`displayed` 集合与卡片参数）
- Test: `tests/test_option_flow_pipeline.py`

**Interfaces:**
- Consumes: Task 1/2 的函数与配置

- [x] **Step 1: 失败测试**——fake enricher 记录被 enrich 的合约集合，断言 = 展示集合（含去重后顶上来的行）
- [x] **Step 2: RED**
- [x] **Step 3: 实现**：`displayed = display_top_by_side(snapshot, "call", top_n, ...) + display_top_by_side(snapshot, "put", ...)`；`option_flow_card` 传显示开关。质量校验 `_validate_snapshot` 不动。
- [x] **Step 4: GREEN**
- [x] **Step 5: 全量** `pytest -q` + `mypy src/` 全绿；commit `feat: enrich exactly the displayed option rows`
