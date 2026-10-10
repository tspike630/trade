# Phase 5 Result — 代理池 + 自有 API + 多数据源

**Time (HK / UTC+8):** 2026-10-10 ~01:23 HKT

## What was added

| Piece | Default | How to enable |
|---|---|---|
| Proxy pool (`proxy_pool.py`) | **OFF** | `USE_PROXY=1` + `PROXY_LIST` or `proxies.txt` |
| FastAPI (`api_server.py`) | not running | `python -m api_server` |
| Multi-source stubs (`sources/`) | only primary DexScreener | `python main.py --sources solana_stub,x_stub` |

Phases 1–4 behavior unchanged when Phase 5 features are left off.

## Verification

### Proxy demo (no real proxies)
```
python demo_proxy.py
→ PROXY_DEMO_OK urls=2 snapshot_healthy=1
```
Crawl log without USE_PROXY: `proxy pool empty/disabled (default)`.

### Multi-source dry-run
```
python main.py --skip-lists --sources solana_stub,x_stub --force-new
```
- `solana_stub`: loaded 2 tokens from fixture → `tokens_upserted=2`
- `x_stub`: no bearer → no-op
- Extra sources logged; cycle completed

### API (live DB)
```bash
python -m api_server   # 127.0.0.1:8080
curl -s http://127.0.0.1:8080/health   # ok=true, heartbeat age
curl -s http://127.0.0.1:8080/stats    # tokens=84 pairs=57 alerts=18
curl -s 'http://127.0.0.1:8080/tokens?limit=2'
curl -s 'http://127.0.0.1:8080/pairs?chain=solana&limit=1'
curl -s 'http://127.0.0.1:8080/alerts?limit=2'
curl -s http://127.0.0.1:8080/tokens/solana/<address>
```

## Start API
```bash
cd /workspace/dexscreener-crawler
source .venv/bin/activate
pip install -r requirements.txt
python -m api_server
# uvicorn api_server:app --host 127.0.0.1 --port 8080
```

## Files
- `proxy_pool.py`, `demo_proxy.py`
- `api_server.py`
- `sources/{base,dexscreener,stub_solana_logs,stub_x}.py`
- `fixtures/solana_logs_sample.json`
- `client.py` (optional proxy per attempt)
- `pipeline.py` (`--sources` merge hook)
- `store.py` (API query helpers)
- `requirements.txt` (+ fastapi, uvicorn, socksio)
- `README.md`, `PHASE5_RESULT.md`

## Note
Prefer batch/tier/rate-limit tuning before proxies. Local API has no auth — add before public exposure.
