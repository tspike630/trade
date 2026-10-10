"""
Optional post-enrich hooks (default OFF via env).

- DD_AUTO_ENABLED: write deep DD markdown when quality_score >= DD_AUTO_MIN_SCORE
- DD_AUTO_ON_NEW_LISTING: queue token for DD after first score (lightweight flag file)
- PAPER_AUTO_OPEN: open paper position size PAPER_AUTO_SIZE_USD when score >= PAPER_AUTO_MIN_SCORE
- JEV_ON_NEW_LISTING: queue token for JEV enricher (exports/jev_pending.jsonl); default OFF
- INDICATORS_ON_UPDATE_HOOK: optional post-indicator callback (default OFF; no scoring/alerts)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import (
    DB_PATH,
    DD_AUTO_ENABLED,
    DD_AUTO_MIN_SCORE,
    DD_AUTO_ON_NEW_LISTING,
    GROK_REPORT_DIR,
    INDICATORS_ON_UPDATE_HOOK,
    JEV_ON_NEW_LISTING,
    JEV_PENDING_PATH,
    PAPER_AUTO_MIN_SCORE,
    PAPER_AUTO_OPEN,
    PAPER_AUTO_SIZE_USD,
    PAPER_DB_PATH,
)
from logging_setup import get_logger

log = get_logger("auto_hooks")

_PENDING_DD = Path("exports/dd_pending.jsonl")


def note_new_listing_for_dd(chain_id: str, token_address: str) -> None:
    if not DD_AUTO_ON_NEW_LISTING:
        return
    try:
        _PENDING_DD.parent.mkdir(parents=True, exist_ok=True)
        with _PENDING_DD.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"chain_id": chain_id, "token_address": token_address}) + "\n"
            )
        log.info("dd_pending queued %s/%s", chain_id, token_address[:12])
    except Exception as exc:  # noqa: BLE001
        log.warning("dd_pending failed: %s", exc)


def note_new_listing_for_jev(chain_id: str, token_address: str) -> None:
    """Queue a new listing for JEV enricher. No-op unless JEV_ON_NEW_LISTING=1.

    Enable: export JEV_ON_NEW_LISTING=1
    The JEV loop (`python -m sources.jev --loop`) drains exports/jev_pending.jsonl
    (or JEV_PENDING_PATH) on each pass. Does not call HTTP from the crawl path.
    """
    if not JEV_ON_NEW_LISTING:
        return
    try:
        path = Path(JEV_PENDING_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"chain_id": chain_id, "token_address": token_address}) + "\n"
            )
        log.info("jev_pending queued %s/%s", chain_id, token_address[:12])
    except Exception as exc:  # noqa: BLE001
        log.warning("jev_pending failed: %s", exc)


def _pending_dd_match(chain_id: str, token_address: str) -> bool:
    if not _PENDING_DD.is_file():
        return False
    try:
        lines = _PENDING_DD.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    key = f"{chain_id}|{token_address}".lower()
    keep: list[str] = []
    found = False
    for line in lines:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        k = f"{o.get('chain_id')}|{o.get('token_address')}".lower()
        if k == key and not found:
            found = True
            continue
        keep.append(line)
    if found:
        _PENDING_DD.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return found


def maybe_auto_dd(store: Any, chain_id: str, token_address: str, score: float) -> None:
    want = False
    if DD_AUTO_ENABLED and score >= DD_AUTO_MIN_SCORE:
        want = True
    if DD_AUTO_ON_NEW_LISTING and _pending_dd_match(chain_id, token_address):
        if score >= DD_AUTO_MIN_SCORE or DD_AUTO_ENABLED:
            want = True
        elif score >= max(40.0, DD_AUTO_MIN_SCORE - 20):
            # new listing with moderate score still gets a short DD
            want = True
    if not want:
        return
    try:
        import importlib.util
        from datetime import datetime, timezone

        root = Path(__file__).resolve().parent
        spec = importlib.util.spec_from_file_location(
            "grok_dd_report", root / "scripts" / "grok_dd_report.py"
        )
        if not spec or not spec.loader:
            return
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ctx = mod.assemble_token_context(store, chain_id, token_address)
        md = mod.render_dd_markdown(ctx)
        out = Path(GROK_REPORT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = out / f"dd-auto-{chain_id}-{token_address[:10]}-{stamp}.md"
        path.write_text(md, encoding="utf-8")
        log.info("auto_dd written %s score=%.1f", path, score)
    except Exception as exc:  # noqa: BLE001
        log.warning("auto_dd failed: %s", exc)


def maybe_paper_auto_open(
    store: Any,
    chain_id: str,
    token_address: str,
    pair_address: str,
    score: float,
) -> None:
    if not PAPER_AUTO_OPEN:
        return
    if score < PAPER_AUTO_MIN_SCORE:
        return
    try:
        from paper_trade import PaperLedger, latest_price

        with PaperLedger(PAPER_DB_PATH) as ledger:
            # skip if already open on this pair
            for p in ledger.list_open():
                if (
                    p["chain_id"] == chain_id
                    and p["pair_address"] == pair_address
                ):
                    return
            px = latest_price(store, chain_id, pair_address)
            if px is None or px <= 0:
                return
            size = min(PAPER_AUTO_SIZE_USD, ledger.get_cash())
            if size < 1:
                log.info("paper_auto skip: insufficient cash")
                return
            pid = ledger.open_position(
                chain_id=chain_id,
                pair_address=pair_address,
                price=px,
                size_usd=size,
                token_address=token_address,
                tp_pct=10.0,
                sl_pct=5.0,
                max_hold_min=120.0,
                meta={"auto": True, "score": score},
            )
            log.info(
                "paper_auto opened id=%s score=%.1f size=%.1f pair=%s",
                pid,
                score,
                size,
                pair_address[:12],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("paper_auto failed: %s", exc)


def maybe_on_indicator(
    chain_id: str,
    token_address: str,
    row: dict[str, Any] | None = None,
) -> None:
    """
    Optional post-indicator hook. Gated by INDICATORS_ON_UPDATE_HOOK (default OFF).

    Does NOT modify scoring, alerts, or crawl tables. Safe no-op until explicitly enabled.
    Intended for future side effects (e.g. export / debug) — keep empty by default.
    """
    if not INDICATORS_ON_UPDATE_HOOK:
        return
    try:
        # Placeholder: log only. Do not wire scoring/alerts here.
        sym = (row or {}).get("tier") or "?"
        log.info(
            "on_indicator hook %s/%s tier=%s composite=%s",
            chain_id,
            token_address[:12],
            sym,
            (row or {}).get("composite_score"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("on_indicator hook failed: %s", exc)

