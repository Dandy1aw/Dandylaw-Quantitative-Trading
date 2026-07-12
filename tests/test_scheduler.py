from datetime import datetime, timedelta, timezone
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from quant_signal.scheduler import (
    HEARTBEAT_FAIL_THRESHOLD,
    Heartbeat,
    JobHealth,
    build_scheduler,
)


class _Event:
    def __init__(
        self, job_id: str, exception: object | None, code: int | None = None
    ) -> None:
        self.job_id = job_id
        self.exception = exception
        self.code = code


class FakeNotifier:
    def __init__(self, success: bool = True) -> None:
        self.cards: list[object] = []
        self.success = success

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return self.success


class _OptionFlowEngine:
    def __init__(self) -> None:
        from conftest import make_test_settings
        from quant_signal.config import (
            ExecutionPlanSettings,
            NotifySettings,
            OptionFlowSettings,
        )

        self.settings = make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False),
            notify=NotifySettings(action_card_only=True),
            option_flow=OptionFlowSettings(enabled=True),
        )
        self.calls: list[bool] = []
        self.drains = 0

    def run_option_flow(
        self, now: datetime, *, force_summary: bool = False
    ) -> None:
        self.calls.append(force_summary)

    def run_option_flow_delivery(self, now: datetime) -> None:
        self.drains += 1


def test_scheduler_registers_all_jobs() -> None:
    """默认(生产)配置: 执行建议任务上线, 旧价格偏离任务下线。"""
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {
        "premarket", "intraday", "postmarket", "maintenance", "heartbeat",
        "rotation_asia_open", "rotation_asia_close", "enrichment",
        "performance", "data_qa", "market_scan", "negative_overreaction",
        "execution_brief", "execution_watch",
    }
    assert "watch_deviation" not in ids


def test_execution_jobs_run_at_0815_and_watch_is_staggered_one_minute() -> None:
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    jobs = {job.id: job for job in sched.get_jobs()}

    brief = str(jobs["execution_brief"].trigger)
    assert "hour='8'" in brief and "minute='15'" in brief
    assert str(jobs["execution_brief"].trigger.timezone) == "America/New_York"

    watch = str(jobs["execution_watch"].trigger)
    assert "hour='9-15'" in watch and "minute='1-56/5'" in watch
    assert str(jobs["execution_watch"].trigger.timezone) == "America/New_York"
    assert jobs["execution_watch"].misfire_grace_time == 240


def test_option_flow_jobs_registered_only_when_enabled() -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import ExecutionPlanSettings, OptionFlowSettings

    disabled = SimpleNamespace(
        settings=make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False),
            option_flow=OptionFlowSettings(enabled=False),
        )
    )
    disabled_jobs = {
        job.id
        for job in build_scheduler(
            engine=disabled, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }
    assert "option_flow" not in disabled_jobs
    assert "option_flow_close" not in disabled_jobs
    assert "option_flow_drain" not in disabled_jobs

    enabled = SimpleNamespace(
        settings=make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False),
            option_flow=OptionFlowSettings(enabled=True),
        )
    )
    sched = build_scheduler(
        engine=enabled, ledger=None, store=None, notifier=FakeNotifier()
    )
    jobs = {job.id: job for job in sched.get_jobs()}
    assert {"option_flow", "option_flow_close", "option_flow_drain"} <= jobs.keys()

    intraday = jobs["option_flow"]
    assert "hour='10-15'" in str(intraday.trigger)
    assert "minute='0,15,30,45'" in str(intraday.trigger)
    assert str(intraday.trigger.timezone) == "America/New_York"
    assert intraday.max_instances == 1
    assert intraday.coalesce is True
    assert intraday.misfire_grace_time == 600

    closing = jobs["option_flow_close"]
    assert "hour='0'" in str(closing.trigger)
    assert "minute='0'" in str(closing.trigger)
    assert str(closing.trigger.timezone) == "UTC"  # 北京 08:00, 不随美东夏令时漂移
    assert closing.max_instances == 1
    assert closing.coalesce is True
    assert closing.misfire_grace_time == 1800

    drain = jobs["option_flow_drain"]
    assert "hour='16-21'" in str(drain.trigger)
    assert "minute='35'" in str(drain.trigger)
    assert str(drain.trigger.timezone) == "America/New_York"
    assert drain.max_instances == 1
    assert drain.coalesce is True
    assert drain.misfire_grace_time == 600


