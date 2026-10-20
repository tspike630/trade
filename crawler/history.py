"""P2 pair_history snapshots — due by hot/warm/cold interval after pair upsert."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    HISTORY_COLD_INTERVAL_SEC,
    HISTORY_HOT_INTERVAL_SEC,
    HISTORY_WARM_INTERVAL_SEC,
    HOT_SEEN_WINDOW_SEC,
)
from logging_setup import get_logger

log = get_logger("history")

# R1 observation only. Does not count toward strategy validity.
# The 10-19 weekly check may cite these rates as 旁证.
# Live calibration: ~34% of hot pair_history rows in the last hour had
# liquidity_usd NULL or <=0 while price was often present. That is WARN.
# DexScreener omitting `liquidity` stays NULL — never invent a number.
HOT_LIQ_MIN_USD = 50_000.0
HOT_LIQ_WINDOW_HOURS = 1.0
HOT_LIQ_CONTEXT_HOURS = 24.0
HOT_LIQ_MISSING_WARN = 0.25  # WARN when hot missing rate is strictly greater
HOT_LIQ_MISSING_FAIL = 0.60  # FAIL when hot missing rate is strictly greater
HOT_LIQ_FAIL_MIN_N = 20  # thinner windows stay WARN even above the FAIL rate
EVIDENCE_ROLE_SIDE = "10-19旁证"


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    """Parse one present payload value. Empty, bool, or non-finite → None."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _root_optional_float(pair: dict[str, Any], *keys: str) -> float | None:
    """First present root key as a float.

    A key that exists but is empty or non-numeric is NULL and does not fall
    through to a later alias. A missing key is skipped.
    """
    for key in keys:
        if key not in pair:
            continue
        return _optional_float(pair.get(key))
    return None


def pair_history_column_sql(present: set[str]) -> str:
    """Column list for pair_history readers, including the G-5 denominator.

    `market_cap` and `fdv` are always projected. Before ALTER they read as
    NULL so a read-only open of an old database still works.
    """
    names = [
        "id",
        "chain_id",
        "pair_address",
        "token_address",
        "recorded_at",
        "price_usd",
        "volume_h24",
        "volume_h1",
        "liquidity_usd",
        "txns_h24",
        "tier",
    ]
    for extra in ("market_cap", "fdv"):
        if extra in present:
            names.append(extra)
        else:
            names.append(f"NULL AS {extra}")
    return ", ".join(names)


def history_tier_for_token(
    store: Any, chain_id: str, token_address: str | None, pair_address: str | None = None
) -> str:
    """Map token recency → hot|warm|cold for snapshot cadence.

    Open paper positions are pinned hot (PIN_OPEN_POSITIONS_HOT=0 disables).
    """
    from position_pin import is_pinned

    if is_pinned(chain_id, token_address, pair_address):
        return "hot"
    if not token_address:
        return "warm"
    row = store._conn.execute(
        """
        SELECT last_seen_at, updated_at, has_pairs FROM tokens
        WHERE chain_id = ? AND token_address = ?
        """,
        (chain_id, token_address),
    ).fetchone()
    if not row:
        return "warm"
    seen = _parse_iso(row["last_seen_at"] or row["updated_at"])
    if seen is None:
        return "cold"
    age = (datetime.now(timezone.utc) - seen).total_seconds()
    if age <= HOT_SEEN_WINDOW_SEC and row["has_pairs"] == 1:
        return "hot"
    if age <= HOT_SEEN_WINDOW_SEC * 6:
        return "warm"
    return "cold"


def interval_for_tier(tier: str) -> int:
    if tier == "hot":
        return HISTORY_HOT_INTERVAL_SEC
    if tier == "cold":
        return HISTORY_COLD_INTERVAL_SEC
    return HISTORY_WARM_INTERVAL_SEC


def history_due(store: Any, chain_id: str, pair_address: str, tier: str) -> bool:
    last = store.get_pair_last_history_at(chain_id, pair_address)
    if not last:
        return True
    ts = _parse_iso(last)
    if ts is None:
        return True
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= interval_for_tier(tier)


