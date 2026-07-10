# Screenshot Portfolio, Action Card, and Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the incorrect 100,000 USD PAPER sizing source with a validated 6,000 USD screenshot account, support Codex-powered screenshot imports, produce one concise action card, and harden market-data and notification delivery.

**Architecture:** Keep screenshot observations separate from exact broker positions, expose them through a read-only screenshot account provider, and make execution sizing consume an explicit portfolio budget. Use a persistent retrying Alpaca client, structured card sections, and a durable SQLite notification outbox. All production changes remain advisory-only and fail closed when account, currency, or position precision is insufficient.

**Tech Stack:** Python 3.11, Pydantic 2, SQLite, httpx, APScheduler, Codex CLI, pytest, mypy, Feishu interactive cards.

---

## File map

- Create `src/quant_signal/portfolio_import.py`: import schema, validation, Codex CLI extraction, and import receipt.
- Create `src/quant_signal/screenshot_account.py`: read-only provider over the active screenshot import.
- Create `src/quant_signal/action_card.py`: daily action-card view model and compact formatting policy.
- Create `tests/test_portfolio_import.py`, `tests/test_screenshot_account.py`, `tests/test_action_card.py`.
- Modify `src/quant_signal/ledger.py`: schema v3 observations, active import, plan invalidation, and delivery outbox.
- Modify `src/quant_signal/config.py`, `config/settings.yaml`: screenshot account and 6,000/20% financing policy.
- Modify `src/quant_signal/account.py`, `src/quant_signal/engine.py`: account metadata and provider wiring.
- Modify `src/quant_signal/execution.py`: financing/cumulative-notional/theme/currency limits.
- Modify `src/quant_signal/pipelines/execution_plan.py`: account source, currency propagation, one-card flow, outbox, and failure propagation.
- Modify `src/quant_signal/datafeed/alpaca_source.py`, `src/quant_signal/scheduler.py`: shared client, retry, and one-minute offset.
- Modify `src/quant_signal/notifier/base.py`, `src/quant_signal/notifier/cards.py`, `src/quant_signal/notifier/feishu.py`: structured sections and compact rendering.
- Modify `src/quant_signal/pipelines/market_scan.py`, `src/quant_signal/pipelines/premarket.py`: store detailed research without duplicate fixed pushes.
- Modify `deploy/install-task.ps1`, `deploy/status.ps1`, `.gitignore`, `README.md` only where required by the new workflow; preserve unrelated existing README edits.

### Task 1: Persist validated screenshot observations

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Create: `tests/test_portfolio_import.py`
- Modify: `tests/test_ledger.py`

- [ ] **Step 1: Write failing migration and import lifecycle tests**

Cover schema version 3, SHA256 idempotency, account summary persistence, nullable position detail, `PARTIAL` not replacing an existing complete import, `ACTIVE` atomic replacement, and `invalidate_active_plans("ACCOUNT_CHANGED")`.

```python
def test_partial_import_keeps_last_complete_positions(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    complete = screenshot_import(status="ACTIVE", symbols=("MU", "SMH"))
    partial = screenshot_import(status="PARTIAL", symbols=("MU",))
    ledger.save_portfolio_import(complete)
    ledger.save_portfolio_import(partial)
    assert [p.symbol for p in ledger.active_observed_positions()] == ["MU", "SMH"]
    assert ledger.latest_observed_account()["equity"] == "5995.52"
```

- [ ] **Step 2: Verify the tests fail**

Run: `.venv\Scripts\pytest.exe tests/test_ledger.py tests/test_portfolio_import.py -q`  
Expected: FAIL because observation tables and APIs do not exist.

- [ ] **Step 3: Implement schema v3 and repository methods**

Add `portfolio_imports`, `observed_account_snapshots`, and `observed_positions`; store Decimal as TEXT. Expose `save_portfolio_import`, `latest_observed_account`, `active_observed_positions`, `portfolio_import_by_hash`, and `invalidate_active_plans`. Use a single SQLite transaction for activation.

