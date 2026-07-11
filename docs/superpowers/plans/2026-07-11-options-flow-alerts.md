# Options Flow Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable, mobile-friendly Cboe four-venue Call/Put Top10 alerts with material-change detection and optional Alpaca Indicative enrichment.

**Architecture:** Keep option discovery separate from the existing stock `DataSource`: a Cboe provider aggregates visible contract volume from C1/C2/BZX/EDGX, an optional Alpaca provider adds OI/quote/Greeks, and pure domain functions rank and compare scans. A dedicated SQLite scan/outbox subsystem atomically records each 15-minute slot and its card; a scheduled pipeline retries delivery and only emits baseline, material-change, and closing summaries.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, SQLite, APScheduler, pytest, mypy, Feishu interactive cards.

---

### Task 1: Domain models, OCC symbols, aggregation, and material-change rules

**Files:**
- Create: `src/quant_signal/options_flow.py`
- Create: `tests/test_options_flow.py`

- [x] **Step 1: Write failing tests for standard contract parsing and four-venue aggregation**

```python
def test_aggregate_visible_venue_volume_and_build_occ_symbol() -> None:
    rows = (
        venue_row("cone", "NVDA", "call", "2026-07-17", "210", 7000),
        venue_row("opt", "NVDA", "call", "2026-07-17", "210", 3000),
    )
    ranked = aggregate_and_rank(rows, top_n=10, discovery_limit=50)
    assert ranked[0].contract_symbol == "NVDA260717C00210000"
    assert ranked[0].volume == 10_000
    assert ranked[0].venues == ("cone", "opt")


def test_adjusted_and_expired_contracts_are_rejected() -> None:
    rows = (
        venue_row("cone", "2BMY", "put", "2026-07-17", "61.02", 50_000),
        venue_row("cone", "AAPL1", "call", "2026-07-17", "300", 40_000),
        venue_row("cone", "AAPL", "call", "2026-07-09", "300", 30_000),
    )
    assert aggregate_and_rank(rows, top_n=10, discovery_limit=50, session=date(2026, 7, 10)) == ()
```

- [x] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/test_options_flow.py -q`  
Expected: import failure because `quant_signal.options_flow` does not exist.

- [x] **Step 3: Implement immutable models and deterministic ranking**

```python
@dataclass(frozen=True)
class VenueOptionVolume:
    venue: str
    underlying: str
    side: Literal["call", "put"]
    expiration: date
    strike: Decimal
    volume: int
    captured_at: datetime


@dataclass(frozen=True)
class OptionContractVolume:
    contract_symbol: str
    underlying: str
    side: Literal["call", "put"]
    expiration: date
    strike: Decimal
    volume: int
    rank: int
    venues: tuple[str, ...]
    captured_at: datetime
    enrichment: OptionEnrichment | None = None
```

Implement `occ_symbol()`, `aggregate_and_rank()`, JSON serialization helpers, stable tie-breaking by volume descending then contract symbol, and `top_by_side()`.

- [x] **Step 4: Add RED tests for rank jump, volume surge, 0DTE threshold, and duplicate-slot identity**

```python
def test_detect_changes_requires_material_evidence() -> None:
    previous = snapshot(call("NVDA", rank=12, volume=8_000))
    current = snapshot(call("NVDA", rank=4, volume=19_000))
    changes = detect_material_changes(previous, current, policy())
    assert changes[0].flags == ("NEW_TOP10", "RANK_JUMP", "VOLUME_SURGE")
    assert changes[0].score >= 50


def test_zero_dte_uses_higher_surge_threshold() -> None:
    previous = snapshot(call("SPY", rank=2, volume=50_000, expiration=SESSION))
    current = snapshot(call("SPY", rank=2, volume=65_000, expiration=SESSION))
    assert detect_material_changes(previous, current, policy()) == ()
```

- [x] **Step 5: Implement `OptionFlowPolicy`, `OptionFlowChange`, `scan_slot()`, and `detect_material_changes()`**

Use hard gates from the design: new Top10, rank jump >=3, ordinary delta >=10,000, 0DTE/ETF delta >=20,000; `HIGH_TURNOVER` never triggers alone. Cap focus to two contracts per underlying after score sorting.

- [x] **Step 6: Run tests and commit**

Run: `pytest tests/test_options_flow.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/options_flow.py tests/test_options_flow.py
git commit -m "feat: model option flow rankings"
```

### Task 2: Cboe four-venue discovery provider

**Files:**
- Create: `src/quant_signal/datafeed/cboe_options.py`
- Create: `tests/test_cboe_options.py`

- [x] **Step 1: Write RED tests for venue parsing, category selection, retries, and fail-closed coverage**

```python
def test_fetch_aggregates_all_required_venues() -> None:
    client = FakeClient({
        "cone": cboe_payload(category="equity"),
        "ctwo": cboe_payload(category="all"),
        "opt": cboe_payload(category="all"),
        "exo": cboe_payload(category="all"),
    })
    result = CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)
    assert result.venue_coverage == 1.0
    assert {call["params"]["mkt"] for call in client.calls} == {"cone", "ctwo", "opt", "exo"}


