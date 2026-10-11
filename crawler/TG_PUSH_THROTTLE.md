# Telegram push throttle (2026-10-11 HKT)

## Goal

Cut Telegram **delivery** volume to avoid multi-hour flood-wait bans.
**Detect + store alerts unchanged.** Webhook still follows `should_push_alert` / `ALERT_MIN_SCORE`.

Background: ~53k alerts / 24h; `new_listing` ~27k was the main push source. Existing 429 / per-chat rate / ban-drop was not enough — must filter at the source.

## New env (defaults conservative)

| Env | Default | Effect |
|---|---|---|
| `TELEGRAM_PUSH_EXCLUDE_TYPES` | `new_listing` | Comma-separated types **not** sent to Telegram (still stored) |
| `TELEGRAM_PUSH_MIN_SEVERITY` | `high` | Only `high` / `critical` (and admin/system) are pushed |
| `TELEGRAM_PUSH_MIN_SCORE` | `35` | Telegram score floor; effective min = `max(ALERT_MIN_SCORE, TELEGRAM_PUSH_MIN_SCORE)` |
| `TELEGRAM_PUSH_COOLDOWN_SEC` | `600` | In-memory push-only cooldown; same key fields as store `(type, chain, token, pair)` — does **not** change `has_recent_alert` semantics |
| `ALERT_COOLDOWN_SEC` | **`600`** (was 300) | Store dedupe window default raised; override via env if needed |

`ALERT_MIN_SCORE` stays **25** (store + shared push gate for webhook).

Admin/system types always push: `system_error`, `pipeline_error`, `heartbeat_fail`, `proxy_pool_low`.

## Code

- `config.py` — env knobs
- `alerts.py` — `telegram_push_allowed()` before `send_alert`; logs `telegram push skip (...)`
- `alerts_telegram.py` — `telegram_policy_allows()` defense-in-depth; logs `telegram drop policy (...)`

## Expected volume (qualitative)

- `new_listing` (severity=`info`) → **no Telegram** (exclude + severity)
- `volume_h24_jump` / medium `price_change` → **no Telegram** (severity)
- `liquidity_drop` / high `price_change` / escalated social → still eligible if score ≥ 35
- Net: pushes drop from “thousands/day of listings” to a thin high-severity stream

## Ops

- Do **not** clear ban state — wait for natural unban
- Do **not** enable live trading / touch business DB schema
- Smooth restart: SIGTERM main → `./stabilize_run.sh`
