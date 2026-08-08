# Extreme Movers and Bot Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Record daily ±10% US-stock events, publish close and premarket rankings, add bot query/monitor/re-push commands, and apply the 2026-08-08 portfolio screenshot safely.

**Architecture:** A pure extreme_movers domain module computes immutable events and rankings. SQLite stores run metadata, events, and manual monitor state; pipelines own market-data/profile I/O and cards; the bot reads or mutates those explicit surfaces under existing p2p/group permissions. Production rollout follows the repository verify recipe and never drives the option-flow durable outbox.

**Tech Stack:** Python 3.12, pandas, Pydantic, SQLite, DuckDB, APScheduler, Alpaca SIP, yfinance profiles, pytest, mypy, Ruff.

---

### Task 1: Extreme-mover domain model and event detection

**Files:**
- Create: src/quant_signal/extreme_movers.py
- Create: tests/test_extreme_movers.py

- [ ] **Step 1: Write failing boundary and filtering tests**

~~~python
def test_detects_exact_positive_and_negative_thresholds() -> None:
    bars = daily_bars({"UP": [100, 110], "DOWN": [100, 90], "FLAT": [100, 109.99]})
    events = detect_extreme_movers(bars, date(2026, 8, 7), threshold=0.10)
    assert [(row.ticker, row.direction) for row in events] == [
        ("DOWN", MoverDirection.DOWN),
        ("UP", MoverDirection.UP),
    ]

def test_qualification_requires_equity_price_and_liquidity() -> None:
    assert qualify_event(event("A", close="5", adv="20000000"), profile("EQUITY")) is Eligibility.ELIGIBLE
    assert qualify_event(event("ETF", close="20", adv="30000000"), profile("ETF")) is Eligibility.NON_EQUITY
    assert qualify_event(event("LOW", close="4.99", adv="30000000"), profile("EQUITY")) is Eligibility.LOW_PRICE
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py -q

Expected: collection fails because quant_signal.extreme_movers does not exist.

- [ ] **Step 3: Implement immutable types and pure detection**

~~~python
class MoverDirection(str, Enum):
    UP = "up"
    DOWN = "down"

class Eligibility(str, Enum):
    ELIGIBLE = "eligible"
    NON_EQUITY = "non_equity"
    PROFILE_UNAVAILABLE = "profile_unavailable"
    LOW_PRICE = "low_price"
    LOW_LIQUIDITY = "low_liquidity"

@dataclass(frozen=True)
class ExtremeMoverEvent:
    session: date
    ticker: str
    direction: MoverDirection
    daily_return: Decimal
    close: Decimal
    avg_dollar_volume_20d: Decimal | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None
    eligibility: Eligibility = Eligibility.PROFILE_UNAVAILABLE
    source: str = "alpaca_sip"
    backfilled: bool = False
~~~

Implement detect_extreme_movers, average_dollar_volume, and qualify_event. Require two distinct completed sessions, finite positive closes, and deterministic ticker order.

- [ ] **Step 4: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py -q

Expected: all boundary and qualification tests pass.

- [ ] **Step 5: Commit**

~~~powershell
git add src/quant_signal/extreme_movers.py tests/test_extreme_movers.py
git commit -m "feat: detect daily extreme movers"
~~~

### Task 2: Window and sector rankings

**Files:**
- Modify: src/quant_signal/extreme_movers.py
- Modify: tests/test_extreme_movers.py

- [ ] **Step 1: Write failing aggregation tests**

~~~python
def test_rank_movers_counts_days_and_compounds_event_returns() -> None:
    rows = [eligible("A", "0.10", day=1), eligible("A", "0.20", day=2), eligible("B", "0.15", day=2)]
    ranked = rank_movers(rows, window_sessions=60)
    assert ranked[0].ticker == "A"
    assert ranked[0].event_days == 2
    assert ranked[0].event_compound_return == Decimal("0.32")

def test_rank_sectors_uses_event_days_then_repeat_intensity() -> None:
    ranked = rank_sectors(events_for_sector_fixture(), window_sessions=60)
    assert ranked[0].sector == "Information Technology"
    assert ranked[0].event_days == 4
    assert ranked[0].unique_movers == 2
    assert ranked[0].repeat_intensity == Decimal(4) / Decimal(2 * 60)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py -q

Expected: aggregation APIs are missing.

- [ ] **Step 3: Implement ranking types and functions**

Add MoverRanking, SectorRanking, rank_movers, rank_sectors, and window_total_return. Keep up/down separate. Sort stocks by event days, compound magnitude, most recent event, then ticker. Sort sectors by event days, repeat intensity, unique movers, recent event, then sector.

- [ ] **Step 4: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py -q

- [ ] **Step 5: Commit**

