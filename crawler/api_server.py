"""
Read API over SQLite with P0 API-key access control.

  export API_SECRET='your-long-secret'
  python -m api_server

  curl -H 'X-API-Key: your-long-secret' http://127.0.0.1:8080/stats

/health stays open for monitors. All other routes require X-API-Key when
API_SECRET is set. If API_SECRET is unset and ALLOW_INSECURE_API!=1, data
routes return 503. Prefer firewall IP allowlist in front of this service.
"""

from __future__ import annotations

import json
import secrets as pysecrets
from contextlib import contextmanager
from typing import Any, Iterator

from pathlib import Path as FsPath

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from config import (
    ALLOW_INSECURE_API,
    API_HOST,
    API_PORT,
    API_SECRET,
    DB_PATH,
    ENABLED_CHAINS,
    HEARTBEAT_STALE_SEC,
)
from chains import DOCUMENTED_CHAINS
from heartbeat import check_health, read_heartbeat
from logging_setup import get_logger, setup_logging
from store import Store

log = get_logger("api")

app = FastAPI(
    title="DexScreener Crawler API",
    version="p3",
    description="Read-only API + admin UI (P2). Data routes require X-API-Key when API_SECRET is set.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def _store() -> Iterator[Store]:
    st = Store(DB_PATH)
    try:
        yield st
    finally:
        st.close()


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Gate data routes:
    - API_SECRET set → require matching X-API-Key (constant-time compare)
    - API_SECRET unset + ALLOW_INSECURE_API=1 → allow (local/dev)
    - else → 503 with setup message
    """
    if API_SECRET:
        if not x_api_key or not pysecrets.compare_digest(x_api_key, API_SECRET):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing X-API-Key",
                headers={"WWW-Authenticate": "ApiKey"},
            )
        return
    if ALLOW_INSECURE_API:
        log.warning("ALLOW_INSECURE_API=1 — data routes unauthenticated (dev only)")
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "API_SECRET is not set. Export API_SECRET and send X-API-Key, "
            "or set ALLOW_INSECURE_API=1 for local insecure mode only."
        ),
    )


@app.on_event("startup")
def _on_startup() -> None:
    setup_logging()
    if API_SECRET:
        log.info("API auth enabled (X-API-Key required on data routes); /health open")
    elif ALLOW_INSECURE_API:
        log.warning(
            "API_SECRET unset + ALLOW_INSECURE_API=1 — insecure local mode; "
            "do not expose publicly"
        )
    else:
        log.warning(
            "API_SECRET unset — data routes return 503 until secret is configured "
            "(/health still open)"
        )


@app.get("/health")
def health() -> dict[str, Any]:
    """Open for ops monitors (no API key)."""
    ok, msg, hb = check_health(stale_sec=HEARTBEAT_STALE_SEC)
    return {
        "ok": ok,
        "message": msg,
        "heartbeat": hb,
        "stale_sec": HEARTBEAT_STALE_SEC,
        "auth": "api_secret_set" if API_SECRET else (
            "insecure_allowed" if ALLOW_INSECURE_API else "secret_required"
        ),
    }


@app.get("/tokens", dependencies=[Depends(require_api_key)])
def tokens(
    chain: str | None = None,
    has_pairs: int | None = Query(default=None, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    with _store() as st:
        items = st.list_tokens_api(chain=chain, has_pairs=has_pairs, limit=limit)
    return {"count": len(items), "items": items}


@app.get("/tokens/{chain}/{address}", dependencies=[Depends(require_api_key)])
def token_detail(chain: str, address: str) -> dict[str, Any]:
    with _store() as st:
        item = st.get_token_api(chain, address)
    if item is None:
        raise HTTPException(status_code=404, detail="token not found")
    return item


@app.get("/pairs", dependencies=[Depends(require_api_key)])
def pairs(
    chain: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    with _store() as st:
        items = st.list_pairs_api(chain=chain, limit=limit)
    return {"count": len(items), "items": items}


@app.get("/pairs/{chain}/{pair_address}", dependencies=[Depends(require_api_key)])
def pair_detail(chain: str, pair_address: str) -> dict[str, Any]:
    with _store() as st:
        item = st.get_pair_api(chain, pair_address)
    if item is None:
        raise HTTPException(status_code=404, detail="pair not found")
    return item


@app.get("/alerts", dependencies=[Depends(require_api_key)])
def alerts(
    since: str | None = None,
    type: str | None = Query(default=None, alias="type"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    with _store() as st:
        items = st.list_alerts_api(since=since, alert_type=type, limit=limit)
    return {"count": len(items), "items": items}


@app.get("/stats", dependencies=[Depends(require_api_key)])
def stats() -> dict[str, Any]:
    with _store() as st:
        cp = st.latest_checkpoint()
        stats_json = None
        if cp and cp.get("stats_json"):
            try:
                stats_json = json.loads(cp["stats_json"])
            except json.JSONDecodeError:
                stats_json = None
        body = {
            "tokens": st.count_tokens(),
            "pairs": st.count_pairs(),
            "alerts": st.count_alerts(),
            "pending_enrich": st.count_pending_enrich(),
            "tokens_has_pairs_0": st.count_tokens_has_pairs(0),
            "tokens_has_pairs_1": st.count_tokens_has_pairs(1),
            "tokens_unenriched": st.count_tokens_unenriched(),
            "cycle_number": st.get_cycle_number(),
            "last_success_at": st.get_last_success_at(),
            "last_profile_poll_at": st.get_last_profile_poll_at(),
            "counters": st.get_counters(),
            "latest_checkpoint": {
                "cycle_id": cp.get("cycle_id") if cp else None,
                "status": cp.get("status") if cp else None,
                "step": cp.get("step") if cp else None,
                "started_at": cp.get("started_at") if cp else None,
                "finished_at": cp.get("finished_at") if cp else None,
                "stats": stats_json,
            },
            "heartbeat": read_heartbeat(),
        }
    return body


@app.get("/history", dependencies=[Depends(require_api_key)])
def history_query(
    chain: str = Query(...),
    pair: str = Query(..., description="pair address"),
    limit: int = Query(default=200, ge=1, le=2000),
    since: str | None = None,
) -> dict[str, Any]:
    with _store() as st:
        items = st.list_pair_history(chain, pair, limit=limit, since=since)
    return {"count": len(items), "chain": chain, "pair": pair, "items": items}


@app.get("/pairs/{chain}/{pair_address}/history", dependencies=[Depends(require_api_key)])
def pair_history(
    chain: str,
    pair_address: str,
    limit: int = Query(default=200, ge=1, le=2000),
    since: str | None = None,
) -> dict[str, Any]:
    with _store() as st:
        items = st.list_pair_history(chain, pair_address, limit=limit, since=since)
    return {
        "count": len(items),
        "chain": chain,
        "pair": pair_address,
        "items": items,
    }


@app.get("/dashboard/summary", dependencies=[Depends(require_api_key)])
def dashboard_summary() -> dict[str, Any]:
    """Aggregates for the light admin UI."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    with _store() as st:
        ok, msg, hb = check_health(stale_sec=HEARTBEAT_STALE_SEC)
        return {
            "health_ok": ok,
            "health_message": msg,
            "heartbeat": hb,
            "enabled_chains": ENABLED_CHAINS,
            "documented_chains": list(DOCUMENTED_CHAINS),
            "stats": {
                "tokens": st.count_tokens(),
                "pairs": st.count_pairs(),
                "alerts_total": st.count_alerts(),
                "alerts_today": st.count_alerts_since(today),
                "pending_enrich": st.count_pending_enrich(),
                "pair_history_rows": st.count_pair_history(),
                "cycle_number": st.get_cycle_number(),
                "last_success_at": st.get_last_success_at(),
                "counters": st.get_counters(),
            },
            "recent_alerts": st.list_alerts_api(limit=20),
            "top_volume": st.list_top_pairs_by_metric(metric="volume", limit=10),
            "top_gainers": st.list_top_pairs_by_metric(metric="gainer", limit=10),
        }




def _format_indicator_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a token_indicators row into technical + funding + sentiment + meta."""
    return {
        "chain_id": row.get("chain_id"),
        "token_address": row.get("token_address"),
        "technical": {
            "change_5m": row.get("change_5m"),
            "change_15m": row.get("change_15m"),
            "change_1h": row.get("change_1h"),
            "change_4h": row.get("change_4h"),
            "change_24h": row.get("change_24h"),
            "volatility_24h": row.get("volatility_24h"),
            "volume_strength": row.get("volume_strength"),
        },
        "funding": {
            "jev_raw_score": row.get("jev_raw_score"),
            "jev_score": row.get("jev_score"),
            "jev_signal": row.get("jev_signal"),
            "jev_confidence": row.get("jev_confidence"),
            "jev_strength": row.get("jev_strength"),
            "jev_funding_bias": row.get("jev_funding_bias"),
            "jev_updated_at": row.get("jev_updated_at"),
        },
        "sentiment": {
            "sentiment_mentions_1h": row.get("sentiment_mentions_1h"),
            "sentiment_mentions_6h": row.get("sentiment_mentions_6h"),
            "sentiment_mentions_24h": row.get("sentiment_mentions_24h"),
            "sentiment_growth_1h": row.get("sentiment_growth_1h"),
            "sentiment_growth_6h": row.get("sentiment_growth_6h"),
            "sentiment_growth_24h": row.get("sentiment_growth_24h"),
            "sentiment_heat_score": row.get("sentiment_heat_score"),
            "sentiment_polarity": row.get("sentiment_polarity"),
            "sentiment_score": row.get("sentiment_score"),
            "sentiment_updated_at": row.get("sentiment_updated_at"),
        },
        "composite": {
            "composite_score": row.get("composite_score"),
            "composite_updated_at": row.get("composite_updated_at"),
        },
        "tier": row.get("tier"),
        "primary_pair_address": row.get("primary_pair_address"),
        "sample_count": row.get("sample_count"),
        "updated_at": row.get("updated_at"),
    }


@app.get("/indicators/top", dependencies=[Depends(require_api_key)])
def indicators_top(
    chain: str | None = Query(default=None),
    sort: str = Query(
        default="change_5m",
        description="change_5m | jev_strength | volume_strength",
    ),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    """Top-N from token_indicators (read-only). Whitelisted sort columns."""
    from indicators.base import IndicatorStore

    try:
        with IndicatorStore(DB_PATH) as ist:
            allowed = list(IndicatorStore.TOP_SORT_COLUMNS)
            if sort not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid sort={sort!r}; allowed={allowed}",
                )
            items = ist.list_top_token_indicators(
                chain=chain, sort=sort, limit=limit
            )
        return {
            "count": len(items),
            "chain": chain,
            "sort": sort,
            "limit": limit,
            "items": [_format_indicator_row(r) for r in items],
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("indicators_top failed")
        raise HTTPException(status_code=500, detail=f"indicators query failed: {exc}") from exc


@app.get("/indicators/{chain}/{token_address}", dependencies=[Depends(require_api_key)])
def indicator_detail(chain: str, token_address: str) -> dict[str, Any]:
    """Full token_indicators row for one token (technical + funding). 404 if missing."""
    from indicators.base import IndicatorStore

    try:
        with IndicatorStore(DB_PATH) as ist:
            row = ist.get_token_indicators(chain, token_address, use_cache=False)
        if row is None:
            raise HTTPException(status_code=404, detail="indicators not found")
        return _format_indicator_row(row)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("indicator_detail failed")
        raise HTTPException(status_code=500, detail=f"indicators query failed: {exc}") from exc



@app.get("/export/context", dependencies=[Depends(require_api_key)])
def export_context(
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    top: int = Query(default=15, ge=1, le=100),
    alerts: int = Query(default=25, ge=1, le=200),
):
    """Grok Bot / 蜘蛛爬虫 ingest: scores, history samples, alerts."""
    from fastapi.responses import PlainTextResponse
    from tools.grok_context import build_context, to_markdown

    with _store() as st:
        ctx = build_context(st, top_n=top, alerts_n=alerts)
    if format == "markdown":
        return PlainTextResponse(to_markdown(ctx), media_type="text/markdown")
    return ctx


@app.get("/", response_class=HTMLResponse)

def admin_ui() -> HTMLResponse:
    """Light admin dashboard (static HTML). Data fetched via JS + X-API-Key."""
    path = FsPath(__file__).resolve().parent / "static" / "admin.html"
    if not path.is_file():
        return HTMLResponse("<h1>admin.html missing</h1>", status_code=500)
    return HTMLResponse(path.read_text(encoding="utf-8"))


def main() -> None:

    import uvicorn

    setup_logging()
    uvicorn.run(
        "api_server:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
