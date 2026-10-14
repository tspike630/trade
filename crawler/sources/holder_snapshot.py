"""Top-holder snapshot helper for INTEL_LOOP I-1.

Uses the same Helius HTTP RPC already used for getTransaction
(getTokenLargestAccounts, getTokenSupply, getMultipleAccounts).
There is no DAS client in this repo, and DexScreener payloads have no holder list.

Missing or failed responses stay empty. Callers persist nothing rather than
inventing holders or balances. pct_of_supply is the per-row amount/supply ratio
from that same response, not a concentration factor.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from logging_setup import get_logger
from store import norm_address

log = get_logger("sources.holder_snapshot")

TOP_HOLDER_N = 10
HOLDER_SNAPSHOT_SOURCE = "helius:getTokenLargestAccounts"
_ON_VALUES = frozenset({"1", "true", "yes", "on"})

RpcCaller = Callable[[str, list[Any]], Awaitable[dict[str, Any] | None]]


def flag_enabled(raw: str | None, *, default: str = "1") -> bool:
    """True when a feature flag string is on. None uses default (I-1 default ON)."""
    text = default if raw is None else raw
    return text.strip().lower() in _ON_VALUES


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw_amount(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _ui_amount(value: Any, string_value: Any = None) -> float | None:
    if isinstance(value, bool):
        value = None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(string_value, str):
        try:
            return float(string_value)
        except ValueError:
            return None
    return None


def _amount_sort_key(row: dict[str, Any]) -> int:
    raw = row.get("amount_raw")
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return -1


def rpc_result(payload: Any) -> dict[str, Any] | None:
    """Unwrap a JSON-RPC body. An error or a non-object result is None."""
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def parse_largest_account_rows(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Largest token accounts, biggest first, capped at TOP_HOLDER_N.

    `address` here is the token account, not the wallet. Rows with no address
    are dropped. A missing amount stays None.
    """
    if not isinstance(result, dict):
        return []
    value = result.get("value")
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if not isinstance(address, str) or not address.strip():
            continue
        rows.append(
            {
                "token_account": address.strip(),
                "amount_raw": _raw_amount(item.get("amount")),
                "amount_ui": _ui_amount(item.get("uiAmount"), item.get("uiAmountString")),
            }
        )
    rows.sort(key=_amount_sort_key, reverse=True)
    return rows[:TOP_HOLDER_N]


def parse_token_supply(result: dict[str, Any] | None) -> tuple[str | None, float | None]:
    if not isinstance(result, dict):
        return None, None
    value = result.get("value")
    if not isinstance(value, dict):
        return None, None
    return (
        _raw_amount(value.get("amount")),
        _ui_amount(value.get("uiAmount"), value.get("uiAmountString")),
    )


def parse_account_owners(
    result: dict[str, Any] | None,
    token_accounts: list[str],
) -> dict[str, str]:
    """Map token-account address → owner wallet.

    `result.value` must be aligned with `token_accounts` (the order requested).
    A null account, a non-account parsed type, or a missing owner is skipped.
    """
    out: dict[str, str] = {}
    if not isinstance(result, dict):
        return out
    value = result.get("value")
    if not isinstance(value, list):
        return out
    for requested, item in zip(token_accounts, value):
        if not isinstance(item, dict):
            continue
        data = item.get("data")
        if not isinstance(data, dict):
            continue
        parsed = data.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if str(parsed.get("type") or "").lower() != "account":
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        owner = info.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            continue
        out[norm_address(requested)] = norm_address(owner)
    return out


def pct_of_supply(amount_ui: float | None, supply_ui: float | None) -> float | None:
    """Per-holder ratio from one response. NULL when either side is missing."""
    if amount_ui is None or supply_ui is None or supply_ui <= 0:
        return None
    return float(amount_ui) / float(supply_ui)


