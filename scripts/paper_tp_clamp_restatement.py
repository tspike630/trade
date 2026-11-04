#!/usr/bin/env python3
"""Read-only restatement of paper_eq_*.db under TP-clamp + dead-pool close rules.

Does NOT write back into the paper DBs. Writes a markdown report.

Usage (from anywhere):

  python3 scripts/paper_tp_clamp_restatement.py \\
    --paper-glob '/workspace/dexscreener-crawler/paper_eq_*.db' \\
    --crawler-db /workspace/dexscreener-crawler/dexscreener.db \\
    --out exports/reports/paper_tp_clamp_restatement_2026-11-04.md
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


OUTLIER_BOOKS = {
    "paper_eq_tech_fade.db",
    "paper_eq_rel_strength_m.db",
}
DEFAULT_START_CASH = 10_000.0
DEFAULT_MIN_LIQ = 1000.0
EPS = 1e-12


@dataclass
class TradeFix:
    book: str
    position_id: str
    pair: str
    entry: float
    exit: float
    move_pct: float
    tp_pct: float | None
    orig_pnl: float
    new_pnl: float
    kind: str  # "tp_clamp" | "dead_pool"
    close_liq: float | None = None
    closed_at: str | None = None


@dataclass
class BookSummary:
    book: str
    n_closed: int = 0
    online_net: float = 0.0
    restated_net: float = 0.0
    n_tp: int = 0
    n_tp_overshoot: int = 0
    n_dead_pool: int = 0
    start_cash: float = DEFAULT_START_CASH
    outlier: bool = False
    clamped_trades: list[TradeFix] = field(default_factory=list)
    dead_pool_trades: list[TradeFix] = field(default_factory=list)


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _start_cash(con: sqlite3.Connection) -> float:
    """Infer start cash: current cash - open size + closed pnl? Prefer 10k default.

    Race peers start at PAPER_START_CASH=10000. Use that unless account has a
    recorded start we can derive: cash + sum(open size) - sum(closed pnl).
    """
    try:
        cash = float(con.execute("SELECT cash_usd FROM account WHERE id=1").fetchone()[0])
    except Exception:
        return DEFAULT_START_CASH
    open_size = con.execute(
        "SELECT COALESCE(SUM(size_usd),0) FROM positions WHERE status='open'"
    ).fetchone()[0]
    closed_pnl = con.execute(
        "SELECT COALESCE(SUM(pnl_usd),0) FROM positions WHERE status='closed'"
    ).fetchone()[0]
    inferred = float(cash) + float(open_size) - float(closed_pnl)
    if math.isfinite(inferred) and inferred > 0:
        # Snap near-canonical 10k starts.
        if abs(inferred - DEFAULT_START_CASH) < 1.0:
            return DEFAULT_START_CASH
        return inferred
    return DEFAULT_START_CASH



def _shift_iso(ts: str, seconds: int) -> str:
    """Shift an ISO-ish UTC timestamp by seconds; fall back to raw string."""
    text = str(ts).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        out = (dt + timedelta(seconds=seconds)).astimezone(timezone.utc)
        return out.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return str(ts)


def _liq_at_close(
    crawler: sqlite3.Connection | None,
    chain_id: str,
    pair: str,
    closed_at: str | None,
    cache: dict[tuple[str, str, str], float | None] | None = None,
) -> float | None:
    """Best-effort close-time liquidity matching paper tick's get_pair_raw.

    Runtime tick reads ``pairs.raw_json`` after the same update that emits
    ``liquidity_drop`` alerts — often seconds before ``pair_history`` catches
    up. Restatement therefore prefers:
      1) alerts.liquidity_drop near closed_at (payload new_liquidity_usd)
      2) min pair_history.liquidity in [close-2m, close+20m]
      3) latest pair_history row at/before closed_at
    """
    if crawler is None or not closed_at or not pair:
        return None
    key = (str(chain_id or "solana"), pair.lower(), str(closed_at))
    if cache is not None and key in cache:
        return cache[key]

    val: float | None = None

    # 1) alerts — same-second rug tip
    lo = _shift_iso(closed_at, -60)
    hi = _shift_iso(closed_at, 60)
    try:
        arow = crawler.execute(
            """
            SELECT payload_json, message, created_at FROM alerts
            WHERE type = 'liquidity_drop'
              AND lower(pair_address) = lower(?)
              AND created_at BETWEEN ? AND ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (pair, lo, hi),
        ).fetchone()
    except sqlite3.Error:
        arow = None
    if arow is not None:
        payload = {}
        try:
            payload = json.loads(arow["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        for k in ("new_liquidity_usd", "new_liquidity", "liquidity_usd"):
            if payload.get(k) is not None:
                try:
                    val = float(payload[k])
                    break
                except (TypeError, ValueError):
                    pass
        if val is None:
            m = re.search(r"→\s*([0-9.]+)", str(arow["message"] or ""))
            if m:
                try:
                    val = float(m.group(1))
                except ValueError:
                    pass

    # 2) min liq in a window around close (captures post-alert history sample)
    if val is None:
        wlo = _shift_iso(closed_at, -120)
        whi = _shift_iso(closed_at, 20 * 60)
        row = crawler.execute(
            """
            SELECT MIN(liquidity_usd) AS liq FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
              AND recorded_at BETWEEN ? AND ?
              AND liquidity_usd IS NOT NULL
            """,
            (key[0], pair, wlo, whi),
        ).fetchone()
        if row is not None and row["liq"] is not None:
            try:
                val = float(row["liq"])
            except (TypeError, ValueError):
                val = None

    # 3) fallback: latest at/before close
    if val is None:
        row = crawler.execute(
            """
            SELECT liquidity_usd FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
              AND recorded_at <= ?
              AND liquidity_usd IS NOT NULL
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (key[0], pair, closed_at),
        ).fetchone()
        if row is not None:
            try:
                val = float(row["liquidity_usd"])
            except (TypeError, ValueError):
                val = None

    if cache is not None:
        cache[key] = val
    return val


def _is_tp_reason(reason: str | None) -> bool:
    if not reason:
        return False
    head = str(reason).split(":", 1)[0]
    return head == "take_profit"


def restate_book(
    path: Path,
    *,
    crawler: sqlite3.Connection | None,
    min_liq: float = DEFAULT_MIN_LIQ,
) -> BookSummary:
    name = path.name
    summary = BookSummary(book=name, outlier=name in OUTLIER_BOOKS)
    con = _ro(path)
    liq_cache: dict[tuple[str, str, str], float | None] = {}
    try:
        summary.start_cash = _start_cash(con)
        rows = con.execute(
            """
            SELECT id, chain_id, pair_address, entry_price, exit_price, qty,
                   size_usd, tp_pct, pnl_usd, exit_reason, closed_at, status
            FROM positions
            WHERE status = 'closed'
            """
        ).fetchall()
        summary.n_closed = len(rows)
        for row in rows:
            entry = float(row["entry_price"] or 0.0)
            exit_px = float(row["exit_price"] or 0.0)
            qty = float(row["qty"] or 0.0)
            size = float(row["size_usd"] or 0.0)
            orig_pnl = float(row["pnl_usd"] or 0.0)
            summary.online_net += orig_pnl
            tp_pct = row["tp_pct"]
            try:
                tp_f = float(tp_pct) if tp_pct is not None else None
            except (TypeError, ValueError):
                tp_f = None
            reason = row["exit_reason"]
            if _is_tp_reason(reason):
                summary.n_tp += 1

            move_pct = ((exit_px / entry) - 1.0) * 100.0 if entry > 0 else 0.0
            new_pnl = orig_pnl
            new_exit = exit_px
            kind = None
            close_liq = _liq_at_close(
                crawler,
                str(row["chain_id"] or "solana"),
                str(row["pair_address"] or ""),
                row["closed_at"],
                cache=liq_cache,
            )

            # Dead pool first (B): close-time liq below threshold → flat at entry.
            if (
                close_liq is not None
                and min_liq > 0
                and float(close_liq) < float(min_liq)
                and entry > 0
                and abs(orig_pnl) > 1e-9
            ):
                new_exit = entry
                new_pnl = qty * new_exit - size  # ≈0
                kind = "dead_pool"
                summary.n_dead_pool += 1
                fix = TradeFix(
                    book=name,
                    position_id=str(row["id"]),
                    pair=str(row["pair_address"] or ""),
                    entry=entry,
                    exit=exit_px,
                    move_pct=move_pct,
                    tp_pct=tp_f,
                    orig_pnl=orig_pnl,
                    new_pnl=new_pnl,
                    kind="dead_pool",
                    close_liq=float(close_liq),
                    closed_at=row["closed_at"],
                )
                summary.dead_pool_trades.append(fix)
            elif (
                _is_tp_reason(reason)
                and tp_f is not None
                and entry > 0
            ):
                limit = entry * (1.0 + tp_f / 100.0)
                if exit_px > limit + EPS:
                    summary.n_tp_overshoot += 1
                    new_exit = limit
                    new_pnl = qty * new_exit - size
                    kind = "tp_clamp"
                    fix = TradeFix(
                        book=name,
                        position_id=str(row["id"]),
                        pair=str(row["pair_address"] or ""),
                        entry=entry,
                        exit=exit_px,
                        move_pct=move_pct,
                        tp_pct=tp_f,
                        orig_pnl=orig_pnl,
                        new_pnl=new_pnl,
                        kind="tp_clamp",
                        close_liq=close_liq,
                        closed_at=row["closed_at"],
                    )
                    summary.clamped_trades.append(fix)

            summary.restated_net += new_pnl
    finally:
        con.close()
    return summary


def _pct(net: float, start: float) -> float:
    if start <= 0:
        return float("nan")
    return 100.0 * net / start


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def _fmt_pct(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x:.2f}%"


def render(summaries: list[BookSummary]) -> str:
    main = [s for s in summaries if not s.outlier]
    outliers = [s for s in summaries if s.outlier]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    lines.append("# Paper TP clamp + dead-pool restatement (2026-11-04)")
    lines.append("")
    lines.append(f"Generated at {now} (UTC). **Read-only** — source `paper_eq_*.db` untouched.")
    lines.append("")
    lines.append("Rules applied offline:")
    lines.append("1. **TP clamp**: `take_profit` exits with `exit > entry*(1+tp_pct/100)` → fill at limit; pnl = qty*limit − size.")
    lines.append("2. **Dead pool**: close-time `pair_history.liquidity_usd` < 1000 → fill at entry; pnl≈0. Attributed separately from TP clamp.")
    lines.append("3. OUTLIER books (`paper_eq_tech_fade.db`, `paper_eq_rel_strength_m.db`) listed in their own section, excluded from main totals.")
    lines.append("")

    # Main table
    lines.append("## Main books")
    lines.append("")
    lines.append(
        "| 账面 | n_closed | 现网净利 | 夹后净利 | 差额 | 现网收益率% | 夹后收益率% | TP笔数 | 超阈值笔数 | 死池笔数 |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    tot_online = tot_rest = 0.0
    tot_tp = tot_over = tot_dead = 0
    for s in sorted(main, key=lambda x: x.book):
        delta = s.restated_net - s.online_net
        tot_online += s.online_net
        tot_rest += s.restated_net
        tot_tp += s.n_tp
        tot_over += s.n_tp_overshoot
        tot_dead += s.n_dead_pool
        lines.append(
            "| {book} | {n} | {on} | {rs} | {d} | {op} | {rp} | {tp} | {ov} | {dp} |".format(
                book=s.book.replace("paper_eq_", "").replace(".db", ""),
                n=s.n_closed,
                on=_fmt_money(s.online_net),
                rs=_fmt_money(s.restated_net),
                d=_fmt_money(delta),
                op=_fmt_pct(_pct(s.online_net, s.start_cash)),
                rp=_fmt_pct(_pct(s.restated_net, s.start_cash)),
                tp=s.n_tp,
                ov=s.n_tp_overshoot,
                dp=s.n_dead_pool,
            )
        )
    lines.append(
        "| **TOTAL** |  | {on} | {rs} | {d} |  |  | {tp} | {ov} | {dp} |".format(
            on=_fmt_money(tot_online),
            rs=_fmt_money(tot_rest),
            d=_fmt_money(tot_rest - tot_online),
            tp=tot_tp,
            ov=tot_over,
            dp=tot_dead,
        )
    )
    lines.append("")

    # Highlight tech_boll_fade
    boll = next((s for s in main if "tech_boll_fade" in s.book), None)
    if boll:
        lines.append("### Highlight: `tech_boll_fade`")
        lines.append("")
        lines.append(
            f"- 现网净利 ${_fmt_money(boll.online_net)} → 夹后 ${_fmt_money(boll.restated_net)} "
            f"（差额 ${_fmt_money(boll.restated_net - boll.online_net)}；"
            f"超阈值 TP {boll.n_tp_overshoot}/{boll.n_tp}）"
        )
        lines.append("")

    # Clamped trades detail
    lines.append("## A) TP 超阈值被夹的具体笔")
    lines.append("")
    lines.append(
        "| book | position_id | pair | entry | exit | 移动% | tp% | 原pnl | 夹后pnl |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    clamps = [t for s in main for t in s.clamped_trades]
    clamps.sort(key=lambda t: abs(t.orig_pnl - t.new_pnl), reverse=True)
    for t in clamps:
        lines.append(
            "| {b} | `{pid}` | `{pair}` | {en:.8g} | {ex:.8g} | {mv:.2f} | {tp} | {op} | {np} |".format(
                b=t.book.replace("paper_eq_", "").replace(".db", ""),
                pid=t.position_id[:8],
                pair=(t.pair[:12] + "…") if len(t.pair) > 12 else t.pair,
                en=t.entry,
                ex=t.exit,
                mv=t.move_pct,
                tp=f"{t.tp_pct:.2f}" if t.tp_pct is not None else "n/a",
                op=_fmt_money(t.orig_pnl),
                np=_fmt_money(t.new_pnl),
            )
        )
    if not clamps:
        lines.append("| (none) |  |  |  |  |  |  |  |  |")
    lines.append("")
    lines.append(f"共 {len(clamps)} 笔；合计多记 ${_fmt_money(sum(t.orig_pnl - t.new_pnl for t in clamps))}。")
    lines.append("")

    # Dead pool section
    lines.append("## B) 死池平仓笔（与 A 分开归因）")
    lines.append("")
    lines.append(
        "| book | position_id | pair | entry | exit | 移动% | tp% | close_liq | 原pnl | 夹后pnl |"
    )
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    deads = [t for s in main for t in s.dead_pool_trades]
    deads.sort(key=lambda t: abs(t.orig_pnl), reverse=True)
    for t in deads:
        lines.append(
            "| {b} | `{pid}` | `{pair}` | {en:.8g} | {ex:.8g} | {mv:.2f} | {tp} | {liq} | {op} | {np} |".format(
                b=t.book.replace("paper_eq_", "").replace(".db", ""),
                pid=t.position_id[:8],
                pair=(t.pair[:12] + "…") if len(t.pair) > 12 else t.pair,
                en=t.entry,
                ex=t.exit,
                mv=t.move_pct,
                tp=f"{t.tp_pct:.2f}" if t.tp_pct is not None else "n/a",
                liq=f"{t.close_liq:.4g}" if t.close_liq is not None else "n/a",
                op=_fmt_money(t.orig_pnl),
                np=_fmt_money(t.new_pnl),
            )
        )
    if not deads:
        lines.append("| (none) |  |  |  |  |  |  |  |  |  |")
    lines.append("")
    lines.append(f"共 {len(deads)} 笔；合计虚高 ${_fmt_money(sum(t.orig_pnl - t.new_pnl for t in deads))}。")
    lines.append("")

    # Outliers
    lines.append("## OUTLIER books（不进主表）")
    lines.append("")
    lines.append("| 账面 | n_closed | 现网净利 | 夹后净利 | 差额 | TP超阈值 | 死池 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for s in outliers:
        lines.append(
            "| {b} | {n} | {on} | {rs} | {d} | {ov} | {dp} |".format(
                b=s.book,
                n=s.n_closed,
                on=_fmt_money(s.online_net),
                rs=_fmt_money(s.restated_net),
                d=_fmt_money(s.restated_net - s.online_net),
                ov=s.n_tp_overshoot,
                dp=s.n_dead_pool,
            )
        )
    if not outliers:
        lines.append("| (none found) |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--paper-glob",
        default="paper_eq_*.db",
        help="Glob for paper books (default: paper_eq_*.db in cwd)",
    )
    ap.add_argument(
        "--crawler-db",
        default="dexscreener.db",
        help="Crawler DB for close-time liquidity lookup",
    )
    ap.add_argument(
        "--out",
        default="exports/reports/paper_tp_clamp_restatement_2026-11-04.md",
    )
    ap.add_argument("--min-liq", type=float, default=DEFAULT_MIN_LIQ)
    args = ap.parse_args()

    paths = sorted(Path(p) for p in glob.glob(args.paper_glob))
    if not paths:
        raise SystemExit(f"no paper DBs matched: {args.paper_glob}")

    crawler_path = Path(args.crawler_db)
    crawler = _ro(crawler_path) if crawler_path.is_file() else None
    try:
        summaries = [
            restate_book(p, crawler=crawler, min_liq=float(args.min_liq)) for p in paths
        ]
    finally:
        if crawler is not None:
            crawler.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(summaries), encoding="utf-8")
    print(f"wrote {out} ({len(summaries)} books)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
