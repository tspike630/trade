# Phase 2 Result — 异常重试 + 日志 + 断点续跑

**Time (HK / UTC+8):** 2026-10-10 ~01:03 HKT

## Added / changed files

| File | Change |
|---|---|
| `config.py` | `MAX_RETRIES`, backoff / jitter / retry status codes, log paths |
| `client.py` | Exponential backoff + jitter; `Retry-After` / RateLimit-Reset; structured retry logs |
| `logging_setup.py` | **new** — console + `logs/crawler.log` RotatingFileHandler |
| `store.py` | Table `run_checkpoint` + create/update/get incomplete |
| `pipeline.py` | Checkpoint steps; resume skip lists; interrupt flag; logging |
| `main.py` | `--loop`, `--interval`, `--force-new`; SIGINT/SIGTERM graceful stop |
| `demo_retry.py` | **new** — flaky 503 transport demo |
| `README.md` | Phase 2 中文说明 |
| `.gitignore` | `logs/` |

Phase 1 优化保持不变：`has_pairs` 跳过、小写地址、`pending_enrich` 凑 30、`--flush-pending`。

## How resume works

1. 周期开始写入 `run_checkpoint`：`status=running`, `step=started`
2. 列表拉取 + token upsert 后 → `step=lists_done`（pending 已持久化）
3. Enrich 中 → `step=enriching`，每成功/失败批更新 `last_batch_meta` + `stats_json`
4. 结束 → `status=completed`, `step=done`
5. SIGINT/SIGTERM → `status=interrupted`，pending 保留
6. **下次启动**：若存在 `running|interrupted` 且 `step in (lists_done, enriching)` → **跳过列表 API**，只从 `pending_enrich` 继续；`--force-new` 可强制开新周期

## Verification

### 1) Retry demo (`demo_retry.py`)

Flaky transport: 2× HTTP 503 → 第 3 次 200。

```
WARNING retry ... status=503 attempt=1/5 delay=1.22s
WARNING retry ... status=503 attempt=2/5 delay=2.18s
INFO    ok ... status=200
RETRY_DEMO_OK calls=3 pairs=1 elapsed=3.40s
```

### 2) Live cycle regression (`python main.py --force-new`)

- Lists OK; unique 68; inserted 5 / updated 63
- **skipped has_pairs=0: 25**, skipped already: 38
- New unenriched queued as pending=5 (不足 30，未发批)
- Lowercase intact (`non_lower=0`)
- Checkpoint `…8576d71a` → `completed` / `done`
- Log file: `logs/crawler.log`

### 3) Checkpoint resume

Injected `resume-demo-20261009` as `interrupted` / `lists_done` with pending=5.

```
resuming cycle_id=resume-demo-20261009 from step=lists_done ...
skip list fetch (resume)
... tokens/v1 ethereum batch_size=1
... tokens/v1 solana batch_size=4
cycle done ... pending=0 batches=2  resumed=True
```

Final DB: tokens=73, pairs=48, has_pairs 0=25 / 1=48, pending=0.

## CLI cheat sheet

```bash
python main.py                  # 单轮；自动 resume 未完成周期
python main.py --flush-pending  # 冲刷 <30 余量
python main.py --force-new      # 忽略旧 checkpoint
python main.py --loop --interval 60
```

## Not in scope (Phase 3+)

Tiered polling schedules, async rewrite, alerts, proxies.
