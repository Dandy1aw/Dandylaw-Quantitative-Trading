# US Regime, Nasdaq-100 Candidates, and Position Discipline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the holdings-centric 60-day-momentum morning report with regime-aware Nasdaq-100 candidate discovery and deterministic, leverage-aware position discipline at 08:00 and 15:30 Beijing time.

**Architecture:** Add focused pure-computation modules for market regime, strategy lanes, and position discipline, then persist their snapshots and compose a single deterministic briefing payload. The scheduler invokes one briefing pipeline in two explicit modes; AI only explains the versioned payload and cannot alter prices or quantities.

**Tech Stack:** Python 3.11, pandas, Pydantic 2, DuckDB daily-bar store, SQLite decision ledger, APScheduler, pytest, mypy, existing Feishu card/notifier and index-universe providers.

## Implementation status (2026-07-15)

- Tasks 1–9 and the automated portion of Task 10 are implemented on `feat/us-regime-risk-briefing`.
- The feature is intentionally configured as `enabled: true` and `delivery_mode: shadow`; the production worktree and A-share branches remain untouched.
- The new report creates Nasdaq-100 candidates independently of holdings, calculates account-budgeted buy quantities, and feeds live-mode candidates into the existing five-minute confirmation watch before an entry reminder can fire.
- Live mode suppresses the legacy holdings-centric morning/close cards and the standalone market-scan Top1 card, while shadow mode preserves all current production notifications for safe comparison.
- Large observation pools are summarized by exclusion reason, and the AI receives compact, valid JSON containing deterministic prices, quantities and position actions rather than a character-truncated payload.
- Automated verification: `689 passed, 2 warnings`; `mypy` reports no issues in 77 source files; `uv lock --check` and `git diff --check` pass.
- Remaining release gate: observe five consecutive US sessions in shadow mode, complete the checklist below, then switch to `live` in a separate reviewed release commit.

---

## File map

- Create `src/quant_signal/market_regime.py`: pure daily-bar regime and breadth computation.
- Create `src/quant_signal/candidate_lanes.py`: pure Nasdaq-100 strategy-lane scoring and deterministic levels.
- Create `src/quant_signal/position_discipline.py`: cost-aware staged exits, leverage risk and persisted-state transitions.
- Create `src/quant_signal/pipelines/us_briefing.py`: data orchestration, degradation and report persistence/delivery.
- Modify `src/quant_signal/config.py`: validated briefing/regime/discipline settings.
- Modify `src/quant_signal/ledger.py`: regime, candidate, discipline and report snapshot persistence.
- Modify `src/quant_signal/notifier/cards.py`: compact report rendering.
- Modify `src/quant_signal/ai_briefing.py`: versioned deterministic briefing context and numeric guard.
- Modify `src/quant_signal/engine.py`: expose `run_us_briefing`.
- Modify `src/quant_signal/scheduler.py`: replace duplicate rotation calls with 08:00/15:30 briefing modes.
- Modify `config/settings.yaml`: remove Korean symbols, add SKHY and enable shadow-safe defaults.
- Create tests mirroring every new module and pipeline; update scheduler/config/card/E2E tests.

### Task 1: Configuration and US-only symbol migration

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

```python
def test_us_briefing_defaults_are_safe() -> None:
    settings = USBriefingSettings()
    assert settings.enabled is False
    assert settings.candidate_index == "nasdaq100"
    assert settings.min_coverage == 0.98
    assert settings.morning_hour_utc == 0
    assert settings.afternoon_hour_utc == 7
    assert settings.afternoon_minute_utc == 30


def test_production_universe_uses_skhynix_ads_not_korean_listing(settings_path: Path) -> None:
    settings = load_settings(settings_path)
    assert "000660.KS" not in settings.tickers
    assert "SKHY" in settings.tickers
    assert settings.tickers["SKHY"].currency == "USD"
    assert "KRW" not in settings.momentum_group_top_n
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_config.py -q`

Expected: FAIL because `USBriefingSettings` and `SKHY` configuration do not exist.

- [ ] **Step 3: Add validated settings**

Add `MarketRegimeSettings`, `CandidateLaneSettings`, `PositionDisciplineSettings`, and `USBriefingSettings` to `config.py`. Defaults must include `enabled=False`, Nasdaq-100-only discovery, 98% coverage, top three per lane, 08:00/15:30 Beijing UTC slots, 1x/2x/3x profit ladders, -8%/-5%/-3.5% hard loss caps, and `allow_financing_for_leveraged=False`.

