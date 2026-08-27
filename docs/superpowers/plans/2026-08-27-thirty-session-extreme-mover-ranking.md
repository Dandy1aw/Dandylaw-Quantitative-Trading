# 30-Session Extreme-Mover Ranking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-safe 30-trading-session leaderboard ranked by the number of daily gains at or above 10%, repair its history, and resend today's applicable reports.

**Architecture:** Keep the existing event-ledger architecture. Make 30 sessions the only supported user-facing window, rank eligible upward events by count and recency, refuse incomplete windows, and treat hybrid IEX screening as explicitly partial while retaining strict confirmation coverage. Backfill into a temporary SQLite database, validate it, promote the two mover tables transactionally, restart the scheduler, then execute report-producing jobs without re-running the option-flow scan.

**Tech Stack:** Python 3.12, pandas, SQLite, APScheduler, pytest, Pydantic, Ruff, mypy, Feishu app notifier.

---

## File map

- Modify `src/quant_signal/extreme_movers.py`: stable count-first upward ranking semantics.
- Modify `src/quant_signal/notifier/cards.py`: render the upward 30-session count leaderboard.
- Modify `src/quant_signal/ledger.py`: expose the number of complete sessions through a date.
- Modify `src/quant_signal/pipelines/extreme_movers.py`: enforce complete windows and correct hybrid coverage handling.
- Modify `src/quant_signal/config.py`: make 30 sessions the default supported window.
- Modify `src/quant_signal/feishu_bot.py`: accept and document `异动榜 30`, reject incomplete windows.
- Modify `config/settings.yaml`: configure the production 30-session window.
- Modify `research/backfill_extreme_movers.py`: apply the same hybrid coverage semantics during backfill.
- Modify `README.md`: document the new count leaderboard and command.
- Modify focused test files under `tests/` for each behavior.

### Task 1: Lock the 30-session configuration and bot contract

**Files:**
- Modify: `tests/test_config.py`
- Modify: `tests/test_feishu_bot.py`
- Modify: `src/quant_signal/config.py`
- Modify: `src/quant_signal/feishu_bot.py`
- Modify: `config/settings.yaml`

- [ ] **Step 1: Write failing configuration tests**

Add assertions that `ExtremeMoverSettings(enabled=True)` has `windows == (30,)` and `default_window == 30`. Add a bot parser test that `异动榜 30` resolves to 30 and `异动榜 60` is not accepted as a window command.

```python
def test_extreme_mover_defaults_use_thirty_sessions() -> None:
    cfg = ExtremeMoverSettings(enabled=True)
    assert cfg.windows == (30,)
    assert cfg.default_window == 30

def test_mover_window_accepts_only_thirty_sessions(bot: FeishuBotService) -> None:
    assert bot._mover_window("异动榜 30") == 30
    assert bot._mover_window("异动榜 60") == 30
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_config.py::test_extreme_mover_defaults_use_thirty_sessions tests/test_feishu_bot.py -q
```

Expected: the configuration assertion fails because defaults are `(20, 60, 252)` and `60`; at least one bot expectation fails because the current regex accepts 60.

- [ ] **Step 3: Implement the minimal configuration and parser change**

Set these production and model defaults:

```python
windows: tuple[int, ...] = (30,)
default_window: int = 30
```

```yaml
windows: [30]
default_window: 30
```

Replace the bot regex and help text with:

```python
_MOVER_WINDOW = re.compile(r"^异动榜\s+(30)$")
"\n异动榜 [30] — 最近30个交易日涨超10%次数 Top 榜"
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add tests/test_config.py tests/test_feishu_bot.py src/quant_signal/config.py src/quant_signal/feishu_bot.py config/settings.yaml
git commit -m "feat: set mover ranking to 30 sessions"
```

### Task 2: Make the leaderboard count-only and require a complete window

**Files:**
- Modify: `tests/test_extreme_movers.py`
- Modify: `tests/test_cards.py`
- Modify: `tests/test_ledger.py`
- Modify: `tests/test_extreme_mover_pipeline.py`
- Modify: `src/quant_signal/extreme_movers.py`
- Modify: `src/quant_signal/notifier/cards.py`
- Modify: `src/quant_signal/ledger.py`
- Modify: `src/quant_signal/pipelines/extreme_movers.py`
- Modify: `src/quant_signal/feishu_bot.py`

