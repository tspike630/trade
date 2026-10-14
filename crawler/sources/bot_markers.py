"""Best-effort bot markers for INTEL_LOOP I-3, collection side only.

Reads a getTransaction the crawler already fetched. No extra RPC. Missing
fields stay NULL and do not become a label. There is no "not a bot" label.

Patterns, all fail-closed:

- ``same_slot_repeat``: at least two distinct signatures for the same wallet
  and token in one slot. ``blockTime`` is seconds, so a millisecond gap is
  never invented. A shared slot is the sub-second burst the chain actually
  shows. No slot means this pattern does not fire.
- ``fixed_interval``: at least three points whose ``block_time`` deltas are
  exactly equal and positive, and the run fits in the last hour. Unequal
  gaps, fewer points, or a missing ``block_time`` do not fire. ``recorded_at``
  is ingest time and is not used as the clock.
- ``priority_fee``: the last top-level ``SetComputeUnitPrice`` is at least
  ``ABNORMAL_CU_PRICE_MICROLAMPORTS`` (1 lamport per CU). The runtime uses
  the last top-level compute-budget instruction. A missing or unparsed
  instruction does not fire. ``meta.fee`` is not turned into a bid.

These labels do not skip orders. Execution still owns ``bot_trade_share``.
"""

from __future__ import annotations

from typing import Any

from store import norm_address

BOT_SUSPECT_LABEL = "bot_suspect"
BOT_OBS_SOURCE = "helius:getTransaction"
BOT_LABEL_SOURCE_PREFIX = "helius:bot_suspect"

REASON_SAME_SLOT = "same_slot_repeat"
REASON_FIXED_INTERVAL = "fixed_interval"
REASON_PRIORITY_FEE = "priority_fee"

# 1 lamport per compute unit. Below this, or absent, is not an abnormal bid.
ABNORMAL_CU_PRICE_MICROLAMPORTS = 1_000_000
SAME_SLOT_MIN_SIGS = 2
FIXED_INTERVAL_MIN_POINTS = 3
PATTERN_WINDOW_SEC = 3600
BOT_OBSERVATION_CAP = 50

_COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
_CU_PRICE_TYPE = "setcomputeunitprice"
_CU_LIMIT_TYPE = "setcomputeunitlimit"


