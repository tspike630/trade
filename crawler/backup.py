"""
SQLite consistent backup using Connection.backup().

  python backup.py
  python backup.py --db dexscreener.db --dir backups --retain 7

Retains the newest N backups per prefix under backups/<prefix>-YYYYMMDD-HHMMSS.db.

Sidecars (missing files are skipped, same retain count):
  PRIOR_DB_PATH or PRIOR_DB, else data/prior/db/prior.db
  PAPER_DB_PATH
  EXECUTION_LEDGER_PATH, else execution_ledger.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from config import BACKUP_DIR, BACKUP_RETAIN, DB_PATH, PAPER_DB_PATH
from logging_setup import get_logger, setup_logging

log = get_logger("backup")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sidecar_targets() -> list[tuple[str, Path]]:
    prior = os.environ.get("PRIOR_DB_PATH", "").strip() or os.environ.get("PRIOR_DB", "").strip()
    if not prior:
        prior = str(Path(__file__).resolve().parent / "data" / "prior" / "db" / "prior.db")
    ledger = os.environ.get("EXECUTION_LEDGER_PATH", "").strip() or "execution_ledger.db"
    return [
        ("prior", Path(prior)),
        ("paper", Path(PAPER_DB_PATH)),
        ("execution_ledger", Path(ledger)),
    ]


def _copy_sqlite(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _prune(out_dir: Path, prefix: str, retain: int) -> int:
    existing = sorted(
        out_dir.glob(f"{prefix}-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in existing[retain:]:
        try:
            old.unlink()
            log.info("backup pruned %s", old.name)
        except OSError as exc:
            log.warning("failed to prune %s: %s", old.name, type(exc).__name__)
    return len(existing)


def _backup_one(
    src: Path,
    out_dir: Path,
    prefix: str,
    stamp: str,
    retain: int,
    *,
    required: bool,
) -> Path | None:
    if not src.is_file():
        if required:
            raise FileNotFoundError(f"database not found: {src}")
        log.info("backup skip missing prefix=%s path=%s", prefix, src)
        return None
    dest = out_dir / f"{prefix}-{stamp}.db"
    log.info("backup start src=%s dest=%s", src, dest)
    try:
        _copy_sqlite(src, dest)
    except Exception as exc:  # noqa: BLE001
        if required:
            raise
        log.warning("backup sidecar failed prefix=%s err=%s", prefix, type(exc).__name__)
        return None
    kept = _prune(out_dir, prefix, retain)
    log.info(
        "backup ok dest=%s size=%s retained=%s",
        dest,
        dest.stat().st_size,
        min(retain, kept),
    )
    return dest


def backup_db(
    db_path: str | Path = DB_PATH,
    backup_dir: str | Path = BACKUP_DIR,
    retain: int = BACKUP_RETAIN,
) -> Path:
    src = Path(db_path)
    out_dir = Path(backup_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    retain = max(1, int(retain))
    stamp = _utc_stamp()
    dest = _backup_one(src, out_dir, "dexscreener", stamp, retain, required=True)
    assert dest is not None
    for prefix, path in _sidecar_targets():
        _backup_one(path, out_dir, prefix, stamp, retain, required=False)
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
