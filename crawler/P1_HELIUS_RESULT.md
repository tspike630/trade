# P1.2 Result — Helius on-chain new-pair listener

**Time (HK / UTC+8):** 2026-10-10 ~01:55+ HKT

## What was added

| File | Role |
|---|---|
| `sources/solana_helius.py` | `logsSubscribe` listener, parsers, enqueue → `pending_enrich`, reconnect/ping, admin alert |
| `sources/__init__.py` | Register `helius` / `solana_helius` |
| `main.py` | `--helius-listen` (+ keep-alive when not `--loop`) |
| `config.py` | `HELIUS_*` env (already present) |
| `fixtures/helius_logs_notification.json` | Sample `logsNotification` |
| `fixtures/helius_get_transaction.json` | Sample `getTransaction` for mint extract |
| `demo_helius.py` | Dry-run parser + import; `--live` only if key set |
| `README.md` | P1.2 section |
| `deploy/dexscreener-crawler.env.example` | HELIUS_* comments |
| `P1_HELIUS_RESULT.md` | this file |

Pipeline reuse: **pending_enrich → batch enrich → validate → upsert → new_listing** unchanged. Listener only inserts tokens + pending rows (+ optional `new_listing` via `emit_alert`).

## Default program IDs

| Program ID | Label | Documented source |
|---|---|---|
| `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | Raydium AMM v4 | docs.raydium.io |
| `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | Raydium CLMM | docs.raydium.io |
| `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | Raydium CPMM | docs.raydium.io |
| `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | Pump.fun bonding | solanatracker / pinax IDL |
| `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | PumpSwap AMM | solanatracker |
| `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | Meteora DLMM | docs.rs / pinax |
| `Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB` | Meteora pools | pinax |
| `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | Orca Whirlpool | common public |

Override: `HELIUS_PROGRAM_IDS=id1,id2` (or `HELIUS_MENTIONS`). Re-confirm IDs before production.

## How to enable

```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate

# Dry-run (no key required)
python demo_helius.py

# Live
export HELIUS_API_KEY='your-helius-key'
# optional Telegram admin alerts on persistent disconnect:
# export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID_ADMIN=...

python main.py --helius-listen --loop --interval 60
# or listen-only keep-alive after one cycle:
# python main.py --helius-listen --skip-lists
```

Env:

- `HELIUS_API_KEY` — required for live WS
- `HELIUS_WS_URL` / `HELIUS_HTTP_URL` — optional overrides
- `HELIUS_PROGRAM_IDS` / `HELIUS_MENTIONS` — comma-separated mentions
- `HELIUS_PING_INTERVAL_SEC` (default 120), `HELIUS_RECONNECT_MAX_SEC` (60), `HELIUS_DISCONNECT_ALERT_AFTER` (3)

Without API key: `HeliusSolanaSource.fetch()` is documented no-op; `--helius-listen` logs warning and skips thread.

## Behaviour

1. Subscribe `logsSubscribe` with `{mentions: [programId]}` per ID over WSS.
2. On `logsNotification`: extract `signature`; best-effort mint hints from logs; if empty → HTTP `getTransaction` (jsonParsed) → mint list.
3. `upsert_token(solana, mint)` + `add_pending_enrich`; `new_listing` alert on insert.
4. Dedup via existing `(chain_id, token_address)` PK upsert.
5. Reconnect with exponential backoff; websockets library ping; after N session failures emit `system_error` (Telegram ADMIN if enabled).

## Verification

```bash
python demo_helius.py
# expect: DRY_RUN_OK, mints_from_logs + mints_from_transaction printed
# LIVE_SKIP unless HELIUS_API_KEY + --live

python -c "from sources.solana_helius import helius_enabled, resolve_program_ids; print(helius_enabled(), len(resolve_program_ids()))"
python main.py --sources helius --skip-lists --max-cycles 1  # no-op marker when no key
```

Live WS was **not** opened in this environment (`HELIUS_API_KEY` unset).

## Not in this PR

- X / Twitter live source (still `x_stub` only)
- Full Raydium/Pump instruction ABI decoding (best-effort log + getTransaction mint extract)
