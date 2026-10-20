"""Per-cell notional size multiplier switch for the C-exit shadow book.

Pre-wired only: every cell defaults to mult=1.0 and enabled=False. Nothing in
this repo turns it on. Allowed tiers 1.5 / 2 / 3, each with a single-trade
loss cap (USD). When a cell running >1.0 hits its single-trade cap, or its
realized drawdown from peak crosses DRAWDOWN_REVERT_USD, it is automatically
reverted to 1.0 (enabled=False) and the reason is recorded.

State lives in the shadow DB table ``size_mult`` only; never touches the main
paper book, .env or live gates.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

DEFAULT_MULT = 1.0
# tier -> single-trade loss cap (negative USD). A trade at or below it reverts.
TIER_SINGLE_LOSS_CAP_USD: dict[float, float] = {
    1.5: -150.0,
    2.0: -200.0,
    3.0: -300.0,
}
ALLOWED_MULTS = (DEFAULT_MULT, *TIER_SINGLE_LOSS_CAP_USD.keys())
DRAWDOWN_REVERT_USD = -200.0


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS size_mult (
            tag TEXT PRIMARY KEY,
            mult REAL NOT NULL DEFAULT 1.0,
            enabled INTEGER NOT NULL DEFAULT 0,
            last_reason TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def get_state(conn: sqlite3.Connection, tag: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT mult, enabled, last_reason FROM size_mult WHERE tag=?", (tag,)
    ).fetchone()
    if row is None:
        return {"tag": tag, "mult": DEFAULT_MULT, "enabled": False, "last_reason": None}
    return {
        "tag": tag,
        "mult": float(row[0]),
        "enabled": bool(row[1]),
        "last_reason": row[2],
    }


def set_state(
    conn: sqlite3.Connection, tag: str, *, mult: float, enabled: bool, reason: str
) -> None:
    mult = float(mult)
    if mult not in ALLOWED_MULTS:
        raise ValueError(f"mult {mult} not in {ALLOWED_MULTS}")
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO size_mult (tag, mult, enabled, last_reason, updated_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(tag) DO UPDATE SET mult=excluded.mult, enabled=excluded.enabled,
            last_reason=excluded.last_reason, updated_at=excluded.updated_at
        """,
        (tag, mult, 1 if enabled else 0, reason, _utc()),
    )


def effective_mult(conn: sqlite3.Connection, tag: str) -> float:
    st = get_state(conn, tag)
    if not st["enabled"] or st["mult"] not in TIER_SINGLE_LOSS_CAP_USD:
        return DEFAULT_MULT
    return st["mult"]


def capped_size(base_size_usd: float, mult: float, sl_pct: float | None) -> float:
    """Scale size by mult but keep worst SL loss within the tier's cap."""
    size = float(base_size_usd) * float(mult)
    cap = TIER_SINGLE_LOSS_CAP_USD.get(float(mult))
    if cap is not None and sl_pct:
        max_size = abs(cap) / (float(sl_pct) / 100.0)
        size = max(min(size, max_size), float(base_size_usd))
    return size


def cell_drawdown_usd(conn: sqlite3.Connection, tag: str) -> float:
    rows = conn.execute(
        "SELECT pnl_usd FROM positions WHERE grid_tag=? AND status='closed' "
        "ORDER BY closed_at, rowid",
        (tag,),
    ).fetchall()
    cum = peak = 0.0
    dd = 0.0
    for (p,) in rows:
        cum += float(p or 0.0)
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def after_close(conn: sqlite3.Connection, tag: str, pnl_usd: float) -> str | None:
    """Auto-revert to 1.0 if a boosted cell breaches its line. Returns reason."""
    st = get_state(conn, tag)
    if not st["enabled"] or st["mult"] == DEFAULT_MULT:
        return None
    cap = TIER_SINGLE_LOSS_CAP_USD.get(st["mult"])
    reason = None
    if cap is not None and float(pnl_usd) <= cap:
        reason = f"single_loss_cap:{pnl_usd:.2f}<= {cap}"
    else:
        dd = cell_drawdown_usd(conn, tag)
        if dd <= DRAWDOWN_REVERT_USD:
            reason = f"drawdown:{dd:.2f}<= {DRAWDOWN_REVERT_USD}"
    if reason:
        set_state(conn, tag, mult=DEFAULT_MULT, enabled=False, reason="auto_revert " + reason)
    return reason


