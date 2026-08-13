# Holding Alert Rate Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce minute-level holding alert noise with higher thresholds, a durable two-alert-per-ticker state machine, and a strict five-alert daily account budget whose final slot is reserved for upgrades or reversals.

**Architecture:** Keep price detection pure and add a separate pure rate-limit decision module that consumes current signals plus successful alerts already persisted for the current US trading day. The pipeline remains responsible for I/O: loading history, applying decisions, running cause research only for approved alerts, writing every approved or suppressed event, and sending cards. Existing `signals` rows remain the single source of truth, so restarts preserve quotas without a parallel state table.

**Tech Stack:** Python 3.11, pandas, Pydantic, SQLite, pytest, APScheduler/production Feishu integration.

---

### Task 1: Raise detector thresholds and expose stable alert strength

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `src/quant_signal/holding_price_alert.py`
- Test: `tests/test_config.py`
- Test: `tests/test_holding_price_alert.py`

- [ ] **Step 1: Write failing threshold boundary tests**

Add tests proving stock boundaries are 2%/4%/6%/10%, ETF boundaries are 1.2%/2.5%/4%/6%, and volume-only triggering needs both 4× volume and 1% one-minute movement. Assert each generated signal stores its numeric `strength_score = abs(move) / effective_threshold`, direction, severity, and selected window.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_holding_price_alert.py -q`

Expected: failures show old defaults (1.5%/3%/5%/8% and 0.75%) and missing `strength_score`.

- [ ] **Step 3: Implement the minimal detector/config changes**

Update `HoldingPriceAlertSettings` defaults and tracked production YAML. Store the exact winning candidate score under `extra["strength_score"]`; retain existing `severity`, moves, thresholds, and observational card semantics.

- [ ] **Step 4: Run focused tests and confirm GREEN**

Run the command from Step 2 and require zero failures.

- [ ] **Step 5: Commit**

```powershell
git add src/quant_signal/config.py config/settings.yaml src/quant_signal/holding_price_alert.py tests/test_config.py tests/test_holding_price_alert.py
git commit -m "feat: raise holding alert thresholds"
```

### Task 2: Build the pure daily rate-limit state machine

**Files:**
- Create: `src/quant_signal/holding_alert_rate_limit.py`
- Create: `tests/test_holding_alert_rate_limit.py`

- [ ] **Step 1: Write failing state-machine tests**

Define wished-for immutable types `PriorHoldingAlert`, `AlertDisposition`, and `HoldingAlertDecision`, plus `select_holding_alerts(candidates, prior_alerts, regular_slots=4, daily_cap=5, per_ticker_cap=2)`. Cover:

- first ticker alert is `FIRST`;
- different detection window alone is `NO_MEANINGFUL_UPGRADE`;
- same direction requires a strictly higher severity and score at least 1.5;
- opposite direction over threshold is `REVERSAL`;
- third ticker alert is `TICKER_DAILY_CAP`;
- first four global slots accept ordinary first alerts;
- slot five accepts only `UPGRADE` or `REVERSAL`;
- daily cap five suppresses all remaining candidates;
- deterministic priority is reversal, upgrade, first; then score descending and ticker ascending.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m pytest tests/test_holding_alert_rate_limit.py -q`

Expected: module import fails because the pure decision module does not exist.

- [ ] **Step 3: Implement immutable history and decision types**

`PriorHoldingAlert` must contain ticker, direction, severity, strength score, pushed_at, and alert kind. `HoldingAlertDecision` must pair each candidate with a disposition, `should_send`, and a stable suppression reason. Reject malformed history (nonfinite/nonpositive strength, invalid severity) rather than silently relaxing limits.

- [ ] **Step 4: Implement classification and quota selection**

Group successful history by ticker. Classify each candidate independently from detection window: no history → FIRST; opposite direction → REVERSAL; same direction with higher severity and score ≥1.5 → UPGRADE; otherwise suppress. Apply per-ticker and global quotas in deterministic priority order. Decisions must be returned for every input signal so suppressed events remain auditable.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run the command from Step 2 and require all tests pass.

- [ ] **Step 6: Run Ruff/MyPy for the new unit and commit**

```powershell
D:\claudeCode\量化交易\.venv\Scripts\python.exe -m ruff check src/quant_signal/holding_alert_rate_limit.py tests/test_holding_alert_rate_limit.py
$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m mypy src/quant_signal/holding_alert_rate_limit.py
git add src/quant_signal/holding_alert_rate_limit.py tests/test_holding_alert_rate_limit.py
git commit -m "feat: add holding alert daily rate limiter"
```

### Task 3: Read durable successful alert history from the ledger

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Test: `tests/test_ledger.py`

- [ ] **Step 1: Write failing ledger query tests**