~~~powershell
git add src/quant_signal/extreme_movers.py tests/test_extreme_movers.py
git commit -m "feat: rank mover events by stock and sector"
~~~

### Task 3: SQLite events, runs, and manual monitors

**Files:**
- Modify: src/quant_signal/ledger.py
- Modify: tests/test_ledger.py

- [ ] **Step 1: Write failing persistence tests**

~~~python
def test_extreme_mover_snapshot_replaces_same_session_atomically(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    ledger.replace_extreme_mover_run(run("2026-08-07"), [eligible_event("A")])
    ledger.replace_extreme_mover_run(run("2026-08-07"), [eligible_event("B")])
    assert [row.ticker for row in ledger.extreme_mover_events(date(2026, 8, 7))] == ["B"]

def test_manual_monitors_are_idempotent_and_soft_deleted(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    assert ledger.enable_manual_monitor("AAOI", now=NOW)
    assert not ledger.enable_manual_monitor("AAOI", now=NOW)
    assert ledger.active_manual_monitors() == ["AAOI"]
    assert ledger.disable_manual_monitor("AAOI", now=NOW)
    assert ledger.active_manual_monitors() == []
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_ledger.py -q

Expected: new ledger APIs are absent.

- [ ] **Step 3: Add schema and typed methods**

Create extreme_mover_runs, extreme_mover_events, and manual_price_monitors. Use session_date+ticker as the event primary key and one transaction for run/event replacement. Implement replace_extreme_mover_run, latest_complete_extreme_mover_session, extreme_mover_events, enable_manual_monitor, disable_manual_monitor, and active_manual_monitors.

- [ ] **Step 4: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_ledger.py -q

- [ ] **Step 5: Commit**

~~~powershell
git add src/quant_signal/ledger.py tests/test_ledger.py
git commit -m "feat: persist mover events and manual monitors"
~~~

### Task 4: Configuration, close pipeline, and cards

**Files:**
- Modify: src/quant_signal/config.py
- Modify: config/settings.yaml
- Create: src/quant_signal/pipelines/extreme_movers.py
- Modify: src/quant_signal/engine.py
- Modify: src/quant_signal/notifier/cards.py
- Create: tests/test_extreme_mover_pipeline.py
- Modify: tests/test_config.py
- Modify: tests/test_cards.py

- [ ] **Step 1: Write failing settings, pipeline, and card tests**

~~~python
def test_extreme_mover_defaults_are_bounded() -> None:
    cfg = ExtremeMoverSettings(enabled=True)
    assert cfg.threshold == 0.10
    assert cfg.windows == (20, 60, 252)
    assert cfg.default_window == 60

def test_close_pipeline_enriches_only_detected_movers(tmp_path: Path) -> None:
    engine, source, profiles = extreme_engine(tmp_path)
    assert engine.run_extreme_movers_close(NOW) is True
    assert source.asset_list_calls == 1
    assert profiles.requested == {"UP", "DOWN"}
    assert engine.ledger.latest_complete_extreme_mover_session() == date(2026, 8, 7)
~~~

Card tests assert separate up/down sections, coverage labels, no recommendation language, sector Top5, and survivorship warnings for backfill.

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_cards.py tests/test_extreme_mover_pipeline.py -q

- [ ] **Step 3: Implement settings and close pipeline**

Add ExtremeMoverSettings with threshold, floors, windows, chunk size, deadline, top limits, and schedule toggles. Close flow: list assets; fetch two completed SIP sessions in chunks; fail closed below coverage; fetch 260 days only for detected movers; profile/cache only movers; qualify and atomically replace the session; then send one close card.

- [ ] **Step 4: Implement cards and engine delegation**

Add extreme_movers_close_card, extreme_movers_premarket_card, Engine.run_extreme_movers_close, and Engine.run_extreme_movers_premarket.

- [ ] **Step 5: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_cards.py tests/test_extreme_mover_pipeline.py -q

- [ ] **Step 6: Commit**

~~~powershell
git add src/quant_signal/config.py config/settings.yaml src/quant_signal/pipelines/extreme_movers.py src/quant_signal/engine.py src/quant_signal/notifier/cards.py tests/test_config.py tests/test_cards.py tests/test_extreme_mover_pipeline.py
git commit -m "feat: build extreme mover close pipeline"
~~~

### Task 5: Premarket query and scheduler jobs

**Files:**
- Modify: src/quant_signal/pipelines/extreme_movers.py
- Modify: src/quant_signal/scheduler.py
- Modify: tests/test_extreme_mover_pipeline.py
- Modify: tests/test_scheduler.py

- [ ] **Step 1: Write failing read-only premarket and schedule tests**

~~~python
def test_premarket_reads_latest_complete_session_without_refetch(tmp_path: Path) -> None:
    engine = engine_with_mover_history(tmp_path)
    assert engine.run_extreme_movers_premarket(PREOPEN) is True
    assert engine.source.calls == []
    assert engine.notifier.cards[-1].title.startswith("盘前极端动量榜")

def test_scheduler_registers_both_mover_jobs() -> None:
    engine = SimpleNamespace(
        settings=make_test_settings(extreme_movers=ExtremeMoverSettings(enabled=True))
    )
    jobs = {
        job.id: job
        for job in build_scheduler(
            engine=engine, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }
    assert "extreme_movers_close" in jobs
    assert "extreme_movers_premarket" in jobs
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_mover_pipeline.py tests/test_scheduler.py -q

- [ ] **Step 3: Implement jobs**

Schedule close at 16:30 ET and premarket at 08:00 ET with NYSE gates. Premarket reads the latest successful complete session and never refetches or rewrites events.

- [ ] **Step 4: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_mover_pipeline.py tests/test_scheduler.py -q

- [ ] **Step 5: Commit**

~~~powershell
git add src/quant_signal/pipelines/extreme_movers.py src/quant_signal/scheduler.py tests/test_extreme_mover_pipeline.py tests/test_scheduler.py
git commit -m "feat: schedule close and premarket mover rankings"
~~~

### Task 6: Bot query, monitor, and re-push commands

**Files:**
- Modify: src/quant_signal/feishu_bot.py
- Modify: tests/test_feishu_bot.py

- [ ] **Step 1: Write failing parser and permission tests**

~~~python
@pytest.mark.parametrize(("text", "intent"), [
    ("异动榜", BotIntent.MOVERS),
    ("异动榜 60", BotIntent.MOVERS),
    ("异动板块 科技", BotIntent.MOVER_SECTORS),
    ("异动 AAOI", BotIntent.MOVER_TICKER),
    ("监控 AAOI", BotIntent.MONITOR_ADD),
    ("取消监控 AAOI", BotIntent.MONITOR_REMOVE),
    ("重推 异动榜", BotIntent.REPUSH_MOVERS),
])
def test_new_commands_route(text: str, intent: BotIntent) -> None:
    assert route(msg(content={"text": text}), ALLOWED) is intent

def test_group_rejects_mutating_monitor_and_repush_commands() -> None:
    out = FakeTransport()
    service = bot_service(out=out)
    for index, text in enumerate(("监控 AAOI", "取消监控 AAOI", "重推")):
        service.handle(
            msg(
                message_id=f"om_mutating_{index}",
                chat_type="group",
                mentioned=True,
                content={"text": f"@_user_1 {text}"},
            )
        )
    assert all("单聊" in text for _, text in out.texts)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_feishu_bot.py -q

- [ ] **Step 3: Implement strict parsing and queries**

Use 1–6 alphabetic ticker validation and windows limited to 20, 60, 252. Query commands read stored events and render cards/text without refetching.

- [ ] **Step 4: Implement p2p mutations and re-push**

监控 lists forced holdings and manual symbols. Add/remove only mutates manual rows; cancelling a held symbol reports that holding monitoring remains active. Enforce a manual limit. 重推 uses the latest applicable stored action surface and never fabricates a non-trading-day plan. 重推 异动榜 rebuilds from the latest complete mover session.

- [ ] **Step 5: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_feishu_bot.py -q

- [ ] **Step 6: Commit**

~~~powershell
git add src/quant_signal/feishu_bot.py tests/test_feishu_bot.py
git commit -m "feat: add mover and monitor bot commands"
~~~

### Task 7: Merge manual tickers into minute monitoring

**Files:**
- Modify: src/quant_signal/pipelines/holding_price_alert.py
- Modify: src/quant_signal/holding_price_alert.py
- Modify: tests/test_holding_price_alert_pipeline.py
- Modify: tests/test_holding_price_alert.py

- [ ] **Step 1: Write failing union and card tests**

~~~python
def test_pipeline_monitors_exact_holdings_and_manual_symbols(tmp_path: Path) -> None:
    engine = engine_with_positions(tmp_path, [position("GOOGL")])
    engine.ledger.enable_manual_monitor("AAOI", now=NOW)
    engine.run_holding_price_alert(NOW)
    assert engine.source.requested == ["AAOI", "GOOGL"]

def test_manual_symbol_alert_omits_position_pnl() -> None:
    assert "持仓盈亏" not in holding_price_alert_card(manual_alert("AAOI")).body_md
~~~

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_holding_price_alert.py tests/test_holding_price_alert_pipeline.py -q

- [ ] **Step 3: Implement the monitored union**

Combine exact positive positions with active manual tickers. Manual rows carry monitor_origin=manual and no cost basis. Preserve ETF classification, limits, dedupe, quotas, volatility adaptation, volume confirmation, and cause search.

- [ ] **Step 4: Run tests and verify GREEN**

Run: .venv\Scripts\python.exe -m pytest tests/test_holding_price_alert.py tests/test_holding_price_alert_pipeline.py -q

- [ ] **Step 5: Commit**

~~~powershell
git add src/quant_signal/pipelines/holding_price_alert.py src/quant_signal/holding_price_alert.py tests/test_holding_price_alert.py tests/test_holding_price_alert_pipeline.py
git commit -m "feat: monitor manual tickers with holdings"
~~~

### Task 8: Backfill and documentation

**Files:**
- Create: research/backfill_extreme_movers.py
- Create: tests/test_extreme_mover_backfill.py
- Modify: README.md
- Modify: .gitignore

- [ ] **Step 1: Write failing resumable backfill tests**

Cover 252-session bounds, chunk checkpoints, current-active survivorship label, idempotent rerun, and no notification by default.

- [ ] **Step 2: Run tests and verify RED**

Run: .venv\Scripts\python.exe -m pytest tests/test_extreme_mover_backfill.py -q

- [ ] **Step 3: Implement bounded backfill CLI**

The command .venv\Scripts\python.exe research\backfill_extreme_movers.py --sessions 252 --no-notify uses SIP adjusted bars, saves per-session checkpoints, profiles only detected movers, and never starts scheduler or option-flow.

- [ ] **Step 4: Document commands, formulas, caveats, and schedules**

Update README command and scheduler tables. Ignore checkpoint logs and raw caches while retaining structured summary JSON.

- [ ] **Step 5: Run focused tests and commit**

~~~powershell
.venv\Scripts\python.exe -m pytest tests/test_extreme_mover_backfill.py -q
git add research/backfill_extreme_movers.py tests/test_extreme_mover_backfill.py README.md .gitignore
git commit -m "feat: backfill and document extreme mover history"
~~~

### Task 9: Apply the 2026-08-08 screenshot

**Files:**
- Create: research/e2e/apply_portfolio_20260808.py
- Create: research/e2e/portfolio-20260808/result.json

- [ ] **Step 1: Encode and validate the exact screenshot**

Use equity 6045.21, market value 6025.66, cash 19.55, buying power 2779.33, six exact positions, and the uploaded image SHA-256. Assert VALIDATED, reconciliation within one dollar, and exact symbols AAOI, GOOGL, MRVL, SKHY, SMH, MUU.

- [ ] **Step 2: Dry-run against a temporary ledger**

Run the script with a temporary database and assert totals and active positions without production writes.

- [ ] **Step 3: Apply once to data/signals.db**

Use .venv\Scripts\python.exe, capture status/totals/symbols only, and write redacted evidence. Confirm old positions inactive and six new positions active.

- [ ] **Step 4: Commit reproducible evidence**

~~~powershell
git add research/e2e/apply_portfolio_20260808.py research/e2e/portfolio-20260808/result.json
git commit -m "ops: record august portfolio update"
~~~

### Task 10: Full verification and production E2E

**Files:**
- Create: research/e2e/run_extreme_movers_live_e2e.py
- Create: research/e2e/extreme-movers-20260808/result.json

- [ ] **Step 1: Run full automated verification**

~~~powershell
.venv\Scripts\python.exe -m pytest -q --cov --cov-report=term-missing
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m ruff check src tests scripts --select E9,F63,F7,F82
$env:PYTHONUTF8='1'; .venv\Scripts\python.exe -m pip_audit
.venv\Scripts\python.exe scripts/check_secrets.py
.venv\Scripts\python.exe scripts/check_release_state.py
git diff --check HEAD
~~~

Expected: zero failures, at least70% coverage, no known dependency vulnerability.

- [ ] **Step 2: Restart production safely**

Use deployment scripts to stop/start the scheduled task. Do not run uv sync while the service holds .venv\Scripts\quant-signal.exe. Confirm a new process, scheduler.started, both new jobs, and empty new stderr.

- [ ] **Step 3: Run isolated calculation E2E**

Use the latest backup bars.duckdb, real asset/SIP/profile sources, temporary SQLite, and recording notifier. Do not drive option-flow. Verify detection, qualification, rankings, coverage, and cards.

- [ ] **Step 4: Send minimum real cards**

Send exactly one portfolio/monitor confirmation and one latest applicable mover card to push_receive_id. Because the run date is non-trading, do not fabricate an action plan.

- [ ] **Step 5: Verify bot commands**

Use unique p2p BotMessage values for 持仓, 监控, and 异动榜 60 with the allowed sender and configured receive ID.

- [ ] **Step 6: Commit evidence and push**

~~~powershell
git add research/e2e/run_extreme_movers_live_e2e.py research/e2e/extreme-movers-20260808/result.json
git commit -m "test: verify extreme mover production flow"
git push -u origin feat/v0
~~~