def assemble_holder_snapshot(
    *,
    largest: dict[str, Any] | None,
    supply: dict[str, Any] | None,
    owners: dict[str, Any] | None,
    recorded_at: str,
    snapshot_id: str,
    source: str = HOLDER_SNAPSHOT_SOURCE,
) -> dict[str, Any] | None:
    """One snapshot dict, or None when the largest-account list is empty.

    `owners` is aligned with the sorted top-N token accounts, not the raw
    RPC order. Every row shares `recorded_at`, `snapshot_id`, and the supply
    basis. Supply failure leaves the supply columns and pct NULL.
    """
    accounts = parse_largest_account_rows(largest)
    if not accounts or not recorded_at or not snapshot_id:
        return None
    supply_raw, supply_ui = parse_token_supply(supply)
    owner_map = parse_account_owners(
        owners, [str(row["token_account"]) for row in accounts]
    )
    rows: list[dict[str, Any]] = []
    for rank, account in enumerate(accounts, start=1):
        token_account = str(account["token_account"])
        owner = owner_map.get(norm_address(token_account))
        if owner:
            holder = owner
            kind = "owner"
        else:
            holder = token_account
            kind = "token_account"
        amount_ui = account["amount_ui"]
        if not isinstance(amount_ui, float):
            amount_ui = None
        rows.append(
            {
                "rank": rank,
                "holder_address": holder,
                "address_kind": kind,
                "amount_raw": account["amount_raw"],
                "amount_ui": amount_ui,
                "pct_of_supply": pct_of_supply(amount_ui, supply_ui),
            }
        )
    if not rows:
        return None
    return {
        "snapshot_id": snapshot_id,
        "recorded_at": recorded_at,
        "total_supply_raw": supply_raw,
        "total_supply_ui": supply_ui,
        "source": source,
        "rows": rows,
    }


async def post_json_rpc(
    http_url: str,
    method: str,
    params: list[Any],
    *,
    timeout: float = 12.0,
) -> dict[str, Any] | None:
    """POST one JSON-RPC call. Failures return None. The URL is never logged."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(http_url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        log.warning(
            "holder rpc %s failed status=%s",
            method,
            exc.response.status_code,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("holder rpc %s failed err=%s", method, type(exc).__name__)
        return None
    return rpc_result(data)


async def fetch_holder_snapshot(
    mint: str,
    *,
    http_url: str | None,
    enabled: bool = True,
    rpc: RpcCaller | None = None,
    timeout: float = 12.0,
    recorded_at: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort top-10 snapshot. Disabled, or any empty largest list, is None.

    Supply and owner lookups are optional. Their failure still returns holder
    rows with NULL supply and token-account addresses where the owner is unknown.
    """
    if not enabled or not http_url or not str(mint).strip():
        return None

    async def _call(method: str, params: list[Any]) -> dict[str, Any] | None:
        if rpc is not None:
            return await rpc(method, params)
        return await post_json_rpc(http_url, method, params, timeout=timeout)

    try:
        largest = await _call(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("holder snapshot largest failed err=%s", type(exc).__name__)
        return None
    accounts = parse_largest_account_rows(largest)
    if not accounts:
        return None

    supply: dict[str, Any] | None
    try:
        supply = await _call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    except Exception as exc:  # noqa: BLE001
        log.warning("holder snapshot supply failed err=%s", type(exc).__name__)
        supply = None

    owners: dict[str, Any] | None
    try:
        owners = await _call(
            "getMultipleAccounts",
            [
                [row["token_account"] for row in accounts],
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("holder snapshot owners failed err=%s", type(exc).__name__)
        owners = None

    stamped = recorded_at or _utc_now_iso()
    return assemble_holder_snapshot(
        largest={"value": [
            {
                "address": row["token_account"],
                "amount": row["amount_raw"],
                "uiAmount": row["amount_ui"],
            }
            for row in accounts
        ]},
        supply=supply,
        owners=owners,
        recorded_at=stamped,
        snapshot_id=snapshot_id or f"{stamped}-{uuid.uuid4().hex[:12]}",
    )