Specify `pushed_strategy_signals_since(strategy_id, since)` returning only `pushed=1` rows for that strategy, ordered by `pushed_at`, with parsed `extra` mappings. Prove unsuccessful/suppressed rows, other strategies, and rows before US-day midnight are excluded.

- [ ] **Step 2: Run the test and confirm RED**

Run the exact new `tests/test_ledger.py` cases and confirm the method is missing.

- [ ] **Step 3: Implement the read-only query**

Use the existing ledger lock, UTC ISO comparison, parameterized SQL, and defensive JSON parsing. Do not add a new table or schema migration.

- [ ] **Step 4: Run focused ledger tests and commit**

```powershell
$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m pytest tests/test_ledger.py -q
git add src/quant_signal/ledger.py tests/test_ledger.py
git commit -m "feat: expose pushed strategy alert history"
```

### Task 4: Integrate rate limiting, audit metadata, and cards

**Files:**
- Modify: `src/quant_signal/pipelines/holding_price_alert.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Test: `tests/test_holding_price_alert_pipeline.py`
- Test: `tests/test_cards.py`

- [ ] **Step 1: Write failing pipeline integration tests**

Cover restart-persistent history, different-window suppression, meaningful upgrade, reversal, per-ticker cap, regular global cap at four, emergency fifth slot, strict cap five, deterministic same-batch priority, and failed sends not consuming later quota. Assert every suppressed signal is inserted with `pushed=False` and `extra["suppression_reason"]`; approved signals contain `alert_kind` and `ticker_alert_number`.

- [ ] **Step 2: Write failing research/card tests**

Prove `research_price_move_causes` receives only approved candidates. Assert card text displays `首次异动`, `重大升级（当日第 2 次）`, or `方向反转（当日第 2 次）` from audit metadata without changing existing price-window fields.

- [ ] **Step 3: Run focused tests and confirm RED**

Run: `$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m pytest tests/test_holding_price_alert_pipeline.py tests/test_cards.py -q`

Expected: existing window-key cooldown allows duplicates and card lacks alert-kind text.

- [ ] **Step 4: Replace window-key cooldown with daily durable decisions**

Load successful current-US-day history through the new ledger API, convert rows to `PriorHoldingAlert`, run the pure selector, and write suppressed candidates immediately with standard reason metadata. Only approved candidates proceed to news/search and notifier delivery. After each successful send, append that alert to in-memory history/update batch quotas so later candidates in the same minute cannot overrun limits.

- [ ] **Step 5: Add audit labels and structured logging**

Enrich approved signals with `alert_kind`, `ticker_alert_number`, and strength. Extend completion logs with first/upgrade/reversal counts and suppression-reason counts. Card formatting must remain observational and must not add trading instructions.

- [ ] **Step 6: Run focused tests and confirm GREEN**

Run the command from Step 3 and require zero failures.

- [ ] **Step 7: Commit**

```powershell
git add src/quant_signal/pipelines/holding_price_alert.py src/quant_signal/notifier/cards.py tests/test_holding_price_alert_pipeline.py tests/test_cards.py
git commit -m "feat: enforce holding alert daily quotas"
```

### Task 5: Documentation, full verification, and production E2E

**Files:**
- Modify: `README.md`
- Modify: `docs/安全与运维治理.md`

- [ ] **Step 1: Document the exact policy**

Record the new stock/ETF thresholds, 4×/1% volume rule, two-alert ticker cap, four regular plus one emergency account slots, US-day reset, restart persistence, and suppression audit behavior.

- [ ] **Step 2: Run changed-file verification**

Run focused holding tests, strict `mypy src`, critical Ruff rules on every changed Python file, full Ruff on the new pure module/tests, `git diff --check`, and lock check.

- [ ] **Step 3: Run the full test suite**

Run: `$env:PYTHONPATH='src'; D:\claudeCode\量化交易\.venv\Scripts\python.exe -m pytest -q`

Expected: all repository tests pass.

- [ ] **Step 4: Request full code review and fix all Critical/Important findings**

Review the complete feature range for quota correctness, time-zone boundaries, same-batch races, failed-send accounting, and production compatibility. Re-run Step 2–3 after every fix.

- [ ] **Step 5: Perform production end-to-end verification**

Stop the scheduled task and exact service process, fast-forward the feature branch into `feat/v0`, run a real holding alert through the production scheduler/pipeline with a controlled qualifying bar set and real Feishu notifier, and verify one intended card plus persisted audit metadata. Do not manufacture more than one external test alert. Restart the scheduled task and confirm `holding_price_alert` is registered with no new stderr/traceback.

- [ ] **Step 6: Push and clean up**

Push `feat/v0` to `origin`, confirm local/remote SHA equality and clean status, then remove the merged worktree and feature branch.
