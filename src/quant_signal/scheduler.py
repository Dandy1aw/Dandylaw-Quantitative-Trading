from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from quant_signal.calendar import is_trading_day

log = structlog.get_logger()
ET = ZoneInfo("America/New_York")
HEARTBEAT_FAIL_THRESHOLD = 2


class Heartbeat:
    def __init__(self, notifier: Any, check: Callable[[], bool]) -> None:
        self._notifier = notifier
        self._check = check
        self._fails = 0
        self._alerted = False

    def tick(self) -> None:
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
            from quant_signal.notifier.cards import alert_card

            self._notifier.send(
                alert_card("心跳检查失败", f"连续 {self._fails} 次自检失败，请检查进程/数据源")
            )
            self._alerted = True


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def build_scheduler(
    engine: Any, ledger: Any, store: Any, notifier: Any
) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=ET)

    def premarket() -> None:
        now_et = _now_et()
        if not is_trading_day(now_et.date()):
            log.info("skip.non_trading_day", job="premarket")
            return
        engine.run_premarket(datetime.now(timezone.utc))

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

    def rotation_push() -> None:
        """08:00/15:30 北京时间的补充推送，不用 NYSE 日历门控（服务港股/韩股
        独立于美股假期），工作日过滤已由 CronTrigger 的 day_of_week 处理。"""
        engine.run_premarket(datetime.now(timezone.utc))

    def watch_deviation() -> None:
        engine.run_watch_deviation(datetime.now(timezone.utc))

    def maintenance() -> None:
        from quant_signal.ingest import ingest_daily_split

        if engine is not None:
            ingest_daily_split(
                store,
                engine.settings,
                engine.settings.universe + engine.settings.watchlist,
                days=10,
            )

    hb = Heartbeat(notifier=notifier, check=lambda: True)

    sched.add_job(premarket, CronTrigger(hour=8, minute=0), id="premarket")
    sched.add_job(intraday, CronTrigger(hour="9-15", minute="*/5"), id="intraday")
    sched.add_job(postmarket, CronTrigger(hour=16, minute=30), id="postmarket")
    sched.add_job(maintenance, CronTrigger(hour=3, minute=0), id="maintenance")
    sched.add_job(hb.tick, IntervalTrigger(minutes=15), id="heartbeat")
    # 以下三个 job 用固定 UTC 时刻门控，不随 scheduler 默认时区(ET)的夏令时漂移
    sched.add_job(
        rotation_push,
        CronTrigger(hour=0, minute=0, day_of_week="mon-fri", timezone=timezone.utc),
        id="rotation_asia_open",  # 08:00 北京时间
    )
    sched.add_job(
        rotation_push,
        CronTrigger(hour=7, minute=30, day_of_week="mon-fri", timezone=timezone.utc),
        id="rotation_asia_close",  # 15:30 北京时间
    )
    sched.add_job(
        watch_deviation,
        CronTrigger(hour="0-21", minute="*/5", day_of_week="mon-fri", timezone=timezone.utc),
        id="watch_deviation",  # 覆盖亚洲(约01:00-08:00 UTC)与美股(13:30-21:00 UTC，含冬令时缓冲)
    )
    return sched
