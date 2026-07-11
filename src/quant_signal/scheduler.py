import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from quant_signal.calendar import is_trading_day, previous_trading_day

log = structlog.get_logger()
ET = ZoneInfo("America/New_York")
HEARTBEAT_FAIL_THRESHOLD = 2
JOB_ALERT_SILENCE = timedelta(hours=2)
JOB_RUNNING_DEADLINE = timedelta(minutes=10)
# market_scan 07:00 ET 触发, 07:30 之后仍无当日成功即视为停摆
_SCAN_DUE_ET = time(7, 30)


class JobRuntime:
    """记录每个 job 的最近开始时间、时长与最近成功时间, 供心跳判断关键任务是否停摆。"""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._starts: dict[str, datetime] = {}
        self._last_success: dict[str, datetime] = {}
        self._last_duration: dict[str, float] = {}
        self.created_at = self._now_fn()

    def record_start(self, job_id: str) -> None:
        with self._lock:
            self._starts[job_id] = self._now_fn()

    def wrap(self, job_id: str, func: Callable[[], None]) -> Callable[[], None]:
        def _wrapped() -> None:
            self.record_start(job_id)
            try:
                func()
            except Exception:
                raise
            else:
                end = self._now_fn()
                with self._lock:
                    start = self._starts.get(job_id, end)
                    self._last_success[job_id] = end
                    self._last_duration[job_id] = (end - start).total_seconds()
            finally:
                with self._lock:
                    self._starts.pop(job_id, None)

        return _wrapped

    def last_success(self, job_id: str) -> datetime | None:
        with self._lock:
            return self._last_success.get(job_id)

    def last_duration(self, job_id: str) -> float | None:
        with self._lock:
            return self._last_duration.get(job_id)

    def running_since(self, job_id: str) -> datetime | None:
        with self._lock:
            return self._starts.get(job_id)

    def snapshot(self) -> dict[str, dict[str, object]]:
        """所有已知 job 的运行状态只读快照，供机器人「健康」指令渲染。"""
        with self._lock:
            job_ids = (
                set(self._starts) | set(self._last_success) | set(self._last_duration)
            )
            return {
                job_id: {
                    "last_success": self._last_success.get(job_id),
                    "last_duration": self._last_duration.get(job_id),
                    "running_since": self._starts.get(job_id),
                }
                for job_id in sorted(job_ids)
            }


def _expected_scan_run(now_et: datetime) -> datetime:
    """最近一个应该完成 market_scan 的 07:00 ET 时点(交易日历感知)。"""
    day = now_et.date()
    if is_trading_day(day) and now_et.time() >= _SCAN_DUE_ET:
        run_day = day
    else:
        run_day = previous_trading_day(day)
    return datetime.combine(run_day, time(7, 0), tzinfo=ET)


def build_runtime_check(
    runtime: JobRuntime, job_id: str = "market_scan"
) -> Callable[[], bool]:
    """心跳自检: 关键 job 卡死(超过运行 deadline)或最近一个应跑时点没有成功即失败。"""

    def _check() -> bool:
        now = runtime._now_fn()
        started = runtime.running_since(job_id)
        if started is not None and now - started > JOB_RUNNING_DEADLINE:
            return False
        effective = runtime.last_success(job_id) or runtime.created_at
        return effective >= _expected_scan_run(now.astimezone(ET))

    return _check


