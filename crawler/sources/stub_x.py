"""
Legacy stub X source (Phase 5). Prefer sources/x_api.py via --sources x (P1.3).

Without X_BEARER_TOKEN: no-op (log only).
Kept for backward compatibility; live social search is in XApiSource.
"""

from __future__ import annotations

import os
from typing import Any

from config import X_BEARER_TOKEN
from logging_setup import get_logger
from sources.base import SourceResult, TokenSource

log = get_logger("sources.x_stub")


class StubXSource(TokenSource):
    name = "x_stub"

    def __init__(self, store: Any | None = None, **_kwargs: Any):
        self._store = store

    async def fetch(self) -> SourceResult:
        if not X_BEARER_TOKEN:
            log.info(
                "x_stub: X_BEARER_TOKEN unset — no-op "
                "(would enrich tokens that have twitter links in raw_json)"
            )
            return SourceResult(source_name=self.name, notes="no_bearer")

        live = os.environ.get("X_STUB_LIVE", "0").strip() in ("1", "true", "yes")
        if not live:
            log.info(
                "x_stub: bearer present but X_STUB_LIVE not set — dry-run no-op "
                "(set X_STUB_LIVE=1 to attempt live enrichment)"
            )
            return SourceResult(source_name=self.name, notes="dry_run_bearer_set")

        # Best-effort: do not hammer; Phase 5 keeps this minimal.
        log.warning("x_stub: live mode requested but live X calls are stubbed empty in Phase 5")
        return SourceResult(source_name=self.name, notes="live_stub_empty")