- [ ] **Step 4: Run focused tests and commit**

Run: `.venv\Scripts\pytest.exe tests/test_ledger.py tests/test_portfolio_import.py -q`  
Expected: PASS.

Commit: `feat: persist validated screenshot portfolios`

### Task 2: Extract and validate screenshots with Codex CLI

**Files:**
- Create: `src/quant_signal/portfolio_import.py`
- Modify: `src/quant_signal/main.py`
- Modify: `tests/test_portfolio_import.py`

- [ ] **Step 1: Write failing extractor and validation tests**

Test argument construction without invoking Codex, strict null handling, account reconciliation, reported/visible count mismatch, weight reconciliation, duplicate image idempotency, timeout, malformed JSON, and last-known-good preservation.

```python
def test_visible_count_mismatch_is_partial() -> None:
    result = validate_extraction(extraction(reported_position_count=6, positions=five_rows()))
    assert result.status == ImportStatus.PARTIAL
    assert "POSITION_COUNT_MISMATCH" in result.validation_errors
    assert all(row.qty is None for row in result.positions)
```

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_portfolio_import.py -q`  
Expected: FAIL because extractor models are absent.

- [ ] **Step 3: Implement models, validation, and CLI runner**

Use Pydantic models with Decimal values and `extra="forbid"`. Invoke `codex exec --image ... --output-schema ... --output-last-message ... --ephemeral --sandbox read-only` from an empty temporary directory with a 180-second default timeout. Do not infer missing quantities. Add a module entrypoint accepting repeated `--image`, `--capital-limit`, `--max-financing-ratio`, `--apply`, and `--dry-run`.

- [ ] **Step 4: Verify focused tests and a dry-run fixture**

Run: `.venv\Scripts\pytest.exe tests/test_portfolio_import.py -q`  
Expected: PASS.

Run against a deterministic JSON fixture with a fake subprocess runner; expected receipt includes `VALIDATED account / PARTIAL positions`, `reported=6`, `visible=5`, and no AAPL position.

Commit: `feat: import portfolio screenshots with codex`

### Task 3: Activate the screenshot account and funding policy

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `src/quant_signal/account.py`
- Create: `src/quant_signal/screenshot_account.py`
- Modify: `src/quant_signal/engine.py`
- Create: `tests/test_screenshot_account.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_engine.py`

- [ ] **Step 1: Write failing provider/config tests**

```python
def test_screenshot_provider_caps_equity_and_financing(ledger: SignalLedger) -> None:
    state = ScreenshotAccountProvider(ledger, capital_limit=Decimal("6000"), max_financing_ratio=Decimal("0.20")).snapshot(NOW)
    assert state.snapshot.equity == Decimal("5995.52")
    assert state.snapshot.capital_limit == Decimal("6000")
    assert state.snapshot.max_gross_exposure == Decimal("7200")
    assert "AAPL" not in {p.symbol for p in state.observed_positions}
```

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_config.py tests/test_engine.py tests/test_screenshot_account.py -q`  
Expected: FAIL on unsupported `screenshot` provider and metadata.

- [ ] **Step 3: Implement provider and production configuration**