In `settings.yaml`, remove `000660.KS`, remove the KRW group, add:

```yaml
  SKHY:
    asset_type: STOCK
    currency: USD
    leverage: 1.0
```

and add an initially disabled `us_briefing` section so production behavior is unchanged until release.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/config.py config/settings.yaml tests/test_config.py
git commit -m "feat: configure US regime briefing and SKHY"
```

### Task 2: Market regime and breadth engine

**Files:**
- Create: `src/quant_signal/market_regime.py`
- Create: `tests/test_market_regime.py`

- [ ] **Step 1: Write failing behavior tests**

Use deterministic bar fixtures with QQQ and at least ten constituents. Cover:

```python
def test_healthy_uptrend_is_trend() -> None:
    snapshot = classify_market_regime(trend_fixture(), as_of=DAY, settings=CFG)
    assert snapshot.regime == Regime.TREND
    assert snapshot.coverage == 1.0
    assert snapshot.breadth_above_50d > 0.7


def test_broken_long_trend_is_risk_off() -> None:
    snapshot = classify_market_regime(risk_off_fixture(), as_of=DAY, settings=CFG)
    assert snapshot.regime == Regime.RISK_OFF
    assert "QQQ_BELOW_200D" in snapshot.reasons


def test_insufficient_coverage_returns_data_incomplete() -> None:
    snapshot = classify_market_regime(sparse_fixture(), as_of=DAY, settings=CFG)
    assert snapshot.data_quality == "INCOMPLETE"
    assert snapshot.regime == Regime.UNKNOWN
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_market_regime.py -q`

Expected: collection FAIL because `market_regime` is missing.

- [ ] **Step 3: Implement pure regime computation**

Define immutable `RegimeSnapshot` with regime, as-of date, coverage, QQQ MA relationships, breadth above 20/50/200 days, five-day participation, realized volatility, ATR percentage, reasons and data quality. Exclude incomplete current bars and non-finite closes. Require full lookback per metric rather than silently shortening windows.

Classification order must be deterministic: insufficient data → `UNKNOWN`; long-trend/volatility failure → `RISK_OFF`; healthy trend/breadth → `TREND`; positive long trend with short deterioration → `PULLBACK`; otherwise → `RANGE`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_market_regime.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/market_regime.py tests/test_market_regime.py
git commit -m "feat: classify Nasdaq market regimes"
```

### Task 3: Nasdaq-100 strategy lanes and data-readiness rules

**Files:**
- Create: `src/quant_signal/candidate_lanes.py`
- Create: `tests/test_candidate_lanes.py`

- [ ] **Step 1: Write failing lane tests**

```python
def test_holdings_do_not_change_candidate_output() -> None:
    first = discover_candidates(BARS, MEMBERS, REGIME, holdings={"MU"})
    second = discover_candidates(BARS, MEMBERS, REGIME, holdings={"AAPL"})
    assert first == second


def test_new_ads_is_observation_until_history_is_ready() -> None:
    result = discover_candidates(BARS_WITH_10_DAY_SKHY, MEMBERS | {"SKHY"}, REGIME)
    assert all(row.ticker != "SKHY" for row in result.candidates)
    assert result.observations[0].ticker == "SKHY"
    assert result.observations[0].reason == "INSUFFICIENT_HISTORY"


def test_risk_off_blocks_all_long_lanes() -> None:
    result = discover_candidates(BARS, MEMBERS, RISK_OFF)
    assert result.candidates == ()
    assert result.block_reason == "REGIME_RISK_OFF"
```

