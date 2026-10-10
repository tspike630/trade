"""Payload validation / coercion for list items and pair objects."""

from __future__ import annotations

from typing import Any


def _nonempty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s if s else None


def validate_token_profile(item: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Require chainId + tokenAddress as non-empty strings."""
    if not isinstance(item, dict):
        return None, "not_object"
    chain_s = _nonempty_str(item.get("chainId", item.get("chain_id")))
    addr_s = _nonempty_str(item.get("tokenAddress", item.get("token_address")))
    if chain_s is None:
        return None, "missing_or_invalid_chainId"
    if addr_s is None:
        return None, "missing_or_invalid_tokenAddress"
    out = dict(item)
    out["chainId"] = chain_s
    out["tokenAddress"] = addr_s
    return out, None


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def validate_pair(
    pair: Any,
    *,
    default_chain: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Require chainId, pairAddress, baseToken.address; coerce priceUsd/volume numerics."""
    if not isinstance(pair, dict):
        return None, "not_object"

    chain_s = _nonempty_str(pair.get("chainId", pair.get("chain_id")))
    if chain_s is None and default_chain:
        chain_s = _nonempty_str(default_chain)
    pair_s = _nonempty_str(pair.get("pairAddress", pair.get("pair_address")))
    if chain_s is None:
        return None, "missing_or_invalid_chainId"
    if pair_s is None:
        return None, "missing_or_invalid_pairAddress"

    base = pair.get("baseToken")
    if not isinstance(base, dict):
        return None, "missing_or_invalid_baseToken"
    base_addr = _nonempty_str(base.get("address"))
    if base_addr is None:
        return None, "missing_or_invalid_baseToken.address"

    out = dict(pair)
    out["chainId"] = chain_s
    out["pairAddress"] = pair_s
    out_base = dict(base)
    out_base["address"] = base_addr
    out["baseToken"] = out_base

    if "priceUsd" in out:
        price = _coerce_float(out.get("priceUsd"))
        if price is not None:
            out["priceUsd"] = price
        elif out.get("priceUsd") not in (None, ""):
            out.pop("priceUsd", None)

    vol = out.get("volume")
    if isinstance(vol, dict):
        vol_out = dict(vol)
        for k in ("h24", "h6", "h1", "m5"):
            if k in vol_out:
                coerced = _coerce_float(vol_out[k])
                if coerced is not None:
                    vol_out[k] = coerced
        out["volume"] = vol_out

    return out, None