def _as_int(value: Any) -> int | None:
    """Non-negative int, or None. Bools and floats are not coerced."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def _fee_payer(tx: dict[str, Any]) -> str | None:
    message = (tx.get("transaction") or {}).get("message") if isinstance(tx, dict) else None
    if not isinstance(message, dict):
        return None
    keys = message.get("accountKeys")
    if not isinstance(keys, list) or not keys:
        return None
    first = keys[0]
    if isinstance(first, str) and first.strip():
        return norm_address(first)
    if isinstance(first, dict):
        if first.get("signer") is False:
            return None
        pubkey = first.get("pubkey")
        if isinstance(pubkey, str) and pubkey.strip():
            return norm_address(pubkey)
    return None


def _outer_instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    message = (tx.get("transaction") or {}).get("message") if isinstance(tx, dict) else None
    if not isinstance(message, dict):
        return []
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        return []
    return [ix for ix in instructions if isinstance(ix, dict)]


def _compute_budget(tx: dict[str, Any]) -> tuple[int | None, int | None]:
    """Last parsable top-level compute-budget price and limit.

    An unparsable last price instruction clears the price. Inner instructions
    are ignored: compute budget is applied from the top-level message.
    """
    price: int | None = None
    limit: int | None = None
    for ix in _outer_instructions(tx):
        program = ix.get("programId")
        if not isinstance(program, str) or norm_address(program) != norm_address(_COMPUTE_BUDGET_PROGRAM):
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        kind = parsed.get("type")
        info = parsed.get("info")
        if not isinstance(kind, str) or not isinstance(info, dict):
            continue
        if kind.strip().lower() == _CU_PRICE_TYPE:
            price = _as_int(info.get("microLamports"))
        elif kind.strip().lower() == _CU_LIMIT_TYPE:
            limit = _as_int(info.get("units"))
    return price, limit


def _balance_amounts(balances: Any) -> dict[tuple[str, str], int | None]:
    """(owner, mint) → raw amount. None means the amount was present but unusable.

    Two accounts for the same owner and mint are summed. One bad amount poisons
    the pair so a partial sum is not stored as a fill.
    """
    found: dict[tuple[str, str], int | None] = {}
    if not isinstance(balances, list):
        return found
    for entry in balances:
        if not isinstance(entry, dict):
            continue
        owner = entry.get("owner")
        mint = entry.get("mint")
        if not isinstance(owner, str) or not isinstance(mint, str):
            continue
        owner_n = norm_address(owner)
        mint_n = norm_address(mint)
        if not owner_n or not mint_n:
            continue
        token_amount = entry.get("uiTokenAmount")
        raw = token_amount.get("amount") if isinstance(token_amount, dict) else None
        parsed = _as_int(raw)
        key = (owner_n, mint_n)
        if key in found and found[key] is None:
            continue
        if parsed is None:
            found[key] = None
            continue
        previous = found.get(key, 0)
        if not isinstance(previous, int):
            found[key] = None
            continue
        found[key] = previous + parsed
    return found


def _traded_mints(tx: dict[str, Any], wallet: str, mints: set[str]) -> set[str]:
    """Mints whose raw balance for this wallet changed. Missing amounts drop the mint."""
    meta = tx.get("meta") if isinstance(tx, dict) else None
    if not isinstance(meta, dict):
        return set()
    pre = _balance_amounts(meta.get("preTokenBalances"))
    post = _balance_amounts(meta.get("postTokenBalances"))
    traded: set[str] = set()
    for mint in mints:
        key = (wallet, mint)
        if key in pre and pre[key] is None:
            continue
        if key in post and post[key] is None:
            continue
        if key not in pre and key not in post:
            continue
        before = 0 if key not in pre else pre[key]
        after = 0 if key not in post else post[key]
        if before is None or after is None:
            continue
        if before != after:
            traded.add(mint)
    return traded


def extract_bot_observations(
    tx: dict[str, Any],
    *,
    signature: str,
    recorded_at: str,
    mints: list[str],
    skip_addresses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """One observation per traded mint. Empty when the fee payer or a balance is missing.

    ``priority_fee_lamports`` is set only when both the CU price and
    ``computeUnitsConsumed`` are present. It is not the label by itself.
    """
    sig = signature.strip()
    recorded = recorded_at.strip()
    if not sig or not recorded or not isinstance(tx, dict):
        return []
    wallet = _fee_payer(tx)
    if not wallet:
        return []
    skipped = {norm_address(item) for item in (skip_addresses or set()) if str(item).strip()}
    if wallet in skipped:
        return []
    mint_set = {norm_address(mint) for mint in mints if str(mint).strip()}
    traded = _traded_mints(tx, wallet, mint_set)
    if not traded:
        return []
    price, limit = _compute_budget(tx)
    meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
    consumed = _as_int(meta.get("computeUnitsConsumed")) if isinstance(meta, dict) else None
    priority = None
    if price is not None and consumed is not None:
        priority = price * consumed // 1_000_000
    slot = _as_int(tx.get("slot"))
    block_time = _as_int(tx.get("blockTime"))
    fee = _as_int(meta.get("fee")) if isinstance(meta, dict) else None
    rows: list[dict[str, Any]] = []
    for mint in sorted(traded):
        rows.append(
            {
                "chain_id": "solana",
                "signature": sig,
                "wallet": wallet,
                "token_address": mint,
                "slot": slot,
                "block_time": block_time,
                "recorded_at": recorded,
                "fee_lamports": fee,
                "compute_unit_price": price,
                "compute_unit_limit": limit,
                "priority_fee_lamports": priority,
                "source": BOT_OBS_SOURCE,
            }
        )
    return rows


def _signature(row: dict[str, Any]) -> str | None:
    sig = row.get("signature")
    if isinstance(sig, str) and sig.strip():
        return sig.strip()
    return None


def _fixed_interval_signatures(points: list[tuple[int, str]]) -> set[str]:
    """Signatures inside an exact positive-delta run that fits the lookback window."""
    if len(points) < FIXED_INTERVAL_MIN_POINTS:
        return set()
    latest = points[-1][0]
    floor = latest - PATTERN_WINDOW_SEC
    series = [point for point in points if floor <= point[0] <= latest]
    flagged: set[str] = set()
    count = len(series)
    if count < FIXED_INTERVAL_MIN_POINTS:
        return set()
    index = 0
    while index < count - 2:
        delta = series[index + 1][0] - series[index][0]
        if delta <= 0:
            index += 1
            continue
        end = index + 1
        while end < count - 1 and series[end + 1][0] - series[end][0] == delta:
            end += 1
        if end - index + 1 >= FIXED_INTERVAL_MIN_POINTS:
            for cursor in range(index, end + 1):
                flagged.add(series[cursor][1])
            index = end
        else:
            index += 1
    return flagged


def classify_bot_fills(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Map signature → comma-sorted reasons. Unmatched signatures are omitted.

    A missing slot, block_time, or compute-unit price simply skips that
    pattern. Omission is not a human label.
    """
    reasons: dict[str, set[str]] = {}

    def _add(signature: str, reason: str) -> None:
        reasons.setdefault(signature, set()).add(reason)

    by_slot: dict[int, set[str]] = {}
    timed: list[tuple[int, str]] = []
    seen_times: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        signature = _signature(row)
        if signature is None:
            continue
        slot = _as_int(row.get("slot"))
        if slot is not None:
            by_slot.setdefault(slot, set()).add(signature)
        block_time = _as_int(row.get("block_time"))
        if block_time is not None and signature not in seen_times:
            seen_times.add(signature)
            timed.append((block_time, signature))
        price = _as_int(row.get("compute_unit_price"))
        if price is not None and price >= ABNORMAL_CU_PRICE_MICROLAMPORTS:
            _add(signature, REASON_PRIORITY_FEE)
    for signatures in by_slot.values():
        if len(signatures) < SAME_SLOT_MIN_SIGS:
            continue
        for signature in signatures:
            _add(signature, REASON_SAME_SLOT)
    timed.sort()
    for signature in _fixed_interval_signatures(timed):
        _add(signature, REASON_FIXED_INTERVAL)
    return {signature: ",".join(sorted(parts)) for signature, parts in reasons.items()}