def test_missing_venue_fails_closed() -> None:
    client = FakeClient({"cone": cboe_payload(), "ctwo": httpx.ReadTimeout("timeout")})
    with pytest.raises(OptionFlowFetchError, match="ctwo"):
        CboeOptionFlowSource(client=client, sleep=lambda _: None).fetch(NOW)
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_cboe_options.py -q`  
Expected: module import failure.

- [x] **Step 3: Implement the provider**

Use one persistent `httpx.Client`, separate connect/read/write/pool timeouts, and venues `cone/ctwo/opt/exo`. Parse `equity` for `cone`, `all` elsewhere. Retry `ConnectTimeout`, `ReadTimeout`, `ConnectError`, `RemoteProtocolError`, 5xx and 429 (honor `Retry-After`); do not retry other 4xx. Reject malformed categories, fewer than ten valid rows per side, partial venue coverage, negative volume, or invalid dates/strikes.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_cboe_options.py tests/test_options_flow.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/datafeed/cboe_options.py tests/test_cboe_options.py
git commit -m "feat: discover active options from Cboe venues"
```

### Task 3: Optional Alpaca Indicative enrichment

**Files:**
- Create: `src/quant_signal/datafeed/alpaca_options.py`
- Create: `tests/test_alpaca_options.py`

- [x] **Step 1: Write RED tests for 100-symbol batching, nullable fields, OI dates, and no OPRA downgrade**

```python
def test_enrich_joins_snapshot_and_contract_metadata() -> None:
    enricher = AlpacaOptionEnricher("k", "s", feed="indicative", client=client_for_sample())
    rows = (contract("AAPL260717C00300000"),)
    result = enricher.enrich(rows, NOW)
    item = result[rows[0].contract_symbol]
    assert item.open_interest == 1250
    assert item.open_interest_date == date(2026, 7, 8)
    assert item.multiplier == 100
    assert item.implied_volatility is not None


def test_opra_403_is_not_silently_downgraded() -> None:
    client = FakeClient([response(403, {"message": "OPRA agreement is not signed"})])
    with pytest.raises(httpx.HTTPStatusError):
        AlpacaOptionEnricher("k", "s", feed="opra", client=client).enrich((contract(),), NOW)
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_alpaca_options.py -q`  
Expected: module import failure.

- [x] **Step 3: Implement snapshot and metadata enrichment**

Fetch `/v1beta1/options/snapshots` in batches of at most 100 and `/v2/options/contracts/{symbol}` from paper API only for metadata absent from the in-memory daily cache. Preserve missing trade, quote, Greeks, IV, OI, OI date, and multiplier as `None`; calculate premium and Volume/OI only in the domain model when required inputs are present. Use the same bounded TLS retry rules as Task 2.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_alpaca_options.py tests/test_options_flow.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/datafeed/alpaca_options.py tests/test_alpaca_options.py src/quant_signal/options_flow.py
git commit -m "feat: enrich option flow with Alpaca indicative data"
```

### Task 4: Configuration and production policy

**Files:**
- Modify: `src/quant_signal/config.py`
- Modify: `config/settings.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_deploy.py`

- [x] **Step 1: Add RED configuration tests**

```python
def test_production_options_flow_policy_is_explicit() -> None:
    settings = load_settings()
    assert settings.option_flow.enabled is True
    assert settings.option_flow.feed == "indicative"
    assert settings.option_flow.venues == ["cone", "ctwo", "opt", "exo"]
    assert settings.option_flow.top_n == 10
    assert settings.option_flow.max_alerts_per_day == 4


