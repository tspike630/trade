#!/usr/bin/env python3
"""Heartbeat gap watchdog (collection layer).

Polls logs/heartbeat.json every --interval seconds (default 60). If the last
heartbeat (``timestamp`` field, falling back to file mtime) is older than
--stale seconds (default 600), APPENDS one open gap row to
exports/reports/supply_gaps.csv. When the heartbeat recovers, fills end_at /
end_hkt of that open row only, via .bak copy + atomic rewrite that keeps every
other row byte-identical and asserts the row count is unchanged.

Missing heartbeat file => gap from last-seen heartbeat (persisted in
logs/heartbeat_gap_watch.state.json); if never seen, from watchdog start with
reason=heartbeat_missing. All CSV writes hold flock(LOCK_EX) on
supply_gaps.csv.lock. Each check touches logs/heartbeat_gap_watch.alive
(dashboard: yellow if >5 min stale).

Read-only on all DBs (never opens one). Idempotent across restarts: an existing
open row (component+reason+source match, end_at empty) is reused, never duplicated.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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
REASON_MISSING = "heartbeat_missing"
REASONS = (REASON, REASON_MISSING)
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
        log.warning("heartbeat file missing path=%s", path)
        return None
    try:
        ts = json.loads(path.read_text()).get("timestamp")
        if ts:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
        log.warning("heartbeat has no timestamp field; falling back to mtime path=%s", path)
    except Exception as exc:  # noqa: BLE001 - partial write etc.
        log.warning("heartbeat parse failed (%s); falling back to mtime path=%s", exc, path)
    return datetime.fromtimestamp(st.st_mtime, timezone.utc)


def lock_path(csv_path: Path) -> Path:
    return csv_path.with_name(csv_path.name + ".lock")


@contextlib.contextmanager
def csv_lock(csv_path: Path):
    """Exclusive flock on <csv>.lock; every writer of supply_gaps.csv must hold it."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path(csv_path), "a") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def append_row_locked(csv_path: Path, fields: list[str]) -> None:
    """Append one CSV row (caller must hold csv_lock). Writes header if new."""
    if len(fields) != 7 or any("," in f or "\n" in f for f in fields):
        raise ValueError("need 7 fields without commas/newlines")
    new = not csv_path.exists() or csv_path.stat().st_size == 0
    with open(csv_path, "ab") as fh:
        if new:
            fh.write((HEADER + "\n").encode())
        else:
            with open(csv_path, "rb") as r:
                r.seek(-1, os.SEEK_END)
                if r.read(1) != b"\n":
                    fh.write(b"\n")
        fh.write((",".join(fields) + "\n").encode())
        fh.flush()
        os.fsync(fh.fileno())


def _split_lines(raw: bytes) -> list[bytes]:
    return raw.splitlines(keepends=True)


def _fields(line: bytes) -> list[str]:
    return line.decode("utf-8").rstrip("\r\n").split(",")


def _is_open_ours(line: bytes) -> bool:
    f = _fields(line)
    return (len(f) == 7 and f[0] == COMPONENT and f[2] == "" and f[5] in REASONS
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


def open_gap(csv_path: Path, start: datetime, reason: str = REASON) -> bool:
    """Append an open gap row under the lock. False if one is already open."""
    with csv_lock(csv_path):
        if find_open_gap(csv_path) is not None:
            return False
        append_row_locked(csv_path, [COMPONENT, fmt_utc(start), "", fmt_hkt(start), "", reason, SOURCE])
    log.warning("gap opened start_at=%s reason=%s", fmt_utc(start), reason)
    return True


def _sig(p: Path) -> tuple[int, int]:
    st = p.stat()
    return st.st_size, st.st_mtime_ns


def close_gap(csv_path: Path, end: datetime, retries: int = 5) -> bool:
    with csv_lock(csv_path):
        for _ in range(retries):
            sig = _sig(csv_path) if csv_path.exists() else None
            idx = find_open_gap(csv_path)
            if idx is None:
                return False
            lines = _split_lines(csv_path.read_bytes())
            f = _fields(lines[idx])
            eol = lines[idx][len(lines[idx].rstrip(b"\r\n")):] or b"\n"
            f[2], f[4] = fmt_utc(end), fmt_hkt(end)
            new_lines = list(lines)
            new_lines[idx] = ",".join(f).encode() + eol
            assert len(new_lines) == len(lines)
            for i, (x, y) in enumerate(zip(lines, new_lines)):
                assert i == idx or x == y
            shutil.copy2(csv_path, csv_path.with_name(csv_path.name + ".bak"))
            fd, tmp = tempfile.mkstemp(dir=csv_path.parent, prefix=".supply_gaps.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(b"".join(new_lines))
                    fh.flush()
                    os.fsync(fh.fileno())
                assert len(_split_lines(Path(tmp).read_bytes())) == len(lines), "row count changed"
                if _sig(csv_path) != sig:  # a non-locking writer touched it; redo
                    log.warning("supply_gaps.csv changed during close; retrying")
                    os.unlink(tmp)
                    continue
                os.replace(tmp, csv_path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
            log.warning("gap closed end_at=%s", fmt_utc(end))
            return True
    raise RuntimeError("close_gap: file kept changing, gave up")


def load_state(p: Path | None) -> dict:
    if p is None or not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        log.warning("state file unreadable path=%s", p)
        return {}


def save_state(p: Path | None, state: dict) -> None:
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


def touch_alive(p: Path | None) -> None:
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def check_once(
    hb_path: Path,
    csv_path: Path,
    stale_s: float,
    now: datetime | None = None,
    state_path: Path | None = None,
    alive_path: Path | None = None,
    started_at: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    touch_alive(alive_path)
    state = load_state(state_path)
    hb = last_heartbeat(hb_path)
    if hb is None:
        seen = state.get("last_seen")
        if seen:
            start, reason = datetime.fromisoformat(seen.replace("Z", "+00:00")), REASON
        else:
            start, reason = (started_at or now), REASON_MISSING
        return "opened" if open_gap(csv_path, start, reason) else "still_open"
    if state.get("last_seen") != fmt_utc(hb):
        state["last_seen"] = fmt_utc(hb)
        save_state(state_path, state)
    if (now - hb).total_seconds() > stale_s:
        return "opened" if open_gap(csv_path, hb) else "still_open"
    return "closed" if close_gap(csv_path, hb) else "ok"


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--heartbeat", default=str(root / "logs" / "heartbeat.json"))
    p.add_argument("--csv", default=str(root / "exports" / "reports" / "supply_gaps.csv"))
    p.add_argument("--state", default=str(root / "logs" / "heartbeat_gap_watch.state.json"))
    p.add_argument("--alive", default=str(root / "logs" / "heartbeat_gap_watch.alive"))
    p.add_argument("--interval", type=float, default=60)
    p.add_argument("--stale", type=float, default=600)
    p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    while True:
        try:
            log.info("check result=%s", check_once(
                Path(a.heartbeat), Path(a.csv), a.stale, state_path=Path(a.state),
                alive_path=Path(a.alive), started_at=started))
        except Exception:  # noqa: BLE001
            log.exception("check failed")
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
