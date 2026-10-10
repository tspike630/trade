"""Dry-run execution adapter — records only (no orders)."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.dry_adapter")


class DryAdapter:
    """Step 1: do not place orders; optionally re-log accepted signals."""

    name = "dry"

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def submit(self, signal: dict[str, Any]) -> dict[str, Any]:
        self.received.append(signal)
        log.info(
            "dry_adapter ACCEPT (no order) action=%s %s/%s",
            signal.get("action"),
            signal.get("chain_id"),
            (signal.get("token_address") or "")[:16],
        )
        return {"ok": True, "mode": "dry_run", "signal": signal}