@pytest.mark.parametrize("field,value", [("top_n", 0), ("min_venue_coverage", 0.9)])
def test_options_flow_rejects_unsafe_policy(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        OptionFlowSettings(**{field: value})
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_config.py tests/test_deploy.py -q`  
Expected: missing `OptionFlowSettings` / `option_flow`.

- [x] **Step 3: Implement `OptionFlowSettings` and YAML**

Add all fields from the design with Pydantic bounds; require exactly four unique supported venue codes when enabled and require `min_venue_coverage == 1.0` for production fail-closed behavior. Normalize excluded roots to uppercase. `Settings.option_flow` defaults disabled for isolated tests; production YAML explicitly enables it.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_config.py tests/test_deploy.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/config.py config/settings.yaml tests/test_config.py tests/test_deploy.py
git commit -m "feat: configure delayed options flow alerts"
```

### Task 5: SQLite scans and dedicated durable outbox

**Files:**
- Modify: `src/quant_signal/ledger.py`
- Modify: `tests/test_ledger.py`

- [x] **Step 1: Write RED ledger tests**

```python
def test_option_scan_and_card_are_saved_atomically_and_idempotently(ledger: SignalLedger) -> None:
    assert ledger.save_option_flow_scan(scan(), "baseline", card(), now=NOW, expires_at=LATER)
    assert not ledger.save_option_flow_scan(scan(), "baseline", card(), now=NOW, expires_at=LATER)
    assert len(ledger.due_option_flow_alerts(NOW)) == 1


def test_expired_option_alert_is_cancelled_not_delivered(ledger: SignalLedger) -> None:
    ledger.save_option_flow_scan(scan(), "change", card(), now=NOW, expires_at=NOW)
    assert ledger.due_option_flow_alerts(NOW + timedelta(seconds=1)) == []
    assert ledger.option_flow_alert_status(scan().slot) == "EXPIRED"


def test_account_change_does_not_cancel_option_outbox(ledger: SignalLedger) -> None:
    ledger.save_option_flow_scan(scan(), "change", card(), now=NOW, expires_at=LATER)
    ledger.invalidate_active_plans("ACCOUNT_CHANGED", now=NOW)
    assert len(ledger.due_option_flow_alerts(NOW)) == 1
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_ledger.py -q`  
Expected: missing option-flow ledger methods.

- [x] **Step 3: Add schema v5 and atomic methods**

Create `option_flow_scans`, `option_flow_rows`, and `option_flow_outbox`. Implement one transaction for scan rows plus optional queued card, stable event key `option-flow:{slot}:{alert_type}`, latest same-session scan reconstruction, pending/sent daily count, last alert time, due/expire, mark failed with retry, and mark sent. Never change `invalidate_active_plans()` to touch the new table.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_ledger.py tests/test_backup.py -q`  
Expected: PASS and schema version 5.

```bash
git add src/quant_signal/ledger.py tests/test_ledger.py tests/test_backup.py
git commit -m "feat: persist option flow scans and alerts"
```

### Task 6: Mobile Feishu card

**Files:**
- Modify: `src/quant_signal/notifier/cards.py`
- Create: `tests/test_option_flow_card.py`

- [x] **Step 1: Write RED card tests**

```python
def test_option_flow_card_has_two_top10_sections_without_tables() -> None:
    card = option_flow_card(snapshot_with_20_rows(), changes(), "baseline", NOW)
    assert len(card.sections) <= 5
    assert "CALL Top10" in card.body_md and "PUT Top10" in card.body_md
    assert "|---" not in card.body_md
    assert "Cboe四市场" in card.title
    assert "INDICATIVE" in card.body_md and "约15分钟" in card.body_md
    assert "Call成交不等于看涨" in card.body_md
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_option_flow_card.py -q`  
Expected: missing `option_flow_card`.

- [x] **Step 3: Implement five-section compact card**

Render data identity, at most five focus items, ten Call lines, ten Put lines, and interpretation/footer. Show delta/rank/DTE on ranking lines; show premium and Volume/OI only when present. Label Cboe visible-volume sum as a lower-bound approximation and Alpaca as Indicative. Keep body under 3,500 characters.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_option_flow_card.py tests/test_feishu.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/notifier/cards.py tests/test_option_flow_card.py
git commit -m "feat: render mobile option flow card"
```

### Task 7: Pipeline and Engine facade

**Files:**
- Create: `src/quant_signal/pipelines/option_flow.py`
- Modify: `src/quant_signal/engine.py`
- Modify: `src/quant_signal/main.py`
- Create: `tests/test_option_flow_pipeline.py`
- Modify: `tests/test_engine.py`

- [x] **Step 1: Write RED pipeline tests for baseline, silence, material change, close, and retry**

```python
def test_first_scan_queues_and_sends_one_baseline(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, scans=[baseline_scan()])
    engine.run_option_flow(NOW)
    assert [card.title for card in engine.notifier.cards] == ["美股期权热度 · Cboe四市场"]
    assert engine.ledger.option_flow_alert_count(SESSION) == 1


def test_ordinary_rank_noise_is_stored_without_push(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, scans=[baseline_scan(), insignificant_scan()])
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=15))
    assert len(engine.notifier.cards) == 1


def test_failed_send_is_retried_from_option_outbox(tmp_path: Path) -> None:
    engine = make_engine(tmp_path, notifier_results=[False, True])
    engine.run_option_flow(NOW)
    engine.run_option_flow(NOW + timedelta(minutes=15))
    assert engine.ledger.due_option_flow_alerts(NOW + timedelta(minutes=15)) == []
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_option_flow_pipeline.py tests/test_engine.py -q`  
Expected: missing pipeline/facade.

- [x] **Step 3: Implement pipeline**

At each run: expire and retry due cards; fetch and validate all four venues; load prior same-day scan; optionally enrich only the 20 displayed contracts; choose `baseline`, `change`, `close`, or no-card using cooldown/daily limit; atomically save; deliver due cards and mark result. Source and parsing failures must re-raise to JobHealth. Enrichment failure logs a warning and degrades to Cboe-only with an explicit card label.

- [x] **Step 4: Wire Engine and main**

Add optional `option_flow_source` and `option_enricher` constructor parameters plus `run_option_flow(now, force_summary=False)`. In `main.py`, instantiate providers only when enabled; never print credentials.

- [x] **Step 5: Run tests and commit**

Run: `pytest tests/test_option_flow_pipeline.py tests/test_engine.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/pipelines/option_flow.py src/quant_signal/engine.py src/quant_signal/main.py tests/test_option_flow_pipeline.py tests/test_engine.py
git commit -m "feat: run durable option flow pipeline"
```

### Task 8: Scheduler and production registration

**Files:**
- Modify: `src/quant_signal/scheduler.py`
- Modify: `tests/test_scheduler.py`

- [x] **Step 1: Write RED scheduler tests**

```python
def test_options_jobs_are_registered_only_when_enabled() -> None:
    engine = SimpleNamespace(settings=make_test_settings(option_flow=OptionFlowSettings(enabled=True)), run_option_flow=Mock())
    jobs = {job.id: job for job in build_scheduler(engine, None, None, FakeNotifier()).get_jobs()}
    assert "option_flow" in jobs and "option_flow_close" in jobs
    assert "hour='10-15'" in str(jobs["option_flow"].trigger)
    assert "minute='0,15,30,45'" in str(jobs["option_flow"].trigger)
    assert "hour='16'" in str(jobs["option_flow_close"].trigger)
    assert "minute='20'" in str(jobs["option_flow_close"].trigger)
```

- [x] **Step 2: Run and verify RED**

Run: `pytest tests/test_scheduler.py -q`  
Expected: option jobs absent.

- [x] **Step 3: Implement trading-day-gated jobs**

Register `runtime.wrap("option_flow", ...)` and `runtime.wrap("option_flow_close", ...)` only when enabled. Both use ET, `max_instances=1`, `coalesce=True`, and explicit misfire grace; close calls `force_summary=True`. Do not suppress them under `action_card_only`.

- [x] **Step 4: Run tests and commit**

Run: `pytest tests/test_scheduler.py -q`  
Expected: PASS.

```bash
git add src/quant_signal/scheduler.py tests/test_scheduler.py
git commit -m "feat: schedule option flow alerts"
```

### Task 9: Integration, real read-only smoke tests, review, and production release

**Files:**
- Verify: `docs/superpowers/specs/2026-07-11-options-flow-alerts-design.md`
- Verify: `docs/superpowers/plans/2026-07-11-options-flow-alerts.md`

- [x] **Step 1: Run focused and full verification**

```powershell
.\.venv\Scripts\pytest.exe tests\test_options_flow.py tests\test_cboe_options.py tests\test_alpaca_options.py tests\test_option_flow_card.py tests\test_option_flow_pipeline.py tests\test_ledger.py tests\test_scheduler.py -q
.\.venv\Scripts\pytest.exe -q
.\.venv\Scripts\mypy.exe src\
git diff --check
```

Expected: all commands exit 0.

- [x] **Step 2: Run a real read-only provider smoke test**

Fetch four Cboe venues and enrich only the resulting top 20 using current Alpaca `indicative`. Assert four venues, ten valid calls, ten valid puts, no adjusted roots, and no OPRA fallback. Do not write the production DB and do not output credentials.

- [x] **Step 3: Run isolated SQLite/Feishu E2E**

Use a temporary SQLite DB, prefix the one test card title with `[E2E TEST]`, verify source → rank → atomic outbox → send → `SENT`, and delete the temporary directory. Do not insert test rows into production.

- [x] **Step 4: Request independent code review and close Critical/Important findings**

Review data truthfulness, venue coverage, expiry/OCC parsing, retry behavior, outbox crash windows, stale-card expiry, mobile density, and existing account/execution isolation.

- [x] **Step 5: Merge, tag, restart, and verify production**

Create an annotated release tag, restart `quant-signal`, confirm both option jobs appear in scheduler startup logs, latest `.err.log` is empty, production account still has no AAPL and no pending plan outbox, and no option alert is sent outside a trading day.
