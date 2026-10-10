# P1.3 Result — X social sentiment source

**Time (HK / UTC+8):** 2026-10-10 ~02:05+ HKT

## What was added

| File | Role |
|---|---|
| `sources/x_api.py` | X API v2 recent search, token bucket, budget fuse, spike alerts |
| `sources/__init__.py` | Register `x` / `x_api` |
| `store.py` | `x_watch`, `x_budget` tables + helpers |
| `config.py` | `X_*` budget / spike / watch env |
| `alerts_telegram.py` | Route + format `social_spike` / `sentiment_anomaly` |
| `fixtures/x_search_recent.json` | Sample search response |
| `demo_x.py` | Dry-run parser / spike / budget / import |
| `README.md` | P1.3 section |
| `deploy/dexscreener-crawler.env.example` | X_* comments |
| `P1_X_RESULT.md` | this file |

Legacy `x_stub` kept; prefer `--sources x`.

## Modes

1. **Early mention watch** — tokens with `last_seen` within `X_EARLY_WATCH_SEC` enrolled as `mode=early`; search `$SYMBOL` / address.
2. **Hot velocity** — tokens in `HOT_SEEN_WINDOW_SEC` with `has_pairs=1` as `mode=hot`; compare `mention_count_prev` vs `mention_count_last` → `social_spike` or `sentiment_anomaly`.
3. **Dual signal** — if recent price/volume/liquidity alert on same token → `payload.dual_price_social` + `high_priority` + severity escalated.

Alerts go through existing `emit_alert` (cooldown + webhook + Telegram ALERT chat).

## Persistence (avoid re-billing)

- `x_watch.since_id` — passed as `since_id` on next search
- `mention_count_*` — velocity + totals
- `x_budget` day/hour buckets — hard fuse

## Rate limits

| Control | Env | Default |
|---|---|---|
| Soft token bucket | `X_RATE_CAPACITY` / `X_RATE_REFILL_PER_SEC` | 5 / 0.1 |
| Hourly hard fuse | `X_HOURLY_BUDGET_REQUESTS` | 20 |
| Daily hard fuse | `X_DAILY_BUDGET_REQUESTS` | 100 |

Bearer is **never** logged (Authorization redacted in error paths).

## How to enable

```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate

python demo_x.py                    # no token required

export X_BEARER_TOKEN='YOUR_APP_BEARER'   # do not commit / log
export X_DAILY_BUDGET_REQUESTS=100
export X_HOURLY_BUDGET_REQUESTS=20

python main.py --sources x --loop --interval 60
# optional combo:
# python main.py --sources x,helius --helius-listen --loop
```

## Verification

```bash
python demo_x.py
# expect: DRY_RUN_OK ; X_BEARER_TOKEN set: False ; notes=no_bearer on fetch

python -c "from sources import available_source_names; assert 'x' in available_source_names()"
python main.py --sources x --skip-lists 2>&1 | rg 'sources.x|Extra sources'
```

Live search only with token + `demo_x.py --live` (or `--sources x` in a cycle). This environment had **no** `X_BEARER_TOKEN` → no live calls.

## Not in this PR

- Full NLP sentiment scoring (velocity / count based)
- X filtered stream / academic full-archive
