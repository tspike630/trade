#!/usr/bin/env python3
"""
Worker process: claim enrich jobs and mark done (skeleton).

Default action adds token to pending_enrich / no-ops if already present —
full batch enrich still runs via main pipeline. This skeleton proves the
claim/complete path for multi-worker expansion.

  python -m worker.run_worker --once
  python -m worker.run_worker --loop
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH, WORKER_POLL_SEC  # noqa: E402
from logging_setup import get_logger, setup_logging  # noqa: E402
from store import Store, norm_address, norm_chain  # noqa: E402
from worker.queue import create_queue  # noqa: E402

log = get_logger("worker.run")


def process_job(job, *, db: str) -> dict:
    payload = job.payload or {}
    typ = payload.get("type") or "enrich"
    if typ != "enrich":
        return {"skipped": True, "reason": f"unknown_type:{typ}"}
    chain = norm_chain(payload.get("chain_id") or "")
    addr = norm_address(payload.get("token_address") or "")
    if not chain or not addr:
        raise ValueError("missing chain_id/token_address")
    with Store(db) as store:
        store.add_pending_enrich(chain, addr)
    return {"ok": True, "chain_id": chain, "token_address": addr}


def run_once(*, db: str, worker_id: str) -> bool:
    q = create_queue()
    job = q.claim(worker_id)
    if not job:
        return False
    try:
        result = process_job(job, db=db)
        q.complete(job.id, result=result)
        log.info("job done id=%s result=%s", job.id, result)
    except Exception as exc:  # noqa: BLE001
        q.fail(job.id, error=str(exc))
        log.error("job fail id=%s err=%s", job.id, exc)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim and process enrich jobs")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--poll", type=float, default=WORKER_POLL_SEC)
    args = parser.parse_args(argv)
    setup_logging()
    wid = args.worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    print(f"worker_id={wid}")
    if args.loop:
        while True:
            if not run_once(db=args.db, worker_id=wid):
                time.sleep(max(0.2, args.poll))
        return 0
    ran = run_once(db=args.db, worker_id=wid)
    print("claimed" if ran else "idle", create_queue().stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
