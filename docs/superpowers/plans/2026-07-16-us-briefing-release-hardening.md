# US Briefing Release Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the release-blocking US briefing lifecycle, Codex integration, idempotency and replay defects, then deploy the repaired feature in production shadow mode.

**Architecture:** Keep report calculation pure, delay state activation until confirmed delivery, use one durable run per report slot, and make strategy validation follow executable next-session rules. Optional context providers fail closed without changing deterministic prices or quantities.

**Tech Stack:** Python 3.11, subprocess/Codex CLI, pandas, SQLite, APScheduler, pytest, mypy, YAML.

---

### Task 1: Codex Windows execution and AI guard

**Files:** `src/quant_signal/ai_briefing.py`, `tests/test_ai_briefing.py`

- [ ] Add failing tests proving the native packaged Codex executable is preferred over `codex.cmd`, numeric tokens in payload keys are allowed, and payload enum terms are allowed.
- [ ] Implement native executable discovery and key/value token collection.
- [ ] Run the AI unit tests and an actual native Codex read-only smoke.
- [ ] Commit the isolated fix.

### Task 2: Delivery-gated state activation

**Files:** `src/quant_signal/pipelines/us_briefing.py`, `tests/test_us_briefing_pipeline.py`

- [ ] Add failing tests proving shadow runs do not consume discipline state and failed delivery creates no active plans.
- [ ] Return dormant plans from candidate sizing and persist them only after successful live delivery.
- [ ] Verify failed-delivery retry activates plans and discipline exactly once after success.
- [ ] Commit the isolated fix.

### Task 3: Report-slot idempotency

**Files:** `src/quant_signal/ledger.py`, `src/quant_signal/pipelines/us_briefing.py`, `tests/test_ledger.py`, `tests/test_us_briefing_pipeline.py`

- [ ] Add failing tests for stable account versions and one run per `(report_kind, as_of)` despite changed retrieval time.
- [ ] Remove retrieval timestamps from the substantive account hash and add a unique report-slot index with a stable run id.
- [ ] Run migration and retry/idempotency tests.
- [ ] Commit the isolated fix.

### Task 4: Earnings, cluster and Asia safeguards

**Files:** `src/quant_signal/config.py`, `src/quant_signal/candidate_lanes.py`, `src/quant_signal/pipelines/us_briefing.py`, `config/settings.yaml`, corresponding tests.

- [ ] Add failing tests for earnings blackout, cross-lane cluster caps and futures/VIX context.
- [ ] Filter preliminary candidates by upcoming earnings without replacement and pass configured clusters into candidate discovery.
- [ ] Expand risk clusters and add non-tradable `NQ=F`, `ES=F`, `^VIX` context.
- [ ] Run candidate, config and pipeline tests; commit.

### Task 5: Execution-aware candidate replay

**Files:** `research/backtest_us_candidate_lanes.py`, `tests/test_us_candidate_replay.py`

- [ ] Add failing tests proving no same-close execution, no fill above the entry zone, conservative stop/target ordering and point-in-time membership CLI support.
- [ ] Implement next-session entry, multi-session positions, exits, costs and execution metrics while reusing production regime/candidate functions.
- [ ] Run replay tests and a synthetic CLI replay; commit.

### Task 6: Review, end-to-end verification and shadow deployment

**Files:** implementation status in this plan and existing release documentation.

- [ ] Review the complete diff for accidental A-share changes, secrets, broad exceptions and notification/state ordering.
- [ ] Run focused tests, full pytest, mypy, lock and diff checks, scheduler dry run, and actual Codex CLI guard smoke.
- [ ] Fast-forward the clean production worktree to the verified branch while retaining `delivery_mode: shadow`.
- [ ] Restart the documented service safely, verify health/logs and confirm both new briefing jobs are registered without sending a test trade message.
- [ ] Record exact verification evidence and commit the status update.
