"""C-exit shadow runner: consume decisions → eight-tag shadow book.

Signal source is configurable (--decision-db / --signal-purpose, or env
C_EXIT_SIGNAL_DB / C_EXIT_SIGNAL_PURPOSE) so a future decision-layer shadow-entry
stream can be plugged in. Default stays decision_log purpose=live_candidate.

Does NOT write paper_trading.db or execution_ledger.db.
Does NOT change .env / live gates / main TP-SL-hold.

  python -m execution.c_exit_shadow_run --init
  python -m execution.c_exit_shadow_run --once
  python -m execution.c_exit_shadow_run --loop
  python -m execution.c_exit_shadow_run --status
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from logging_setup import get_logger, setup_logging  # noqa: E402

from execution.c_exit_grid import cells  # noqa: E402
from execution import c_exit_size_mult as size_mult  # noqa: E402
from execution.c_exit_shadow_ledger import (  # noqa: E402
    DEFAULT_START_CASH,
    CExitShadowLedger,
)
from execution.gateway import (  # noqa: E402
    PURPOSE_LIVE_CANDIDATE,
    parse_decision_row,
    purpose_reject_reason,
    read_decision_purpose,
)
from execution.price_confirm import StopPriceConfirmer, latest_quote  # noqa: E402
from execution.pricing import buy_fill_price, slippage_rate  # noqa: E402
from store import fetch_latest_pair_price, norm_address, norm_chain  # noqa: E402

log = get_logger("execution.c_exit_shadow_run")

DEFAULT_SHADOW_DB = _ROOT / "paper_trading_c_exit.db"
DEFAULT_POLL_SEC = float(os.environ.get("C_EXIT_POLL_SEC", "10"))
DEFAULT_ACCEPT_PURPOSES = tuple(
    x.strip()
    for x in os.environ.get("C_EXIT_ACCEPT_PURPOSE", PURPOSE_LIVE_CANDIDATE).split(",")
    if x.strip()
)


def _decision_db() -> Path:
    try:
        from decision_engine.config import DB_PATH as _DE_DB

        return Path(_DE_DB).resolve()
    except Exception:  # noqa: BLE001
        return (_ROOT / "dexscreener.db").resolve()


def _open_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _main_cursor(execution_ledger: Path) -> int:
    if not execution_ledger.exists():
        return 0
    con = _open_ro(execution_ledger)
    try:
        row = con.execute(
            "SELECT value FROM execution_cursor WHERE key='last_processed_id'"
        ).fetchone()
        return int(row["value"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0
    finally:
        con.close()


def _latest_mid(crawler: Path, chain_id: str, pair_address: str) -> tuple[float | None, float | None]:
    con = _open_ro(crawler)
    try:
        spot = fetch_latest_pair_price(con, chain_id, pair_address)
        if not spot:
            return None, None
        px = spot.get("price_usd") or spot.get("priceUsd")
        liq = spot.get("liquidity_usd")
        try:
            mid = float(px) if px is not None else None
        except (TypeError, ValueError):
            mid = None
        try:
            liq_f = float(liq) if liq is not None else None
        except (TypeError, ValueError):
            liq_f = None
        return mid, liq_f
    finally:
        con.close()


def _fetch_pending(decision_db: Path, since_id: int, limit: int) -> list[sqlite3.Row]:
    con = _open_ro(decision_db)
    try:
        cols = [
            "id",
            "ts",
            "strategy_id",
            "action",
            "chain_id",
            "token_address",
            "size_pct",
            "reason",
            "risk_passed",
            "mode",
            "raw_json",
            "purpose",
        ]
        sql = (
            "SELECT "
            + ", ".join(cols)
            + " FROM decision_log WHERE id > ? ORDER BY id ASC LIMIT ?"
        )
        return list(con.execute(sql, (int(since_id), int(limit))).fetchall())
    finally:
        con.close()


def _already_open(ledger: CExitShadowLedger, tag: str, chain_id: str, pair: str) -> bool:
    row = ledger.conn.execute(
        """
        SELECT 1 FROM positions
        WHERE status='open' AND grid_tag=? AND chain_id=? AND pair_address=?
        LIMIT 1
        """,
        (tag, chain_id, pair),
    ).fetchone()
    return row is not None


# Pre-stop anomaly confirmation: shadow default ON (C_EXIT_STOP_CONFIRM=0 to disable).
STOP_CONFIRM_ENABLED = os.environ.get("C_EXIT_STOP_CONFIRM", "1").strip() not in ("0", "false", "no")
CONFIRMER = StopPriceConfirmer()

DEDUP_WINDOW_MIN = float(os.environ.get("C_EXIT_DEDUP_WINDOW_MIN", "60"))


def _dedup_block(
    ledger: CExitShadowLedger,
    tag: str,
    chain_id: str,
    pair: str,
    token: str | None,
    window_min: float = DEDUP_WINDOW_MIN,
) -> str | None:
    """Per cell: same token/pair already open, or opened within window → skip."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(minutes=float(window_min))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    keys = [pair] + ([token] if token else [])
    q = ",".join("?" for _ in keys)
    row = ledger.conn.execute(
        f"""
        SELECT status FROM positions
        WHERE grid_tag=? AND chain_id=?
          AND (pair_address IN ({q}) OR token_address IN ({q}))
          AND (status='open' OR opened_at >= ?)
        ORDER BY status='open' DESC LIMIT 1
        """,
        (tag, chain_id, *keys, *keys, since),
    ).fetchone()
    if row is None:
        return None
    return "dup_open" if row["status"] == "open" else "dup_recent"


