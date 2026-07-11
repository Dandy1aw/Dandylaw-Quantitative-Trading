from datetime import datetime, timedelta, timezone
import threading
import time

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
    assert {"option_flow", "option_flow_close"} <= jobs.keys()

    intraday = jobs["option_flow"]
    assert "hour='10-15'" in str(intraday.trigger)
    assert "minute='0,15,30,45'" in str(intraday.trigger)
    assert str(intraday.trigger.timezone) == "America/New_York"
    assert intraday.max_instances == 1
    assert intraday.coalesce is True
    assert intraday.misfire_grace_time == 600

    closing = jobs["option_flow_close"]
    assert "hour='16'" in str(closing.trigger)
    assert "minute='20'" in str(closing.trigger)
    assert str(closing.trigger.timezone) == "America/New_York"
    assert closing.max_instances == 1
    assert closing.coalesce is True
    assert closing.misfire_grace_time == 1800


def test_option_flow_jobs_use_trading_day_gate_and_ignore_action_card_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import make_test_settings
    from quant_signal.config import (
        ExecutionPlanSettings,
        NotifySettings,
        OptionFlowSettings,
    )

    class Engine:
        def __init__(self) -> None:
            self.settings = make_test_settings(
                execution_plan=ExecutionPlanSettings(enabled=False),
                notify=NotifySettings(action_card_only=True),
                option_flow=OptionFlowSettings(enabled=True),
            )
            self.calls: list[bool] = []

        def run_option_flow(
            self, now: datetime, *, force_summary: bool = False
        ) -> None:
            self.calls.append(force_summary)

    engine = Engine()
    sched = build_scheduler(
        engine=engine, ledger=None, store=None, notifier=FakeNotifier()
    )
    jobs = {job.id: job for job in sched.get_jobs()}

    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: False)
    jobs["option_flow"].func()
    jobs["option_flow_close"].func()
    assert engine.calls == []

    monkeypatch.setattr("quant_signal.scheduler.is_trading_day", lambda day: True)
    jobs["option_flow"].func()
    jobs["option_flow_close"].func()
    assert engine.calls == [False, True]


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
    for job_id in ("premarket", "intraday", "postmarket", "maintenance", "enrichment",
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
