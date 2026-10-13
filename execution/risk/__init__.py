"""Pre-trade risk checkers (NautilusTrader-style, pluginable). Default off."""

from __future__ import annotations

from typing import Any, Callable

RISK_CHECKERS: list[tuple[str, Callable[..., dict[str, Any]]]] = []


def register(name: str, fn: Callable[..., dict[str, Any]]) -> None:
    RISK_CHECKERS.append((name, fn))


def run_all(ledger, sig: dict[str, Any], **ctx: Any) -> list[dict[str, Any]]:
    """返回拒绝理由列表；空 = 通过。"""
    reasons: list[dict[str, Any]] = []
    for name, fn in RISK_CHECKERS:
        try:
            r = fn(ledger, sig, **ctx)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "reason": f"{name}:error:{exc}"}
        if r and not r.get("ok"):
            reasons.append(r)
    return reasons