- [ ] **Step 1: Write failing ranking and card tests**

Create five upward events for `AAOI`, four for `TEAM`, and one newer but larger-gain event for `ZZZZ`. Assert the order is `AAOI`, `TEAM`, `ZZZZ`. Add a tie test where equal counts order by latest event date and then ticker. Assert the rendered card contains `30日涨超10%次数榜`, `出现 5 次`, and `最近 08/05`, and does not contain `事件日复合` or `下跌个股`.

```python
assert [row.ticker for row in rank_movers(rows, window_sessions=30)] == [
    "AAOI", "TEAM", "ZZZZ"
]
assert "30日涨超10%次数榜" in card.title
assert "出现 5 次" in card.body_md
assert "事件日复合" not in card.body_md
assert "下跌个股" not in card.body_md
```

- [ ] **Step 2: Write failing ledger and pipeline completeness tests**

Add `SignalLedger.complete_extreme_mover_session_count(through)` tests for 0, 29, and 30 complete runs, excluding failed runs. Add pipeline tests asserting 29 complete sessions return `False` without sending and 30 complete sessions send once.

```python
assert ledger.complete_extreme_mover_session_count(date(2026, 8, 26)) == 29
assert run_premarket(engine, NOW, window_sessions=30) is False
assert engine.notifier.cards == []
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py tests/test_cards.py tests/test_ledger.py tests/test_extreme_mover_pipeline.py -q
```

Expected: failures for the missing ledger method, old compound-return tie-break, old card content, and missing 30-session gate.

- [ ] **Step 4: Implement minimal count-first behavior**

In `rank_movers`, retain event aggregation but sort with:

```python
rows.sort(
    key=lambda row: (
        row.direction.value,
        -row.event_days,
        -row.most_recent_event.toordinal(),
        row.ticker,
    )
)
```

Render only upward rows in the premarket card:

```python
rows = [row for row in movers if row.direction is MoverDirection.UP][:top_stocks]
lines.extend(
    f"{index}. **{row.ticker}**｜出现 {row.event_days} 次｜最近 {row.most_recent_event:%m/%d}"
    for index, row in enumerate(rows, start=1)
)
```

Add this ledger query:

```python
def complete_extreme_mover_session_count(self, through: date) -> int:
    with self._lock:
        row = self._con.execute(
            "SELECT COUNT(*) AS count FROM extreme_mover_runs "
            "WHERE status = 'COMPLETE' AND session_date <= ?",
            (through.isoformat(),),
        ).fetchone()
    return int(row["count"])
```

Before reading events in `run_premarket`, require the count to be at least the requested window and log `extreme_movers.premarket_skip` with `reason="incomplete_window"`. Apply the same guard in the bot card/replay path and return the text `异动榜数据不足30个完整交易日，暂不推送。`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 3. Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add tests/test_extreme_movers.py tests/test_cards.py tests/test_ledger.py tests/test_extreme_mover_pipeline.py src/quant_signal/extreme_movers.py src/quant_signal/notifier/cards.py src/quant_signal/ledger.py src/quant_signal/pipelines/extreme_movers.py src/quant_signal/feishu_bot.py
git commit -m "fix: rank movers by 30-session event count"
```

### Task 3: Repair hybrid coverage and make 30-session backfill viable

**Files:**
- Modify: `tests/test_extreme_mover_pipeline.py`
- Modify: `tests/test_extreme_mover_backfill.py`
- Modify: `src/quant_signal/pipelines/extreme_movers.py`
- Modify: `research/backfill_extreme_movers.py`

- [ ] **Step 1: Write failing hybrid coverage tests**

Add a pipeline test where hybrid IEX bars cover two of four active symbols and candidate confirmation is complete; expect success and the recorded `covered_count == 2`. Add the same case with `feed="sip"`; expect `UNIVERSE_COVERAGE_FAILED`. Add a backfill helper test confirming hybrid mode permits nonzero partial IEX coverage while SIP enforces `min_coverage`.

```python
assert run_close(hybrid_engine, NOW, notify=False) is True
assert hybrid_engine.ledger.extreme_mover_run(date(2026, 8, 7))["covered_count"] == 2
assert run_close(sip_engine, NOW, notify=False) is False
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_extreme_mover_pipeline.py tests/test_extreme_mover_backfill.py -q
```

Expected: hybrid partial coverage fails under the current unconditional 90% gate.

- [ ] **Step 3: Implement explicit hybrid coverage semantics**

Add a shared pure predicate in `quant_signal.pipelines.extreme_movers`:

```python
def _coverage_is_acceptable(*, feed: str, covered: int, universe: int, required: float) -> bool:
    if universe <= 0 or covered <= 0:
        return False
    if feed == "hybrid":
        return True
    return covered / universe >= required