def test_option_flow_jobs_use_trading_day_gate_and_ignore_action_card_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    et = ZoneInfo("America/New_York")
    now = {"value": datetime(2026, 7, 10, 15, 45, tzinfo=et)}
    close = datetime(2026, 7, 10, 16, 0, tzinfo=et).astimezone(timezone.utc)
    monkeypatch.setattr("quant_signal.scheduler._now_et", lambda: now["value"])
    monkeypatch.setattr("quant_signal.scheduler.session_close_utc", lambda day: close)

    engine = _OptionFlowEngine()
    sched = build_scheduler(
        engine=engine, ledger=None, store=None, notifier=FakeNotifier()
    )
    jobs = {job.id: job for job in sched.get_jobs()}

    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: False)
    jobs["option_flow"].func()
    jobs["option_flow_close"].func()
    jobs["option_flow_drain"].func()
    assert engine.calls == [] and engine.drains == 0

    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: True)
    jobs["option_flow"].func()
    now["value"] = datetime(2026, 7, 10, 20, 0, tzinfo=et)  # 北京 08:00 晚间槽
    jobs["option_flow_close"].func()
    jobs["option_flow_drain"].func()
    assert engine.calls == [False, True]
    assert engine.drains == 1


@pytest.mark.parametrize(
    ("now_et", "should_run"),
    [
        (datetime(2026, 7, 10, 13, 0), True),
        (datetime(2026, 7, 10, 13, 0, 1), False),
    ],
    ids=["at-close", "after-close"],
)
def test_option_flow_skips_only_after_session_close(
    monkeypatch: pytest.MonkeyPatch,
    now_et: datetime,
    should_run: bool,
) -> None:
    et = ZoneInfo("America/New_York")
    now_et = now_et.replace(tzinfo=et)
    close = datetime(2026, 7, 10, 13, 0, tzinfo=et).astimezone(timezone.utc)
    monkeypatch.setattr("quant_signal.scheduler._now_et", lambda: now_et)
    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: True)
    monkeypatch.setattr("quant_signal.scheduler.session_close_utc", lambda day: close)

    engine = _OptionFlowEngine()
    jobs = {
        job.id: job
        for job in build_scheduler(
            engine=engine, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }

    jobs["option_flow"].func()

    assert engine.calls == ([False] if should_run else [])


@pytest.mark.parametrize(
    ("close_hour", "should_run"),
    [
        (16, True),   # 正常日：北京 08:00 晚间槽照常出收盘榜
        (13, True),   # 半日市：同一晚间槽，无需双槽
    ],
    ids=["full-day", "half-day"],
)
def test_option_flow_close_runs_on_trading_evening_regardless_of_close_hour(
    monkeypatch: pytest.MonkeyPatch,
    close_hour: int,
    should_run: bool,
) -> None:
    et = ZoneInfo("America/New_York")
    now_et = datetime(2026, 7, 10, 20, 0, tzinfo=et)  # UTC 00:00 = 北京 08:00
    close = datetime(2026, 7, 10, close_hour, 0, tzinfo=et).astimezone(
        timezone.utc
    )
    monkeypatch.setattr("quant_signal.scheduler._now_et", lambda: now_et)
    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: True)
    monkeypatch.setattr("quant_signal.scheduler.session_close_utc", lambda day: close)

    engine = _OptionFlowEngine()
    jobs = {
        job.id: job
        for job in build_scheduler(
            engine=engine, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }

    jobs["option_flow_close"].func()

    assert engine.calls == ([True] if should_run else [])


class _OptionIntelEngine:
    def __init__(self) -> None:
        from conftest import make_test_settings
        from quant_signal.config import (
            ExecutionPlanSettings,
            NotifySettings,
            OptionIntelSettings,
        )

        self.settings = make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False),
            notify=NotifySettings(action_card_only=True),
            option_intel=OptionIntelSettings(enabled=True),
        )
        self.calls = 0

    def run_option_intel(self, now: datetime) -> None:
        self.calls += 1


