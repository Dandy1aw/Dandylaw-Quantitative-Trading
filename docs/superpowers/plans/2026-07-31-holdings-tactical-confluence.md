# Holdings Tactical Confluence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing holdings option-intelligence card with deterministic weekly mean-reversion, option-wall, gamma-concentration, OPEX, and risk-reference analysis for every observed holding.

**Architecture:** Add a network-free pure calculation module and attach its immutable result to `OptionIntel`. The existing option-intel pipeline fetches each option chain once, computes both the existing metrics and the tactical analysis, and renders one combined Feishu card. Missing history or option data degrades per symbol without blocking other holdings.

**Tech Stack:** Python 3.12, pandas, Pydantic v2, Alpaca option snapshots/contracts, DuckDB, pytest, mypy.

---

## File structure

- Create `src/quant_signal/position_tactical.py`: pure weekly/option/OPEX calculations and frozen result models.
- Create `tests/test_position_tactical.py`: characterization of all pure calculation rules.
- Modify `src/quant_signal/options_intel.py`: carry optional tactical analysis and option gamma.
- Modify `src/quant_signal/datafeed/alpaca_options.py`: parse gamma into option-chain contracts.
- Modify `src/quant_signal/pipelines/option_intel.py`: load 420-day history and compute tactical analysis from the already-fetched chain.
- Modify `src/quant_signal/notifier/cards.py`: render the tactical fields in Chinese.
- Modify `src/quant_signal/config.py` and `config/settings.yaml`: tactical thresholds, SKHY risk cluster, and AKHY alias.
- Modify `src/quant_signal/index_universe.py`: canonicalize AKHY to SKHY at import boundaries.
- Modify the associated tests for data source, pipeline, card, configuration, aliasing, and screenshot import.
- Add an isolated E2E probe under `research/e2e/` that records one real holdings tactical card without touching the production option-flow outbox.

### Task 1: Weekly technical state

**Files:**
- Create: `tests/test_position_tactical.py`
- Create: `src/quant_signal/position_tactical.py`

- [ ] **Step 1: Write failing tests for weekly indicators and insufficient history**

Create daily OHLC fixtures that resample into 24 weeks. Assert:

```python
technical = compute_weekly_technical(daily, as_of=date(2026, 7, 31))
assert technical.weekly_rsi is not None
assert technical.bollinger_lower < technical.bollinger_mid < technical.bollinger_upper
assert technical.weekly_atr > 0
assert technical.state == "OVERSOLD_AT_LOWER_BAND"
```

Also assert 20 weeks returns `state == "DATA_INSUFFICIENT"` and all unsupported numeric fields remain `None`.

- [ ] **Step 2: Verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_position_tactical.py -q
```

Expected: collection fails because `quant_signal.position_tactical` does not exist.

- [ ] **Step 3: Implement weekly resampling and metrics**

Add frozen models:

```python
@dataclass(frozen=True)
class WeeklyTechnical:
    state: TechnicalState
    weekly_rsi: float | None
    bollinger_lower: float | None
    bollinger_mid: float | None
    bollinger_upper: float | None
    middle_slope_4w: float | None
    weekly_atr: float | None
    reversal_confirmed: bool | None
    latest_week_partial: bool
    recent_four_week_low: float | None
    history_weeks: int
```

Implement `compute_weekly_technical(daily: pd.DataFrame, *, as_of: date)`.
Use `W-FRI`, 14-week Wilder RSI, 20-week mean/sample standard deviation, 14-week ATR, four-week middle-band slope, complete-week reversal checks, and the exact state precedence from the design.

- [ ] **Step 4: Verify GREEN**

Run the Task 1 tests and:

```powershell
.venv\Scripts\python.exe -m mypy src/quant_signal/position_tactical.py
```

Expected: all pass.

### Task 2: Walls, Max Pain, gamma concentration, and OPEX

**Files:**
- Modify: `tests/test_position_tactical.py`
- Modify: `src/quant_signal/position_tactical.py`

- [ ] **Step 1: Write failing option-structure tests**

Build one expiration with known Call/Put OI and gamma. Assert:

```python
structure = compute_option_structure(
    contracts, spot=Decimal("100"), session=date(2026, 7, 17),
    truncated=False, policy=PositionTacticalPolicy(),
)
assert structure.put_wall == Decimal("95")
assert structure.call_wall == Decimal("105")
assert structure.max_pain == Decimal("100")
assert structure.reliability == "HIGH"
assert structure.gamma_pin_score is not None
assert structure.dealer_gamma_state == "UNKNOWN"
```

Add tests proving:

- a Put above spot is not used as support;
- a Call below spot is not used as resistance;
- truncated chains cannot be `HIGH`;
- absent OI returns missing walls and `LOW`;
- monthly OPEX day, 1–3-session window, and first session after expiry are classified correctly.

- [ ] **Step 2: Verify RED**

Expected: missing functions and models.

- [ ] **Step 3: Implement deterministic option structure**

Add:

```python
@dataclass(frozen=True)
class OptionStructure:
    expiry: date | None
    put_wall: Decimal | None
    call_wall: Decimal | None
    max_pain: Decimal | None
    put_buffer_pct: float | None
    call_upside_pct: float | None
    gamma_pin_score: float | None
    dealer_gamma_state: Literal["UNKNOWN"]
    reliability: WallReliability
    oi_coverage: float
    quote_coverage: float
    truncated: bool

