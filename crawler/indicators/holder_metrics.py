"""Read-only INTEL I-1 metrics: top10_share and dev_balance_delta.

Compute-on-read helpers over rows the caller already holds, or over a
SELECT against address_labels, token_holder_snapshots, and
token_transfer_edges. Nothing here writes those tables, token_indicators,
factor_candidates, or decision weights.

Empty input and rows that cannot support the ratio yield None. A missing
amount is omitted, not replaced with zero. This module does not score
effectiveness.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping, Sequence

from indicators.base import parse_iso
from store import norm_address, norm_chain

TOP_HOLDER_RANKS = 10
DEV_LABEL = "dev"

_HOLDER_COLUMNS = (
    "snapshot_id",
    "recorded_at",
    "rank",
    "holder_address",
    "amount_ui",
    "pct_of_supply",
    "total_supply_ui",
    "token_address",
)


def top10_share(rows: Sequence[Mapping[str, Any]]) -> float | None:
    """Share of supply held by ranks 1..10 in one snapshot.

    Prefers sum(amount_ui) / total_supply_ui when every ranked row has an
    amount and the snapshot has one positive supply. Otherwise sums
    pct_of_supply when every ranked row has one. Mixed or empty snapshots
    return None. The ratio is not rescaled and not clamped.
    """
    ranked = _ranked_rows(rows)
    if ranked is None:
        return None
    consistent, supply = _supply_state(ranked)
    if not consistent:
        return None
    if supply is not None and _all_amounts(ranked):
        total = 0.0
        for row in ranked:
            amount = _finite_nonneg(row.get("amount_ui"))
            if amount is None:
                return None
            total += amount
        return total / supply
    pcts: list[float] = []
    for row in ranked:
        pct = _finite_nonneg(row.get("pct_of_supply"))
        if pct is None:
            return None
        pcts.append(pct)
    return sum(pcts)


def top10_share_history(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One point per snapshot_id, oldest recorded_at first.

    A snapshot that cannot be summed stays in the list with top10_share None.
    Rows without snapshot_id are left out. Callers that need a 24h change
    still have to apply their own window; this list is only the point series.
    """
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        snapshot_id = _text(row.get("snapshot_id"))
        if snapshot_id is None:
            continue
        groups[snapshot_id].append(row)
    points: list[dict[str, Any]] = []
    for snapshot_id, group in groups.items():
        recorded_at = _earliest_stamp(group)
        points.append(
            {
                "snapshot_id": snapshot_id,
                "recorded_at": recorded_at,
                "top10_share": top10_share(group),
            }
        )
    points.sort(key=_history_sort_key)
    return points


def dev_balance_delta(
    labels: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]] | None = None,
    balances: Sequence[Mapping[str, Any]] | None = None,
    *,
    token_address: str | None = None,
) -> float | None:
    """Net change on addresses labeled dev for one token.

    Positive is net inflow to the dev cohort (accumulation hypothesis).
    Negative is net outflow (distribution hypothesis). Dev-to-dev transfers
    cancel.

    Usable transfer edges win. amount_ui NULL is skipped, not counted as
    zero. When no usable edge touches a dev address, the fallback is the
    change in observed holder-snapshot balances: last finite amount minus
    first, per address that appears at least twice. An address missing from
    the top-10 list is not treated as a zero balance. No labels, or no
    usable edges and no repeated balances, returns None.
    """
    devs = _dev_addresses(labels, token_address)
    if not devs:
        return None
    flow = _net_flow(devs, edges or (), token_address)
    if flow is not None:
        return flow
    return _balance_change(devs, balances or (), token_address)


def read_top10_share(
    conn: sqlite3.Connection,
    chain_id: str,
    token_address: str,
    *,
    snapshot_id: str | None = None,
) -> float | None:
    """Latest snapshot share, or one snapshot_id. Missing table → None."""
    rows = _select_holder_rows(conn, chain_id, token_address, snapshot_id=snapshot_id)
    if not rows:
        return None
    if snapshot_id is None:
        latest = _latest_snapshot_id(rows)
        rows = [row for row in rows if _text(row.get("snapshot_id")) == latest]
    return top10_share(rows)


def read_top10_share_history(
    conn: sqlite3.Connection,
    chain_id: str,
    token_address: str,
) -> list[dict[str, Any]]:
    """Point series for one token. Missing table → empty list."""
    rows = _select_holder_rows(conn, chain_id, token_address)
    if rows is None:
        return []
    return top10_share_history(rows)


