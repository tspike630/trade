# Phase 3 Result — 分级轮询 + 异步 IO + 数据校验

**Time (HK / UTC+8):** 2026-10-10 ~01:09–01:10 HKT

## Design

### Tiered polling
- List endpoints gated by `PROFILE_POLL_INTERVAL_SEC` (60) via `schema_meta.last_profile_poll_at`
- Enrich tiers using `last_seen_at` / `last_enriched_at` + `cycle_number`:
  - **new** — never enriched (`has_pairs IS NULL`)
  - **hot** — `has_pairs=1` and seen recently / this cycle; min interval `HOT_ENRICH_MIN_INTERVAL_SEC`
  - **warm** — `has_pairs=1` otherwise; every `WARM_ENRICH_EVERY_CYCLES` + min interval
  - **cold** — `has_pairs=0` rare re-check (`RECHECK_ZERO_PAIR_SECONDS` **or** every `RECHECK_ZERO_PAIR_EVERY_CYCLES`)
- Replaces permanent hard-skip of zero-pair with configurable rare re-check (Phase 1 skip when not due still counts as `enrich_skipped_no_pairs`)

### Async IO
- `AsyncDexScreenerClient` (`httpx.AsyncClient`) with async retry/backoff
- Enrich: plan batches → `asyncio.gather` under `asyncio.Semaphore(ASYNC_MAX_CONCURRENCY)`
- SQLite upserts after HTTP completes (ordered), avoiding concurrent DB writers
- CLI: `asyncio.run(_run_once_async(...))`

### Validation
- `validate.py`: token profiles require non-empty `chainId`+`tokenAddress`; pairs require `chainId`+`pairAddress`+`baseToken.address`
- Coerce `priceUsd` / `volume.*` to float when possible
- Invalid items: WARNING log, skip, increment `validation_*` stats

## Files changed / added

| File | Notes |
|---|---|
| `config.py` | Tier + concurrency knobs |
| `client.py` | Async client |
| `validate.py` | **new** |
| `store.py` | `last_seen_at`, `last_enriched_at`, cycle/poll meta |
| `pipeline.py` | Tiers, async enrich, validation, scheduler hooks |
| `main.py` | `--max-cycles`, `--force-lists`, `--skip-lists` |
| `demo_retry.py` | Async flaky transport |
| `demo_validate.py` | **new** |
| `README.md` / `PHASE3_RESULT.md` | Docs |

## Config knobs

`PROFILE_POLL_INTERVAL_SEC`, `HOT_SEEN_WINDOW_SEC`, `HOT_ENRICH_MIN_INTERVAL_SEC`, `WARM_ENRICH_EVERY_CYCLES`, `WARM_ENRICH_MIN_INTERVAL_SEC`, `RECHECK_ZERO_PAIR_SECONDS`, `RECHECK_ZERO_PAIR_EVERY_CYCLES`, `ASYNC_MAX_CONCURRENCY`, plus Phase 2 retry settings.

## Verification

### Validation unit
`demo_validate.py` → `VALIDATE_DEMO_OK` (rejects bad token/pair; coerces priceUsd/volume).

### Retry (async)
`demo_retry.py` → 2×503 then 200, `calls=3`.

### Live cycle (`--force-new --force-lists --flush-pending`)
- lists OK, validation_errors=0
- tiers: **hot=48, new=3** (seen this cycle); cold skipped 25 (not due)
- **async peak_in_flight=3** (semaphore bound)
- batches=5, pending=0, tokens=76, pairs=51

### Tier simulation (`--skip-lists --flush-pending`)
Aged `last_seen_at`/`last_enriched_at`, set `cycle_number=4` → bump to 5:
- **warm=51, cold=25**, hot=0, new=0
- skip list fetch (scheduler)
- peak_in_flight=3

### Loop (`--loop --max-cycles 3 --interval 2`)
- All 3 ticks: `will_fetch_lists=False` (within 60s of last profile poll)
- enrich not due → skipped_no_pairs=25, skipped_not_due=51
- Confirms scheduler separates list poll from enrich ticks

## Phase 1–2 intact
Lowercase addresses, pending batch fill, checkpoint/resume, retry logging, `--flush-pending` / SIGINT behavior retained.
