# Index Universe and Execution Advisory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unstable all-US-equity scan with a cached Nasdaq-100/S&P-500 discovery universe and produce read-only PAPER execution plans that state when, at what limit, and how many shares to consider.

**Architecture:** Keep the existing core strategy universe unchanged. Add an independent index-universe provider and SIP-backed incremental scanner, then feed only the top candidates into a deterministic execution planner backed by read-only Alpaca paper account data and a persisted state machine. AI may explain the generated plan but may not invent or alter structured prices or quantities.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, pandas, openpyxl, DuckDB, SQLite, APScheduler, pytest, mypy.

---

## File map

- Create `src/quant_signal/index_universe.py`: official constituent fetch, validation, symbol aliases, last-known-good JSON cache.
- Create `src/quant_signal/account.py`: read-only account protocol, Alpaca paper implementation, account/position/order models.
- Create `src/quant_signal/execution.py`: plan models, sizing rules, state transitions, validation.
- Create `src/quant_signal/pipelines/execution_plan.py`: generate daily plans and evaluate intraday transitions.
- Modify `src/quant_signal/config.py`: index-universe and execution-plan settings.
- Modify `src/quant_signal/datafeed/alpaca_source.py`: historical SIP daily-bar method and request feed selection.
- Modify `src/quant_signal/ledger.py`: additive schema and persistence methods for scan candidates, targets, plans, and events.
- Modify `src/quant_signal/pipelines/market_scan.py`: use the index provider, incremental SIP cache, write all valid bars and Top candidates.
- Modify `src/quant_signal/engine.py`: assemble providers and expose execution pipelines.
- Modify `src/quant_signal/notifier/cards.py`: discovery and PAPER execution-plan cards.
- Modify `src/quant_signal/ai_briefing.py`: structured execution-plan guardrails.
- Modify `src/quant_signal/scheduler.py`: replace `watch_deviation` with execution jobs, add runtime health signals.
- Modify `src/quant_signal/main.py`: dependency assembly.
- Modify `config/settings.yaml`: enable the new index pool and PAPER advisory; disable legacy deviation notifications.
- Modify `pyproject.toml` and `uv.lock`: add `openpyxl` for the official SPY daily holdings workbook.
- Create/modify tests listed in each task.

### Task 1: Configuration boundaries and feature flags

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing configuration tests**

Add tests asserting that index symbols do not expand `settings.universe`, invalid risk limits are rejected, and production defaults select read-only paper mode:

```python
def test_index_universe_does_not_expand_core_strategy_universe() -> None:
    settings = make_test_settings(
        index_universe={"enabled": True, "indices": ["sp500", "nasdaq100"]}
    )
    assert settings.universe == ["AAA", "BBB", "CCC", "DDD"]


def test_execution_plan_rejects_invalid_risk_limits() -> None:
    with pytest.raises(ValueError, match="risk_per_trade"):
        make_test_settings(execution_plan={"enabled": True, "risk_per_trade": 1.1})
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest tests/test_config.py -q`  
Expected: FAIL because `IndexUniverseSettings` and `ExecutionPlanSettings` do not exist.

- [ ] **Step 3: Add settings models**

Implement bounded Pydantic models with these defaults:

```python
class IndexUniverseSettings(BaseModel):
    enabled: bool = False
    indices: list[Literal["sp500", "nasdaq100"]] = ["sp500", "nasdaq100"]
    cache_path: str = "data/index_universe.json"
    refresh_days: int = Field(default=7, ge=1, le=30)
    max_stale_days: int = Field(default=14, ge=1, le=60)
    scan_top_n: int = Field(default=20, ge=5, le=50)
    execution_top_n: int = Field(default=5, ge=1, le=10)
    min_coverage: float = Field(default=0.98, ge=0.8, le=1.0)
    min_dollar_volume: float = Field(default=50_000_000, gt=0)


class ExecutionPlanSettings(BaseModel):
    enabled: bool = False
    account_provider: Literal["alpaca_paper", "none"] = "alpaca_paper"
    risk_per_trade: float = Field(default=0.005, gt=0, le=0.05)
    max_daily_new_risk: float = Field(default=0.01, gt=0, le=0.10)
    max_position_weight: float = Field(default=0.12, gt=0, le=0.50)
    max_cluster_weight: float = Field(default=0.35, gt=0, le=1.0)
    cash_reserve: float = Field(default=0.20, ge=0, lt=1.0)
    max_new_positions_per_day: int = Field(default=2, ge=1, le=10)
    min_stop_distance: float = Field(default=0.02, gt=0, lt=1.0)
    max_stop_distance: float = Field(default=0.20, gt=0, lt=1.0)
    quote_max_age_seconds: int = Field(default=420, ge=60, le=1800)
    account_max_age_seconds: int = Field(default=60, ge=10, le=600)


class LegacyPriceDeviationSettings(BaseModel):
    enabled: bool = False
```

