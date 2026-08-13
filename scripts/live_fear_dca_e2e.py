"""Explicit, production-safe driver for the fear-index DCA pipeline.

The default mode fetches real Yahoo data but records cards and images in memory.
Only ``--send`` opens ``data/signals.db`` and constructs the configured notifier.
"""

# isort: skip_file

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
os.chdir(REPO_ROOT)
src_path = str(REPO_ROOT / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

from quant_signal.config import Settings, load_settings
from quant_signal.datafeed.base import get_source
from quant_signal.datafeed.store import BarStore
from quant_signal.engine import Engine
from quant_signal.feishu_bot import LarkTransport
from quant_signal.ledger import SignalLedger
from quant_signal.logging_setup import setup_logging
from quant_signal.notifier.base import Card, ConsoleNotifier, Notifier
from quant_signal.notifier.cards import fear_dca_rules_card
from quant_signal.notifier.feishu import get_notifier
from quant_signal.pipelines.us_briefing import (
    last_completed_us_session,
)


@dataclass(frozen=True)
class CliOptions:
    send: bool = False
    replay: bool = False
    rules: bool = False
    bar_store: Path | None = None


@dataclass
class RecordingNotifier:
    """Image-capable notifier that performs no external writes."""

    cards: list[Card] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)

    @property
    def supports_message_uuid(self) -> bool:
        return True

    def upload_image(self, image_bytes: bytes) -> str:
        self.images.append(image_bytes)
        return "dry-run-image-key"

    def send(self, card: Card) -> bool:
        self.cards.append(card)
        return True


