# Multi-paper peer launcher notes · 2026-10-30 HKT

## What exists today

| Runner | Accepts purpose | Ledger / DB | Can peer-isolate? |
|---|---|---|---|
| `python -m execution.run --loop` | **default** only `live_candidate` | shared `execution_ledger.db` + `EXECUTION_PAPER_DB` | Yes **with** `--accept-purpose` + isolated `--ledger` + `paper_eq_*.db` |
| `python -m execution.run --loop --accept-purpose race_* …` | allowlist for **this process only** | required isolated `execution_ledger_race_*.db` + `paper_eq_*.db` | Yes — race/shadow peers |
| `python -m execution.c_exit_shadow_run --loop` | `--accept-purpose` / `C_EXIT_ACCEPT_PURPOSE` (default `live_candidate`) | own cursor inside `paper_trading_c_exit.db` | Yes for C-exit grid only |

## Peer CLI / env (execution.run)

| Flag / env | Meaning |
|---|---|
| `--accept-purpose` / `EXECUTION_ACCEPT_PURPOSE` | Peer allowlist (repeatable / comma-separated). Unset → main `live_candidate`-only gate. |
| `--ledger` / `--ledger-db` / `EXECUTION_LEDGER_PATH` / `EXECUTION_LEDGER_DB` | Execution cursor + `execution_log`. Peer mode **must not** use shared `execution_ledger.db`. |
| `--paper-db` / `EXECUTION_PAPER_DB` | PaperLedger path. Peer mode **must not** target `paper_trading.db`. |
| `EXECUTION_PAPER_SIZE_MULT` | Unchanged; inherited from env (do not edit `.env` trading params here). |

Safety: when `--accept-purpose` is set, the process refuses to start if ledger resolves to the main `execution_ledger.db` or paper DB is `paper_trading.db`. Other purposes (including `live_candidate` unless listed) are rejected for that process.

## Start the 5 race peers

```bash
# After paper_eq_*.db exist (scripts/init_multi_paper_peers.sh):
./scripts/start_multi_paper_race_peers.sh
```

Or one-by-one:

```bash
python -m execution.run --loop --paper-enabled --paper-tick \
  --paper-db paper_eq_rsi_bounce.db \
  --ledger execution_ledger_race_rsi_bounce.db \
  --accept-purpose race_rsi_bounce
# … selfbuilt_a / macd_cross / boll_breakout / tech_mom similarly
```

## Main baseline (unchanged)

```bash
# Peer-equality baseline — live_candidate only → paper_trading.db
python -m execution.run --loop
```

## C-exit sidebook

```bash
python -m execution.c_exit_shadow_run --status
# only if Yang unhalts:
# python -m execution.c_exit_shadow_run --loop
```

## entry_ih peer (when decision stream exists)

```bash
python -m execution.run --loop --paper-enabled --paper-tick \
  --paper-db paper_eq_entry_ih1h.db \
  --ledger execution_ledger_shadow_entry_ih1h.db \
  --accept-purpose shadow_entry_ih_ge1h
# If signal DB is not dexscreener.db:
#   EXECUTION_ALLOW_NONSTANDARD_DECISION_DB=1 --db decision_entry_ih_ge1h.db …
```