Add a validator requiring `max_stop_distance > min_stop_distance`.

- [ ] **Step 4: Enable production flags without modifying the core ticker registry**

Add `index_universe.enabled: true`, `execution_plan.enabled: true`, `account_provider: alpaca_paper`, and `legacy_price_deviation.enabled: false` to `config/settings.yaml`. Do not append index members to `tickers` or `watchlist`.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_config.py -q`  
Expected: PASS.

### Task 2: Official index-universe provider and safe cache

**Files:**
- Create: `src/quant_signal/index_universe.py`
- Create: `tests/test_index_universe.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write failing parser, alias, and cache tests**

Cover Nasdaq API rows, a generated in-memory SPY workbook, overlap membership, `BRK.B` aliases, invalid counts, atomic last-known-good fallback, and stale snapshots:

```python
def test_merge_preserves_memberships_and_provider_aliases() -> None:
    snapshot = merge_members({"sp500": {"BRK.B", "AAPL"}, "nasdaq100": {"AAPL", "MSFT"}}, NOW)
    assert snapshot.members["AAPL"].memberships == ("nasdaq100", "sp500")
    assert to_yfinance_symbol("BRK.B") == "BRK-B"
    assert to_canonical_symbol("BRK-B") == "BRK.B"


def test_failed_refresh_keeps_last_known_good(tmp_path: Path) -> None:
    cache = UniverseCache(tmp_path / "index.json")
    cache.save(valid_snapshot())
    got = IndexUniverseProvider(cache=cache, fetchers={"sp500": failing_fetcher}).load(NOW)
    assert got.content_hash == valid_snapshot().content_hash
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_index_universe.py -q`  
Expected: FAIL on missing module.

- [ ] **Step 3: Implement immutable models and canonical aliases**

Create `IndexMember` and `IndexUniverseSnapshot` dataclasses, deterministic SHA-256 content hashing, `BRK.B/BF.B` provider mappings, count validation, and JSON serialization.

- [ ] **Step 4: Implement official fetchers**

Use Nasdaq's official JSON endpoint:

```text
https://api.nasdaq.com/api/quote/list-type/nasdaq100
```

Use State Street's official daily SPY holdings workbook as the S&P-500 constituent source:

```text
https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx
```

Filter workbook rows to valid equity tickers and exclude cash/futures. Parse with `pandas.read_excel(BytesIO(content), engine="openpyxl")`.

- [ ] **Step 5: Implement safe refresh and fallback**

Write JSON to a sibling temporary file and use `Path.replace()` only after full validation. If fetch/parse/validation fails, load the last-known-good snapshot. If its age exceeds `max_stale_days`, raise `StaleUniverseError` so callers fail closed.

- [ ] **Step 6: Lock dependencies and run tests**

Run: `uv add 'openpyxl>=3.1,<4'`  
Run: `uv run pytest tests/test_index_universe.py -q`  
Expected: PASS.

### Task 3: SIP historical bars and index scanner

**Files:**
- Modify: `src/quant_signal/datafeed/alpaca_source.py`
- Modify: `src/quant_signal/pipelines/market_scan.py`
- Modify: `src/quant_signal/scanner.py`
- Modify: `src/quant_signal/ledger.py`
- Modify: `tests/test_alpaca_source.py`
- Modify: `tests/test_market_scan.py`
- Modify: `tests/test_scanner.py`

- [ ] **Step 1: Write failing SIP and bounded-universe tests**

Assert `feed=sip` is sent for prior-session daily bars, the scanner never calls `list_active_symbols()` when index mode is enabled, all valid fetched bars are cached, coverage below the configured floor blocks candidates, and Top20 are persisted with memberships.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_alpaca_source.py tests/test_market_scan.py tests/test_scanner.py -q`  
Expected: FAIL because no SIP method or index provider integration exists.

- [ ] **Step 3: Add an explicit SIP method**

Refactor `_fetch` to accept `feed: Literal["iex", "sip"]` and add:

```python
def fetch_sip_daily_bars(self, tickers: list[str], start: date, end: date) -> pd.DataFrame:
    return self._fetch(tickers, "1Day", start.isoformat(), end.isoformat(), feed="sip")
