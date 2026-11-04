#!/usr/bin/env python3
"""Read-only restatement of paper_eq_*.db under TP-clamp + profit-side dead-pool.

Does NOT write back into the paper DBs. Writes a markdown report.

v3 rules (2026-11-04): no forward-looking liquidity; alerts first, then last
sample at/before closed_at; evidence tier recorded per fill.
  - TP clamp: take_profit exit > entry*(1+tp%/100) → fill at limit.
  - Dead pool: close-time liq < min_liq AND exit > entry (profit only) →
    flatten at entry. Loss-side low-liq keeps original pnl.
  - liq is None → never dead-pool.

Usage:

  python3 scripts/paper_tp_clamp_restatement.py \\
    --paper-glob '/workspace/dexscreener-crawler/paper_eq_*.db' \\
    --crawler-db /workspace/dexscreener-crawler/dexscreener.db \\
    --out exports/reports/paper_tp_clamp_restatement_v2_2026-11-04.md
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
    kind: str  # tp_clamp | dead_pool_profit | dead_pool_loss_kept
    close_liq: float | None = None
    liq_source: str | None = None
    closed_at: str | None = None
    tp_pnl: float = 0.0


@dataclass
class BookSummary:
    book: str
    n_closed: int = 0
    online_net: float = 0.0
    tp_only_net: float = 0.0
    full_net: float = 0.0
    n_tp: int = 0
    n_tp_overshoot: int = 0
    n_dead_profit: int = 0
    n_dead_profit_alert: int = 0
    n_dead_profit_sample: int = 0
    dead_profit_alert_usd: float = 0.0
    dead_profit_sample_usd: float = 0.0
    n_dead_loss_kept: int = 0
    start_cash: float = DEFAULT_START_CASH
    outlier: bool = False
    clamped_trades: list[TradeFix] = field(default_factory=list)
    dead_profit_trades: list[TradeFix] = field(default_factory=list)
    dead_loss_kept_trades: list[TradeFix] = field(default_factory=list)


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _start_cash(con: sqlite3.Connection) -> float:
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
        if abs(inferred - DEFAULT_START_CASH) < 1.0:
            return DEFAULT_START_CASH
        return inferred
    return DEFAULT_START_CASH


def _shift_iso(ts: str, seconds: int) -> str:
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


ALERT_GRACE_S = 60


def _liq_at_close(
    crawler: sqlite3.Connection | None,
    chain_id: str,
    pair: str,
    closed_at: str | None,
    cache: dict[tuple[str, str, str], tuple[float | None, str | None]] | None = None,
) -> tuple[float | None, str | None]:
    """Close-time liquidity and the evidence tier behind it.

    Returns ``(liq, source)`` where source is ``"alert"`` (a
    ``liquidity_drop`` alert at the rug instant), ``"sample"`` (last
    ``pair_history`` sample at or before the close) or ``None`` (no evidence).
    """
    if crawler is None or not closed_at or not pair:
        return None, None
    key = (str(chain_id or "solana"), pair.lower(), str(closed_at))
    if cache is not None and key in cache:
        return cache[key]

    val: float | None = None
    source: str | None = None
    # Look BACK only. The one tolerance is an alert that lands seconds after
    # ``closed_at`` because the collector wrote it late: it reports the pool
    # state at the rug instant, not a later state, so a small grace keeps the
    # event from being missed. Any *sample* after ``closed_at`` is lookahead
    # and is never used (v2 used a MIN over [close-120s, close+20min], which
    # booked pools that only died later).
    lo = _shift_iso(closed_at, -600)
    hi = _shift_iso(closed_at, ALERT_GRACE_S)
    try:
        arow = crawler.execute(
            """
            SELECT payload_json, message FROM alerts
            WHERE type = 'liquidity_drop'
              AND lower(pair_address) = lower(?)
              AND created_at BETWEEN ? AND ?
            ORDER BY created_at ASC LIMIT 1
            """,
            (pair, lo, hi),
        ).fetchone()
    except sqlite3.Error:
        arow = None
    if arow is not None:
        payload: dict = {}
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
        if val is not None:
            source = "alert"

    if val is None:
        # Last known liquidity at or before the close.
        row = crawler.execute(
            """
            SELECT liquidity_usd AS liq FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
              AND recorded_at <= ?
              AND liquidity_usd IS NOT NULL
            ORDER BY recorded_at DESC LIMIT 1
            """,
            (key[0], pair, closed_at),
        ).fetchone()
        if row is not None and row["liq"] is not None:
            try:
                val = float(row["liq"])
                source = "sample"
            except (TypeError, ValueError):
                val = None

    if cache is not None:
        cache[key] = (val, source)
    return val, source


def _is_tp_reason(reason: str | None) -> bool:
    if not reason:
        return False
    return str(reason).split(":", 1)[0] == "take_profit"


def restate_book(
    path: Path,
    *,
    crawler: sqlite3.Connection | None,
    min_liq: float = DEFAULT_MIN_LIQ,
) -> BookSummary:
    name = path.name
    summary = BookSummary(book=name, outlier=name in OUTLIER_BOOKS)
    con = _ro(path)
    liq_cache: dict[tuple[str, str, str], tuple[float | None, str | None]] = {}
    try:
        summary.start_cash = _start_cash(con)
        rows = con.execute(
            """
            SELECT id, chain_id, pair_address, entry_price, exit_price, qty,
                   size_usd, tp_pct, pnl_usd, exit_reason, closed_at
            FROM positions WHERE status = 'closed'
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

            close_liq, close_liq_source = _liq_at_close(
                crawler,
                str(row["chain_id"] or "solana"),
                str(row["pair_address"] or ""),
                row["closed_at"],
                cache=liq_cache,
            )

            # --- Scenario A: TP clamp only ---
            tp_pnl = orig_pnl
            if _is_tp_reason(reason) and tp_f is not None and entry > 0:
                limit = entry * (1.0 + tp_f / 100.0)
                if exit_px > limit + EPS:
                    summary.n_tp_overshoot += 1
                    tp_pnl = qty * limit - size
                    summary.clamped_trades.append(
                        TradeFix(
                            book=name,
                            position_id=str(row["id"]),
                            pair=str(row["pair_address"] or ""),
                            entry=entry,
                            exit=exit_px,
                            move_pct=move_pct,
                            tp_pct=tp_f,
                            orig_pnl=orig_pnl,
                            new_pnl=tp_pnl,
                            kind="tp_clamp",
                            close_liq=close_liq,
                            liq_source=close_liq_source,
                            closed_at=row["closed_at"],
                        )
                    )
            summary.tp_only_net += tp_pnl

            # --- Scenario B: TP clamp + profit-side dead pool ---
            # Dead pool checked on original exit (naive fill proxy). If profit
            # and low liq → flatten. Else keep tp_pnl (already TP-clamped).
            full_pnl = tp_pnl
            low_liq = (
                close_liq is not None
                and min_liq > 0
                and float(close_liq) < float(min_liq)
            )
            if low_liq and entry > 0 and exit_px > entry + EPS:
                # Profit-side dead pool: wipe to ~0 (overrides TP clamp).
                full_pnl = qty * entry - size  # ≈0
                summary.n_dead_profit += 1
                if close_liq_source == "alert":
                    summary.n_dead_profit_alert += 1
                    summary.dead_profit_alert_usd += tp_pnl - full_pnl
                else:
                    summary.n_dead_profit_sample += 1
                    summary.dead_profit_sample_usd += tp_pnl - full_pnl
                summary.dead_profit_trades.append(
                    TradeFix(
                        book=name,
                        position_id=str(row["id"]),
                        pair=str(row["pair_address"] or ""),
                        entry=entry,
                        exit=exit_px,
                        move_pct=move_pct,
                        tp_pct=tp_f,
                        orig_pnl=orig_pnl,
                        new_pnl=full_pnl,
                        kind="dead_pool_profit",
                        close_liq=close_liq,
                        liq_source=close_liq_source,
                        closed_at=row["closed_at"],
                        tp_pnl=tp_pnl,
                    )
                )
                # If this trade was also counted as TP overshoot, remove from
                # clamped list attribution for "dead overrides" clarity — keep
                # both lists; full_net uses dead. TP-only net already counted.
            elif low_liq and entry > 0 and exit_px <= entry + EPS:
                # Loss-side: keep original (or tp_pnl which equals orig for non-TP).
                summary.n_dead_loss_kept += 1
                summary.dead_loss_kept_trades.append(
                    TradeFix(
                        book=name,
                        position_id=str(row["id"]),
                        pair=str(row["pair_address"] or ""),
                        entry=entry,
                        exit=exit_px,
                        move_pct=move_pct,
                        tp_pct=tp_f,
                        orig_pnl=orig_pnl,
                        new_pnl=orig_pnl,  # kept
                        kind="dead_pool_loss_kept",
                        close_liq=close_liq,
                        liq_source=close_liq_source,
                        closed_at=row["closed_at"],
                    )
                )
            summary.full_net += full_pnl
    finally:
        con.close()
    return summary


