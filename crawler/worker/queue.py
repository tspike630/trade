"""
Queue interface for enrich jobs.

Backends:
  - sqlite (default, single-node / multi-process on one host)
  - redis (optional; requires redis package + REDIS_URL)

Job payload example:
  {"type": "enrich", "chain_id": "solana", "token_address": "..."}
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import REDIS_URL, WORKER_CLAIM_TTL_SEC, WORKER_DB_PATH
from logging_setup import get_logger

log = get_logger("worker.queue")


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    id: str
    payload: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    created_at: str = ""
    claimed_by: str | None = None
    claimed_at: str | None = None


class QueueBackend(ABC):
    @abstractmethod
    def enqueue(self, payload: dict[str, Any], *, job_id: str | None = None) -> str:
        ...

    @abstractmethod
    def claim(self, worker_id: str, *, ttl_sec: int = WORKER_CLAIM_TTL_SEC) -> Job | None:
        ...

    @abstractmethod
    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> None:
        ...

    @abstractmethod
    def fail(self, job_id: str, *, error: str) -> None:
        ...

    @abstractmethod
    def stats(self) -> dict[str, int]:
        ...


class SqliteQueue(QueueBackend):
    def __init__(self, path: str | Path = WORKER_DB_PATH):
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                claimed_by TEXT,
                claimed_at TEXT,
                result_json TEXT,
                error TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at)"
        )
        self._conn.commit()

    def enqueue(self, payload: dict[str, Any], *, job_id: str | None = None) -> str:
        jid = job_id or str(uuid.uuid4())
        self._conn.execute(
            """
            INSERT INTO jobs (id, payload_json, status, attempts, created_at)
            VALUES (?, ?, 'pending', 0, ?)
            """,
            (jid, json.dumps(payload, ensure_ascii=False), _utc()),
        )
        self._conn.commit()
        return jid

    def claim(self, worker_id: str, *, ttl_sec: int = WORKER_CLAIM_TTL_SEC) -> Job | None:
        # reclaim expired claimed jobs
        cutoff = time.time() - ttl_sec
        rows = self._conn.execute(
            "SELECT id, claimed_at FROM jobs WHERE status = 'claimed'"
        ).fetchall()
        for r in rows:
            try:
                ts = datetime.strptime(r["claimed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except Exception:  # noqa: BLE001
                ts = 0
            if ts < cutoff:
                self._conn.execute(
                    "UPDATE jobs SET status='pending', claimed_by=NULL, claimed_at=NULL WHERE id=?",
                    (r["id"],),
                )
        self._conn.commit()

        row = self._conn.execute(
            """
            SELECT * FROM jobs WHERE status = 'pending'
            ORDER BY created_at ASC LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        now = _utc()
        self._conn.execute(
            """
            UPDATE jobs SET status='claimed', claimed_by=?, claimed_at=?, attempts=attempts+1
            WHERE id=? AND status='pending'
            """,
            (worker_id, now, row["id"]),
        )
        self._conn.commit()
        # re-read
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        if not row or row["status"] != "claimed" or row["claimed_by"] != worker_id:
            return None
        return Job(
            id=row["id"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            claimed_by=row["claimed_by"],
            claimed_at=row["claimed_at"],
        )

    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            """
            UPDATE jobs SET status='done', result_json=?, error=NULL
            WHERE id=?
            """,
            (json.dumps(result or {}, ensure_ascii=False), job_id),
        )
        self._conn.commit()

    def fail(self, job_id: str, *, error: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status='failed', error=? WHERE id=?",
            (error[:500], job_id),
        )
        self._conn.commit()

    def stats(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for st in ("pending", "claimed", "done", "failed"):
            out[st] = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status=?", (st,)
                ).fetchone()[0]
            )
        return out


class RedisQueue(QueueBackend):
    """Optional Redis list + hash; requires `redis` package."""

    def __init__(self, url: str | None = None):
        try:
            import redis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "redis package not installed — pip install redis, or use WORKER_BACKEND=sqlite"
            ) from exc
        self.url = url or REDIS_URL
        if not self.url:
            raise RuntimeError("REDIS_URL required for redis backend")
        self.r = redis.Redis.from_url(self.url, decode_responses=True)
        self.key = "dex:enrich:queue"
        self.hash = "dex:enrich:jobs"

    def enqueue(self, payload: dict[str, Any], *, job_id: str | None = None) -> str:
        jid = job_id or str(uuid.uuid4())
        body = {
            "id": jid,
            "payload": payload,
            "status": "pending",
            "attempts": 0,
            "created_at": _utc(),
        }
        self.r.hset(self.hash, jid, json.dumps(body))
        self.r.lpush(self.key, jid)
        return jid

    def claim(self, worker_id: str, *, ttl_sec: int = WORKER_CLAIM_TTL_SEC) -> Job | None:
        jid = self.r.rpop(self.key)
        if not jid:
            return None
        raw = self.r.hget(self.hash, jid)
        if not raw:
            return None
        body = json.loads(raw)
        body["status"] = "claimed"
        body["claimed_by"] = worker_id
        body["claimed_at"] = _utc()
        body["attempts"] = int(body.get("attempts") or 0) + 1
        self.r.hset(self.hash, jid, json.dumps(body))
        return Job(
            id=jid,
            payload=body.get("payload") or {},
            status="claimed",
            attempts=body["attempts"],
            created_at=body.get("created_at") or "",
            claimed_by=worker_id,
            claimed_at=body["claimed_at"],
        )

    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> None:
        raw = self.r.hget(self.hash, job_id)
        if not raw:
            return
        body = json.loads(raw)
        body["status"] = "done"
        body["result"] = result or {}
        self.r.hset(self.hash, job_id, json.dumps(body))

    def fail(self, job_id: str, *, error: str) -> None:
        raw = self.r.hget(self.hash, job_id)
        if not raw:
            return
        body = json.loads(raw)
        body["status"] = "failed"
        body["error"] = error[:500]
        self.r.hset(self.hash, job_id, json.dumps(body))

    def stats(self) -> dict[str, int]:
        # best-effort scan
        counts = {"pending": int(self.r.llen(self.key)), "claimed": 0, "done": 0, "failed": 0}
        for _, raw in self.r.hscan_iter(self.hash, count=100):
            try:
                st = json.loads(raw).get("status")
                if st in counts:
                    counts[st] = counts.get(st, 0) + (1 if st != "pending" else 0)
            except Exception:  # noqa: BLE001
                continue
        return counts


def create_queue(backend: str | None = None) -> QueueBackend:
    from config import WORKER_BACKEND

    b = (backend or WORKER_BACKEND or "sqlite").lower()
    if b == "redis":
        return RedisQueue()
    return SqliteQueue()
