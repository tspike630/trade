"""
SQLite consistent backup using Connection.backup().

  python backup.py
  python backup.py --db dexscreener.db --dir backups --retain 7

Retains the newest N backups under backups/dexscreener-YYYYMMDD-HHMMSS.db.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import BACKUP_DIR, BACKUP_RETAIN, DB_PATH
from logging_setup import get_logger, setup_logging

log = get_logger("backup")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def backup_db(
    db_path: str | Path = DB_PATH,
    backup_dir: str | Path = BACKUP_DIR,
    retain: int = BACKUP_RETAIN,
) -> Path:
    src = Path(db_path)
    if not src.is_file():
        raise FileNotFoundError(f"database not found: {src}")

    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"dexscreener-{_utc_stamp()}.db"

    log.info("backup start src=%s dest=%s", src, dest)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    # prune older backups (keep newest `retain`)
    retain = max(1, int(retain))
    existing = sorted(
        out_dir.glob("dexscreener-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in existing[retain:]:
        try:
            old.unlink()
            log.info("backup pruned %s", old.name)
        except OSError as exc:
            log.warning("failed to prune %s: %s", old, exc)

    size = dest.stat().st_size
    log.info("backup ok dest=%s size=%s retained=%s", dest, size, min(retain, len(existing)))
    return dest


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Backup dexscreener SQLite DB")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dir", default=BACKUP_DIR)
    parser.add_argument("--retain", type=int, default=BACKUP_RETAIN)
    args = parser.parse_args(argv)
    try:
        path = backup_db(args.db, args.dir, args.retain)
    except Exception as exc:  # noqa: BLE001
        log.error("backup failed: %s", exc)
        print(f"BACKUP_FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"BACKUP_OK {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