Extend `ExecutionPlanSettings.account_provider` with `screenshot`; add `capital_limit_usd=6000`, `max_financing_ratio=0.20`, `screenshot_max_age_hours`, and set `cash_reserve=0`. Extend account state with source/limit/observed exposure without pretending nullable observed rows are exact `BrokerPosition`. Wire `Engine` to `ScreenshotAccountProvider` and prevent Alpaca PAPER refresh when selected.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\pytest.exe tests/test_config.py tests/test_account.py tests/test_engine.py tests/test_screenshot_account.py -q`  
Expected: PASS.

Commit: `feat: size plans from screenshot account`

### Task 4: Enforce financing, cumulative budget, concentration, and currency

**Files:**
- Modify: `src/quant_signal/execution.py`
- Modify: `src/quant_signal/pipelines/execution_plan.py`
- Modify: `tests/test_execution.py`
- Modify: `tests/test_execution_pipeline.py`

- [ ] **Step 1: Write failing risk-policy tests**

Cover: gross exposure never above 7,200; all 1,751.13 cash may be used; borrowing never above 1,200; actual buying power still caps plans; accepted plans consume budget sequentially; observed MU/SMH exposure prevents duplicate/overweight entries; cluster exposure over 35% blocks correlated additions; non-USD candidates block as `UNSUPPORTED_MARKET`; AAPL is treated as a new position.

```python
def test_portfolio_budget_never_exceeds_twenty_percent_financing() -> None:
    limited = apply_portfolio_limits(plans, budget=PortfolioBudget(equity=D("5995.52"), cash=D("1751.13"), current_exposure=D("4244.15"), capital_limit=D("6000"), max_financing_ratio=D("0.20")))
    assert sum(D(str(p.suggested_notional or 0)) for p in limited if p.state is PlanState.CANDIDATE) <= D("2955.85")
```

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_execution.py tests/test_execution_pipeline.py -q`  
Expected: FAIL because current limits reuse cash and ignore cluster/currency.

- [ ] **Step 3: Implement the portfolio budget**

Add a `PortfolioBudget` value object. Clamp effective equity and gross exposure, preallocate accepted plan notional, and carry observed positions/cluster classification into portfolio limits. Propagate ticker currency from settings; block non-USD before quantity math. On account change, expire stale plans before inserting regenerated versions.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\pytest.exe tests/test_execution.py tests/test_execution_pipeline.py -q`  
Expected: PASS.

Commit: `feat: enforce real account portfolio budget`

### Task 5: Make Alpaca market data resilient

**Files:**
- Modify: `src/quant_signal/datafeed/alpaca_source.py`
- Modify: `src/quant_signal/pipelines/execution_plan.py`
- Modify: `src/quant_signal/scheduler.py`
- Modify: `tests/test_alpaca_source.py`
- Modify: `tests/test_execution_pipeline.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing retry/schedule tests**

Test one connect/read timeout then success, three transient failures then raise, 401 without retry, client reuse across fetches, `execution_watch` trigger `1-56/5`, and network exhaustion propagating to JobRuntime.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_alpaca_source.py tests/test_scheduler.py tests/test_execution_pipeline.py -q`  
Expected: FAIL because requests use module-level `httpx.get`, no retry, and jobs share a minute.

- [ ] **Step 3: Implement shared client and bounded retry**

Inject an `_HTTPClient`, default to one `httpx.Client` with keepalive and `Timeout(connect=10, read=30, write=10, pool=10)`. Retry only transient connection/protocol errors up to three attempts with injectable sleep delays `(0.5, 1.5)`. Change execution watch to `minute="1-56/5"` and re-raise final bar fetch failure.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\pytest.exe tests/test_alpaca_source.py tests/test_scheduler.py tests/test_execution_pipeline.py -q`  
Expected: PASS.

Commit: `fix: retry transient alpaca tls failures`

### Task 6: Render one concise daily action card

**Files:**
- Modify: `src/quant_signal/notifier/base.py`
- Create: `src/quant_signal/action_card.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/notifier/feishu.py`
- Modify: `src/quant_signal/pipelines/market_scan.py`
- Modify: `src/quant_signal/pipelines/premarket.py`
- Modify: `src/quant_signal/pipelines/execution_plan.py`
- Create: `tests/test_action_card.py`
- Modify: `tests/test_cards.py`
- Modify: `tests/test_feishu.py`
- Modify: `tests/test_market_scan.py`

- [ ] **Step 1: Write failing compact-card tests**