# ---- per-cell notional cap (shadow books only; main book untouched) ----
NOTIONAL_CAP_1X_USD = float(__import__("os").environ.get("C_EXIT_NOTIONAL_CAP_1X_USD", "193"))


def notional_cap_for_mult(mult: float, cap_1x: float = NOTIONAL_CAP_1X_USD) -> float:
    """1x -> $193; boosted tiers cap = 193 / mult (1.5x≈$129, 2x≈$97, 3x≈$64)."""
    return float(cap_1x) / max(float(mult), 1.0)


def tail_worst_loss_rate(conn: sqlite3.Connection, tag: str) -> float | None:
    """Worst loss rate (|pnl|/size) over the later half of the cell's closed trades."""
    rows = conn.execute(
        "SELECT pnl_usd, size_usd FROM positions WHERE grid_tag=? AND status='closed' "
        "ORDER BY closed_at, rowid",
        (tag,),
    ).fetchall()
    tail = rows[len(rows) // 2 :]
    rates = [-(float(p) / float(s)) for p, s in tail if s and p is not None and float(p) < 0]
    return max(rates) if rates else None


def cell_notional_cap(
    conn: sqlite3.Connection,
    tag: str,
    mult: float,
    *,
    cap_1x: float = NOTIONAL_CAP_1X_USD,
    max_loss_usd: float | None = None,
) -> float:
    """min(193/mult, max_loss_usd / tail_worst_loss_rate) — second term optional."""
    cap = notional_cap_for_mult(mult, cap_1x)
    if max_loss_usd is not None:
        rate = tail_worst_loss_rate(conn, tag)
        if rate and rate > 0:
            cap = min(cap, float(max_loss_usd) / rate)
    return cap


# ---- shadow-entry labels ----
_REGIME_FILE = Path(__file__).resolve().parents[1] / "exports" / "regime" / "x_budget_regime.json"
_FALLBACK_NEW4_AT = "2026-10-20T16:40:25Z"  # 00:40:25 HKT
_FALLBACK_FRESH_AT = "2026-10-21T00:00:00Z"  # 08:00 HKT


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    t = str(v).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def load_regime_bounds(path: Path | None = None) -> tuple[datetime, datetime]:
    """(new4 effective_at, first fresh budget at) from x_budget_regime.json."""
    new4_at, fresh_at = _ts(_FALLBACK_NEW4_AT), _ts(_FALLBACK_FRESH_AT)
    try:
        doc = json.loads(Path(path or _REGIME_FILE).read_text())
        for r in doc.get("regimes") or []:
            if r.get("regime") == "new4":
                new4_at = _ts(r.get("effective_at_utc") or r.get("effective_at_hkt")) or new4_at
                fresh_at = _ts(r.get("first_fresh_budget_at_hkt")) or fresh_at
    except Exception:  # noqa: BLE001
        pass
    return new4_at, fresh_at  # type: ignore[return-value]


def regime_at(ts: Any, bounds: tuple[datetime, datetime] | None = None) -> str | None:
    """old20 < effective_at <= exhausted < 08:00 HKT fresh <= new4."""
    d = _ts(ts)
    if d is None:
        return None
    new4_at, fresh_at = bounds or load_regime_bounds()
    if d < new4_at:
        return "old20"
    if d < fresh_at:
        return "exhausted"
    return "new4"


def normalize_labels(
    raw: dict[str, Any] | None,
    ts: Any = None,
    bounds: tuple[datetime, datetime] | None = None,
) -> dict[str, Any]:
    """liq_bucket from raw_json.extras; x_budget_regime by decision ts.

    Raw label (budget_regime / x_budget_regime, 20h/4h/8h) is kept as
    x_budget_regime_raw for audit; the time split is authoritative.
    """
    ex = (raw or {}).get("extras") or {}
    if not isinstance(ex, dict):
        ex = {}
    out: dict[str, Any] = {}
    if "liq_bucket" in ex:
        out["liq_bucket"] = ex.get("liq_bucket")
    rawreg = ex.get("x_budget_regime", ex.get("budget_regime"))
    if rawreg is not None:
        out["x_budget_regime_raw"] = str(rawreg)
    reg = regime_at(ts or (raw or {}).get("ts"), bounds)
    if reg is not None:
        out["x_budget_regime"] = reg
    return out