Also test trend continuation, controlled pullback, range reversion, overheat rejection, earnings veto, top-three cap and cross-lane deduplication.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_candidate_lanes.py -q`

Expected: collection FAIL because `candidate_lanes` is missing.

- [ ] **Step 3: Implement lane discovery**

Define `CandidateLane`, `Candidate`, `CandidateObservation` and `CandidateDiscovery`. Calculate only from the passed Nasdaq-100 membership and bars. Keep scores lane-specific. Generate entry/invalid/target levels with ATR and nearby moving averages; reject non-finite or incorrectly ordered levels. Accept holdings only as a deprecated ignored keyword for the invariance test, then remove it from all production calls.

Use regime gates:

```python
ALLOWED_LANES = {
    Regime.TREND: {CandidateLane.TREND_CONTINUATION, CandidateLane.TREND_PULLBACK},
    Regime.PULLBACK: {CandidateLane.TREND_PULLBACK, CandidateLane.RANGE_REVERSION},
    Regime.RANGE: {CandidateLane.RANGE_REVERSION},
    Regime.RISK_OFF: set(),
    Regime.UNKNOWN: set(),
}
```

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_candidate_lanes.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/candidate_lanes.py tests/test_candidate_lanes.py
git commit -m "feat: discover regime-aware Nasdaq candidates"
```

### Task 4: Stateful staged exits and leverage risk

**Files:**
- Create: `src/quant_signal/position_discipline.py`
- Create: `tests/test_position_discipline.py`

- [ ] **Step 1: Write failing discipline tests**

```python
def test_first_profit_stage_is_cumulative_and_idempotent() -> None:
    advice = advise_position(POSITION_AT_16_PERCENT, leverage=1, prior=EMPTY_STATE, cfg=CFG)
    assert advice.status == DisciplineStatus.TAKE_PROFIT_DUE
    assert advice.cumulative_sell_fraction == Decimal("0.25")
    assert advice.incremental_sell_qty == Decimal("25")
    repeated = advise_position(POSITION_AT_16_PERCENT, leverage=1, prior=advice.next_state, cfg=CFG)
    assert repeated.incremental_sell_qty == Decimal("0")


def test_two_x_product_has_tighter_hard_stop_and_no_financing() -> None:
    advice = advise_position(TWO_X_POSITION, leverage=2, prior=EMPTY_STATE, cfg=CFG)
    assert advice.hard_stop_price == Decimal("95.00")
    assert advice.financing_allowed is False


def test_missing_qty_never_invents_share_count() -> None:
    advice = advise_position(PARTIAL_SCREENSHOT_POSITION, leverage=1, prior=EMPTY_STATE, cfg=CFG)
    assert advice.incremental_sell_qty is None
    assert advice.incremental_sell_fraction == Decimal("0.25")
```

Also test implied cost from PnL%, stale/partial trust labels, +20/+30 cumulative stages, stage reset on cost-basis change, peak trailing stop, stop breach, effective exposure, cluster aggregation and risk-off financing block.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_position_discipline.py -q`

Expected: collection FAIL because `position_discipline` is missing.

- [ ] **Step 3: Implement deterministic advice**

Define immutable `DisciplineState`, `PositionInput`, `PositionAdvice`, `PortfolioRiskSummary` and `DisciplineStatus`. Quantize shares down to whole shares for non-fractional plans. Store both incremental and cumulative sale fractions. Derive estimated cost only when current price and finite PnL percentage make the denominator positive. Combine cost cap, trend invalidation and trailing protection using the strictest valid protection line. Label every input field `EXACT`, `ESTIMATED` or `MISSING`.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_position_discipline.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/position_discipline.py tests/test_position_discipline.py
git commit -m "feat: add staged exits and leverage controls"
```

### Task 5: Persist snapshots and idempotent report runs

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Modify: `tests/test_ledger.py`

- [ ] **Step 1: Write failing persistence tests**

Add tests that save/load the latest regime, replace candidates for one report slot, upsert a discipline state, reset it when `basis_version` changes, and create the same `(report_kind, as_of, data_version)` run twice without duplication.

```python
def test_report_run_key_is_idempotent(ledger: Ledger) -> None:
    first = ledger.begin_us_briefing_run("US_CLOSE", AS_OF, "bars-v1")
    second = ledger.begin_us_briefing_run("US_CLOSE", AS_OF, "bars-v1")
    assert first.run_id == second.run_id
    assert ledger.count_us_briefing_runs() == 1
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_ledger.py -q`

Expected: FAIL because the tables and methods are missing.

- [ ] **Step 3: Add schema and repository methods**

Add SQLite ledger tables `market_regime_snapshots`, `candidate_lane_snapshots`, `position_discipline_states`, and `us_briefing_runs`. Use JSON payloads for versioned domain details plus indexed identity columns for queries. All replacement writes use explicit transactions; report identity has a unique constraint.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_ledger.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/ledger.py tests/test_ledger.py
git commit -m "feat: persist US briefing decision state"
```

### Task 6: Compact report card and AI numeric boundary

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/ai_briefing.py`
- Modify: `tests/test_cards.py`
- Modify: `tests/test_ai_briefing.py`
- Modify: `tests/test_ai_briefing_price_guard.py`

