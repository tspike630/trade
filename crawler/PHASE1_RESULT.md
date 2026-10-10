# Phase 1 Live Run Result

**Time (HK / UTC+8):** 2026-10-10 ~00:47 HKT  
**Command:** `.venv/bin/python main.py`  
**DB:** `/workspace/dexscreener-crawler/dexscreener.db`

## First cycle (empty DB)

```
List endpoints:
  token-profiles/latest:         30
  token-profiles/recent-updates: 30
  community-takeovers/latest:    8
Unique tokens (deduped):         68
Token upserts:
  inserted: 68
  updated:  0
  skipped:  0
Pair enrichment:
  batches:       5
  pairs fetched: 43
  inserted:      43
  updated:       0
  skipped:       0
DB totals:
  tokens: 68
  pairs:  43
Cycle completed successfully.
```

## Second cycle (upsert verification)

Same list sizes; all 68 tokens and 43 pairs reported as **updated** (0 inserted). Totals unchanged: tokens=68, pairs=43. Confirms `(chain_id, token_address)` / `(chain_id, pair_address)` primary-key upserts work.

## Errors

None on either run.

## Notes

- Enrichment used `GET /tokens/v1/{chainId}/{addresses}` only (batch size ≤ 30); legacy fallback not needed.
- Some profile tokens return 0 pairs from `/tokens/v1` (empty list) — expected; pair count can be lower than unique token count.
- Not implemented (by design): retry/backoff beyond fail-and-report, checkpoint, async, alerts, proxies.
