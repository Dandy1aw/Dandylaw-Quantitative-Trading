# Fear Index DCA Reminder Implementation Plan

> **For Codex:** Execute this plan with `executing-plans`, task by task, using test-driven development and the repository `verify` recipe before completion.

**Goal:** Deliver a weekday 09:30 Asia/Shanghai Feishu reminder based on the latest completed VIX/VXN session, with SPY/QQQM context, deterministic multiplier rules, a 60-session PNG chart, persisted/idempotent results, and `定投` / `定投规则` bot commands.

**Architecture:** Keep market calculations and recommendation rules pure in `fear_dca.py`. A dedicated pipeline fetches and aligns adjusted Yahoo daily bars, validates completeness, persists a session-keyed run, renders the optional chart, and sends a stored card. The ledger owns durable state and replay, while scheduler/engine/bot are thin adapters.

**Tech Stack:** Python 3.11, pandas, yfinance, Pillow, SQLite, APScheduler, Feishu/Lark SDK, pytest.

---

### Task 1: Pure metrics and recommendation rules

**Files:**
- Create: `src/quant_signal/fear_dca.py`
- Test: `tests/test_fear_dca.py`

1. Write failing boundary tests for every VIX/VXN tier, drawdown bonus, 3x cap, `0x` semantics, MA interpretation, and exact 1/5/20-session returns.
2. Run `python -m pytest tests/test_fear_dca.py -q` and confirm RED.
3. Add frozen value objects for fear metrics, ETF metrics, and recommendation decisions plus finite-data validation helpers.
4. Implement exact tier and one-time bonus rules from the approved design.
5. Re-run the focused tests and commit.

### Task 2: Durable session ledger and card serialization

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Modify: `src/quant_signal/notifier/base.py`
- Test: `tests/test_ledger.py`
- Test: `tests/test_notifier_base.py`

1. Write failing tests for `fear_dca_runs`: unique session, FAILED-to-COMPLETE supersession, COMPLETE non-downgrade, latest COMPLETE lookup, stored card replay, chart/send status and error metadata.
2. Add the schema migration and typed save/read methods; store metrics and decisions as canonical JSON.
3. Extend `Card` serialization with an optional Feishu image key while remaining backward compatible with all existing stored cards.
4. Run focused tests and commit.

### Task 3: Text card and 60-session PNG chart

**Files:**
- Create: `src/quant_signal/fear_dca_chart.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_fear_dca_chart.py`
- Test: `tests/test_cards.py`

1. Write failing tests for card content (data date/source, current values, MA20/60, ETF 1/5/20 returns, base/bonus/final multipliers, rule legend, disclaimer, and data-incomplete alert).
2. Write failing image tests that open the result as a valid PNG and verify deterministic dimensions/non-empty output.
3. Add Pillow as a declared runtime dependency and implement a dependency-light two-panel chart with VIX/VXN close, MA20, MA60, thresholds, and latest annotations.
4. Implement complete and incomplete cards; support a full text fallback when image upload fails.
5. Run focused tests and commit.

### Task 4: Feishu image upload and image card element

**Files:**
- Modify: `src/quant_signal/feishu_bot.py`
- Modify: `src/quant_signal/notifier/feishu.py`
- Test: `tests/test_feishu.py`
- Test: `tests/test_feishu_bot.py`

1. Write failing tests for the Lark image upload call and interactive card image element.
2. Add `upload_image(bytes) -> image_key` to the app transport and an optional image-capable notifier method without breaking webhook/console notifiers.
3. Make `_to_feishu_payload` include an image element only when `Card.image_key` exists.
4. Verify upload errors degrade to text and never suppress the recommendation card.
5. Run focused tests and commit.

### Task 5: Pipeline validation, idempotency, and replay

**Files:**
- Create: `src/quant_signal/pipelines/fear_dca.py`
- Modify: `src/quant_signal/engine.py`
- Test: `tests/test_fear_dca_pipeline.py`

1. Write failing tests for latest completed US session selection, four-symbol same-session alignment, >=60-session/full-window validation, stale-session dedupe, failed-run notice dedupe, successful persistence/send, chart upload degradation, and replay without refetch.
2. Fetch >=100 natural days through `YFinanceSource`, normalize each symbol, align the exact target session, and fail closed on incomplete/nonfinite data.
3. Persist FAILED before sending one incomplete notice; persist COMPLETE with the fully rendered card and decision reasons before/after delivery without allowing downgrade.
4. Upload the chart when supported; otherwise deliver the same complete text card and record degradation.
5. Expose `run_fear_dca()` and `resend_latest_fear_dca()` on `Engine`.
6. Run focused tests and commit.

### Task 6: Configuration, scheduler, and bot commands

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/config.example.yaml`
- Modify: `config/config.yaml`
- Modify: `src/quant_signal/scheduler.py`
- Modify: `src/quant_signal/feishu_bot.py`
- Test: `tests/test_config.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_feishu_bot.py`

1. Write failing tests for config defaults/validation and a Mon-Fri 09:30 `Asia/Shanghai` scheduler trigger.
2. Add an enabled `fear_dca` settings block with schedule, symbols, lookback, thresholds, drawdown rules, and chart settings.
3. Add the scheduler job with coalescing, single instance, and session idempotency handled by the pipeline.
4. Add `定投` (replay latest COMPLETE only) and `定投规则` (static rules) commands, help text, authorization/group behavior, and tests proving no market refetch for replay.
5. Run focused tests and commit.

### Task 7: Full verification and production end-to-end

**Files:**
- Create: `scripts/live_fear_dca_e2e.py`
- Modify: `README.md` or operational docs if command/schedule documentation is missing

1. Run changed-file Ruff, MyPy, focused tests, full `pytest -q`, and `git diff --check` using the current repository virtualenv.
2. Run the live pipeline against Yahoo and the configured Feishu app, confirm the four symbols share the latest completed session, inspect the persisted COMPLETE run, and confirm one real card is delivered (with chart when upload is supported).
3. Exercise `定投规则` and latest-card replay through the real bot transport path without refetching.
4. Stop the production scheduled task and exact `quant-signal` process, merge the feature branch into `feat/v0`, then restart the scheduled task.
5. Verify `deploy/status.ps1`, zero new scheduler stderr, registered `fear_dca` job, and logs/ledger evidence.
6. Request code review, address findings, re-run verification, commit, and push `feat/v0` to `origin`.