class JobHealth:
    """监听 APScheduler 的 job 执行事件，记录失败的任务，供心跳汇总告警。

    定时任务跑在后台线程池里，异常默认只进 APScheduler 日志、不会中断进程，
    容易像之前 ledger 跨线程 bug 那样长期无声失败。挂这个监听器把失败收集起来。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._errors: list[tuple[str, str]] = []

    def listen(self, event: Any) -> None:
        exc = getattr(event, "exception", None)
        if exc is not None:
            with self._lock:
                self._errors.append((getattr(event, "job_id", "?"), str(exc)))
            return
        code = getattr(event, "code", None)
        label = {EVENT_JOB_MISSED: "missed", EVENT_JOB_MAX_INSTANCES: "max_instances"}.get(code)
        if label is not None:
            with self._lock:
                self._errors.append((getattr(event, "job_id", "?"), label))

    def drain_errors(self) -> list[tuple[str, str]]:
        with self._lock:
            errs = self._errors[:]
            self._errors.clear()
            return errs


class Heartbeat:
    def __init__(
        self,
        notifier: Any,
        check: Callable[[], bool],
        health: JobHealth | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._notifier = notifier
        self._check = check
        self._health = health
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._job_alerted_at: dict[str, datetime] = {}
        self._fails = 0
        self._alerted = False

    def tick(self) -> None:
        from quant_signal.notifier.cards import alert_card

        # 1) 定时任务执行失败：汇总本周期内失败的 job 并告警（每个 job 只报一次样本）
        if self._health is not None:
            errs = self._health.drain_errors()
            if errs:
                by_job: dict[str, tuple[int, str]] = {}
                for jid, msg in errs:
                    cnt = by_job.get(jid, (0, msg))[0] + 1
                    by_job[jid] = (cnt, msg)
                now = self._now_fn()
                reportable = {
                    jid: detail
                    for jid, detail in by_job.items()
                    if jid not in self._job_alerted_at
                    or now - self._job_alerted_at[jid] >= JOB_ALERT_SILENCE
                }
                if reportable:
                    lines = [
                        f"- **{jid}** 失败 {cnt} 次：{msg[:150]}"
                        for jid, (cnt, msg) in reportable.items()
                    ]
                    delivered = self._notifier.send(
                        alert_card("定时任务执行失败", "\n".join(lines))
                    )
                    if delivered:
                        for jid in reportable:
                            self._job_alerted_at[jid] = now
                    log.warning(
                        "heartbeat.job_errors",
                        jobs=list(reportable),
                        delivered=delivered,
                    )

        # 2) 进程/数据源自检
        try:
            ok = self._check()
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat.check_error", error=str(e))
            ok = False
        if ok:
            self._fails = 0
            self._alerted = False
            return
        self._fails += 1
        if self._fails >= HEARTBEAT_FAIL_THRESHOLD and not self._alerted:
            self._notifier.send(
                alert_card("心跳检查失败", f"连续 {self._fails} 次自检失败，请检查进程/数据源")
            )
            self._alerted = True


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def build_scheduler(
    engine: Any,
    ledger: Any,
    store: Any,
    notifier: Any,
    runtime: "JobRuntime | None" = None,
) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)
    rotation_lock = threading.Lock()

    def run_rotation_once() -> None:
        # premarket 与亚洲两次 rotation 共用同一 Engine/BarStore，必须跨 job 串行。
        with rotation_lock:
            engine.run_premarket(datetime.now(timezone.utc))

    def premarket() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="premarket")
            return
        run_rotation_once()

    def intraday() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()) or now_et.time() < time(9, 30):
            log.info("skip.non_trading_day_or_before_open", job="intraday")
            return
        engine.run_intraday(datetime.now(timezone.utc))

    def postmarket() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="postmarket")
            return
        from quant_signal.report import build_daily_report

        notifier.send(build_daily_report(ledger, store, now_et.date()))

    def negative_overreaction() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="negative_overreaction")
            return
        engine.run_negative_overreaction(datetime.now(timezone.utc))

    def rotation_push() -> None:
        """08:00/15:30 北京时间的补充推送，不用 NYSE 日历门控（服务港股/韩股
        独立于美股假期），工作日过滤已由 CronTrigger 的 day_of_week 处理。"""
        run_rotation_once()

    def watch_deviation() -> None:
        engine.run_watch_deviation(datetime.now(timezone.utc))

    def enrichment() -> None:
        if engine.settings.notify.action_card_only:
            log.info("skip.action_card_only", job="enrichment")
            return
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="enrichment")
            return
        engine.run_enrichment(datetime.now(timezone.utc))

    def maintenance() -> None:
        from quant_signal.backup import run_backup
        from quant_signal.ingest import ingest_daily_split

        if engine is not None:
            ingest_daily_split(
                store,
                engine.settings,
                engine.settings.universe + engine.settings.watchlist,
                days=10,
            )
            # T3(O1)：每日备份台账(不可再生)与行情缓存(尽力)，保留14天
            run_backup(ledger, engine.settings.db_path, datetime.now(timezone.utc))

    settings = getattr(engine, "settings", None)
    legacy_deviation_enabled = (
        bool(settings.legacy_price_deviation.enabled) if settings is not None else False
    )
    execution_enabled = (
        bool(settings.execution_plan.enabled) if settings is not None else True
    )
    option_flow_enabled = (
        bool(settings.option_flow.enabled) if settings is not None else False
    )

    health = JobHealth()
    sched.add_listener(
        health.listen, EVENT_JOB_ERROR | EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES
    )
    runtime = runtime or JobRuntime()
    hb = Heartbeat(
        notifier=notifier, check=build_runtime_check(runtime), health=health
    )

    sched.add_job(
        runtime.wrap("premarket", premarket),
        CronTrigger(hour=8, minute=0, timezone=ET), id="premarket",
        misfire_grace_time=3600,
    )
    sched.add_job(
        runtime.wrap("intraday", intraday),
        CronTrigger(hour="9-15", minute="*/5", timezone=ET), id="intraday",
        misfire_grace_time=240,
    )
    sched.add_job(
        runtime.wrap("postmarket", postmarket),
        CronTrigger(hour=16, minute=30, timezone=ET), id="postmarket"
    )
    sched.add_job(
        runtime.wrap("negative_overreaction", negative_overreaction),
        CronTrigger(hour=16, minute=45, timezone=ET),
        id="negative_overreaction",
        misfire_grace_time=3600,
    )
    sched.add_job(
        runtime.wrap("maintenance", maintenance),
        CronTrigger(hour=3, minute=0, timezone=ET), id="maintenance"
    )
    sched.add_job(hb.tick, IntervalTrigger(minutes=15), id="heartbeat")
    # 以下三个 job 用固定 UTC 时刻门控，不随 scheduler 默认时区(ET)的夏令时漂移
    sched.add_job(
        runtime.wrap("rotation_asia_open", rotation_push),
        CronTrigger(hour=0, minute=0, day_of_week="mon-fri", timezone=timezone.utc),
        id="rotation_asia_open",  # 08:00 北京时间
        misfire_grace_time=3600,
    )
    sched.add_job(
        runtime.wrap("rotation_asia_close", rotation_push),
        CronTrigger(hour=7, minute=30, day_of_week="mon-fri", timezone=timezone.utc),
        id="rotation_asia_close",  # 15:30 北京时间
        misfire_grace_time=3600,
    )
    if legacy_deviation_enabled:
        # 旧普通价格偏离提醒: 默认下线, 保留 feature flag 以便快速回滚
        sched.add_job(
            runtime.wrap("watch_deviation", watch_deviation),
            CronTrigger(hour="0-21", minute="*/5", day_of_week="mon-fri", timezone=timezone.utc),
            id="watch_deviation",  # 覆盖亚洲(约01:00-08:00 UTC)与美股(13:30-21:00 UTC，含冬令时缓冲)
            misfire_grace_time=240,
        )

    if execution_enabled:

        def execution_brief() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job="execution_brief")
                return
            engine.run_execution_brief(datetime.now(timezone.utc))

        def execution_watch() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job="execution_watch")
                return
            engine.run_execution_watch(datetime.now(timezone.utc))

        # 08:15 ET: 指数扫描(07:00)之后、盘前早报(08:00)之外的独立执行计划卡
        sched.add_job(
            runtime.wrap("execution_brief", execution_brief),
            CronTrigger(hour=8, minute=15, timezone=ET),
            id="execution_brief",
            misfire_grace_time=3600,
        )
        # 美股盘中每5分钟评估计划状态迁移(入场窗口约束在状态机内部)
        sched.add_job(
            runtime.wrap("execution_watch", execution_watch),
            CronTrigger(hour="9-15", minute="1-56/5", timezone=ET),
            id="execution_watch",
            misfire_grace_time=240,
        )

    if option_flow_enabled:

        def option_flow() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job="option_flow")
                return
            engine.run_option_flow(datetime.now(timezone.utc))

        def option_flow_close() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job="option_flow_close")
                return
            engine.run_option_flow(
                datetime.now(timezone.utc), force_summary=True
            )

        def option_flow_drain() -> None:
            now_et = _now_et()
            if not is_trading_day(now_et.date()):
                log.info("skip.non_trading_day", job="option_flow_drain")
                return
            engine.run_option_flow_delivery(datetime.now(timezone.utc))

        # 累计成交量在开盘半小时后更有解释力；每15分钟扫描，避免普通噪音刷屏。
        sched.add_job(
            runtime.wrap("option_flow", option_flow),
            CronTrigger(
                hour="10-15", minute="0,15,30,45", timezone=ET
            ),
            id="option_flow",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
        # 收盘后单独保留一个名额，输出当日最终可见榜单。
        sched.add_job(
            runtime.wrap("option_flow_close", option_flow_close),
            CronTrigger(hour=16, minute=20, timezone=ET),
            id="option_flow_close",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        # 16:20 发送失败的收盘卡在 12 小时过期窗口内需要真实重试机会：
        # 傍晚每小时只排空 outbox，不再抓取数据。
        sched.add_job(
            runtime.wrap("option_flow_drain", option_flow_drain),
            CronTrigger(hour="16-21", minute=35, timezone=ET),
            id="option_flow_drain",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,
        )
    sched.add_job(
        enrichment, CronTrigger(hour=8, minute=45, timezone=ET), id="enrichment"
    )

    def performance() -> None:
        engine.run_performance(datetime.now(timezone.utc))

    def data_qa() -> None:
        engine.run_data_qa(datetime.now(timezone.utc))

    def market_scan() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="market_scan")
            return
        engine.run_market_scan(datetime.now(timezone.utc))

    # 每交易日 07:00 ET(早于盘前早报)全市场扫描 Top1；最重的 job, 错过宽限 1h
    sched.add_job(
        runtime.wrap("market_scan", market_scan),
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=ET),
        id="market_scan",
        misfire_grace_time=3600,
    )

    # 每日 03:30 ET(maintenance 之后)体检两源收盘价偏差
    sched.add_job(
        runtime.wrap("data_qa", data_qa),
        CronTrigger(hour=3, minute=30, timezone=ET), id="data_qa"
    )

    # 每周六 09:00 ET(周五收盘后)复盘近90天信号虚拟盘；错过宽限6小时
    sched.add_job(
        runtime.wrap("performance", performance),
        CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=ET),
        id="performance",
        misfire_grace_time=21600,
    )
    return sched
