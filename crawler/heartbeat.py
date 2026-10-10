"""Heartbeat file + health check for monitoring."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import HEARTBEAT_PATH, HEARTBEAT_STALE_SEC, LOG_DIR
from logging_setup import get_logger

log = get_logger("heartbeat")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def write_heartbeat(
    *,
    cycle_id: str,
    status: str,
    stats: dict[str, Any] | None = None,
    last_error: str | None = None,
    counters: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> Path:
    path = Path(path or HEARTBEAT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _utc_now_iso(),
        "cycle_id": cycle_id,
        "status": status,
        "last_error": last_error,
        "pid": os.getpid(),
        "stats": {
            k: stats.get(k)
            for k in (
                "cycle_number",
                "tokens_total",
                "pairs_total",
                "enrich_batches",
                "validation_errors",
                "alerts_emitted",
                "enrich_pending_remaining",
                "interrupted",
            )
            if stats
        },
        "counters": counters or {},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info(
        "heartbeat written path=%s status=%s cycle_id=%s",
        path,
        status,
        cycle_id,
    )
    return path


def read_heartbeat(path: str | Path | None = None) -> dict[str, Any] | None:
    path = Path(path or HEARTBEAT_PATH)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def check_health(
    *,
    stale_sec: int | None = None,
    path: str | Path | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Return (ok, message, heartbeat_dict).
    FAIL if missing, unreadable, or timestamp older than stale_sec.
    """
    stale = HEARTBEAT_STALE_SEC if stale_sec is None else stale_sec
    hb = read_heartbeat(path)
    if hb is None:
        return False, "FAIL: heartbeat missing or unreadable", None
    ts = _parse_iso(hb.get("timestamp"))
    if ts is None:
        return False, "FAIL: heartbeat timestamp invalid", hb
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    status = hb.get("status") or "unknown"
    if age > stale:
        return (
            False,
            f"FAIL: heartbeat stale age={age:.0f}s > {stale}s status={status}",
            hb,
        )
    if status in ("error", "failed"):
        return (
            False,
            f"FAIL: heartbeat status={status} age={age:.0f}s last_error={hb.get('last_error')}",
            hb,
        )
    if status == "degraded":
        return (
            True,
            f"OK (degraded): age={age:.0f}s cycle_id={hb.get('cycle_id')} "
            f"last_error={hb.get('last_error')}",
            hb,
        )
    return True, f"OK: age={age:.0f}s status={status} cycle_id={hb.get('cycle_id')}", hb
