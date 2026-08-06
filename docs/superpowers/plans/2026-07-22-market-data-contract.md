# Market Data Contract Implementation Plan

> **For agentic workers:** Execute this plan task-by-task in the current session. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make daily-bar provenance, final/live separation, session coverage, and read-only research access explicit without breaking existing OHLCV consumers.

**Architecture:** Extend the existing DuckDB tables in place and migrate legacy rows to explicit legacy metadata. Maintain a per-session coverage ledger atomically with bar writes; expose fail-closed coverage checks and a read-only store mode for research.

**Tech Stack:** Python 3.11, DuckDB, pandas, pytest, mypy.

---

### Task 1: Metadata and final-only reads

**Files:**
- Modify: `src/quant_signal/datafeed/store.py`
- Modify: `tests/test_store.py`

- [ ] Write failing tests asserting new writes persist `bar_state`, `adjustment`, `fetched_at`, and `data_version`; default daily reads exclude `live`; explicit `bar_state=None` reads both; legacy databases migrate with `final/split/legacy` metadata.
- [ ] Run `pytest tests/test_store.py -q` and confirm failures are caused by missing metadata APIs.
- [ ] Extend fresh schema and startup migration. Keep `read_daily_bars()` output columns unchanged. Add keyword-only metadata parameters to `write_daily_bars()` and a `read_daily_bar_metadata()` inspection method.
- [ ] Run `pytest tests/test_store.py -q` and confirm green.

### Task 2: Atomic coverage ledger and fail-closed checks

**Files:**
- Modify: `src/quant_signal/datafeed/store.py`
- Modify: `src/quant_signal/ingest.py`
- Modify: `tests/test_store.py`
- Modify: `tests/test_ingest.py`

- [ ] Write failing tests for per-ticker missing sessions, multi-ticker coverage failure, source replacement, and ingestion using the coverage ledger.
- [ ] Run the focused tests and confirm failure on missing `missing_daily_sessions()` and `require_daily_coverage()`.
- [ ] Add `bar_coverage_sessions`, update it in the same DuckDB transaction as bars, backfill legacy rows, add `BarCoverageError`, and route `find_missing_sessions()` through the ledger.
- [ ] Run `pytest tests/test_store.py tests/test_ingest.py -q` and confirm green.

### Task 3: Read-only research boundary

**Files:**
- Modify: `src/quant_signal/datafeed/store.py`
- Modify: `tests/test_store.py`

- [ ] Write a failing test that closes a writer, opens `BarStore(path, read_only=True)`, reads final bars, and rejects writes.
- [ ] Implement read-only connection mode, skip schema mutation in that mode, add `close()` and context-manager methods, and raise a clear error before any write.
- [ ] Run store tests, then `pytest -q` and `mypy src/quant_signal`.

### Task 4: Verification

- [ ] Run full pytest and mypy.
- [ ] Run `git diff --check` on all changed files.
- [ ] Do not migrate or mutate the live production database during verification; migration occurs on the next normal service startup.
