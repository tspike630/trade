# pairs.pair_created_at

## Goal

Promote DexScreener `pairCreatedAt` (ms epoch in `pairs.raw_json`) to a queryable column for review `token_age` attribution. No alert / live-trading / scoring cadence changes.

## Schema

- Column: `pairs.pair_created_at INTEGER` (nullable, ms epoch, same unit as DexScreener)
- Migration: `Store._migrate_pair_created_at()` via `ALTER TABLE` + `idx_pairs_pair_created_at` (same style as `last_history_at`)
- Missing / invalid `pairCreatedAt` → `NULL` (never invented)

## Upsert

`Store.upsert_pair` parses `raw["pairCreatedAt"]` with `_parse_pair_created_at` and writes `pair_created_at` on INSERT/UPDATE.

## Backfill

```bash
cd /workspace/dexscreener-crawler && source .venv/bin/activate
python -c 'from store import Store; s=Store("dexscreener.db"); print(s.backfill_pair_created_at()); s.close()'
```

## token_age (SQL example)

```sql
-- hours since pair creation vs now (UTC ms)
SELECT chain_id, pair_address, pair_created_at,
       (strftime('%s','now') * 1000 - pair_created_at) / 3600000.0 AS token_age_hours
FROM pairs
WHERE pair_created_at IS NOT NULL
ORDER BY pair_created_at DESC
LIMIT 20;
```

## Files touched

- `store.py` — migrate, upsert, `backfill_pair_created_at`
- `PAIR_CREATED_AT.md` — this note

No secrets / DB committed. `LIVE_TRADING_DISABLED` unchanged. Main crawler needs a graceful restart so the long-running process loads the new upsert path.