- [ ] **Step 1: Write failing presentation and guard tests**

Verify one compact card contains at most three candidates per lane, shows staged action as `卖出 25 股（累计 25%）`, labels estimated costs, displays leverage effective exposure, and has no wide Markdown tables. Verify the AI prompt contains a versioned `us_briefing` JSON block and rejects any output that introduces a ticker or changes a structured price/quantity.

```python
def test_ai_cannot_change_discipline_price() -> None:
    body = validate_ai_briefing_output("MU 止损 91.00", structured_prices={"MU": {"stop": 92.0}})
    assert "91.00" not in body
    assert "AI 数值校验失败" in body
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_cards.py tests/test_ai_briefing.py tests/test_ai_briefing_price_guard.py -q`

Expected: FAIL because the new card/context are absent.

- [ ] **Step 3: Implement report rendering and strict AI context**

Add `us_briefing_card(...)` with short sections and bullet-based mobile layout. Add a `USBriefingAIContext` or a discriminated extension of `AIBriefingContext`; serialize exact numeric facts in JSON. Validate AI tickers and numeric tokens against the payload. On failure, omit the AI paragraph while retaining the deterministic card.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_cards.py tests/test_ai_briefing.py tests/test_ai_briefing_price_guard.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/notifier/cards.py src/quant_signal/ai_briefing.py tests/test_cards.py tests/test_ai_briefing.py tests/test_ai_briefing_price_guard.py
git commit -m "feat: render compact guarded US briefings"
```

### Task 7: Briefing pipeline and graceful degradation

**Files:**
- Create: `src/quant_signal/pipelines/us_briefing.py`
- Modify: `src/quant_signal/engine.py`
- Create: `tests/test_us_briefing_pipeline.py`

- [ ] **Step 1: Write failing pipeline tests**

Test both `US_CLOSE` and `ASIA_CONFIRM` modes. Assert that membership comes from Nasdaq-100, candidates remain identical when holdings change, account failure removes exact quantities but not candidates, Asia-provider failure produces an unavailable label, SKHY is observation-only with short history, and repeating a run does not resend the card.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_us_briefing_pipeline.py -q`

Expected: collection FAIL because the pipeline is missing.

- [ ] **Step 3: Implement orchestration**

The pipeline must:

1. resolve the last complete US session;
2. load/refresh Nasdaq-100 membership and daily bars;
3. calculate regime and lanes;
4. load exact or observed account data and calculate discipline separately;
5. optionally load non-tradable Asia context in `ASIA_CONFIRM` mode;
6. persist all snapshots transactionally;
7. render deterministic card, optionally append guarded AI commentary;
8. send once and record delivery.

Expose `Engine.run_us_briefing(now, mode=...)`. Do not invoke `engine.momentum`, change momentum virtual holdings, or call the legacy premarket pipeline.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_us_briefing_pipeline.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/pipelines/us_briefing.py src/quant_signal/engine.py tests/test_us_briefing_pipeline.py
git commit -m "feat: orchestrate deterministic US briefings"
```

### Task 8: Scheduler consolidation and card-storm prevention

**Files:**
- Modify: `src/quant_signal/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing schedule tests**

Assert `us_close_briefing` runs at 00:00 UTC weekdays and `asia_confirm_briefing` at 07:30 UTC weekdays. Assert legacy `rotation_asia_open`/`rotation_asia_close` are absent when the new feature is enabled. Assert the option close summary is invoked through the 08:00 orchestrator or shifted into an ordered follow-up without sharing the same trigger second.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_scheduler.py -q`

Expected: FAIL because legacy rotation jobs still exist.

- [ ] **Step 3: Add feature-gated schedule**

When `us_briefing.enabled` is false, preserve legacy jobs. When true, register the two named briefing jobs and prevent duplicate rotation jobs. Serialize the 08:00 briefing and option summary through one lock/orchestrator; a failure in options must not mark the main briefing failed.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `uv run pytest tests/test_scheduler.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/quant_signal/scheduler.py tests/test_scheduler.py
git commit -m "feat: schedule two ordered US briefings"
```

### Task 9: Research replay and release gate

**Files:**
- Create: `research/backtest_us_candidate_lanes.py`
- Create: `tests/test_us_candidate_replay.py`
- Modify: `docs/superpowers/specs/2026-07-15-us-regime-risk-briefing-design.md`

- [ ] **Step 1: Write failing replay tests**

Build small fixed fixtures for `TREND`, `PULLBACK`, `RANGE`, `RISK_OFF` and ensure the replay script reports CAGR/return, max drawdown, Sharpe, turnover, win rate, trade count and QQQ relative return. Require an explicit `survivorship_bias=True` flag when only current members are available.

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/test_us_candidate_replay.py -q`