```

Keep existing strategy data behavior unchanged unless it explicitly calls the SIP method.

- [ ] **Step 4: Add scan-candidate persistence**

Extend the additive SQLite schema with `scan_candidates(scan_date, ticker, rank, score, price, extra_json, as_of, PRIMARY KEY(scan_date,ticker))` and add `replace_scan_candidates()` / `latest_scan_candidates()` methods.

- [ ] **Step 5: Replace all-market enumeration**

When `settings.index_universe.enabled` is true, load canonical symbols from the provider, query SIP bars in bounded chunks, write every valid bar to BarStore, read the 210-day window from BarStore, validate freshness/coverage, score the pool, persist Top20, and render one observation card. Do not use the current 12,420-symbol path.

- [ ] **Step 6: Add execution fields for Top candidates**

For the first `execution_top_n`, compute `entry_low`, `entry_high`, `target_buy`, `stop_loss`, and `take_profit` with existing indicators. Store them in candidate `extra_json`. Invalid price ordering must add a block reason rather than produce a plan.

- [ ] **Step 7: Run focused and 600-symbol load tests**

Run: `uv run pytest tests/test_alpaca_source.py tests/test_market_scan.py tests/test_scanner.py -q`  
Expected: PASS and the synthetic 600-symbol test completes under 10 seconds locally.

### Task 4: Read-only Alpaca paper account adapter

**Files:**
- Create: `src/quant_signal/account.py`
- Create: `tests/test_account.py`

- [ ] **Step 1: Write failing account parsing tests**

Mock `/v2/account`, `/v2/positions`, open orders, and closed orders. Assert Decimal-safe parsing, canonical symbols, empty account lists, no POST/DELETE methods, and credential redaction in errors.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_account.py -q`  
Expected: FAIL on missing module.

- [ ] **Step 3: Implement read models and provider protocol**

Create frozen `AccountSnapshot`, `BrokerPosition`, and `BrokerOrder` dataclasses plus:

```python
class AccountProvider(Protocol):
    def snapshot(self, now: datetime) -> AccountState: ...
```

Implement `AlpacaPaperAccountProvider` using only GET requests to `https://paper-api.alpaca.markets`. Store no credentials in repr, exception messages, cards, or logs.

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_account.py -q`  
Expected: PASS.

### Task 5: Pure execution sizing and state machine

**Files:**
- Create: `src/quant_signal/execution.py`
- Create: `tests/test_execution.py`

- [ ] **Step 1: Write failing sizing tests**

Cover risk, target-weight gap, cash reserve, existing positions, open orders, max-position cap, zero quantity, stop distance guards, stale account/quote, and scan candidates without target weights:

```python
def test_quantity_is_minimum_of_gap_risk_cash_and_cap() -> None:
    plan = build_plan(candidate(), account(), positions=(), orders=(), config=CONFIG, now=NOW)
    assert plan.suggested_qty == min(plan.gap_qty, plan.risk_qty, plan.cash_qty, plan.cap_qty)


def test_too_tight_stop_blocks_buy() -> None:
    plan = build_plan(candidate(entry_low=100, entry_high=101, stop_loss=100), account(), (), (), CONFIG, NOW)
    assert plan.state is PlanState.BLOCKED
    assert plan.block_reason == "STOP_TOO_TIGHT"
```

- [ ] **Step 2: Write failing state-transition tests**

Cover `CANDIDATE -> ARMED -> IN_ENTRY_ZONE -> ACTIONABLE`, invalidation below stop, expiry, no action before 09:45 ET, full 5-minute confirmation, position-detected `OPEN`, stop/take-profit exits, and illegal transition rejection.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/test_execution.py -q`  
Expected: FAIL on missing module.

- [ ] **Step 4: Implement immutable plan models and sizing**

Use integer whole-share quantities, finite positive price validation, conservative `min()` sizing, and explicit block reasons. The deterministic output must include every intermediate cap for auditability.

- [ ] **Step 5: Implement the transition function**

Use a pure function:

```python
def advance_plan(plan: ExecutionPlan, observation: PlanObservation, config: ExecutionPlanSettings) -> PlanTransition:
    ...
```

Only a complete 5-minute observation may create `ACTIONABLE`. Repeated observations in the same state return no event.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_execution.py -q`  
Expected: PASS.

### Task 6: Execution-plan persistence and idempotent events

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Modify: `tests/test_ledger.py`

- [ ] **Step 1: Write failing migration and idempotency tests**

Verify an existing database upgrades without losing signals, target holdings are stored separately, plans upsert by `(plan_id, plan_version)`, and `record_plan_event()` rejects duplicate successful events.

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/test_ledger.py -q`  
Expected: FAIL on missing tables/methods.

- [ ] **Step 3: Add additive schema**

Create `schema_meta`, `strategy_targets`, `account_snapshots`, `broker_positions`, `broker_orders`, `execution_plans`, and `plan_events`; keep the old holdings table readable for migration compatibility. Store plan payload as validated JSON plus indexed state/date/ticker columns.

- [ ] **Step 4: Implement atomic methods**

