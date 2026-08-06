# Production Reliability and Research Closure Implementation Plan

> **For agentic workers:** Execute inline in this session. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the production reliability, data quality, research evidence, AI latency, and module-boundary gaps found in the July audit.

**Architecture:** Keep public pipeline APIs stable while adding explicit degradation contracts, consistent exports, coverage-aware QA, immutable forward evaluations, and deadline-aware AI orchestration. Extract formatting and company-analysis responsibilities only after behavioral tests are green.

**Tech Stack:** Python 3.12, pytest, mypy, DuckDB, SQLite, APScheduler, httpx, Pydantic.

---

### Task 1: Cboe partial-venue degradation

**Files:**
- Modify: `src/quant_signal/datafeed/cboe_options.py`
- Modify: `src/quant_signal/config.py`
- Modify: `src/quant_signal/main.py`
- Modify: `config/settings.yaml`
- Test: `tests/test_options_flow.py`
- Test: `tests/test_config.py`

- [ ] Add a test where one of four venues has fewer than ten rows and assert a 0.75-coverage snapshot is returned.
- [ ] Run the test and confirm the current all-or-nothing fetch fails.
- [ ] Add `min_venue_coverage` to the source and collect per-venue failures.
- [ ] Fail only below the configured threshold or when the aggregate lacks ten calls/puts.
- [ ] Run focused option-flow and configuration tests.

### Task 2: Consistent DuckDB export and restore probe

**Files:**
- Modify: `src/quant_signal/datafeed/store.py`
- Modify: `src/quant_signal/backup.py`
- Modify: `src/quant_signal/scheduler.py`
- Test: `tests/test_store.py`
- Test: `tests/test_backup.py`
- Test: `tests/test_scheduler.py`

- [ ] Add a failing test for online export while the source store remains open.
- [ ] Add a failing test for recursive manifest/mirror contents and restore probing.
- [ ] Implement `BarStore.export_database()` using the active connection.
- [ ] Make `run_backup()` export through `BarStore`, hash files recursively, mirror directories, and fail when required artifacts are absent.
- [ ] Run backup, store, and scheduler tests.

### Task 3: Secret and CI release gates

**Files:**
- Modify: `scripts/check_secrets.py`
- Modify: `.github/workflows/ci.yml`
- Create: `scripts/check_release_state.py`
- Test: `tests/test_release_policy.py`

- [ ] Add failing tests for a tracked forbidden file, untracked CI files, and a clean release state.
- [ ] Implement a release checker that fails on forbidden tracked files and missing tracked CI/security scripts.
- [ ] Add the release checker and `git diff --check` to CI.
- [ ] Keep Git-history rewriting out of scope and document the remaining maintenance action.

### Task 4: Dynamic-universe data QA

**Files:**
- Modify: `src/quant_signal/pipelines/dataqa.py`
- Modify: `src/quant_signal/engine.py`
- Test: `tests/test_dataqa_pipeline.py`

- [ ] Add a failing test proving dynamic index members are included with static tickers.
- [ ] Add tests for partial batch failure and insufficient overall coverage.
- [ ] Load the index universe, batch both sources, retain successful comparisons, and report coverage/missing counts.
- [ ] Return an explicit success boolean so `JobRuntime` can mark insufficient coverage as failure.

### Task 5: Immutable 21/63-session forward evaluation

**Files:**
- Create: `src/quant_signal/forward_evaluation.py`
- Modify: `src/quant_signal/ledger.py`
- Modify: `src/quant_signal/scheduler.py`
- Test: `tests/test_forward_evaluation.py`
- Test: `tests/test_ledger.py`
- Test: `tests/test_scheduler.py`

- [ ] Add failing domain tests for 21/63-session evaluation, missing coverage, and immutable repeated evaluation.
- [ ] Add the evaluation table and ledger methods.
- [ ] Implement final-bar-only evaluation with data-version and point-in-time evidence fields.
- [ ] Schedule evaluation after daily maintenance and expose counts in structured logs.

### Task 6: AI total deadline, cache, and deterministic fallback

**Files:**
- Create: `src/quant_signal/company_analysis.py`
- Modify: `src/quant_signal/config.py`
- Modify: `src/quant_signal/pipelines/us_briefing.py`
- Test: `tests/test_company_analysis.py`
- Test: `tests/test_us_briefing_pipeline.py`

- [ ] Add failing tests for total-budget exhaustion, cache hits, and fallback coverage.
- [ ] Move company orchestration into `company_analysis.py`.
- [ ] Bound each retry by remaining time and fill missing blocks from structured fields.
- [ ] Cache accepted blocks by semantic hash and verify every candidate receives a block.

### Task 7: Focused module extraction

**Files:**
- Create: `src/quant_signal/notifier/briefing_format.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Test: `tests/test_cards.py`

- [ ] Add characterization tests for all translated sector, industry, block-reason, percent, and USD formats.
- [ ] Move pure formatting constants/functions without behavior changes.
- [ ] Keep compatibility imports private to `cards.py`.
- [ ] Run card tests and mypy.

### Task 8: Full verification and real-data release

**Files:**
- Modify: `research/e2e/run_three_stage_live_e2e.py`
- Create: `research/e2e/production-closure-20260729/result.json`

- [ ] Run focused suites after every task.
- [ ] Run full `pytest -q`, `mypy src`, secret/release checks, and `git diff --check`.
- [ ] Create a current production snapshot without corrupting the live databases.
- [ ] Run the real-data E2E with an isolated notifier.
- [ ] Restart the scheduled service and inspect new logs.
- [ ] Send one real-data notification suite and verify delivery results.
