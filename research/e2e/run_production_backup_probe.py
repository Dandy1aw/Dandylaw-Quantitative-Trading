from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant_signal.backup import run_backup  # noqa: E402
from quant_signal.config import load_settings  # noqa: E402
from quant_signal.datafeed.store import BarStore  # noqa: E402
from quant_signal.ledger import SignalLedger  # noqa: E402


def main() -> None:
    settings = load_settings()
    store = BarStore(ROOT / "data" / "bars.duckdb")
    ledger = SignalLedger(ROOT / "data" / "signals.db")
    try:
        destination = run_backup(
            ledger,
            ROOT / "data",
            datetime.now(timezone.utc),
            bar_store=store,
            keep_days=settings.backup.keep_days,
            mirror_dir=Path(settings.backup.mirror_dir),
            require_mirror=settings.backup.require_mirror,
        )
    finally:
        store.close()
    manifest = json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )
    print(
        json.dumps(
            {
                "destination": str(destination),
                "mirror": str(
                    Path(settings.backup.mirror_dir) / destination.name
                ),
                "restore_probe": manifest["restore_probe"],
                "file_count": len(manifest["files"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