@pytest.mark.parametrize(
    ("trading_day", "should_run"),
    [
        (True, True),    # 交易日晚间(北京 08:10)运行
        (False, False),  # 周末/假日跳过
    ],
    ids=["trading-evening", "non-trading-day"],
)
def test_option_intel_runs_on_trading_evening(
    monkeypatch: pytest.MonkeyPatch,
    trading_day: bool,
    should_run: bool,
) -> None:
    et = ZoneInfo("America/New_York")
    now_et = datetime(2026, 7, 10, 20, 10, tzinfo=et)  # UTC 00:10 = 北京 08:10
    monkeypatch.setattr("quant_signal.scheduler._now_et", lambda: now_et)
    monkeypatch.setattr(
        "quant_signal.scheduler.is_trading_day", lambda day: trading_day
    )

    engine = _OptionIntelEngine()
    jobs = {
        job.id: job
        for job in build_scheduler(
            engine=engine, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }
    trigger = str(jobs["option_intel"].trigger)
    assert "hour='0'" in trigger and "minute='10'" in trigger
    assert str(jobs["option_intel"].trigger.timezone) == "UTC"

    jobs["option_intel"].func()

    assert engine.calls == (1 if should_run else 0)


def test_postmarket_trigger_is_beijing_morning() -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import ExecutionPlanSettings

    engine = SimpleNamespace(
        settings=make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False)
        ),
    )
    jobs = {
        job.id: job
        for job in build_scheduler(
            engine=engine, ledger=None, store=None, notifier=FakeNotifier()
        ).get_jobs()
    }
    trigger = str(jobs["postmarket"].trigger)
    assert "hour='0'" in trigger and "minute='5'" in trigger
    assert str(jobs["postmarket"].trigger.timezone) == "UTC"  # 北京 08:05


def test_option_intel_job_absent_when_disabled() -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import ExecutionPlanSettings

    engine = SimpleNamespace(
        settings=make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False)
        ),
    )
    sched = build_scheduler(
        engine=engine, ledger=None, store=None, notifier=FakeNotifier()
    )
    assert "option_intel" not in {job.id for job in sched.get_jobs()}


def test_build_scheduler_accepts_injected_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import ExecutionPlanSettings, OptionFlowSettings
    from quant_signal.scheduler import JobRuntime

    runtime = JobRuntime()
    engine = SimpleNamespace(
        settings=make_test_settings(
            execution_plan=ExecutionPlanSettings(enabled=False),
            option_flow=OptionFlowSettings(enabled=True),
        ),
        run_enrichment=lambda now: None,
    )
    sched = build_scheduler(
        engine=engine, ledger=None, store=None, notifier=FakeNotifier(),
        runtime=runtime,
    )
    jobs = {job.id: job for job in sched.get_jobs()}
    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: False)
    jobs["option_flow"].func()  # 非交易日跳过=正常返回，注入的 runtime 应记到成功
    assert runtime.last_success("option_flow") is not None
    jobs["enrichment"].func()
    assert runtime.last_success("enrichment") is not None


def test_job_runtime_snapshot_exposes_per_job_state() -> None:
    from datetime import datetime, timezone

    from quant_signal.scheduler import JobRuntime

    times = [datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)]
    runtime = JobRuntime(now_fn=lambda: times[-1])
    wrapped = runtime.wrap(
        "demo",
        lambda: times.append(datetime(2026, 7, 10, 12, 0, 30, tzinfo=timezone.utc)),
    )
    wrapped()
    snap = runtime.snapshot()
    assert snap["demo"]["last_success"] is not None
    assert snap["demo"]["last_duration"] == 30.0
    assert snap["demo"]["running_since"] is None


def test_legacy_deviation_job_registered_only_when_enabled() -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import (
        ExecutionPlanSettings,
        LegacyPriceDeviationSettings,
    )

    settings = make_test_settings(
        legacy_price_deviation=LegacyPriceDeviationSettings(enabled=True),
        execution_plan=ExecutionPlanSettings(enabled=False),
    )
    engine = SimpleNamespace(settings=settings)
    sched = build_scheduler(engine=engine, ledger=None, store=None, notifier=FakeNotifier())
    ids = {j.id for j in sched.get_jobs()}
    assert "watch_deviation" in ids
    assert "execution_brief" not in ids
    assert "execution_watch" not in ids