Assert one daily card, no Markdown table pipes, at most three action candidates/two alternates/three holding actions, grouped Chinese blocked reasons, account source and 6,000/20% limits, AAPL labelled new candidate rather than holding, AI at most 300 Chinese characters, and one Feishu `div` per section.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_action_card.py tests/test_cards.py tests/test_feishu.py tests/test_market_scan.py -q`  
Expected: FAIL because current cards use one Markdown table and separate pushes.

- [ ] **Step 3: Implement structured sections and message orchestration**

Add `CardSection` and optional `sections` on `Card`; retain `body_md` for console compatibility. Build the action view model from account, plans, observed holding risks, Top5 scan results, and one AI summary. Stop the standalone Top20 and duplicate execution-AI pushes; data remains in the ledger. Translate block reasons to Chinese.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\pytest.exe tests/test_action_card.py tests/test_cards.py tests/test_feishu.py tests/test_market_scan.py tests/test_ai_briefing.py -q`  
Expected: PASS.

Commit: `feat: send one concise daily action card`

### Task 7: Deliver execution events through an outbox

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Modify: `src/quant_signal/pipelines/execution_plan.py`
- Modify: `tests/test_ledger.py`
- Modify: `tests/test_execution_pipeline.py`

- [ ] **Step 1: Write failing delivery lifecycle tests**

Test `PENDING -> SENT`, failed send retaining payload and incrementing attempts, retry after `next_retry_at`, duplicate state observations not creating duplicates, and multiple same-cycle events being batched with STOP events first.

- [ ] **Step 2: Verify failure**

Run: `.venv\Scripts\pytest.exe tests/test_ledger.py tests/test_execution_pipeline.py -q`  
Expected: FAIL because `record_plan_event` currently means “row exists”, not delivered.

- [ ] **Step 3: Implement outbox**

Migrate `plan_events` with `status`, `payload_json`, `attempts`, `next_retry_at`, `last_error`, and `sent_at`. Queue immutable card payload transactionally, send due events outside the transaction, then mark sent or schedule bounded retry. `run_watch` first drains due events and counts a send only after notifier success.

- [ ] **Step 4: Verify and commit**

Run: `.venv\Scripts\pytest.exe tests/test_ledger.py tests/test_execution_pipeline.py -q`  
Expected: PASS.

Commit: `fix: retry execution notifications from outbox`

### Task 8: Import the current screenshot and run end-to-end verification

**Files:**
- Data only: `data/signals.db` through the importer; do not commit runtime databases.
- Modify if needed: `.gitignore`, `README.md`, `deploy/status.ps1`
- Test: full suite.

- [ ] **Step 1: Run static and unit verification**

Run: `.venv\Scripts\pytest.exe -q`  
Expected: all tests pass.

Run: `.venv\Scripts\python.exe -m mypy src`  
Expected: success with no issues.

- [ ] **Step 2: Import image 3**

Run the importer with the supplied absolute image path, `--capital-limit 6000 --max-financing-ratio 0.20 --apply`. Expected receipt: equity 5,995.52; cash 1,751.13; market value 4,244.15; visible holdings DRAM/MU/RAM/SMH/SNXX; AAPL absent; reported six/visible five; account valid and positions partial.

- [ ] **Step 3: Generate a dry-run action card and execution plan**

Expected: no 100,000 USD/PAPER label; no 19-share AAPL plan; account limit 6,000 and maximum gross exposure 7,200; no non-USD quantity; no Markdown table; no duplicate daily AI card.

- [ ] **Step 4: Verify deployment scripts and register the task safely**

Run `deploy/status.ps1`, then re-register with `deploy/install-task.ps1` only if the current token can create the configured S4U/AtStartup task. Verify task state, trigger, logon type, last result, process, and log freshness. If registration fails, leave the existing running task intact and report the exact permission boundary.

- [ ] **Step 5: Restart production and observe two scheduler cycles**

Verify the service starts from the new commit, latest `.err.log` remains empty, intraday/execution_watch are one minute apart, and no account refresh replaces the screenshot with 100,000 USD PAPER.

- [ ] **Step 6: Commit code/config/docs and tag the release**

Stage only owned source, test, config, deploy, and documentation changes; preserve pre-existing README edits unless deliberately reconciled. Create a release commit and annotated version tag after all evidence is captured.
