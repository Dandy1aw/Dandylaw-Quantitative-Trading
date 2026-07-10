import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_signal.backup import run_backup
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
    (tmp_path / "bars.duckdb").write_bytes(b"fake")
    old_dir = tmp_path / "backups" / (NOW - timedelta(days=20)).strftime("%Y%m%d")
    old_dir.mkdir(parents=True)

    dest = run_backup(ledger, tmp_path, NOW, keep_days=14)

    con = sqlite3.connect(str(dest / "signals.db"))
    n = con.execute("SELECT count(*) FROM signals").fetchone()[0]
    con.close()
    assert n == 1                              # 备份可打开且含数据
    assert (dest / "bars.duckdb").exists()     # 行情缓存也复制了
    assert not old_dir.exists()                # 超过14天的备份被清理
