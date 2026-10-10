# P1.1 Result — Telegram native alerting

**Time (HK / UTC+8):** 2026-10-10 ~01:32+ HKT

## What was added

| File | Role |
|---|---|
| `alerts_telegram.py` | `TelegramAlerter`, HTML format, chat routing |
| `alerts.py` | `emit_alert` delivers webhook **and/or** Telegram; cooldown unchanged |
| `config.py` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID_*`, fallback `TELEGRAM_CHAT_ID` |
| `demo_telegram.py` | Dry-run format/routing; `--send` if token present |
| `README.md` | P1.1 section |
| `P1_TELEGRAM_RESULT.md` | this file |

Detection (`detect_pair_anomalies`), types, and `ALERT_COOLDOWN_SEC` are **100% reused**.

## Routing

| Types | Chat |
|---|---|
| `new_listing`, `cto_new` | `TELEGRAM_CHAT_ID_MAIN` → else `TELEGRAM_CHAT_ID` |
| `price_change`, `volume_h24_jump`, `liquidity_drop` | `TELEGRAM_CHAT_ID_ALERT` → else fallback |
| `proxy_pool_low`, `system_error`, … | `TELEGRAM_CHAT_ID_ADMIN` → else fallback |

## How to enable

```bash
export TELEGRAM_BOT_TOKEN='<bot token from @BotFather>'
export TELEGRAM_CHAT_ID_MAIN='<chat id>'
export TELEGRAM_CHAT_ID_ALERT='<chat id>'
export TELEGRAM_CHAT_ID_ADMIN='<chat id>'
# OR single channel:
export TELEGRAM_CHAT_ID='<chat id>'

python demo_telegram.py          # dry-run
python demo_telegram.py --send   # live test message
python main.py --force-lists     # real alerts while crawling
```

Optional: keep `ALERT_WEBHOOK_URL` — both channels can fire; `delivered` if either succeeds.

## Verification

```bash
python demo_telegram.py
# expect: DRY_RUN_OK formatting+routing
# LIVE_SEND skipped if no token
```

If `TELEGRAM_BOT_TOKEN` is already in the environment, `--send` posts one admin/system test message.

## Not in this PR
Helius / X live integrations (deferred).