def extract_metrics(pair: dict[str, Any]) -> dict[str, Any]:
    """Copy numeric fields from a pair payload.

    If DexScreener omits `liquidity` (or `liquidity.usd`), `liquidity_usd` is
    None. Callers store that NULL. Do not invent a liquidity number — decision
    fail-closes the buy as no_liquidity.

    Root `marketCap` (alias `market_cap`) and `fdv` are copied the same way.
    Omitted, empty, or non-numeric values stay None. `fdv` is never copied
    into `market_cap`. This does not compute a turnover ratio.
    """
    vol = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    liq = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
    h24 = txns.get("h24") if isinstance(txns.get("h24"), dict) else {}
    buys = h24.get("buys")
    sells = h24.get("sells")
    txns_h24 = None
    try:
        if buys is not None or sells is not None:
            txns_h24 = int(buys or 0) + int(sells or 0)
    except (TypeError, ValueError):
        txns_h24 = None
    return {
        "price_usd": _num(pair.get("priceUsd")),
        "volume_h24": _num(vol.get("h24")),
        "volume_h1": _num(vol.get("h1")),
        "liquidity_usd": _num(liq.get("usd")),
        "txns_h24": txns_h24,
        "market_cap": _root_optional_float(pair, "marketCap", "market_cap"),
        "fdv": _root_optional_float(pair, "fdv"),
    }


def maybe_snapshot_pair(
    store: Any,
    chain_id: str,
    pair_address: str,
    pair: dict[str, Any],
    *,
    token_address: str | None = None,
) -> bool:
    """
    Insert a history row if the tier interval has elapsed.
    Returns True if a snapshot was written.
    """
    tok = token_address or (pair.get("baseToken") or {}).get("address")
    if tok:
        from store import norm_address

        tok = norm_address(tok)
    tier = history_tier_for_token(store, chain_id, tok, pair_address)
    if not history_due(store, chain_id, pair_address, tier):
        return False
    m = extract_metrics(pair)
    store.insert_pair_history(
        chain_id,
        pair_address,
        token_address=tok,
        price_usd=m["price_usd"],
        volume_h24=m["volume_h24"],
        volume_h1=m["volume_h1"],
        liquidity_usd=m["liquidity_usd"],
        txns_h24=m["txns_h24"],
        market_cap=m["market_cap"],
        fdv=m["fdv"],
        tier=tier,
    )
    log.debug(
        "pair_history snap chain=%s pair=%s tier=%s price=%s",
        chain_id,
        pair_address[:12],
        tier,
        m["price_usd"],
    )
    return True


