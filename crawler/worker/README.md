# Worker skeleton (P3)

**Single-node default:** `WORKER_BACKEND=sqlite` → `worker_queue.db` on the same host.
Scheduler enqueues from `pending_enrich`; worker claims jobs. The main `python main.py --loop` pipeline remains the primary enrich path; workers are additive for horizontal experiments.

**Multi-worker:** run multiple `python -m worker.run_worker --loop` processes against the same SQLite file (coarse locking) or set:

```bash
pip install redis
export WORKER_BACKEND=redis
export REDIS_URL=redis://127.0.0.1:6379/0
```

```bash
python -m worker.scheduler --once
python -m worker.run_worker --once
python -m worker.run_worker --loop --worker-id w1
```
