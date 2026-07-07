# Quant Signal Refactor Phases 1-4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement phases 1-4 from `docs/2026-07-08-重构方案.md` while preserving the public scheduler/engine entry points and keeping the full test suite and strict mypy green.

**Architecture:** Keep `Engine` as dependency assembly and compatibility facade, but move each scheduled workflow into a focused `pipelines` module. Introduce a typed ticker registry that derives legacy settings, use at-least-once notification persistence, and make ranking/health/data-quality behavior explicit and independently testable.

**Tech Stack:** Python 3.11+, pandas, Pydantic 2, APScheduler 3, yfinance, DuckDB, SQLite, pytest, mypy strict.

---

### Task 1: Momentum ranking report

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/engine.py`
- Test: `tests/test_cards.py`
- Test: `tests/test_engine.py`

- [ ] Add a failing card test proving the report contains Top 5 candidates, Bottom 3 warnings, holding markers, and trend-insufficient/FLAT markers.
- [ ] Run `uv run pytest tests/test_cards.py -q` and confirm the new test fails because the ranking card does not exist.
- [ ] Implement `momentum_ranking_card()` as a display-only formatter over `MomentumRotation.rank()` output.
- [ ] Add a failing engine test proving every premarket run sends the ranking report without changing selected holdings.
- [ ] Run the focused engine test and confirm the ranking card is absent.
- [ ] Preserve `apply_trend_gate()` diagnostics and send the ranking card after market signal cards.
- [ ] Run `uv run pytest tests/test_cards.py tests/test_engine.py -q`.

### Task 2: Correctness guards

**Files:**
- Modify: `src/quant_signal/strategies/momentum_rotation.py`
- Modify: `src/quant_signal/strategies/trend_gate.py`
- Modify: `src/quant_signal/config.py`
- Test: `tests/test_momentum.py`
- Test: `tests/test_trend_gate.py`
- Test: `tests/test_config.py`

- [ ] Add a failing momentum test proving a non-USD ticker is excluded when its FX rate is absent.
- [ ] Implement fail-closed FX eligibility and emit a structured warning for missing currencies.
- [ ] Add a failing trend-gate test proving insufficient-history picks are excluded and returned as explicit diagnostics.
- [ ] Implement an `INSUFFICIENT` diagnostic without treating it as LONG.
- [ ] Add failing configuration tests for missing US asset type and missing international currency mapping.
- [ ] Add Pydantic model validation with actionable startup errors.
- [ ] Run `uv run pytest tests/test_momentum.py tests/test_trend_gate.py tests/test_config.py -q`.

### Task 3: Scheduler and notification reliability

**Files:**
- Modify: `src/quant_signal/scheduler.py`
- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/datafeed/yf_source.py`
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `deploy/run-service.ps1`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_datafeed.py`

- [ ] Add failing scheduler tests for one-hour rotation/premarket misfire grace, four-minute intraday/deviation grace, and two-hour per-job alert silence.
- [ ] Implement explicit APScheduler job defaults and per-job failure silence state.
- [ ] Add failing engine tests proving failed sends are persisted with `pushed=False` and successful sends with `pushed=True`.
- [ ] Reorder delivery and ledger persistence to at-least-once semantics for all signal workflows.
- [ ] Add a failing data-source test proving multiple live prices use one `yf.download()` call.
- [ ] Implement `fetch_live_prices()` and retain `fetch_live_price()` compatibility.
- [ ] Reduce enrichment default timeout to 60 seconds.
- [ ] Rotate and clean supervisor logs older than 14 days.
- [ ] Run focused scheduler, engine, and datafeed tests.

### Task 4: Typed ticker registry and independent test settings

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `tests/conftest.py`
- Modify: tests currently calling `load_settings()`
- Test: `tests/test_config.py`

- [ ] Add failing tests proving the new `tickers` mapping derives `universe`, `asset_type`, and `international_tickers`, while legacy settings still load.
- [ ] Implement `TickerSettings` and registry-to-legacy derivation.
- [ ] Convert production YAML to the registry form.
- [ ] Add `test_settings()` fixture/factory built entirely in memory.
- [ ] Replace production-YAML reads in behavioral tests with the factory; retain one repository configuration smoke test.
- [ ] Run `uv run pytest tests/test_config.py tests/test_engine.py -q`.

### Task 5: Pipeline extraction and channel quotas

**Files:**
- Create: `src/quant_signal/pipelines/__init__.py`
- Create: `src/quant_signal/pipelines/premarket.py`
- Create: `src/quant_signal/pipelines/intraday.py`
- Create: `src/quant_signal/pipelines/deviation.py`
- Create: `src/quant_signal/pipelines/enrichment.py`
- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/config.py`
- Modify: `src/quant_signal/ledger.py`
- Test: `tests/test_dedup.py`
- Test: `tests/test_ledger.py`
- Test: `tests/test_engine.py`

- [ ] Add failing ledger tests for counting pushed signals by strategy set.
- [ ] Add channel-specific hourly limits and make `_dedup()` select the relevant strategy set and quota.
- [ ] Move each scheduled workflow into one focused pipeline module, passing an Engine protocol to avoid circular runtime imports.
- [ ] Keep `Engine.run_premarket/run_intraday/run_watch_deviation/run_enrichment` as thin delegates.
- [ ] Run all engine, ledger, dedup, and scheduler tests after each extraction.

### Task 6: Research report and final verification

**Files:**
- Modify: `research/backtest_momentum.py` only if required for registry compatibility
- Create: `research/reports/momentum_32_ticker_<date>.md`
- Modify: `README.md` for new configuration and ranking behavior

- [ ] Run the 32-ticker momentum backtest against available local history and record data coverage, comparison metrics, costs, and survivorship/sample caveats; if coverage is insufficient, record that fact rather than fabricating metrics.
- [ ] Run `uv run pytest -q` and require zero failures.
- [ ] Run `uv run mypy src` and require zero issues.
- [ ] Run configuration load and a fake-notifier premarket smoke test without external side effects.
- [ ] Review `git diff --check`, the original refactor checklist, and repository status before completion.