```

Use it in `run_close`. Reuse or mirror the same predicate in the research backfill validation so hybrid mode remains explicitly best-effort while SIP stays strict. Do not weaken `min_confirmation_coverage` for screened candidates.

- [ ] **Step 4: Run tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```powershell
git add tests/test_extreme_mover_pipeline.py tests/test_extreme_mover_backfill.py src/quant_signal/pipelines/extreme_movers.py research/backfill_extreme_movers.py
git commit -m "fix: align hybrid mover coverage with partial IEX feed"
```

### Task 4: Documentation and repository verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update documentation**

Replace the 20/60/252 user-facing description with a 30-session count definition, document count-first tie-breaking, update `异动榜 [30]`, and state that incomplete 30-session history is not published.

- [ ] **Step 2: Run focused and full verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_extreme_movers.py tests/test_extreme_mover_pipeline.py tests/test_extreme_mover_backfill.py tests/test_cards.py tests/test_config.py tests/test_feishu_bot.py tests/test_ledger.py -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy src
git diff --check
```

Expected: all pytest tests pass, Ruff reports no errors, mypy reports success, and `git diff --check` is silent.

- [ ] **Step 3: Commit Task 4**

```powershell
git add README.md
git commit -m "docs: explain 30-session mover count ranking"
```

### Task 5: Backfill, deploy, and push today's applicable reports

**Files:**
- Create temporarily: `scratchpad/extreme-movers-30.db`
- Create temporarily: `scratchpad/extreme-movers-30-checkpoint.json`
- Create backup: `data/backups/predeploy-<timestamp>/signals.db`

- [ ] **Step 1: Build and validate a temporary 30-session ledger**

Run through the latest complete US session:

```powershell
.venv\Scripts\python.exe research/backfill_extreme_movers.py --sessions 30 --through 2026-08-26 --db scratchpad/extreme-movers-30.db --checkpoint scratchpad/extreme-movers-30-checkpoint.json --no-notify
```

Validate exactly 30 complete sessions, no failed sessions, distinct dates, at least one eligible upward event, and count-first ordering. Expected JSON status: `complete` with `sessions: 30`.

- [ ] **Step 2: Stop production and create a recoverable SQLite backup**

Stop the scheduled task, verify the resolved repository and backup target paths, terminate only remaining `quant-signal` child processes if necessary, then use SQLite's online backup API to write `signals.db` into a timestamped `data/backups/predeploy-*` directory. Confirm the backup opens and passes `PRAGMA integrity_check`.

- [ ] **Step 3: Promote validated mover tables transactionally**

Attach the temporary database to the production SQLite connection. In one transaction delete and replace rows in `extreme_mover_runs` and `extreme_mover_events` for the 30-session range, then verify 30 complete sessions and render the expected AAOI/count leaderboard without sending.

- [ ] **Step 4: Start and verify the production scheduler**

Start the `quant-signal` scheduled task. Confirm a new process start time, `scheduler.started`, a fresh zero-byte stderr log, and a live Feishu proxy connection. The new source code is not considered deployed until this evidence exists.

- [ ] **Step 5: Execute today's report-producing jobs**

Build a production engine with the real SQLite ledger and the live data sources. Run independent jobs sequentially, capturing structured results and continuing after isolated failures:

```text
market_scan
premarket
execution_brief / daily_action_briefing
extreme_movers_premarket
fear_dca
intraday
holding_price_alert
option_intel
us_close_briefing or asia_confirm_briefing when its source session is valid
option_flow_drain (delivery only)
```

Do not call `run_option_flow` or `extreme_movers_close` before the current US session closes. Do not run maintenance, data QA, heartbeat, or performance as user-facing pushes.

- [ ] **Step 6: Verify real delivery and final service health**

For each job record whether it sent, skipped, or failed. Confirm no `feishu_bot.send_failed`, confirm the corrected mover card was delivered, and re-run `deploy/status.ps1`. Inspect Git status and report any operational failures without claiming they passed.