def process_once(
    ledger: CExitShadowLedger,
    *,
    decision_db: Path,
    crawler_db: Path,
    limit: int = 50,
    accept_purposes: tuple[str, ...] = (PURPOSE_LIVE_CANDIDATE,),
    notional_cap_1x: float = size_mult.NOTIONAL_CAP_1X_USD,
    max_loss_usd: float | None = None,
) -> dict[str, Any]:
    if ledger.is_halted():
        return {"halted": True, "processed": 0, "opened": 0, "raw_buys": 0, "dedup_skipped": 0, "cursor": ledger.get_last_processed_id(), "tick_closed": []}

    since = ledger.get_last_processed_id()
    rows = _fetch_pending(decision_db, since, limit)
    opened = 0
    skipped = 0
    raw_buys = 0
    dedup_skipped = 0
    last_id = since

    for row in rows:
        d = dict(row)
        last_id = int(d["id"])
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            raw = {}
        purpose = read_decision_purpose(d, raw)
        if purpose not in accept_purposes:
            skipped += 1
            continue
        sig = parse_decision_row(d)
        action = (sig.get("action") or "").lower()
        if action != "buy":
            skipped += 1
            continue
        chain_id = norm_chain(sig.get("chain_id") or "")
        pair = norm_address(sig.get("pair_address") or "")
        token = sig.get("token_address")
        if not chain_id or not pair:
            skipped += 1
            continue
        try:
            size_pct = float(sig.get("size_pct") or 0)
        except (TypeError, ValueError):
            size_pct = 0.0
        if size_pct <= 0:
            skipped += 1
            continue

        raw_buys += 1
        mid, liq = _latest_mid(crawler_db, chain_id, pair)
        if mid is None or mid <= 0:
            skipped += 1
            continue
        slip = slippage_rate(liq)
        fill = buy_fill_price(mid, slip)

        # size vs first tag cash (同口径 size_pct * equity); each tag sized on its own cash
        results = []
        for cell in cells():
            dup = _dedup_block(
                ledger, cell.tag, chain_id, pair, norm_address(token) if token else None
            )
            if dup:
                results.append({"ok": False, "tag": cell.tag, "error": dup})
                dedup_skipped += 1
                continue
            cash = ledger.get_cash(cell.tag)
            # equity ≈ cash + open notional; skeleton uses cash as equity proxy (flat book)
            open_n = ledger.conn.execute(
                """
                SELECT COALESCE(SUM(size_usd),0) AS s FROM positions
                WHERE grid_tag=? AND status='open'
                """,
                (cell.tag,),
            ).fetchone()["s"]
            equity = float(cash) + float(open_n)
            mult = size_mult.effective_mult(ledger.conn, cell.tag)
            base = size_pct * equity
            cap = size_mult.cell_notional_cap(
                ledger.conn, cell.tag, mult, cap_1x=notional_cap_1x, max_loss_usd=max_loss_usd
            )
            base = min(base, cap)
            size_usd = min(size_mult.capped_size(base, mult, cell.sl_pct), float(cash))
            if size_usd <= 1.0:
                results.append({"ok": False, "tag": cell.tag, "error": "size_too_small"})
                continue
            try:
                pid = ledger._open_one(
                    tag=cell.tag,
                    chain_id=chain_id,
                    pair_address=pair,
                    token_address=norm_address(token) if token else None,
                    entry_price=fill,
                    size_usd=size_usd,
                    tp_pct=cell.tp_pct,
                    sl_pct=cell.sl_pct,
                    max_hold_min=cell.max_hold_min,
                    source_decision_id=last_id,
                    trail_pct=cell.trail_pct,
                    partial_tp_pct=cell.partial_tp_pct,
                    partial_frac=cell.partial_frac,
                    mult=mult,
                    meta={
                        "strategy_id": sig.get("strategy_id"),
                        "decision_log_id": last_id,
                        "size_pct": size_pct,
                        "mid_price": mid,
                        "slippage": slip,
                        "liquidity_usd": liq,
                        "shadow": True,
                        "c_exit_grid": True,
                        "signal_purpose": purpose,
                        "size_mult": mult,
                        **size_mult.normalize_labels(raw, d.get("ts")),
                    },
                )
                results.append({"ok": True, "tag": cell.tag, "position_id": pid})
                opened += 1
            except Exception as exc:  # noqa: BLE001
                results.append({"ok": False, "tag": cell.tag, "error": str(exc)})

        log.info(
            "shadow buy decision_id=%s pair=%s results=%s",
            last_id,
            pair[:12],
            [(r.get("tag"), r.get("ok"), r.get("error")) for r in results],
        )

    if last_id > since:
        ledger.set_last_processed_id(last_id)

    def price_lookup(pos: dict[str, Any]) -> float | None:
        if not STOP_CONFIRM_ENABLED:
            mid, _liq = _latest_mid(crawler_db, pos["chain_id"], pos["pair_address"])
            return mid
        try:
            con = _open_ro(crawler_db)
            try:
                q = latest_quote(con, pos["chain_id"], pos["pair_address"])
            finally:
                con.close()
        except sqlite3.Error:
            return None
        if not q:
            return None
        try:
            liq = float(q["liquidity_usd"]) if q["liquidity_usd"] is not None else None
        except (TypeError, ValueError):
            liq = None
        mark, verdict = CONFIRMER.check(
            (pos["chain_id"], pos["pair_address"]),
            float(q["price_usd"]),
            liq,
            q["recorded_at"],
        )
        if verdict in ("suspect_hold", "rug_immediate", "confirmed"):
            log.warning(
                "stop-confirm pair=%s px=%s liq=%s verdict=%s",
                pos["pair_address"][:12], q["price_usd"], liq, verdict,
            )
        return mark

    tick_closed = ledger.tick(price_lookup)
    return {
        "halted": ledger.is_halted(),
        "processed": len(rows),
        "skipped": skipped,
        "opened": opened,
        "raw_buys": raw_buys,
        "dedup_skipped": dedup_skipped,
        "cursor": ledger.get_last_processed_id(),
        "tick_closed": tick_closed,
    }