def utc_hours_ago_iso(hours: float, *, now: datetime | None = None) -> str:
    """UTC timestamp `hours` before now, matching pair_history.recorded_at."""
    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return (anchor - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rate(n: int, part: int) -> float | None:
    if n <= 0:
        return None
    return round(part / n, 4)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def liquidity_coverage(
    conn: Any,
    *,
    since_iso: str,
    tier: str | None = "hot",
    min_liq_usd: float = HOT_LIQ_MIN_USD,
) -> dict[str, Any]:
    """Bucket recent pair_history rows by stored liquidity_usd.

    missing = NULL or <=0 (no_liquidity, plus non-positive stored values).
    below_min = 0 < liquidity < min_liq_usd.
    ge_min = liquidity >= min_liq_usd.
    The three counts partition `n`. This does not read or fill raw payloads.
    """
    where = "recorded_at >= ?"
    params: list[Any] = [float(min_liq_usd), float(min_liq_usd), since_iso]
    if tier is not None:
        where += " AND lower(COALESCE(tier, '')) = ?"
        params.append(tier.strip().lower())
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS n,
            SUM(CASE
                WHEN liquidity_usd IS NULL OR liquidity_usd <= 0 THEN 1 ELSE 0
            END) AS missing_n,
            SUM(CASE
                WHEN liquidity_usd > 0 AND liquidity_usd < ? THEN 1 ELSE 0
            END) AS below_min_n,
            SUM(CASE
                WHEN liquidity_usd >= ? THEN 1 ELSE 0
            END) AS ge_min_n,
            SUM(CASE
                WHEN price_usd IS NOT NULL AND price_usd > 0 THEN 1 ELSE 0
            END) AS price_present_n
        FROM pair_history
        WHERE {where}
        """,
        params,
    ).fetchone()
    n = _as_int(row["n"] if row is not None else 0)
    missing_n = _as_int(row["missing_n"] if row is not None else 0)
    below_n = _as_int(row["below_min_n"] if row is not None else 0)
    ge_n = _as_int(row["ge_min_n"] if row is not None else 0)
    price_n = _as_int(row["price_present_n"] if row is not None else 0)
    return {
        "tier": tier,
        "since_iso": since_iso,
        "min_liq_usd": float(min_liq_usd),
        "n": n,
        "missing_n": missing_n,
        "missing_rate": _rate(n, missing_n),
        "below_min_n": below_n,
        "below_min_rate": _rate(n, below_n),
        "ge_min_n": ge_n,
        "ge_min_rate": _rate(n, ge_n),
        "price_present_n": price_n,
        "price_present_rate": _rate(n, price_n),
        "counts_toward_strategy_validity": False,
        "evidence_role": EVIDENCE_ROLE_SIDE,
    }


def market_cap_null_coverage(
    conn: Any,
    *,
    since_iso: str,
    tier: str | None = "hot",
) -> dict[str, Any]:
    """NULL rates for pair_history.market_cap / fdv. Observe only.

    A stored 0 counts as present. Missing columns return available=False so a
    read-only open before ALTER does not fail the liquidity check. Does not
    invent values and does not count toward strategy validity.
    """
    flags: dict[str, Any] = {
        "available": False,
        "tier": tier,
        "since_iso": since_iso,
        "counts_toward_strategy_validity": False,
        "evidence_role": EVIDENCE_ROLE_SIDE,
    }
    try:
        info = conn.execute("PRAGMA table_info(pair_history)").fetchall()
        names: set[str] = set()
        for row in info:
            try:
                names.add(str(row["name"]))
            except (TypeError, KeyError, IndexError):
                names.add(str(row[1]))
        if "market_cap" not in names or "fdv" not in names:
            return {**flags, "reason": "columns_missing"}
        where = "recorded_at >= ?"
        params: list[Any] = [since_iso]
        if tier is not None:
            where += " AND lower(COALESCE(tier, '')) = ?"
            params.append(tier.strip().lower())
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN market_cap IS NULL THEN 1 ELSE 0 END) AS market_cap_null_n,
                SUM(CASE WHEN fdv IS NULL THEN 1 ELSE 0 END) AS fdv_null_n,
                SUM(CASE
                    WHEN market_cap IS NULL AND fdv IS NULL THEN 1 ELSE 0
                END) AS both_null_n
            FROM pair_history
            WHERE {where}
            """,
            params,
        ).fetchone()
    except sqlite3.Error as exc:
        return {**flags, "reason": "query_failed", "error": str(exc)}
    n = _as_int(row["n"] if row is not None else 0)
    market_cap_null_n = _as_int(row["market_cap_null_n"] if row is not None else 0)
    fdv_null_n = _as_int(row["fdv_null_n"] if row is not None else 0)
    both_null_n = _as_int(row["both_null_n"] if row is not None else 0)
    return {
        **flags,
        "available": True,
        "n": n,
        "market_cap_null_n": market_cap_null_n,
        "market_cap_null_rate": _rate(n, market_cap_null_n),
        "market_cap_present_n": n - market_cap_null_n,
        "fdv_null_n": fdv_null_n,
        "fdv_null_rate": _rate(n, fdv_null_n),
        "fdv_present_n": n - fdv_null_n,
        "both_null_n": both_null_n,
        "both_null_rate": _rate(n, both_null_n),
    }


def hot_liq_coverage_level(n: int, missing_rate: float | None) -> str:
    """WARN above 25% missing; FAIL above 60% only when n is large enough.

    Empty or thin windows stay WARN so a quiet hour does not fail daily QA.
    ~34% missing (the live hot-tier observation) is WARN.
    """
    if n <= 0 or missing_rate is None:
        return "WARN"
    if missing_rate > HOT_LIQ_MISSING_FAIL and n >= HOT_LIQ_FAIL_MIN_N:
        return "FAIL"
    if missing_rate > HOT_LIQ_MISSING_WARN:
        return "WARN"
    return "PASS"


def format_rate_pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{100.0 * float(rate):.1f}%"


def format_hot_liq_operator_line(
    coverage: dict[str, Any] | None,
    pending_enrich: int | None,
) -> str:
    """One line for the hourly brief, placed next to health."""
    pending = "n/a" if pending_enrich is None else str(int(pending_enrich))
    if not isinstance(coverage, dict) or coverage.get("error") or "n" not in coverage:
        return (
            f"Hot liq 1h (旁证, 不计有效性): unavailable | pending_enrich={pending}"
        )
    return (
        "Hot liq 1h (旁证, 不计有效性): "
        f"missing={format_rate_pct(coverage.get('missing_rate'))} "
        f"n={int(coverage.get('n') or 0)} "
        f"<50k={format_rate_pct(coverage.get('below_min_rate'))} "
        f">=50k={format_rate_pct(coverage.get('ge_min_rate'))} "
        f"| pending_enrich={pending}"
    )