def read_dev_balance_delta(
    conn: sqlite3.Connection,
    chain_id: str,
    token_address: str,
) -> float | None:
    """Dev net flow for one token from labels, edges, then balances."""
    labels = _select_dicts(
        conn,
        """
        SELECT address, label_type, token_mint
        FROM address_labels
        WHERE chain_id = ? AND label_type = ?
          AND (token_mint = ? OR token_mint = '')
        """,
        (norm_chain(chain_id), DEV_LABEL, norm_address(token_address)),
    )
    if not labels:
        return None
    edges = _select_dicts(
        conn,
        """
        SELECT token_address, from_address, to_address, amount_ui, recorded_at
        FROM token_transfer_edges
        WHERE chain_id = ? AND token_address = ?
        """,
        (norm_chain(chain_id), norm_address(token_address)),
    )
    balances = _select_holder_rows(conn, chain_id, token_address)
    return dev_balance_delta(
        labels,
        edges or (),
        balances or (),
        token_address=token_address,
    )


def _ranked_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]] | None:
    """Ranks 1..10 from a single snapshot, or None when the set is unusable."""
    if not rows:
        return None
    snapshot_ids: set[str | None] = set()
    ranked: list[Mapping[str, Any]] = []
    seen_ranks: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        snapshot_ids.add(_text(row.get("snapshot_id")))
        if len(snapshot_ids) > 1:
            return None
        rank = _rank(row.get("rank"))
        if rank is None:
            continue
        if rank in seen_ranks:
            return None
        seen_ranks.add(rank)
        ranked.append(row)
    if not ranked:
        return None
    return ranked


def _rank(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        rank = value
    elif isinstance(value, float) and value.is_integer():
        rank = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        rank = int(text)
    else:
        return None
    if rank < 1 or rank > TOP_HOLDER_RANKS:
        return None
    return rank


def _supply_state(rows: Sequence[Mapping[str, Any]]) -> tuple[bool, float | None]:
    """(consistent, supply). Conflicting positive supplies are not consistent."""
    found: set[float] = set()
    for row in rows:
        supply = _finite_positive(row.get("total_supply_ui"))
        if supply is not None:
            found.add(supply)
    if len(found) > 1:
        return False, None
    if len(found) == 1:
        return True, next(iter(found))
    return True, None


def _all_amounts(rows: Sequence[Mapping[str, Any]]) -> bool:
    return all(_finite_nonneg(row.get("amount_ui")) is not None for row in rows)


def _finite_nonneg(value: Any) -> float | None:
    number = _as_float(value)
    if number is None or number < 0:
        return None
    return number


def _finite_positive(value: Any) -> float | None:
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number):
        return None
    return number


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _earliest_stamp(rows: Sequence[Mapping[str, Any]]) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    fallback: str | None = None
    for row in rows:
        stamp = _text(row.get("recorded_at"))
        if stamp is None:
            continue
        if fallback is None:
            fallback = stamp
        when = parse_iso(stamp)
        if when is not None:
            parsed.append((when, stamp))
    if parsed:
        parsed.sort(key=lambda item: item[0])
        return parsed[0][1]
    return fallback


def _history_sort_key(point: Mapping[str, Any]) -> tuple[int, datetime, str]:
    stamp = _text(point.get("recorded_at"))
    when = parse_iso(stamp) if stamp else None
    snapshot_id = _text(point.get("snapshot_id")) or ""
    if when is None:
        return (1, datetime.min, snapshot_id)
    return (0, when, snapshot_id)


def _dev_addresses(
    labels: Sequence[Mapping[str, Any]],
    token_address: str | None,
) -> set[str]:
    wanted = norm_address(token_address) if token_address else None
    found: set[str] = set()
    for row in labels:
        if not isinstance(row, Mapping):
            continue
        label_type = _text(row.get("label_type"))
        if label_type is None or label_type.lower() != DEV_LABEL:
            continue
        if wanted is not None and not _label_covers_token(row.get("token_mint"), wanted):
            continue
        address = _text(row.get("address"))
        if address is None:
            continue
        found.add(norm_address(address))
    return found


def _label_covers_token(token_mint: Any, wanted: str) -> bool:
    mint = _text(token_mint)
    if mint is None:
        return True
    return norm_address(mint) == wanted