def test_action_card_only_suppresses_standalone_enrichment_push() -> None:
    from conftest import make_test_settings
    from quant_signal.config import NotifySettings

    class Engine:
        def __init__(self) -> None:
            self.settings = make_test_settings(
                notify=NotifySettings(action_card_only=True)
            )
            self.calls = 0

        def run_enrichment(self, now: datetime) -> None:
            self.calls += 1

    engine = Engine()
    sched = build_scheduler(engine=engine, ledger=None, store=None, notifier=FakeNotifier())
    jobs = {job.id: job for job in sched.get_jobs()}

    jobs["enrichment"].func()

    assert engine.calls == 0


def test_maintenance_prunes_option_history_and_keeps_existing_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from conftest import make_test_settings
    from quant_signal.config import OptionFlowSettings

    fixed_now = datetime(2026, 7, 11, 7, 0, tzinfo=timezone.utc)
    settings = make_test_settings(
        option_flow=OptionFlowSettings(retention_days=45)
    )
    engine = SimpleNamespace(settings=settings)
    store = object()
    calls: dict[str, object] = {"order": []}

    class Ledger:
        def prune_option_flow(self, before: datetime) -> int:
            order = calls["order"]
            assert isinstance(order, list)
            order.append("prune")
            calls["prune_before"] = before
            return 7

        def prune_option_intel(self, before: datetime) -> int:
            order = calls["order"]
            assert isinstance(order, list)
            order.append("prune_intel")
            calls["prune_intel_before"] = before
            return 3

    ledger = Ledger()

    def fake_ingest(
        actual_store: object,
        actual_settings: object,
        symbols: list[str],
        *,
        days: int,
    ) -> None:
        order = calls["order"]
        assert isinstance(order, list)
        order.append("ingest")
        calls["ingest"] = (actual_store, actual_settings, symbols, days)

    def fake_backup(
        actual_ledger: object,
        db_path: object,
        now: datetime,
    ) -> None:
        order = calls["order"]
        assert isinstance(order, list)
        order.append("backup")
        calls["backup"] = (actual_ledger, db_path, now)

    class RecordingLog:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def info(self, event: str, **values: object) -> None:
            self.events.append((event, values))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    logger = RecordingLog()
    monkeypatch.setattr("quant_signal.ingest.ingest_daily_split", fake_ingest)
    monkeypatch.setattr("quant_signal.backup.run_backup", fake_backup)
    sched = build_scheduler(
        engine=engine, ledger=ledger, store=store, notifier=FakeNotifier()
    )
    monkeypatch.setattr("quant_signal.scheduler.datetime", FixedDateTime)
    monkeypatch.setattr("quant_signal.scheduler.log", logger)

    jobs = {job.id: job for job in sched.get_jobs()}
    jobs["maintenance"].func()

    cutoff = fixed_now - timedelta(days=45)
    intel_cutoff = fixed_now - timedelta(
        days=settings.option_intel.retention_days
    )
    assert calls["order"] == ["ingest", "backup", "prune", "prune_intel"]
    assert calls["prune_before"] == cutoff
    assert calls["prune_intel_before"] == intel_cutoff
    assert calls["ingest"] == (
        store,
        settings,
        settings.universe + settings.watchlist,
        10,
    )
    assert calls["backup"] == (ledger, settings.db_path, fixed_now)
    assert logger.events == [
        (
            "maintenance.option_flow_pruned",
            {"deleted_scans": 7, "before": cutoff.isoformat()},
        ),
        (
            "maintenance.option_intel_pruned",
            {"deleted_rows": 3, "before": intel_cutoff.isoformat()},
        ),
    ]


def test_jobhealth_collects_missed_and_max_instances_events() -> None:
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

    h = JobHealth()
    h.listen(_Event("market_scan", None, code=EVENT_JOB_MISSED))
    h.listen(_Event("execution_watch", None, code=EVENT_JOB_MAX_INSTANCES))
    assert h.drain_errors() == [
        ("market_scan", "missed"),
        ("execution_watch", "max_instances"),
    ]