@dataclass(frozen=True)
class Runtime:
    store: BarStore
    ledger: SignalLedger
    notifier: Notifier
    ledger_path: Path
    store_path: Path | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fear DCA pipeline safely. The default is a real-data dry-run; "
            "--send is required for any Feishu delivery or production-ledger write."
        )
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="use data/signals.db and the configured real notifier",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="after the pipeline, send the latest stored COMPLETE card once more",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        help="after the pipeline, send the static bot rules card once",
    )
    parser.add_argument(
        "--bar-store",
        type=Path,
        help=(
            "explicit existing DuckDB path; it is opened read-only. Use the live "
            "data/bars.duckdb only after the scheduler has stopped."
        ),
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> CliOptions:
    parser = _parser()
    namespace = parser.parse_args(argv)
    if (namespace.replay or namespace.rules) and not namespace.send:
        parser.error("--replay and --rules require --send")
    return CliOptions(
        send=bool(namespace.send),
        replay=bool(namespace.replay),
        rules=bool(namespace.rules),
        bar_store=namespace.bar_store,
    )


def planned_deliveries(options: CliOptions) -> tuple[str, ...]:
    if not options.send:
        return ("dry-run recorder: 0 external deliveries",)
    plans = ["fear pipeline: at most 1 (0 when COMPLETE already exists)"]
    if options.replay:
        plans.append("latest COMPLETE replay: 1")
    if options.rules:
        plans.append("static rules card: 1")
    return tuple(plans)


def maximum_external_deliveries(options: CliOptions) -> int:
    """Return the maximum number of logical external card deliveries."""
    if not options.send:
        return 0
    return 1 + int(options.replay) + int(options.rules)


def _backup_candidates(db_dir: Path) -> list[Path]:
    backup_root = db_dir / "backups"
    if not backup_root.exists():
        return []
    candidates = [
        path.resolve() for path in backup_root.rglob("bars.duckdb") if path.is_file()
    ]
    return sorted(
        candidates,
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )


def open_safe_bar_store(
    db_dir: Path,
    *,
    explicit: Path | None,
) -> tuple[BarStore, Path | None]:
    """Open an explicit/newest backup read-only, or an unused memory store."""
    if explicit is not None:
        selected = explicit if explicit.is_absolute() else REPO_ROOT / explicit
        selected = selected.resolve()
        if not selected.is_file():
            raise FileNotFoundError(f"bar store does not exist: {selected}")
        return BarStore(selected, read_only=True), selected

    for candidate in _backup_candidates(db_dir):
        try:
            return BarStore(candidate, read_only=True), candidate
        except Exception as error:  # noqa: BLE001 - skip corrupt backup candidates
            print(
                f"Skipping unreadable backup BarStore {candidate}: {error}",
                file=sys.stderr,
            )
    # The fear pipeline never reads Engine.store. A memory store avoids touching the
    # live DuckDB when no legacy file backup is available (new backups are exports).
    return BarStore(Path(":memory:")), None


def build_runtime(settings: Settings, options: CliOptions) -> Runtime:
    store, store_path = open_safe_bar_store(
        settings.db_path,
        explicit=options.bar_store,
    )
    ledger_path = settings.db_path / "signals.db" if options.send else Path(":memory:")
    ledger = SignalLedger(ledger_path)
    notifier: Notifier = get_notifier(settings) if options.send else RecordingNotifier()
    return Runtime(
        store=store,
        ledger=ledger,
        notifier=notifier,
        ledger_path=ledger_path,
        store_path=store_path,
    )


def run_report(
    target_session: object,
    engine_result: bool,
    row: dict[str, object] | None,
) -> dict[str, object]:
    card = row.get("card") if row is not None else None
    return {
        "target_session": str(target_session),
        "engine_result": engine_result,
        "run_status": row.get("status") if row is not None else None,
        "chart_status": row.get("chart_status") if row is not None else None,
        "send_status": row.get("send_status") if row is not None else None,
        "error": row.get("error") if row is not None else None,
        "chart_error": row.get("chart_error") if row is not None else None,
        "send_error": row.get("send_error") if row is not None else None,
        "card_title": card.title if isinstance(card, Card) else None,
        "message_uuid": card.message_uuid if isinstance(card, Card) else None,
    }


def _targeted_transport(settings: Settings) -> tuple[LarkTransport, str, str]:
    target = settings.feishu_bot.push_receive_id
    if not settings.feishu_app_id or not settings.feishu_app_secret or not target:
        raise RuntimeError(
            "--replay/--rules require FEISHU_APP_ID, FEISHU_APP_SECRET, and "
            "feishu_bot.push_receive_id"
        )
    receive_id_type = "open_id" if target.startswith("ou_") else "chat_id"
    return (
        LarkTransport(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            settings.feishu_proxy,
        ),
        target,
        receive_id_type,
    )


def send_requested_bot_cards(
    settings: Settings,
    runtime: Runtime,
    options: CliOptions,
) -> dict[str, bool]:
    if not options.replay and not options.rules:
        return {}
    transport, target, receive_id_type = _targeted_transport(settings)
    results: dict[str, bool] = {}
    if options.replay:
        card = runtime.ledger.latest_complete_fear_dca_card()
        if card is None:
            results["replay"] = False
        else:
            replay_card = dataclasses.replace(card, message_uuid=str(uuid4()))
            results["replay"] = transport.send_card_to(
                target,
                receive_id_type,
                replay_card,
            )
    if options.rules:
        rules_card = dataclasses.replace(
            fear_dca_rules_card(),
            message_uuid=str(uuid4()),
        )
        results["rules"] = transport.send_card_to(
            target,
            receive_id_type,
            rules_card,
        )
    return results


def execute(options: CliOptions, *, now: datetime | None = None) -> int:
    setup_logging()
    settings = load_settings()
    run_at = now or datetime.now(ZoneInfo(settings.fear_dca.timezone))
    target_session = last_completed_us_session(run_at)
    print(
        json.dumps(
            {
                "mode": "LIVE" if options.send else "DRY-RUN",
                "target_session": target_session.isoformat(),
                "maximum_external_deliveries": maximum_external_deliveries(options),
                "planned_deliveries": planned_deliveries(options),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    runtime = build_runtime(settings, options)
    try:
        if options.send and isinstance(runtime.notifier, ConsoleNotifier):
            raise RuntimeError(
                "--send resolved to ConsoleNotifier; configure a Feishu app or webhook"
            )
        print(
            json.dumps(
                {
                    "ledger": str(runtime.ledger_path),
                    "bar_store": (
                        str(runtime.store_path)
                        if runtime.store_path is not None
                        else ":memory: (unused by fear pipeline)"
                    ),
                    "notifier": type(runtime.notifier).__name__,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        engine = Engine(
            settings,
            runtime.store,
            get_source(settings),
            runtime.ledger,
            runtime.notifier,
        )
        engine_result = engine.run_fear_dca(run_at)
        row = runtime.ledger.fear_dca_run(target_session)
        print(
            json.dumps(
                run_report(target_session, engine_result, row),
                ensure_ascii=False,
                indent=2,
            )
        )
        bot_results = send_requested_bot_cards(settings, runtime, options)
        if bot_results:
            print(json.dumps({"bot_deliveries": bot_results}, indent=2))
        if isinstance(runtime.notifier, RecordingNotifier):
            print(
                json.dumps(
                    {
                        "recorded_cards": len(runtime.notifier.cards),
                        "recorded_images": len(runtime.notifier.images),
                        "recorded_titles": [
                            card.title for card in runtime.notifier.cards
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0 if engine_result and all(bot_results.values()) else 1
    finally:
        runtime.store.close()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return execute(parse_args(argv))
    except Exception as error:  # noqa: BLE001 - CLI prints one concise failure
        print(f"fear DCA E2E failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