def cmd_init(shadow_db: Path, execution_ledger: Path, start_cash: float) -> dict:
    with CExitShadowLedger(shadow_db, start_cash=start_cash) as ledger:
        # Seed cursor to main stream head so we do not backfill hundreds of buys.
        if ledger.get_last_processed_id() == 0:
            seed = _main_cursor(execution_ledger)
            if seed > 0:
                ledger.set_last_processed_id(seed)
                log.info("seeded shadow cursor to main last_processed_id=%s", seed)
        return ledger.status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C-exit eight-cell shadow runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--init", action="store_true", help="create empty shadow DB + seed cursor")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--status", action="store_true")
    parser.add_argument(
        "--shadow-db",
        default=str(DEFAULT_SHADOW_DB),
        help="default paper_trading_c_exit.db",
    )
    parser.add_argument(
        "--decision-db",
        default=os.environ.get("C_EXIT_SIGNAL_DB"),
        help="signal source DB with decision_log (default decision_engine DB)",
    )
    parser.add_argument(
        "--accept-purpose",
        action="append",
        default=None,
        help=(
            "decision_log.purpose to consume; repeatable (default live_candidate). "
            "Shadow DB needs explicit --accept-purpose shadow_010 "
            "(add --accept-purpose live_candidate for legacy rows)."
        ),
    )
    parser.add_argument("--crawler-db", default=None)
    parser.add_argument(
        "--ledger",
        default=str(_ROOT / "execution_ledger.db"),
        help="main execution_ledger (read-only; used only to seed cursor on --init)",
    )
    parser.add_argument(
        "--notional-cap-1x",
        type=float,
        default=size_mult.NOTIONAL_CAP_1X_USD,
        help="per-cell base notional cap at 1x (USD, default 193); boosted = cap/mult",
    )
    parser.add_argument(
        "--max-loss-usd",
        type=float,
        default=None,
        help="optional: cap notional by max_loss_usd / cell tail worst loss rate",
    )
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--start-cash", type=float, default=DEFAULT_START_CASH)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    setup_logging()
    shadow_db = Path(args.shadow_db)
    decision_db = Path(args.decision_db) if args.decision_db else _decision_db()
    crawler_db = Path(args.crawler_db) if args.crawler_db else _decision_db()
    interval = float(args.interval) if args.interval is not None else DEFAULT_POLL_SEC

    if args.init:
        st = cmd_init(shadow_db, Path(args.ledger), args.start_cash)
        print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.status:
        if not shadow_db.exists():
            print(json.dumps({"error": "shadow_db_missing", "path": str(shadow_db)}))
            return 1
        with CExitShadowLedger(shadow_db) as ledger:
            print(json.dumps(ledger.status(), ensure_ascii=False, indent=2, default=str))
        return 0

    with CExitShadowLedger(shadow_db, start_cash=args.start_cash) as ledger:
        # auto-seed if empty cursor
        if ledger.get_last_processed_id() == 0:
            seed = _main_cursor(Path(args.ledger))
            if seed > 0:
                ledger.set_last_processed_id(seed)
                log.info("auto-seeded shadow cursor=%s", seed)

        def tick() -> dict:
            return process_once(
                ledger,
                decision_db=decision_db,
                crawler_db=crawler_db,
                limit=args.limit,
                accept_purposes=tuple(args.accept_purpose or DEFAULT_ACCEPT_PURPOSES),
                notional_cap_1x=args.notional_cap_1x,
                max_loss_usd=args.max_loss_usd,
            )

        if args.once:
            summary = tick()
            summary["status"] = ledger.status()
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
            return 0

        log.info(
            "c_exit shadow loop start db=%s interval=%s cursor=%s",
            shadow_db,
            interval,
            ledger.get_last_processed_id(),
        )
        backoff = interval
        while True:
            if ledger.is_halted():
                log.error("shadow halted; exiting loop")
                print(json.dumps(ledger.status(), ensure_ascii=False, indent=2))
                return 3
            try:
                summary = tick()
                if args.json:
                    print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
                else:
                    log.info(
                        "shadow tick processed=%s raw_buys=%s opened=%s dedup_skipped=%s closed=%s cursor=%s halted=%s",
                        summary["processed"],
                        summary["raw_buys"],
                        summary["opened"],
                        summary["dedup_skipped"],
                        len(summary["tick_closed"]),
                        summary["cursor"],
                        summary["halted"],
                    )
                backoff = interval
            except Exception:
                log.exception("shadow tick failed; backing off")
                time.sleep(min(backoff, 60.0))
                backoff = min(max(backoff * 2.0, interval), 60.0)
                continue
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
