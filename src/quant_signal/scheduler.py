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

    def maintenance() -> None:
        from quant_signal.ingest import ingest_daily

        if engine is not None:
            ingest_daily(
                store,
                engine.source,
                engine.settings.universe + engine.settings.watchlist,
                days=10,
            )

    hb = Heartbeat(notifier=notifier, check=lambda: True)

    sched.add_job(premarket, CronTrigger(hour=8, minute=0), id="premarket")
    sched.add_job(intraday, CronTrigger(hour="9-15", minute="*/5"), id="intraday")
    sched.add_job(postmarket, CronTrigger(hour=16, minute=30), id="postmarket")
    sched.add_job(maintenance, CronTrigger(hour=3, minute=0), id="maintenance")
    sched.add_job(hb.tick, IntervalTrigger(minutes=15), id="heartbeat")
    return sched
