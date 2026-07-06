from quant_signal.scheduler import (
    HEARTBEAT_FAIL_THRESHOLD,
    Heartbeat,
    JobHealth,
    build_scheduler,
)


class _Event:
    def __init__(self, job_id: str, exception: object | None) -> None:
        self.job_id = job_id
        self.exception = exception


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[object] = []

    def send(self, card: object) -> bool:
        self.cards.append(card)
        return True


def test_scheduler_registers_all_jobs() -> None:
    sched = build_scheduler(engine=None, ledger=None, store=None, notifier=FakeNotifier())
    ids = {j.id for j in sched.get_jobs()}
    assert ids == {
        "premarket", "intraday", "postmarket", "maintenance", "heartbeat",
        "rotation_asia_open", "rotation_asia_close", "watch_deviation", "enrichment",
    }


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