Add `set_strategy_targets`, `get_strategy_targets`, `replace_account_state`, `latest_account_snapshot`, `upsert_execution_plan`, `active_execution_plans`, `record_plan_event`, and `event_was_delivered`, all under the existing ledger lock and transaction.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/test_ledger.py -q`  
Expected: PASS.

### Task 7: Pipelines, cards, scheduler, and AI guardrails

**Files:**
- Create: `src/quant_signal/pipelines/execution_plan.py`
- Modify: `src/quant_signal/pipelines/premarket.py`
- Modify: `src/quant_signal/pipelines/deviation.py`
- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/main.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/ai_briefing.py`
- Modify: `src/quant_signal/scheduler.py`
- Create: `tests/test_execution_pipeline.py`
- Modify: `tests/test_cards.py`
- Modify: `tests/test_ai_briefing.py`
- Modify: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing daily-card and intraday-event tests**

Assert the daily card contains the PAPER label, account time, cash, positions, open orders, recent fills, limit/qty/notional/stop/take-profit/expiry, and block reasons. Assert a normal ±2% move emits no card and each plan transition emits at most once.

- [ ] **Step 2: Write failing scheduler tests**

Require `execution_brief` at 08:15 ET and `execution_watch` every 5 minutes during US hours. Require no active `watch_deviation` job when legacy alerts are disabled. Add MISSED/MAX_INSTANCES event collection.

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/test_execution_pipeline.py tests/test_cards.py tests/test_ai_briefing.py tests/test_scheduler.py -q`  
Expected: FAIL.

- [ ] **Step 4: Implement daily plan generation**

At 08:15 ET, load recent core signals plus latest index candidates, fetch one account snapshot, aggregate by ticker, build/store plans, send one PAPER execution card, and optionally send an AI explanation based only on serialized plans.

- [ ] **Step 5: Implement intraday state evaluation**

During the US action window, fetch only active-plan tickers, use complete Alpaca IEX 5-minute bars, advance states, sync account positions/orders, and send only transition events. Keep `pipelines/deviation.py` available for rollback but do not schedule or notify from it when disabled.

- [ ] **Step 6: Separate targets from actual positions**

Change premarket rotation to call `set_strategy_targets`; migrate legacy holdings once as target metadata, never treat it as broker positions. Execution and exit alerts use only the account adapter's positions.

- [ ] **Step 7: Add AI constraints**

Add explicit prompt rules that structured execution `limit_price`, `suggested_qty`, `suggested_notional`, `stop_loss`, and `take_profit` are immutable; missing fields must be reported as unavailable; PAPER may not be described as live.

- [ ] **Step 8: Run focused tests**

Run: `uv run pytest tests/test_execution_pipeline.py tests/test_cards.py tests/test_ai_briefing.py tests/test_scheduler.py -q`  
Expected: PASS.

### Task 8: Reliability, logging, replay, and production verification

**Files:**
- Modify: `src/quant_signal/scheduler.py`
- Modify: `src/quant_signal/logging_setup.py`
- Create: `research/replay_execution.py`
- Create: `tests/test_execution_replay.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_deploy.py`

- [ ] **Step 1: Add failing health and redaction tests**

Test job duration/last-success tracking, stale `market_scan` health failure, ERROR/MISSED/MAX_INSTANCES collection, and webhook URL redaction.

- [ ] **Step 2: Implement job runtime health**

Wrap scheduled jobs to record start, duration, and success. Pass a monotonic deadline into each market-scan fetch chunk and abort between chunks once 10 minutes is reached; every HTTP request also retains its shorter request timeout. Make heartbeat fail when a critical job has no recent success or remains running beyond its deadline.

- [ ] **Step 3: Implement deterministic replay**

Create a pure replay over historical 5-minute observations that compares old target-hit count with new ACTIONABLE count and reports entry rate, forward 1/5/20-day returns, and max adverse excursion. Mark current-membership historical results as survivorship-biased.

- [ ] **Step 4: Run all automated verification**

Run: `uv run pytest -q`  
Expected: all tests pass.  
Run: `uv run mypy src`  
Expected: no issues.  
Run: `git diff --check`  
Expected: no new whitespace errors beyond existing line-ending warnings.

- [ ] **Step 5: Run read-only external smoke tests**

Verify Nasdaq constituent count, SPY holdings count, Alpaca SIP daily bars, paper account, positions, and orders without printing credentials or account amounts. Expected: all endpoints succeed and constituent coverage is at least 98%.

- [ ] **Step 6: Create a rollback point and deploy**

Commit only intended source/config/test/doc changes, tag the release, back up SQLite/config, restart the `quant-signal` Scheduled Task, and verify jobs, process tree, latest service log, execution feature flags, and empty error log. Do not submit any broker order.

- [ ] **Step 7: Verify production output**

Run the market scan and execution brief end to end in PAPER mode. Expected: one bounded index candidate card plus one deterministic PAPER execution card; ordinary price deviations produce no notification.