@dataclass(frozen=True)
class OpexContext:
    monthly_expiry: date
    trading_days_to_expiry: int
    state: OpexState
```

Implement the exact thresholds and payout-minimization formula in the design. Use the repository trading calendar for holiday adjustment.

- [ ] **Step 4: Verify GREEN**

Run all pure-module tests and mypy.

### Task 3: Confluence rating and risk references

**Files:**
- Modify: `tests/test_position_tactical.py`
- Modify: `src/quant_signal/position_tactical.py`

- [ ] **Step 1: Write one failing test for each conclusion**

Construct `WeeklyTechnical` and `OptionStructure` values and assert:

```python
assert classify_confluence(oversold_confirmed, medium_walls).conclusion == "TACTICAL_REBOUND"
assert classify_confluence(oversold_unconfirmed, medium_walls).conclusion == "WAIT_REVERSAL_CONFIRMATION"
assert classify_confluence(oversold_confirmed, breached_wall).conclusion == "PUT_WALL_BREAK_RISK"
assert classify_confluence(falling_knife, medium_walls).conclusion == "FALLING_KNIFE_RISK"
assert classify_confluence(oversold_confirmed, low_walls).conclusion == "TECHNICAL_ONLY"
assert classify_confluence(insufficient, low_walls).conclusion == "DATA_INSUFFICIENT"
```

Assert wall-based invalidation uses `put_wall - max(0.5*ATR, 1%*spot)`, low-reliability invalidation uses the four-week low, and targets are sorted above spot.

- [ ] **Step 2: Verify RED**

Expected: missing `classify_confluence`.

- [ ] **Step 3: Implement the aggregate result**

Add:

```python
@dataclass(frozen=True)
class PositionTacticalAnalysis:
    symbol: str
    spot: Decimal
    technical: WeeklyTechnical
    options: OptionStructure
    opex: OpexContext
    conclusion: ConfluenceConclusion
    invalidation_price: Decimal | None
    target_1: Decimal | None
    target_2: Decimal | None
    holding_note: str
```

Expose `analyze_position_tactical(...)` as the only orchestration entry point.

- [ ] **Step 4: Verify GREEN**

Run all pure-module tests and mypy.

### Task 4: Parse chain gamma and carry tactical analysis

**Files:**
- Modify: `tests/test_alpaca_option_chain.py`
- Modify: `tests/test_options_intel.py`
- Modify: `src/quant_signal/datafeed/alpaca_options.py`
- Modify: `src/quant_signal/options_intel.py`

- [ ] **Step 1: Write failing gamma and model tests**

Add `greeks: {"gamma": 0.0123}` to a chain snapshot and assert:

```python
result = source.fetch_chain("MU", session=SESSION, max_expiry_days=30)
assert result.contracts[0].gamma == 0.0123
```

Assert `OptionIntel(..., tactical=analysis)` retains the frozen tactical result.

- [ ] **Step 2: Verify RED**

Expected: `OptionChainContract` has no gamma attribute and `OptionIntel` rejects `tactical`.

- [ ] **Step 3: Implement minimal model changes**

Add `gamma: float | None = None` to `OptionChainContract` and parse `item["greeks"]["gamma"]`. Add `tactical: PositionTacticalAnalysis | None = None` to `OptionIntel`; use `TYPE_CHECKING` or a runtime import without creating an import cycle.

- [ ] **Step 4: Verify GREEN**

Run the two test files and mypy for the modified modules.

### Task 5: Integrate once-per-symbol pipeline calculation

**Files:**
- Modify: `tests/test_option_intel_pipeline.py`
- Modify: `src/quant_signal/pipelines/option_intel.py`
- Modify: `src/quant_signal/feishu_bot.py`

- [ ] **Step 1: Write failing integration tests**

Assert:

- the daily-bar read starts at least 420 days before `now`;
- each holding causes exactly one `fetch_chain`;
- the returned `OptionIntel.tactical` is populated when bars and options exist;
- SKHY with short history/no chain returns `DATA_INSUFFICIENT` without blocking MU;
- interactive `期权 MU` includes the same tactical result.

- [ ] **Step 2: Verify RED**

Expected: tactical result is `None` and history begins only 90 days back.

- [ ] **Step 3: Integrate analysis**

Refactor the internal helper to return spot, closes, and daily OHLC. Fetch the chain once, pass its contracts to both `compute_intel` and `analyze_position_tactical`, then use `dataclasses.replace(intel, tactical=analysis)`. Preserve fail-open behavior and existing persistence.

- [ ] **Step 4: Verify GREEN**

Run option-intel pipeline and bot tests, then mypy.

### Task 6: Render the combined Chinese card

**Files:**
- Modify: `tests/test_option_intel_pipeline.py`
- Modify: `src/quant_signal/notifier/cards.py`

- [ ] **Step 1: Write failing card tests**

Assert the card contains:

```python
assert "周线 RSI" in card.body_md
assert "布林下轨" in card.body_md
assert "Put墙" in card.body_md
assert "Call墙" in card.body_md
assert "Max Pain" in card.body_md
assert "Gamma集中度" in card.body_md
assert "做市商净Gamma方向不可由公开OI判定" in card.body_md
assert "月度OPEX" in card.body_md
assert "战术反弹" in card.body_md
assert "失效参考" in card.body_md
```

Add a SKHY assertion for `历史不足` or `无可靠期权墙`.

- [ ] **Step 2: Verify RED**

Expected: new Chinese fields are absent.

- [ ] **Step 3: Implement compact rendering**

Render no more than nine concise lines per holding, translate enum labels with fixed dictionaries, use `-` plus the reason for missing fields, and retain the existing feed/disclaimer boundary.

- [ ] **Step 4: Verify GREEN**

Run card and option-intel tests.

### Task 7: Alias, configuration, and risk cluster

**Files:**
- Modify: `tests/test_index_universe.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_portfolio_import.py`
- Modify: `src/quant_signal/index_universe.py`
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`

