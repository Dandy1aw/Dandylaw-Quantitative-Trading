"""每日一致性备份：SQLite 在线副本 + 活动 DuckDB 连接导出。

备份到 data/backups/YYYYMMDD/，保留 keep_days 天。DuckDB 文件在 Windows
运行期不可安全复制，因此通过活动连接 EXPORT DATABASE；SQLite 走 backup API。
"""

from __future__ import annotations

from hashlib import sha256
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import structlog

from quant_signal.ledger import SignalLedger
from quant_signal.datafeed.store import BarStore

log = structlog.get_logger()


def run_backup(
    ledger: SignalLedger,
    db_dir: Path,
    now: datetime,
    keep_days: int = 14,
    *,
    bar_store: BarStore | None = None,
    mirror_dir: Path | None = None,
    require_mirror: bool = False,
) -> Path:
    dest_dir = db_dir / "backups" / now.strftime("%Y%m%d")
    dest_dir.mkdir(parents=True, exist_ok=True)
    ledger.backup_to(dest_dir / "signals.db")
    restore_probe: dict[str, int] | None = None
    if bar_store is not None:
        export_dir = dest_dir / "bars_export"
        if export_dir.exists():
            resolved_export = export_dir.resolve()
            if resolved_export.parent != dest_dir.resolve():
                raise ValueError("bars export must stay inside the dated backup")
            shutil.rmtree(resolved_export)
        restore_probe = bar_store.export_database(export_dir)
    else:
        log.warning("backup.bars_store_missing")
    files = sorted(path for path in dest_dir.rglob("*") if path.is_file())
    manifest = {
        "created_at": now.isoformat(),
        "restore_probe": restore_probe,
        "files": {
            path.relative_to(dest_dir).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": sha256(path.read_bytes()).hexdigest(),
            }
            for path in files
        },
    }
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y%m%d")
    _prune_dated_backups(db_dir / "backups", cutoff)
    mirrored_to: Path | None = None
    if mirror_dir is not None:
        mirrored_to = _mirror_backup(dest_dir, mirror_dir, cutoff)
    elif require_mirror:
        raise RuntimeError("offsite backup is required but no mirror directory is configured")
    log.info(
        "backup.done",
        dest=str(dest_dir),
        mirror=str(mirrored_to) if mirrored_to is not None else None,
    )
    return dest_dir


def _prune_dated_backups(root: Path, cutoff: str) -> None:
    if not root.exists():
        return
    for old in root.iterdir():
        if (
            old.is_dir()
            and re.fullmatch(r"\d{8}", old.name)
            and old.name < cutoff
        ):
            shutil.rmtree(old)


def _mirror_backup(source: Path, mirror_root: Path, cutoff: str) -> Path:
    source_root = source.parent.resolve()
    resolved_mirror = mirror_root.resolve()
    if resolved_mirror == source_root or resolved_mirror.is_relative_to(source_root):
        raise ValueError("backup mirror must be outside the local backup directory")
    resolved_mirror.mkdir(parents=True, exist_ok=True)
    destination = resolved_mirror / source.name
    shutil.copytree(source, destination, dirs_exist_ok=True)
    _prune_dated_backups(resolved_mirror, cutoff)
    return destination