def test_scheduler_sets_explicit_misfire_grace_windows() -> None:
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    jobs = {job.id: job for job in sched.get_jobs()}
    assert jobs["premarket"].misfire_grace_time == 3600
    assert jobs["rotation_asia_open"].misfire_grace_time == 3600
    assert jobs["rotation_asia_close"].misfire_grace_time == 3600
    assert jobs["intraday"].misfire_grace_time == 240
    assert jobs["execution_brief"].misfire_grace_time == 3600


def test_us_market_jobs_use_new_york_trigger_timezone() -> None:
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    jobs = {job.id: job for job in sched.get_jobs()}
    # postmarket 收盘复盘改为固定 UTC(北京 08:05)，见 test_postmarket_trigger_is_beijing_morning
    for job_id in ("premarket", "intraday", "maintenance", "enrichment",
                   "negative_overreaction"):
        assert str(jobs[job_id].trigger.timezone) == "America/New_York"


def test_rotation_and_premarket_share_execution_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: True)

    class RecordingEngine:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.guard = threading.Lock()

        def run_premarket(self, now: datetime) -> None:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1

    engine = RecordingEngine()
    sched = build_scheduler(engine=engine, ledger=None, store=None, notifier=FakeNotifier())
    jobs = {job.id: job for job in sched.get_jobs()}
    start = threading.Barrier(3)

    def invoke(job_id: str) -> None:
        start.wait()
        jobs[job_id].func()

    threads = [
        threading.Thread(target=invoke, args=("premarket",)),
        threading.Thread(target=invoke, args=("rotation_asia_open",)),
    ]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join()

    assert engine.max_active == 1


def test_heartbeat_alerts_after_consecutive_failures() -> None:
    n = FakeNotifier()
    hb = Heartbeat(notifier=n, check=lambda: False)   # 恒失败
    for _ in range(HEARTBEAT_FAIL_THRESHOLD):
        hb.tick()
    assert len(n.cards) == 1                          # 连续 2 次失败发一次告警


def test_heartbeat_recovers_resets_counter() -> None:
    n = FakeNotifier()
    ok = {"v": False}
    hb = Heartbeat(notifier=n, check=lambda: ok["v"])
    hb.tick()
    ok["v"] = True
    hb.tick()                                         # 恢复
    ok["v"] = False
    hb.tick()                                         # 又失败 1 次，不到阈值
    assert n.cards == []


def test_jobhealth_records_errors_and_ignores_success() -> None:
    h = JobHealth()
    h.listen(_Event("heartbeat", None))               # 成功不记
    h.listen(_Event("premarket", RuntimeError("boom")))
    assert h.drain_errors() == [("premarket", "boom")]
    assert h.drain_errors() == []                     # drain 后清空


def test_heartbeat_alerts_on_job_errors_once() -> None:
    n = FakeNotifier()
    h = JobHealth()
    h.listen(_Event("rotation_asia_close", RuntimeError("SQLite ... same thread")))
    h.listen(_Event("watch_deviation", RuntimeError("SQLite ... same thread")))
    hb = Heartbeat(notifier=n, check=lambda: True, health=h)
    hb.tick()
    assert len(n.cards) == 1
    body = n.cards[0].body_md                          # type: ignore[attr-defined]
    assert "rotation_asia_close" in body and "watch_deviation" in body
    hb.tick()                                          # 已 drain，不重复告警
    assert len(n.cards) == 1


def test_job_error_alert_is_silent_for_two_hours_per_job() -> None:
    n = FakeNotifier()
    h = JobHealth()
    now = {"value": datetime(2026, 7, 8, tzinfo=timezone.utc)}
    hb = Heartbeat(
        notifier=n,
        check=lambda: True,
        health=h,
        now_fn=lambda: now["value"],
    )

    h.listen(_Event("watch_deviation", RuntimeError("boom-1")))
    hb.tick()
    h.listen(_Event("watch_deviation", RuntimeError("boom-2")))
    hb.tick()
    assert len(n.cards) == 1

    now["value"] += timedelta(hours=2, seconds=1)
    h.listen(_Event("watch_deviation", RuntimeError("boom-3")))
    hb.tick()
    assert len(n.cards) == 2


def test_failed_job_error_alert_is_retried_next_tick() -> None:
    notifier = FakeNotifier(success=False)
    health = JobHealth()
    hb = Heartbeat(notifier=notifier, check=lambda: True, health=health)
    health.listen(_Event("premarket", RuntimeError("boom-1")))
    hb.tick()
    notifier.success = True
    health.listen(_Event("premarket", RuntimeError("boom-2")))
    hb.tick()
    assert len(notifier.cards) == 2