- [ ] **Step 1: Write failing normalization/config tests**

Assert:

```python
assert to_canonical_symbol("AKHY") == "SKHY"
assert ExtractedPosition(symbol="AKHY").symbol == "SKHY"
assert "SKHY" in settings.execution_plan.risk_clusters["semiconductor_memory"]
assert settings.option_intel.wall_high_min_oi_each_side == 5_000
```

- [ ] **Step 2: Verify RED**

Expected: AKHY remains unchanged, SKHY is not in the cluster, and tactical settings are absent.

- [ ] **Step 3: Implement configuration**

Add the one-way alias, add SKHY to the semiconductor cluster, and add validated fields for the design thresholds. Do not create an SKHY-to-AKHY reverse mapping.

- [ ] **Step 4: Verify GREEN**

Run configuration, universe, portfolio-import, and screenshot tests.

### Task 8: Full verification, production snapshot E2E, holding update, and restart

**Files:**
- Create: `research/e2e/run_holdings_tactical_live_e2e.py`
- Use: `C:\Users\syw\.codex\codex-remote-attachments\019f7b14-0938-73a3-a707-c9a20a4971e8\56697BDD-3FE0-4E39-B69C-6371AF05E040\1-照片-1.jpg`

- [ ] **Step 1: Run full automated verification**

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe scripts/check_secrets.py
.venv\Scripts\python.exe scripts/check_release_state.py
git diff --check HEAD
```

Expected: zero failures.

- [ ] **Step 2: Create an isolated production snapshot**

Stop the `quant-signal` scheduled task briefly, copy `bars.duckdb`, `signals.db`, and `news.db` to a timestamped directory inside `research/e2e`, and immediately restart the task. Verify the restarted service has an empty stderr log and a new `scheduler.started` event.

- [ ] **Step 3: Run real-data E2E without production option-flow writes**

The E2E script must:

- load the isolated bar/ledger snapshot;
- use the real Alpaca option-chain source;
- run only the holdings option-intel pipeline with a recording notifier;
- assert every active holding appears once;
- assert no card claims signed dealer Gamma;
- write `result.json` with technical state, walls, reliability, OPEX, conclusion, and degradation reasons.

- [ ] **Step 4: Update holdings from the approved screenshot**

Import:

```text
equity 5747.18; market value 4709.07; cash 1037.57; buying power 2961.97
GOOGL 2 @ 391.943, current 333.430, value 666.86
MRVL 6 @ 207.875, current 182.404, value 1094.42
NBIS 5 @ 213.770, current 191.154, value 955.77
SKHY 10 @ 147.589, current 145.571, value 1455.71
SMH 1 @ 707.520, current 536.305, value 536.31
```

Use the existing validated screenshot import flow; require `ACTIVE`, five exact positions, and account reconciliation within the existing one-dollar tolerance.

- [ ] **Step 5: Restart and send exactly one live card**

Restart `quant-signal`, run a read-only/live holdings tactical probe that does not touch the option-flow durable outbox, send exactly one combined holdings tactical card, and record send success plus card title.

- [ ] **Step 6: Inspect production evidence**

Verify scheduled task `Running`, `quant-signal` process start time is new, latest stderr is empty, and current active observed positions exactly match the screenshot.
