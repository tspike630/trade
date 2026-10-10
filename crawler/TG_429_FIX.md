# Telegram 429 storm fix (2026-10-10 HKT)

## Diagnosis (logs/crawler.log, last ~24h)

| Metric | Value |
|---|---|
| Log lines (24h) | ~27,958 |
| `ALERT` lines | 1,525 |
| Alert types | `new_listing` 902 · `price_change` 440 · `volume_h24_jump` 106 · `liquidity_drop` 77 |
| Telegram `status=429` | **1,402** (of which **~79 in last 5 min** at audit time) |
| Telegram ERROR lines | ~2,770 |
| `retry_after` in body | 1,383–1,392 mentions |
| `retry_after` range | **44,358 – 48,693 s** (~12–13.5 h flood wait) |
| 429 inter-arrival | min 0s · **median ~1s** · mean ~3.1s · p90 ~6s |
| Cooldown suppressed | **291** (`ALERT_COOLDOWN_SEC=300` working) |

### Conclusion

**Not an in-process retry death-loop.** Old `send_message` had **zero retries** and **ignored** `parameters.retry_after`.

**Real volume + ignore-ban storm:** unique `new_listing` tokens bypass cooldown (per-token key), so ~1 send/s continued while Telegram already returned a **multi-hour** flood-wait → every send logged ERROR 429 and deepened the ban.

Primary fix is **honor retry_after + per-chat rate limit + drop after capped retries**, not raising `ALERT_MIN_SCORE`.

## Changes

### `alerts_telegram.py`
- `parse_retry_after()` — JSON `parameters.retry_after`, description text, `Retry-After` header
- On **429**:
  - **Long** `retry_after` (> `TG_429_SLEEP_CAP_SEC`, default 30): set per-chat `ban_until`, **drop immediately** (no hour-long sleep, no multi-retry storm)
  - **Short** `retry_after`: sleep `min(ra, cap)`, max **`TG_MAX_RETRIES` (3)** then drop + WARN
- Per-chat gates: ~**1 msg/s** (`TG_RATE_MIN_INTERVAL_SEC`) and ~**20/min** (`TG_RATE_PER_MIN`)
- Priority queue (`TG_QUEUE_MAX`): admin/system > new_listing/cto > social > price/vol/liq; drop/merge low when full
- First failure **WARN**; same alert-key repeated → **ERROR**
- While `ban_until` active: skip sends (no HTTP hammer)

### `alerts.py`
- Telegram delivery failure at emit path logged **WARN** (alerter owns escalation)
- Cooldown path unchanged (`store.has_recent_alert` + `ALERT_COOLDOWN_SEC`)

### `config.py`
- `ALERT_COOLDOWN_SEC` now env-overridable (default 300) — verified still suppresses duplicates
- New `TG_*` knobs (see `deploy/dexscreener-crawler.env.example`)

### Demo (no real Telegram)
```bash
python demo_tg_429.py   # TG_429_DEMO_OK
```

## Restart to pick up fix

Live crawl was **not** killed during this fix (pid may still be the pre-fix process and will keep 429-storming until restart).

```bash
cd /workspace/dexscreener-crawler
# Find stabilize pid (do not remove LIVE_TRADING_DISABLED)
pgrep -af 'main.py --helius-listen'
# Graceful stop, then start
kill <pid>          # SIGTERM
# wait until gone, then:
source .venv/bin/activate
export ENABLED_CHAINS="${ENABLED_CHAINS:-solana}"
./stabilize_run.sh
# or: nohup ./stabilize_run.sh >> logs/stabilize.out 2>&1 &
```

After restart, expect: at most one long-ban WARN per chat, then silence until `retry_after` elapses; thereafter ≤ ~1 msg/s and ≤ ~20/min per chat.

Telegram is **not** disabled. Do **not** rely on raising `ALERT_MIN_SCORE` as the fix.

## Ban-period log throttle (follow-up)

Edit: `alerts_telegram.py` only (rate limits / priority / cooldown unchanged).

| Event | Log |
|---|---|
| First drop while long ban | **WARN** — chat, ban_remaining / retry_after, drop policy, first alert-key |
| Further drops during ban | **WARN ≤1/min per chat** — summary `dropped=N`, last alert-key, remaining ban seconds |
| Ban clears | **INFO** — ban ended, resume ~1/s and ~20/min |
| Short-retry exhausted | **ERROR** only |

Per-chat fields: `last_ban_warn_ts`, `ban_dropped_count` (plus `ban_retry_after` / first key for context).

Demo: `python demo_tg_429.py` includes `test_ban_log_throttle`.

**Restart crawler** to pick up (live process not killed).
