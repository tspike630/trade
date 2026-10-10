# P2 Result — History, scoring, admin UI, multi-chain

**Time (HK / UTC+8):** 2026-10-10 ~02:18+ HKT

## Delivered (priority order)

### 1) `pair_history`
| Piece | Detail |
|---|---|
| Table | `pair_history` + `pairs.last_history_at` |
| Writer | `history.maybe_snapshot_pair` after successful upsert in `_process_batch` |
| Cadence | hot `HISTORY_HOT_INTERVAL_SEC` (300), warm 1800, cold 7200 |
| API | `GET /history?chain=&pair=` and `GET /pairs/{chain}/{addr}/history` |

### 2) Quality scoring
| Piece | Detail |
|---|---|
| Module | `scoring.py` — 0–100 + tags |
| Persist | `tokens.quality_score`, `quality_tags`, `scored_at` |
| Push gate | `ALERT_MIN_SCORE` (default 25): alert **stored**, Telegram/webhook **skipped** |
| Demo | `python demo_p2.py` |

### 3) Admin UI
| Piece | Detail |
|---|---|
| Page | `GET /` → `static/admin.html` |
| Data | `GET /dashboard/summary` (auth) |
| Auth | Prompt → `sessionStorage` → `X-API-Key` header (not query string) |

### 4) Multi-chain
| Piece | Detail |
|---|---|
| Config | `ENABLED_CHAINS` / `CHAINS` (default `solana,ethereum,bsc,base`) |
| Filter | List ingest + tier enrich queue + extra sources |
| Helper | `chains.py` |

## How to use

```bash
cd /workspace/dexscreener-crawler && source .venv/bin/activate

# Demo (no network required for scoring/history unit path)
python demo_p2.py

# Live enrich → snapshots + scores
python main.py --skip-lists --flush-pending

# API + UI
export API_SECRET='dev-secret'   # or ALLOW_INSECURE_API=1 locally
python -m api_server
# browser http://127.0.0.1:8080/  → Set API key → Refresh

curl -s -H "X-API-Key: $API_SECRET" \
  "http://127.0.0.1:8080/history?chain=solana&pair=<PAIR>"
curl -s -H "X-API-Key: $API_SECRET" http://127.0.0.1:8080/dashboard/summary | head

# Restrict chains
export ENABLED_CHAINS=solana
python main.py --force-lists
```

## Verification (this environment)

- `demo_p2.py` → `DRY_RUN_OK` (score tags, history due/skip, push gate, GET /)
- `main.py --skip-lists --flush-pending` → `history_snapshots` / `tokens_scored` in cycle
- Live API `/history` + `/dashboard/summary` with TestClient or uvicorn

## Files
`history.py` `scoring.py` `chains.py` `static/admin.html` `demo_p2.py`  
+ updates: `config.py` `store.py` `pipeline.py` `alerts.py` `api_server.py` `README.md`