# ---------------------------------------------------------------- job runtime health


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.value = start

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def test_job_runtime_wrap_records_duration_and_success() -> None:
    from quant_signal.scheduler import JobRuntime

    clock = _Clock(_utc(2026, 7, 10, 11, 5))  # Fri 07:05 ET

    def job() -> None:
        clock.advance(seconds=42)

    runtime = JobRuntime(now_fn=clock.now)
    wrapped = runtime.wrap("market_scan", job)
    assert runtime.last_success("market_scan") is None

    wrapped()

    assert runtime.last_success("market_scan") == clock.value
    assert runtime.last_duration("market_scan") == 42.0
    assert runtime.running_since("market_scan") is None


def test_job_runtime_failure_does_not_record_success() -> None:
    from quant_signal.scheduler import JobRuntime

    clock = _Clock(_utc(2026, 7, 10, 11, 5))

    def bad_job() -> None:
        raise RuntimeError("boom")

    runtime = JobRuntime(now_fn=clock.now)
    wrapped = runtime.wrap("market_scan", bad_job)
    with pytest.raises(RuntimeError):
        wrapped()

    assert runtime.last_success("market_scan") is None
    assert runtime.running_since("market_scan") is None


def test_runtime_check_fails_when_market_scan_misses_todays_run() -> None:
    from quant_signal.scheduler import JobRuntime, build_runtime_check

    clock = _Clock(_utc(2026, 7, 9, 12, 0))  # Thu: 服务启动
    runtime = JobRuntime(now_fn=clock.now)
    check = build_runtime_check(runtime, "market_scan")
    assert check() is True  # 启动宽限: 还没到下一个应跑时点

    clock.value = _utc(2026, 7, 10, 14, 0)  # Fri 10:00 ET, 07:00 的扫描没跑
    assert check() is False

    runtime.wrap("market_scan", lambda: None)()
    assert check() is True


def test_runtime_check_tolerates_weekend_gap() -> None:
    from quant_signal.scheduler import JobRuntime, build_runtime_check

    clock = _Clock(_utc(2026, 7, 10, 11, 5))  # Fri 07:05 ET
    runtime = JobRuntime(now_fn=clock.now)
    runtime.wrap("market_scan", lambda: None)()  # Fri 成功

    check = build_runtime_check(runtime, "market_scan")
    clock.value = _utc(2026, 7, 13, 10, 0)  # Mon 06:00 ET, 今日还没到运行窗口
    assert check() is True

    clock.value = _utc(2026, 7, 13, 14, 0)  # Mon 10:00 ET, 今天该跑没跑
    assert check() is False


def test_runtime_check_fails_when_job_runs_beyond_deadline() -> None:
    from quant_signal.scheduler import JobRuntime, build_runtime_check

    clock = _Clock(_utc(2026, 7, 10, 11, 5))
    runtime = JobRuntime(now_fn=clock.now)
    runtime.record_start("market_scan")
    check = build_runtime_check(runtime, "market_scan")

    clock.advance(minutes=5)
    assert check() is True
    clock.advance(minutes=6)
    assert check() is False


def test_log_processor_redacts_webhook_and_api_secrets() -> None:
    from quant_signal.logging_setup import redact_secrets

    event = {
        "url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc-token-123",
        "error": "POST https://open.feishu.cn/open-apis/bot/v2/hook/abc-token-123 500",
        "note": "APCA-API-SECRET-KEY: sk-super-secret",
        "count": 3,
    }
    out = redact_secrets(None, "info", dict(event))

    flat = str(out)
    assert "abc-token-123" not in flat
    assert "sk-super-secret" not in flat
    assert out["count"] == 3
    assert "open.feishu.cn" in str(out["url"])  # 只遮 token, 保留 host 便于排障


def test_setup_logging_silences_httpx_request_urls() -> None:
    """httpx INFO 日志会打印完整请求 URL(含 webhook token), 必须压到 WARNING。"""
    import logging

    from quant_signal.logging_setup import setup_logging

    setup_logging()
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING
