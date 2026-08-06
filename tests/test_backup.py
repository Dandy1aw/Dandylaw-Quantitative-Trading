import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_signal.backup import run_backup
from quant_signal.datafeed.store import BarStore
from quant_signal.ledger import SignalLedger
from quant_signal.strategies.base import Direction, Signal

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def test_backup_creates_openable_copy_and_prunes(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    ledger.insert(
        Signal(ticker="MU", direction=Direction.BUY, price=100.0, reason="r",
               strategy_id="momentum_rotation", ts=NOW),
        pushed=True, now=NOW,
    )
    store = BarStore(tmp_path / "bars.duckdb")
    old_dir = tmp_path / "backups" / (NOW - timedelta(days=20)).strftime("%Y%m%d")
    old_dir.mkdir(parents=True)

    dest = run_backup(ledger, tmp_path, NOW, keep_days=14, bar_store=store)

    con = sqlite3.connect(str(dest / "signals.db"))
    n = con.execute("SELECT count(*) FROM signals").fetchone()[0]
    con.close()
    assert n == 1                              # 备份可打开且含数据
    assert (dest / "bars_export" / "schema.sql").exists()
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["restore_probe"]["bars_1d"] == 0
    assert "bars_export/schema.sql" in manifest["files"]
    assert not old_dir.exists()                # 超过14天的备份被清理
    store.close()


def test_backup_writes_manifest_and_offsite_mirror(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "data" / "signals.db")
    store = BarStore(tmp_path / "data" / "bars.duckdb")
    mirror = tmp_path / "offsite"

    dest = run_backup(
        ledger,
        tmp_path / "data",
        NOW,
        bar_store=store,
        mirror_dir=mirror,
        require_mirror=True,
    )

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["signals.db"]["bytes"] > 0
    assert len(manifest["files"]["signals.db"]["sha256"]) == 64
    assert (mirror / dest.name / "signals.db").read_bytes() == (
        dest / "signals.db"
    ).read_bytes()
    assert (mirror / dest.name / "manifest.json").exists()
    assert (mirror / dest.name / "bars_export" / "schema.sql").exists()
    store.close()


def test_backup_rejects_mirror_inside_local_backup_tree(tmp_path: Path) -> None:
    ledger = SignalLedger(tmp_path / "signals.db")
    with pytest.raises(ValueError, match="outside"):
        run_backup(
            ledger,
            tmp_path,
            NOW,
            mirror_dir=tmp_path / "backups" / "mirror",
        )