def label_source(reasons: set[str]) -> str:
    ordered = "+".join(sorted(part for part in reasons if part))
    if not ordered:
        return BOT_LABEL_SOURCE_PREFIX
    return f"{BOT_LABEL_SOURCE_PREFIX}:{ordered}"


def apply_bot_observations(
    store: Any,
    mints: list[str],
    observations: list[Any],
    *,
    skip_addresses: set[str] | None = None,
) -> None:
    """Persist observations, then label only wallets a heuristic actually hit.

    Mints outside this enqueue batch are ignored. A program fee payer is ignored.
    Heuristic misses stay NULL on the row and write no ``bot_suspect`` label.
    """
    mint_set = {norm_address(mint) for mint in mints if str(mint).strip()}
    skipped = {norm_address(item) for item in (skip_addresses or set()) if str(item).strip()}
    kept: list[dict[str, Any]] = []
    pairs: set[tuple[str, str]] = set()
    for item in observations:
        if not isinstance(item, dict):
            continue
        wallet = norm_address(str(item.get("wallet") or ""))
        token = norm_address(str(item.get("token_address") or ""))
        if not wallet or wallet in skipped or token not in mint_set:
            continue
        kept.append(item)
        pairs.add((wallet, token))
    if not kept:
        return
    store.insert_bot_observations(kept)
    for wallet, token in pairs:
        loaded = store.list_bot_observations(
            "solana",
            wallet,
            token,
            limit=BOT_OBSERVATION_CAP,
        )
        flagged = classify_bot_fills(loaded)
        if not flagged:
            continue
        for signature, reason in flagged.items():
            store.merge_bot_observation_reason("solana", signature, wallet, token, reason)
            store.merge_bot_edge_reason("solana", token, signature, reason)
        refreshed = store.list_bot_observations(
            "solana",
            wallet,
            token,
            limit=BOT_OBSERVATION_CAP,
        )
        parts: set[str] = set()
        for row in refreshed:
            blob = row.get("bot_reason")
            if isinstance(blob, str):
                parts.update(part for part in blob.split(",") if part)
        if not parts:
            continue
        store.upsert_address_label(
            "solana",
            wallet,
            BOT_SUSPECT_LABEL,
            token_mint=token,
            source=label_source(parts),
        )
