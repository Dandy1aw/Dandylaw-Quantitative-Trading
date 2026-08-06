# Option Flow Underlying Aggregation Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current session. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace separate Call and Put option-flow rankings with one underlying-level heat ranking that shows fully written Call/Put volume, ratio, dominance, and honest comparable deltas.

**Architecture:** Preserve contract-level snapshots, persistence, quality gates, and alerts. Add a pure aggregation view in `options_flow.py`; render that view in the Feishu card while retaining contract-level anomaly focus.

**Tech Stack:** Python 3.11, frozen dataclasses, pytest, existing Feishu Markdown card model.

---

### Task 1: Underlying aggregation domain

**Files:**
- Modify: `src/quant_signal/options_flow.py`
- Test: `tests/test_options_flow.py`

- [ ] **Step 1: Write failing aggregation tests**

Add tests that construct multiple Call and Put contracts for SPY and AAPL and assert:

```python
flows = aggregate_underlying_flows(current, previous, top_n=10)
assert flows[0].underlying == "SPY"
assert flows[0].call_volume == 1_250_000
assert flows[0].put_volume == 820_000
assert flows[0].call_put_ratio == pytest.approx(1.52439)
assert flows[0].dominance == pytest.approx(430_000 / 2_070_000)
assert flows[0].structure_label == "Call 占优"
```

Add separate tests for deterministic total-volume ranking, all five ratio labels, single-side visibility, first snapshot, fully comparable deltas, and partial deltas caused by a newly visible contract.

- [ ] **Step 2: Run the new tests and verify failure**

Run: `pytest tests/test_options_flow.py -q`

Expected: collection failure because `aggregate_underlying_flows` and `UnderlyingOptionFlow` do not exist.

- [ ] **Step 3: Implement the immutable aggregation view**

Add:

```python
@dataclass(frozen=True)
class UnderlyingOptionFlow:
    underlying: str
    call_volume: int
    put_volume: int
    call_contract_count: int
    put_contract_count: int
    known_call_delta: int | None
    known_put_delta: int | None
    call_delta_partial: bool
    put_delta_partial: bool

    @property
    def total_volume(self) -> int: ...

    @property
    def call_put_ratio(self) -> float | None: ...

    @property
    def dominance(self) -> float | None: ...

    @property
    def structure_label(self) -> str: ...


def aggregate_underlying_flows(
    current: OptionFlowSnapshot,
    previous: OptionFlowSnapshot | None = None,
    *,
    top_n: int = 10,
) -> tuple[UnderlyingOptionFlow, ...]: ...
```

Compute known deltas only for OCC symbols visible in both snapshots. A current-only contract marks that side partial and contributes no fake 15-minute delta. Sort by `(-total_volume, underlying)`.

- [ ] **Step 4: Run domain tests**

Run: `pytest tests/test_options_flow.py -q`

Expected: all option-flow domain tests pass.

### Task 2: Merged mobile card

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/pipelines/option_flow.py`
- Test: `tests/test_option_flow_card.py`
- Test: `tests/test_option_flow_pipeline.py`

- [ ] **Step 1: Replace old card expectations with failing merged-card tests**

Assert one `标的热度 Top10` section, no `CALL Top10`/`PUT Top10`, four total sections, ETF labelling, complete Call/Put words, ratio, dominance, known deltas, partial delta text, first-snapshot text, and the coverage disclaimer.

```python
assert "#1 SPY · ETF" in card.body_md
assert "Call 1,250,000 / Put 820,000" in card.body_md
assert "可见量 Call/Put 比 1.52 · Call 占优 21%" in card.body_md
assert "15 分钟增量 Call +85,000 / Put +32,000" in card.body_md
assert "CALL Top10" not in card.body_md
```

- [ ] **Step 2: Run card and pipeline tests and verify failure**

Run: `pytest tests/test_option_flow_card.py tests/test_option_flow_pipeline.py -q`

Expected: failures showing the old two-section card and missing ETF argument.

- [ ] **Step 3: Implement merged rendering**

Update `option_flow_card` to accept `etf_underlyings: frozenset[str]`, call `aggregate_underlying_flows`, and render each underlying as four mobile-first lines. Replace contract suffix `C`/`P` in anomaly focus with full `Call`/`Put`. Retain the identity, anomaly focus, and boundary sections.

Pass `frozenset(cfg.etf_roots)` from `pipelines/option_flow.py`. Keep existing enrichment selection, quality checks, persistence, cooldown, and outbox unchanged.

- [ ] **Step 4: Run focused option tests**

Run: `pytest tests/test_options_flow.py tests/test_option_flow_card.py tests/test_option_flow_pipeline.py -q`

Expected: all pass.

### Task 3: Option feature verification

**Files:**
- Modify only if verification reveals a defect in files from Tasks 1-2.

- [ ] **Step 1: Run all option-related tests**

Run: `pytest tests/test_options_flow.py tests/test_option_flow_card.py tests/test_option_flow_pipeline.py tests/test_option_intel_pipeline.py tests/test_option_intel_ledger.py tests/test_options_intel.py tests/test_cboe_options.py tests/test_alpaca_option_chain.py tests/test_alpaca_options.py -q`

Expected: all pass; holdings option intelligence remains unchanged.

- [ ] **Step 2: Run static typing**

Run: `mypy src/quant_signal`

Expected: success with no issues.

- [ ] **Step 3: Run full regression suite**

Run: `pytest -q`

Expected: all tests pass.
