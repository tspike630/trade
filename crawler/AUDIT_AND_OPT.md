# AUDIT_AND_OPT — Roadmap coverage + gap fixes (2026-10-10 HKT)

Live stabilize **not stopped**: `main.py --helius-listen --sources x --loop` (pid observed during audit).  
`LIVE_TRADING_DISABLED` kill-switch **kept**. Live gates remain closed.

## Checklist (DONE / PARTIAL / MISSING)

### Phase 1–5
| Item | Status | Notes |
|---|---|---|
| API crawl | DONE | DexScreener public API |
| Batch | DONE | tokens/v1 batch |
| Dedupe | DONE | SQLite upserts |
| Empty-pair skip | DONE | tokens_marked_no_pairs |
| Lowercase | DONE | norm_address / norm_chain |
| Batch fill 30 | DONE | client batching |
| Retry / logs / checkpoint | DONE | client + cycle checkpoint |
| Tiered / async / validate | DONE | hot/warm/cold + validate |
| Alerts + heartbeat | DONE | alerts + heartbeat while --loop |
| Proxy pool | DONE | optional USE_PROXY + health eviction |
| Own API | DONE | api_server + X-API-Key |
| Multi-source | DONE | sources plugin (dex / helius / x) |

### P0
| Item | Status | Notes |
|---|---|---|
| API key auth | DONE | API_SECRET / ALLOW_INSECURE_API |
| backup.py + retain 7 | DONE | `backup.py --retain 7`; ran once in audit |
| systemd units | DONE | deploy/*.service + *.timer (not installed on box) |
| Proxy health eviction | DONE | demo_proxy_health.py |

### P1
| Item | Status | Notes |
|---|---|---|
| Telegram alerter | DONE | MAIN/ALERT/ADMIN + fallback; stabilize OK with ADMIN-only |
| Helius listen | DONE | --helius-listen |
| X source | DONE | --sources x |

### P2
| Item | Status | Notes |
|---|---|---|
| pair_history | DONE | tiered snapshots |
| scoring | DONE | quality_score + tags |
| admin UI | DONE | static/admin.html |
| multi-chain | DONE | ENABLED_CHAINS (stabilize defaults solana) |

### P3
| Item | Status | Notes |
|---|---|---|
| risk heuristics | DONE | + optional RISK_API_URL |
| backtest | DONE | hold/tpsl/grid/… |
| grok export | DONE | tools/grok_context |
| worker skeleton | DONE | worker/ sqlite|redis |

### P4
| Item | Status | Notes |
|---|---|---|
| grok_report automation | DONE | scripts/grok_report.py + state file |
| GoPlus risk API | DONE | RISK_PROVIDER=goplus + heuristic fallback |
| multi-strategy backtest | DONE | hold/tpsl/grid |
| paper trade | DONE | init/open/tick/status/demo + --cash |

### P5 deep
| Item | Status | Notes |
|---|---|---|
| live_trade dry-run | DONE | gates closed by default |
| dca / martingale / trend | DONE | backtest + paper simulate |
| arb_scan | DONE | signal-only |
| grok_dd | DONE | scripts/grok_dd_report.py |

### Stabilize ops
| Item | Status | Notes |
|---|---|---|
| stabilize_run.sh | DONE | defaults ENABLED_CHAINS=solana; **no secret exports** |
| paper tick | DONE | CLI + crontab example |
| daily_check | DONE | PASS/WARN/FAIL |
| backups crontab example | DONE | deploy/crontab.example + crontab.stabilize.example |
| ENABLED_CHAINS=solana for stabilize | DONE | stabilize_run.sh default |

## Gaps found → fixes applied

1. **Backup scheduling local** — PARTIAL→improved: `scripts/backup_once.sh` + `deploy/crontab.stabilize.example`; one-shot backup run OK (`backups/dexscreener-20261009-185032.db`). Systemd timer still “provide units only”.
2. **Paper cash 1000 vs 10000** — PARTIAL→DONE: `paper_trade.py init --cash N` / `--reset-cash`; `PAPER_START_CASH` default 10000; documented in README + STABILIZE.md + env.example.
3. **Telegram MAIN/ALERT admin-only** — documented as OK for stabilize (README + env.example + STABILIZE.md).
4. **DD auto on high-score new_listing** — MISSING→DONE (default OFF): `auto_hooks.py` + pipeline wire (`note_new_listing_for_dd`, `maybe_auto_dd`); env `DD_AUTO_*`.
5. **Secrets in stabilize_run** — verified: script only exports `ENABLED_CHAINS`; HELIUS/X/Telegram via process env / `.env` (config.py).
6. **README master command index** — DONE (table at end of README).
7. **daily_check WARN notes** — already filters non-ENABLED residuals (e.g. robinhood); heartbeat stale WARN when loop down documented in STABILIZE.md.
8. **backup once + cron snippet** — DONE (above).
9. **grok_report incremental state** — DONE: `exports/grok_report_state.json` present and refreshed by dry hourly run.
10. **Broken demos** — fixed `IndentationError` in `paper_trade.py` (`p_init` indent); all `demo_*.py` re-run OK.

## Optimize done

- **STABILIZE.md** ops runbook (start/stop/health/daily/paper/hooks).
- **stabilize_run.sh** tightens `ENABLED_CHAINS=${ENABLED_CHAINS:-solana}`; no secrets.
- **PAPER_AUTO_OPEN** (default `0`): score≥`PAPER_AUTO_MIN_SCORE` → open size `PAPER_AUTO_SIZE_USD` (50); wired in pipeline via `maybe_paper_auto_open`.
- **Log noise**: low-score alert push skip demoted `info`→`debug` in `scoring.py`.
- **AUDIT_AND_OPT.md** (this file).
- env.example extended with stabilize / paper-auto / DD-auto / Telegram notes.

## Demo verification (safe)

| Demo | Result |
|---|---|
| demo_validate / retry / alert / proxy / proxy_health | OK |
| demo_telegram / helius / x | DRY_RUN_OK |
| demo_p2 / p3 / p4 / p5 | DRY_RUN_OK (after paper_trade indent fix) |
| backup.py --retain 7 | BACKUP_OK |
| live_trade status | gates_open=false, kill_switch_active=true |
| Import auto_hooks + pipeline | OK |

## How to operate

See **STABILIZE.md**. Short version:

```bash
cd /workspace/dexscreener-crawler && source .venv/bin/activate
./stabilize_run.sh                          # or leave existing loop pid running
python main.py --health
python daily_check.py
./scripts/backup_once.sh
python paper_trade.py tick
python scripts/grok_report.py --mode hourly --incremental
# Optional (off by default):
# PAPER_AUTO_OPEN=1 DD_AUTO_ENABLED=1 DD_AUTO_ON_NEW_LISTING=1
```

Cron: `deploy/crontab.stabilize.example`.  
Do **not** remove `LIVE_TRADING_DISABLED` or set live confirm unless intentionally going live.

## Intentionally not done (safe / out of scope)

- Installing systemd units on this box.
- Enabling PAPER_AUTO / DD_AUTO in the live stabilize process env (defaults remain off; restart would be needed to pick up new pipeline code — **existing pid not killed**).
- Opening live trading gates.
