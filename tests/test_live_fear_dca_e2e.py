from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from quant_signal.datafeed.store import BarStore
from quant_signal.notifier.base import Card, CardKind


def test_replay_and_rules_require_explicit_send() -> None:
    from scripts import live_fear_dca_e2e as script

    with pytest.raises(SystemExit):
        script.parse_args(["--replay"])
    with pytest.raises(SystemExit):
        script.parse_args(["--rules"])

    options = script.parse_args(["--send", "--replay", "--rules"])

    assert script.planned_deliveries(options) == (
        "fear pipeline: at most 1 (0 when COMPLETE already exists)",
        "latest COMPLETE replay: 1",
        "static rules card: 1",
    )
    assert script.maximum_external_deliveries(options) == 3
    assert script.maximum_external_deliveries(script.CliOptions()) == 0


def test_recording_notifier_captures_card_and_chart_without_external_io() -> None:
    from scripts import live_fear_dca_e2e as script

    notifier = script.RecordingNotifier()
    card = Card(CardKind.REPORT, "title", "body")

    assert notifier.upload_image(b"png") == "dry-run-image-key"
    assert notifier.send(card)
    assert notifier.images == [b"png"]
    assert notifier.cards == [card]
    assert notifier.supports_message_uuid


def test_open_safe_bar_store_uses_newest_valid_backup_read_only(
    tmp_path: Path,
) -> None:
    from scripts import live_fear_dca_e2e as script

    older = tmp_path / "backups" / "20260811" / "bars.duckdb"
    newer = tmp_path / "backups" / "20260812" / "bars.duckdb"
    older.parent.mkdir(parents=True)
    newer.parent.mkdir(parents=True)
    BarStore(older).close()
    BarStore(newer).close()
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    store, selected = script.open_safe_bar_store(tmp_path, explicit=None)
    try:
        assert selected == newer.resolve()
        assert store._read_only is True
    finally:
        store.close()


def test_open_safe_bar_store_never_falls_back_to_live_database(
    tmp_path: Path,
) -> None:
    from scripts import live_fear_dca_e2e as script

    live = tmp_path / "bars.duckdb"
    BarStore(live).close()

    store, selected = script.open_safe_bar_store(tmp_path, explicit=None)
    try:
        assert selected is None
        assert store._read_only is False
    finally:
        store.close()


def test_dry_run_runtime_uses_memory_ledger_and_never_builds_real_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import live_fear_dca_e2e as script

    captured_paths: list[Path] = []

    class FakeLedger:
        def __init__(self, path: Path) -> None:
            captured_paths.append(path)

    sentinel_store = object()
    monkeypatch.setattr(script, "SignalLedger", FakeLedger)
    monkeypatch.setattr(
        script,
        "open_safe_bar_store",
        lambda *_args, **_kwargs: (sentinel_store, None),
    )
    monkeypatch.setattr(
        script,
        "get_notifier",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("dry-run must not construct a real notifier")
        ),
    )
    settings = SimpleNamespace(db_path=tmp_path)

    runtime = script.build_runtime(
        settings,
        script.CliOptions(send=False),
    )

    assert captured_paths == [Path(":memory:")]
    assert runtime.store is sentinel_store
    assert isinstance(runtime.notifier, script.RecordingNotifier)
    assert runtime.ledger_path == Path(":memory:")


def test_live_runtime_uses_production_ledger_and_get_notifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import live_fear_dca_e2e as script

    captured_paths: list[Path] = []
    real_notifier = object()

    class FakeLedger:
        def __init__(self, path: Path) -> None:
            captured_paths.append(path)

    monkeypatch.setattr(script, "SignalLedger", FakeLedger)
    monkeypatch.setattr(
        script,
        "open_safe_bar_store",
        lambda *_args, **_kwargs: (object(), tmp_path / "backup.duckdb"),
    )
    monkeypatch.setattr(script, "get_notifier", lambda _settings: real_notifier)
    settings = SimpleNamespace(db_path=tmp_path)

    runtime = script.build_runtime(settings, script.CliOptions(send=True))

    assert captured_paths == [tmp_path / "signals.db"]
    assert runtime.notifier is real_notifier
    assert runtime.ledger_path == tmp_path / "signals.db"


