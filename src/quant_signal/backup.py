"""T3(O1) 每日数据备份：signals.db(不可再生台账) + bars.duckdb(尽力而为)。

备份到 data/backups/YYYYMMDD/，保留 keep_days 天。bars.duckdb 是可重拉的行情
缓存，被进程锁住复制失败就跳过(记 warning)；signals.db 走 sqlite backup API，
与写入互斥安全。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from quant_signal.ledger import SignalLedger

log = structlog.get_logger()


def run_backup(
    ledger: SignalLedger, db_dir: Path, now: datetime, keep_days: int = 14
) -> Path:
    dest_dir = db_dir / "backups" / now.strftime("%Y%m%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    ledger.backup_to(dest_dir / "signals.db")
    bars = db_dir / "bars.duckdb"
    if bars.exists():
        try:
            shutil.copy2(bars, dest_dir / "bars.duckdb")
        except OSError as error:
            log.warning("backup.bars_skipped", error=str(error))
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y%m%d")
    for old in (db_dir / "backups").iterdir():
        if old.is_dir() and old.name < cutoff:
            shutil.rmtree(old, ignore_errors=True)
    log.info("backup.done", dest=str(dest_dir))
    return dest_dir