Expected: FAIL because the replay module is absent.

- [ ] **Step 3: Implement research-only replay**

Reuse production pure functions; do not copy scoring formulas. Include configurable transaction costs and one-session execution delay. Emit JSON and Markdown artifacts under ignored `reports/`. Do not enable production automatically based on a single aggregate return; require drawdown and turnover review.

- [ ] **Step 4: Run tests and a local fixture replay**

Run: `uv run pytest tests/test_us_candidate_replay.py -q`

Run: `uv run python research/backtest_us_candidate_lanes.py --fixture tests/fixtures/us_regimes --output reports/us_candidate_lanes`

Expected: tests PASS and report explicitly states membership-bias status.

- [ ] **Step 5: Commit**

```bash
git add research/backtest_us_candidate_lanes.py tests/test_us_candidate_replay.py docs/superpowers/specs/2026-07-15-us-regime-risk-briefing-design.md
git commit -m "test: add regime candidate replay gate"
```

### Task 10: End-to-end verification and shadow configuration

**Files:**
- Create: `tests/test_us_briefing_e2e.py`
- Modify: `config/settings.yaml`
- Modify: `docs/superpowers/plans/2026-07-15-us-regime-risk-briefing.md`

- [ ] **Step 1: Add end-to-end scenarios before enabling the feature**

Cover exact broker holdings, five-position partial screenshot, 2x leveraged holdings, no account, incomplete index coverage, AI CLI failure, notifier retry, duplicate scheduler invocation and both report modes. Capture card bodies and assert they contain no Korean tradable symbol, no invented quantities and no more than the configured candidate count.

- [ ] **Step 2: Run focused verification**

Run:

```bash
uv run pytest tests/test_market_regime.py tests/test_candidate_lanes.py tests/test_position_discipline.py tests/test_us_briefing_pipeline.py tests/test_us_briefing_e2e.py -q
uv run pytest tests/test_scheduler.py tests/test_cards.py tests/test_ai_briefing.py tests/test_ai_briefing_price_guard.py -q
```

Expected: all PASS.

- [ ] **Step 3: Run repository-wide quality gates**

Run:

```bash
uv run pytest -q
uv run mypy src
uv lock --check
git diff --check
```

Expected: no failures; test count is greater than the 627-test branch baseline.

- [ ] **Step 4: Enable shadow mode only**

Set `us_briefing.enabled: true` and `us_briefing.delivery_mode: shadow` in the feature branch. Shadow mode writes snapshots and renders cards to logs/test notifier but does not replace production delivery. Add the five-session observation checklist to this plan with dates, data coverage, candidate stability, discipline-state transitions and notification volume.

- [ ] **Step 5: Review the complete diff and commit**

Run: `git diff f4bcb15...HEAD --stat` and inspect all changed production files for accidental A-share or production-secret changes.

```bash
git add tests/test_us_briefing_e2e.py config/settings.yaml docs/superpowers/plans/2026-07-15-us-regime-risk-briefing.md
git commit -m "test: verify US briefing end to end"
```

## Release checklist

- [ ] Five consecutive US sessions have complete Nasdaq-100 coverage or an explained degradation.
- [ ] Candidate output is unchanged when only account holdings change.
- [ ] No duplicate 08:00/15:30 cards or repeated cumulative sell instructions.
- [ ] Leveraged positions show effective exposure and never receive financing allocation.
- [ ] SKHY remains observation-only until each required lookback is genuinely available.
- [ ] AI failure or numeric rejection leaves the deterministic report intact.
- [ ] Only after shadow review, change `delivery_mode` from `shadow` to `live` in a separate reviewed release commit.