def _net_flow(
    devs: set[str],
    edges: Sequence[Mapping[str, Any]],
    token_address: str | None,
) -> float | None:
    """Inflow minus outflow. None when no usable edge touches the cohort."""
    wanted = norm_address(token_address) if token_address else None
    saw = False
    net = 0.0
    for row in edges:
        if not isinstance(row, Mapping):
            continue
        if wanted is not None and not _row_token_is(row, "token_address", wanted):
            continue
        amount = _finite_nonneg(row.get("amount_ui"))
        if amount is None:
            continue
        src = _norm_addr(row.get("from_address"))
        dst = _norm_addr(row.get("to_address"))
        touches = (src in devs) or (dst in devs)
        if not touches:
            continue
        if src in devs:
            net -= amount
        if dst in devs:
            net += amount
        saw = True
    if not saw:
        return None
    return net


def _balance_change(
    devs: set[str],
    rows: Sequence[Mapping[str, Any]],
    token_address: str | None,
) -> float | None:
    """Sum of (last − first) over devs observed with an amount at least twice."""
    wanted = norm_address(token_address) if token_address else None
    # snapshot_id → address → (amount or a NULL marker, recorded_at)
    buckets: dict[str, dict[str, list[float | None]]] = defaultdict(
        lambda: defaultdict(list)
    )
    stamps: dict[str, str | None] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if wanted is not None and not _row_token_is(row, "token_address", wanted):
            continue
        holder = _norm_addr(row.get("holder_address"))
        if holder not in devs:
            continue
        snapshot_id = _text(row.get("snapshot_id"))
        if snapshot_id is None:
            continue
        amount = _finite_nonneg(row.get("amount_ui"))
        buckets[snapshot_id][holder].append(amount)
        stamps.setdefault(snapshot_id, _text(row.get("recorded_at")))
    per_address: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
    for snapshot_id, by_holder in buckets.items():
        stamp = stamps.get(snapshot_id)
        when = parse_iso(stamp) if stamp else None
        if when is None:
            continue
        for holder, amounts in by_holder.items():
            if any(amount is None for amount in amounts):
                continue
            total = sum(amount or 0.0 for amount in amounts)
            per_address[holder].append((when, snapshot_id, total))
    cohort = 0.0
    used = False
    for observations in per_address.values():
        if len(observations) < 2:
            continue
        observations.sort(key=lambda item: (item[0], item[1]))
        cohort += observations[-1][2] - observations[0][2]
        used = True
    if not used:
        return None
    return cohort


def _row_token_is(row: Mapping[str, Any], field: str, wanted: str) -> bool:
    token = _text(row.get(field))
    if token is None:
        return False
    return norm_address(token) == wanted


def _norm_addr(value: Any) -> str:
    text = _text(value)
    if text is None:
        return ""
    return norm_address(text)


def _select_holder_rows(
    conn: sqlite3.Connection,
    chain_id: str,
    token_address: str,
    *,
    snapshot_id: str | None = None,
) -> list[dict[str, Any]] | None:
    columns = ", ".join(_HOLDER_COLUMNS)
    sql = (
        f"SELECT {columns} FROM token_holder_snapshots "
        "WHERE chain_id = ? AND token_address = ?"
    )
    params: list[Any] = [norm_chain(chain_id), norm_address(token_address)]
    if snapshot_id is not None:
        sql += " AND snapshot_id = ?"
        params.append(snapshot_id)
    return _select_dicts(conn, sql, tuple(params))


def _latest_snapshot_id(rows: Sequence[Mapping[str, Any]]) -> str | None:
    best: tuple[datetime, str] | None = None
    best_id: str | None = None
    for row in rows:
        snapshot_id = _text(row.get("snapshot_id"))
        if snapshot_id is None:
            continue
        when = parse_iso(_text(row.get("recorded_at")))
        if when is None:
            if best is None and best_id is None:
                best_id = snapshot_id
            continue
        key = (when, snapshot_id)
        if best is None or key > best:
            best = key
            best_id = snapshot_id
    return best_id


def _select_dicts(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> list[dict[str, Any]] | None:
    try:
        cur = conn.execute(sql, params)
    except sqlite3.OperationalError:
        return None
    names = [item[0] for item in cur.description or ()]
    return [dict(zip(names, row)) for row in cur.fetchall()]
