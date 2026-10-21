#!/usr/bin/env python3
"""Heartbeat gap watchdog (collection layer).

Polls logs/heartbeat.json every --interval seconds (default 60). If the last
heartbeat (``timestamp`` field, falling back to file mtime) is older than
--stale seconds (default 600), APPENDS one open gap row to
exports/reports/supply_gaps.csv. When the heartbeat recovers, fills end_at /
end_hkt of that open row only, via .bak copy + atomic rewrite that keeps every
other row byte-identical and asserts the row count is unchanged.

Read-only on all DBs (never opens one). Idempotent across restarts: an existing
open row (component+reason+source match, end_at empty) is reused, never duplicated.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADER = "component,start_at,end_at,start_hkt,end_hkt,reason,source"
COMPONENT = "crawler(dex/helius/x)"
REASON = "heartbeat_gap"
SOURCE = "collection_watchdog"
HKT = timezone(timedelta(hours=8))
log = logging.getLogger("heartbeat_gap_watch")


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_hkt(dt: datetime) -> str:
    return dt.astimezone(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def last_heartbeat(path: Path) -> datetime | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    try:
        ts = json.loads(path.read_text()).get("timestamp")
        if ts:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001 - partial write etc.; fall back to mtime
        pass
    return datetime.fromtimestamp(st.st_mtime, timezone.utc)


def _split_lines(raw: bytes) -> list[bytes]:
    return raw.splitlines(keepends=True)


def _fields(line: bytes) -> list[str]:
    return line.decode("utf-8").rstrip("\r\n").split(",")


def _is_open_ours(line: bytes) -> bool:
    f = _fields(line)
    return (len(f) == 7 and f[0] == COMPONENT and f[2] == "" and f[5] == REASON
            and f[6] == SOURCE)


def find_open_gap(csv_path: Path) -> int | None:
    """Return line index of the (last) open watchdog row, or None."""
    if not csv_path.exists():
        return None
    lines = _split_lines(csv_path.read_bytes())
    for i in range(len(lines) - 1, 0, -1):
        if _is_open_ours(lines[i]):
            return i
    return None


def open_gap(csv_path: Path, start: datetime) -> bool:
    """Append an open gap row. Returns False if one is already open."""
    if find_open_gap(csv_path) is not None:
        return False
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    row = f"{COMPONENT},{fmt_utc(start)},,{fmt_hkt(start)},,{REASON},{SOURCE}\n"
    new = not csv_path.exists() or csv_path.stat().st_size == 0
    with open(csv_path, "ab") as fh:  # append only, never rewrite
        if new:
            fh.write((HEADER + "\n").encode())
        else:
            fh.seek(0, os.SEEK_END)
            with open(csv_path, "rb") as r:
                r.seek(-1, os.SEEK_END)
                if r.read(1) != b"\n":
                    fh.write(b"\n")
        fh.write(row.encode())
        fh.flush()
        os.fsync(fh.fileno())
    log.warning("gap opened start_at=%s", fmt_utc(start))
    return True


def close_gap(csv_path: Path, end: datetime) -> bool:
    idx = find_open_gap(csv_path)
    if idx is None:
        return False
    raw = csv_path.read_bytes()
    lines = _split_lines(raw)
    f = _fields(lines[idx])
    eol = lines[idx][len(lines[idx].rstrip(b"\r\n")):] or b"\n"
    f[2], f[4] = fmt_utc(end), fmt_hkt(end)
    new_lines = list(lines)
    new_lines[idx] = ",".join(f).encode() + eol
    assert len(new_lines) == len(lines)
    for i, (a, b) in enumerate(zip(lines, new_lines)):
        assert i == idx or a == b
    shutil.copy2(csv_path, csv_path.with_name(csv_path.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=csv_path.parent, prefix=".supply_gaps.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"".join(new_lines))
            fh.flush()
            os.fsync(fh.fileno())
        check = _split_lines(Path(tmp).read_bytes())
        assert len(check) == len(lines), "row count changed"
        os.replace(tmp, csv_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log.warning("gap closed end_at=%s", fmt_utc(end))
    return True


def check_once(hb_path: Path, csv_path: Path, stale_s: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    hb = last_heartbeat(hb_path)
    stale = hb is None or (now - hb).total_seconds() > stale_s
    if stale:
        if hb is None:
            return "no_heartbeat"
        return "opened" if open_gap(csv_path, hb) else "still_open"
    return "closed" if close_gap(csv_path, hb) else "ok"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--heartbeat", default=str(root / "logs" / "heartbeat.json"))
    p.add_argument("--csv", default=str(root / "exports" / "reports" / "supply_gaps.csv"))
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--stale", type=float, default=600)
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    while True:
        try:
            log.info("check result=%s", check_once(Path(a.heartbeat), Path(a.csv), a.stale))
        except Exception:  # noqa: BLE001
            log.exception("check failed")
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
