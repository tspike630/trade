# P3 Result — Risk, backtest, Grok export, worker

**Time (HK / UTC+8):** 2026-10-10 ~02:28+ HKT

## 1) On-chain risk (`risk.py`)

| Piece | Detail |
|---|---|
| Interface | `RiskScanner.scan(...)` → `RiskResult(deduction, tags, flags)` |
| Default | `HeuristicRiskScanner` — thin liq, honeypot-like buy/sell skew, high turnover, metadata tax/honeypot/renounce strings |
| Optional | `HttpRiskScanner` via `RISK_API_URL` + `RISK_API_KEY` (GoPlus/RugCheck-compatible JSON; **key required for real vendors, never logged**) |
| Hook | `scoring.score_token_from_store` deducts score + merges `risk_*` tags |

```bash
python demo_p3.py
# enrich path auto-applies risk when scoring
python main.py --skip-lists --flush-pending
```

## 2) Backtest (`backtest.py`)

Buy after X minutes from series start, sell after Y minutes using nearest `pair_history` prices.

```bash
python backtest.py --buy-after 5 --hold 30
python backtest.py --buy-after 5 --hold 30 --seed-demo --out exports/backtest-demo.json
```

Verified: seeded walk → trades with win_rate / PnL distribution JSON.

## 3) Grok Bot linkage

| Piece | Detail |
|---|---|
| CLI | `python -m tools.grok_context --format both --out exports/grok_context` |
| API | `GET /export/context?format=json\|markdown` (API key) |

**蜘蛛爬虫 / Grok Bot usage:** poll `/export/context` or read `exports/grok_context.md` as tool context (top scores, low scores, history samples, recent alerts, health).

```bash
curl -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/export/context?format=markdown"
```

## 4) Distributed worker skeleton (`worker/`)

| Mode | How |
|---|---|
| Single-node default | `WORKER_BACKEND=sqlite` → `worker_queue.db` |
| Multi-worker | several `python -m worker.run_worker --loop` or `WORKER_BACKEND=redis` + `REDIS_URL` |

```bash
python -m worker.scheduler --once    # enqueue from pending_enrich
python -m worker.run_worker --once   # claim → re-queue pending_enrich marker
```

Main `main.py --loop` remains the primary enrich path; workers are additive.

## Verification

```bash
python demo_p3.py          # DRY_RUN_OK
python backtest.py --seed-demo --buy-after 5 --hold 30
python -m tools.grok_context --format both --out exports/grok_context
python -m worker.scheduler --once && python -m worker.run_worker --once
```

Pipeline enrich/scoring unchanged aside from optional risk deduction inside `score_token_from_store`.