def _fmt_money(x: float) -> str:
    return f"{x:,.2f}"


def _fmt_pct(x: float) -> str:
    if not math.isfinite(x):
        return "n/a"
    return f"{x:.2f}%"


def _pct(net: float, start: float) -> float:
    if start <= 0:
        return float("nan")
    return 100.0 * net / start


def render(summaries: list[BookSummary]) -> str:
    main = [s for s in summaries if not s.outlier]
    outliers = [s for s in summaries if s.outlier]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = []
    lines.append("# Paper TP clamp + profit-side dead-pool restatement v3 (2026-11-04)")
    lines.append("")
    lines.append(f"Generated at {now} (UTC). **Read-only** — source `paper_eq_*.db` untouched.")
    lines.append("")
    lines.append("## Notation")
    lines.append("")
    lines.append("`positions.exit_price` is **already slipped** (`mid_raw*(1-slip)`), so the")
    lines.append("shipped rule is `fill = min(exit_price, entry*(1+tp%/100))` — order A.")
    lines.append("Multiplying `exit_price` by `0.99` again applies slippage twice (order B,")
    lines.append("not the code). Any total below is a **snapshot at the timestamp above**;")
    lines.append("live books keep closing, so re-running will not reproduce it exactly.")
    lines.append("")
    lines.append("## Rules (v3)")
    lines.append("")
    lines.append("1. **TP clamp**: `take_profit` with `exit > entry*(1+tp%/100)` → fill at limit.")
    lines.append("2. **Dead pool (profit only)**: close-time `liq < 1000` **and** `exit > entry` → flatten at entry (pnl≈0).")
    lines.append("3. **Dead pool loss kept**: `liq < 1000` but `exit <= entry` → **keep original loss** (do not beautify).")
    lines.append("4. `liq is None` → never dead-pool.")
    lines.append("5. OUTLIER books excluded from main totals.")
    lines.append("6. **No forward-looking liquidity (v3 fix).** Close-time liquidity never uses")
    lines.append("   a sample after `closed_at`. Evidence tier per fill:")
    lines.append("   - `alert` — a `liquidity_drop` alert within `[-600s, +60s]` of the close")
    lines.append("     (the +60s grace only covers the collector writing late; it reports the")
    lines.append("     rug instant).")
    lines.append("   - `sample` — last `pair_history` sample at or before `closed_at` is `< min_liq`.")
    lines.append("   - none — no evidence, not judged a dead pool.")
    lines.append("   v2 used a MIN over `[close-120s, close+20min]`, which banked pools that")
    lines.append("   only died later; the `sample` tier is the corrected form of the same idea.")
    lines.append("7. **SL is never clamped.** TP is a resting limit sell (fills at the limit);")
    lines.append("   SL is a stop market and fills at the gap. Clamping SL would fake the left")
    lines.append("   tail. Any 'symmetric execution' figure is an optimistic bound.")
    lines.append("")

    # Headline totals
    tot_on = sum(s.online_net for s in main)
    tot_tp = sum(s.tp_only_net for s in main)
    tot_full = sum(s.full_net for s in main)
    lines.append("## Headline — 主表全库（剔 OUTLIER）三组净利")
    lines.append("")
    lines.append(f"| 口径 | 净利 USD | vs 现网 |")
    lines.append(f"|---|---:|---:|")
    lines.append(f"| 现网原值 | {_fmt_money(tot_on)} | — |")
    lines.append(f"| 只做 TP 夹价 | {_fmt_money(tot_tp)} | {_fmt_money(tot_tp - tot_on)} |")
    lines.append(f"| TP 夹价 + 修正后死池（仅浮盈） | {_fmt_money(tot_full)} | {_fmt_money(tot_full - tot_on)} |")
    lines.append("")
    n_alert = sum(s.n_dead_profit_alert for s in main)
    n_sample = sum(s.n_dead_profit_sample for s in main)
    dead_effect = tot_tp - tot_full
    alert_effect = sum(s.dead_profit_alert_usd for s in main)
    lines.append("### Dead-pool evidence split")
    lines.append("")
    lines.append(
        f"Dead-pool effect is **${_fmt_money(dead_effect)}** in total "
        f"({n_alert + n_sample} profit-side fills): **${_fmt_money(alert_effect)}** from "
        f"{n_alert} `alert`-backed fills and "
        f"**${_fmt_money(dead_effect - alert_effect)}** from {n_sample} `sample`-backed fills."
    )
    lines.append(
        "The `alert` tier is the **confirmed floor**; the `sample` tier is the corrected "
        "replacement for v2's forward-looking window, not an addition."
    )
    lines.append("")

    # Main table
    lines.append("## Main books")
    lines.append("")
    lines.append(
        "| 账面 | n_closed | 现网 | 只TP夹 | TP+死池 | TP差额 | 死池浮盈抵 | TP笔 | 超阈值 | 死池浮盈 | 死池浮亏保持 |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s in sorted(main, key=lambda x: x.book):
        dead_delta = s.full_net - s.tp_only_net
        lines.append(
            "| {b} | {n} | {on} | {tp} | {fl} | {td} | {dd} | {ntp} | {ov} | {dp} | {dk} |".format(
                b=s.book.replace("paper_eq_", "").replace(".db", ""),
                n=s.n_closed,
                on=_fmt_money(s.online_net),
                tp=_fmt_money(s.tp_only_net),
                fl=_fmt_money(s.full_net),
                td=_fmt_money(s.tp_only_net - s.online_net),
                dd=_fmt_money(dead_delta),
                ntp=s.n_tp,
                ov=s.n_tp_overshoot,
                dp=s.n_dead_profit,
                dk=s.n_dead_loss_kept,
            )
        )
    lines.append(
        "| **TOTAL** |  | {on} | {tp} | {fl} | {td} | {dd} | {ntp} | {ov} | {dp} | {dk} |".format(
            on=_fmt_money(tot_on),
            tp=_fmt_money(tot_tp),
            fl=_fmt_money(tot_full),
            td=_fmt_money(tot_tp - tot_on),
            dd=_fmt_money(tot_full - tot_tp),
            ntp=sum(s.n_tp for s in main),
            ov=sum(s.n_tp_overshoot for s in main),
            dp=sum(s.n_dead_profit for s in main),
            dk=sum(s.n_dead_loss_kept for s in main),
        )
    )
    lines.append("")

    boll = next((s for s in main if "tech_boll_fade" in s.book), None)
    if boll:
        lines.append("### Highlight: `tech_boll_fade`")
        lines.append("")
        lines.append(
            f"- 现网 ${_fmt_money(boll.online_net)} → 只TP ${_fmt_money(boll.tp_only_net)} "
            f"→ TP+死池 ${_fmt_money(boll.full_net)} "
            f"（死池浮盈笔 {boll.n_dead_profit}；预期与只TP相同若无死池）"
        )
        lines.append(
            f"- 收益率：现网 {_fmt_pct(_pct(boll.online_net, boll.start_cash))} / "
            f"只TP {_fmt_pct(_pct(boll.tp_only_net, boll.start_cash))} / "
            f"全量 {_fmt_pct(_pct(boll.full_net, boll.start_cash))}"
        )
        lines.append("")

    # A) TP clamps
    lines.append("## A) TP 超阈值被夹的具体笔")
    lines.append("")
    lines.append("| book | position_id | pair | entry | exit | 移动% | tp% | 原pnl | 夹后pnl |")
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
    tp_over = sum(t.orig_pnl - t.new_pnl for t in clamps)
    lines.append("")
    lines.append(f"共 {len(clamps)} 笔；TP 夹价合计多记 ${_fmt_money(tp_over)}。")
    lines.append("")

    # B1) dead profit wiped
    lines.append("## B1) 死池浮盈被抵（修正后规则生效）")
    lines.append("")
    lines.append("| book | position_id | pair | 证据档 | entry | exit | 移动% | close_liq | 原pnl | 夹后pnl |")
    lines.append("|---|---|---|---|---|---:|---:|---:|---:|---:|")
    dprof = [t for s in main for t in s.dead_profit_trades]
    dprof.sort(key=lambda t: abs(t.orig_pnl), reverse=True)
    for t in dprof:
        lines.append(
            "| {b} | `{pid}` | `{pair}` | {src} | {en:.8g} | {ex:.8g} | {mv:.2f} | {liq} | {op} | {np} |".format(
                b=t.book.replace("paper_eq_", "").replace(".db", ""),
                pid=t.position_id[:8],
                src=t.liq_source or "none",
                pair=(t.pair[:12] + "…") if len(t.pair) > 12 else t.pair,
                en=t.entry,
                ex=t.exit,
                mv=t.move_pct,
                liq=f"{t.close_liq:.4g}" if t.close_liq is not None else "n/a",
                op=_fmt_money(t.orig_pnl),
                np=_fmt_money(t.new_pnl),
            )
        )
    if not dprof:
        lines.append("| (none) |  |  |  |  |  |  |  |  |  |")
    gross = sum(t.orig_pnl - t.new_pnl for t in dprof)
    marginal = sum(t.tp_pnl - t.new_pnl for t in dprof)
    m_alert = sum(t.tp_pnl - t.new_pnl for t in dprof if t.liq_source == "alert")
    m_sample = marginal - m_alert
    n_alert_b1 = sum(1 for t in dprof if t.liq_source == "alert")
    lines.append("")
    lines.append(
        f"共 {len(dprof)} 笔。**原值 → 最终**毛差 ${_fmt_money(gross)}，"
        f"其中 TP 夹价先吃掉 ${_fmt_money(gross - marginal)}，"
        f"**死池规则净效应 ${_fmt_money(marginal)}**（这才是与「只做 TP 夹价」那一档的差）。"
    )
    lines.append("")
    lines.append(
        f"死池净效应按证据档拆：`alert` {n_alert_b1} 笔 ${_fmt_money(m_alert)}、"
        f"`sample` {len(dprof) - n_alert_b1} 笔 ${_fmt_money(m_sample)}。"
        f"后者是 v2 前视窗口的修正替代、不是追加项；两档合计须等于 Headline 里的死池效应，"
        f"否则说明还有第 `none` 档被算了进来。"
    )
    lines.append("")

    # B2) dead loss kept
    lines.append("## B2) 死池候选但浮亏保持原样（v1 会错夹、v2 不夹）")
    lines.append("")
    lines.append("| book | position_id | pair | entry | exit | 移动% | close_liq | 原pnl（保持） |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|")
    dloss = [t for s in main for t in s.dead_loss_kept_trades]
    dloss.sort(key=lambda t: t.orig_pnl)  # most negative first
    for t in dloss:
        lines.append(
            "| {b} | `{pid}` | `{pair}` | {en:.8g} | {ex:.8g} | {mv:.2f} | {liq} | {op} |".format(
                b=t.book.replace("paper_eq_", "").replace(".db", ""),
                pid=t.position_id[:8],
                pair=(t.pair[:12] + "…") if len(t.pair) > 12 else t.pair,
                en=t.entry,
                ex=t.exit,
                mv=t.move_pct,
                liq=f"{t.close_liq:.4g}" if t.close_liq is not None else "n/a",
                op=_fmt_money(t.orig_pnl),
            )
        )
    if not dloss:
        lines.append("| (none) |  |  |  |  |  |  |  |")
    loss_kept = sum(t.orig_pnl for t in dloss)
    lines.append("")
    lines.append(
        f"共 {len(dloss)} 笔；因「保持原样」重新计入的亏损合计 ${_fmt_money(loss_kept)} "
        f"（相对 v1 把它们夹到 0 而言，v2 少美化 ${_fmt_money(-loss_kept)}）。"
    )
    lines.append("")

    lines.append("## OUTLIER books（不进主表）")
    lines.append("")
    lines.append("| 账面 | n_closed | 现网 | 只TP | TP+死池 | TP超阈值 | 死池浮盈 | 死池浮亏保持 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for s in outliers:
        lines.append(
            "| {b} | {n} | {on} | {tp} | {fl} | {ov} | {dp} | {dk} |".format(
                b=s.book,
                n=s.n_closed,
                on=_fmt_money(s.online_net),
                tp=_fmt_money(s.tp_only_net),
                fl=_fmt_money(s.full_net),
                ov=s.n_tp_overshoot,
                dp=s.n_dead_profit,
                dk=s.n_dead_loss_kept,
            )
        )
    if not outliers:
        lines.append("| (none) |  |  |  |  |  |  |  |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paper-glob", default="paper_eq_*.db")
    ap.add_argument("--crawler-db", default="dexscreener.db")
    ap.add_argument(
        "--out",
        default="exports/reports/paper_tp_clamp_restatement_v2_2026-11-04.md",
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