def test_default_execute_uses_yahoo_source_without_alpaca_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import live_fear_dca_e2e as script

    target = date(2026, 8, 12)
    captured_sources: list[object] = []

    class FakeStore:
        def close(self) -> None:
            pass

    class FakeLedger:
        def fear_dca_run(self, _session: date) -> dict[str, object]:
            return {
                "status": "COMPLETE",
                "chart_status": "UPLOADED",
                "send_status": "SENT",
                "error": None,
                "chart_error": None,
                "send_error": None,
                "card": Card(CardKind.REPORT, "report", "body"),
            }

    class FakeYahooSource:
        pass

    class FakeEngine:
        def __init__(
            self,
            _settings: object,
            _store: object,
            source: object,
            _ledger: object,
            _notifier: object,
        ) -> None:
            captured_sources.append(source)

        def run_fear_dca(self, _now: datetime) -> bool:
            return True

    settings = SimpleNamespace(
        data_source="alpaca",
        alpaca_key="",
        alpaca_secret="",
        db_path=tmp_path,
        fear_dca=SimpleNamespace(timezone="Asia/Shanghai"),
    )
    runtime = SimpleNamespace(
        store=FakeStore(),
        ledger=FakeLedger(),
        notifier=script.RecordingNotifier(),
        ledger_path=Path(":memory:"),
        store_path=None,
    )
    monkeypatch.setattr(script, "setup_logging", lambda: None)
    monkeypatch.setattr(script, "load_settings", lambda: settings)
    monkeypatch.setattr(script, "build_runtime", lambda *_args: runtime)
    monkeypatch.setattr(script, "last_completed_us_session", lambda _now: target)
    monkeypatch.setattr(script, "Engine", FakeEngine)
    monkeypatch.setattr(script, "YFinanceSource", FakeYahooSource, raising=False)

    result = script.execute(
        script.CliOptions(),
        now=datetime(2026, 8, 13, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert result == 0
    assert len(captured_sources) == 1
    assert isinstance(captured_sources[0], FakeYahooSource)


def test_run_report_includes_delivery_and_card_identity() -> None:
    from scripts import live_fear_dca_e2e as script

    card = Card(
        CardKind.REPORT,
        "Fear DCA",
        "body",
        message_uuid="stable-message-id",
    )
    row = {
        "status": "COMPLETE",
        "chart_status": "UPLOADED",
        "send_status": "SENT",
        "error": None,
        "chart_error": None,
        "send_error": None,
        "card": card,
    }

    report = script.run_report(date(2026, 8, 12), True, row)

    assert report == {
        "target_session": "2026-08-12",
        "engine_result": True,
        "run_status": "COMPLETE",
        "chart_status": "UPLOADED",
        "send_status": "SENT",
        "error": None,
        "chart_error": None,
        "send_error": None,
        "card_title": "Fear DCA",
        "message_uuid": "stable-message-id",
    }


def test_requested_replay_and_rules_use_direct_bot_transport_without_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import live_fear_dca_e2e as script

    calls: list[tuple[str, str, Card]] = []

    class FakeTransport:
        def send_card_to(
            self, receive_id: str, receive_id_type: str, card: Card
        ) -> bool:
            calls.append((receive_id, receive_id_type, card))
            return True

    stored = Card(
        CardKind.REPORT,
        "Stored report",
        "body",
        message_uuid="stored-uuid",
    )
    ledger = SimpleNamespace(latest_complete_fear_dca_card=lambda: stored)
    runtime = SimpleNamespace(ledger=ledger)
    monkeypatch.setattr(
        script,
        "_targeted_transport",
        lambda _settings: (FakeTransport(), "oc_target", "chat_id"),
    )

    results = script.send_requested_bot_cards(
        SimpleNamespace(),
        runtime,
        script.CliOptions(send=True, replay=True, rules=True),
    )

    assert results == {"replay": True, "rules": True}
    assert [(target, kind) for target, kind, _card in calls] == [
        ("oc_target", "chat_id"),
        ("oc_target", "chat_id"),
    ]
    assert calls[0][2].title == "Stored report"
    assert calls[0][2].message_uuid != "stored-uuid"
    assert calls[1][2].message_uuid is not None
