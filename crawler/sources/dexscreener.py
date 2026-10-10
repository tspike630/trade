"""Primary DexScreener adapter — thin wrapper; main pipeline already uses the client."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger
from sources.base import SourceResult, TokenSource

log = get_logger("sources.dexscreener")


class DexScreenerSource(TokenSource):
    """
    Marker/adapter source. The crawl pipeline already performs DexScreener
    list+enrich as the primary path; this source returns empty when used as
    a 'supplement' to avoid double-fetching. Pass client= to enable a
    lightweight latest-profiles peek for merge demos.
    """

    name = "dexscreener"

    def __init__(self, client: Any | None = None, **_kwargs: Any):
        self._client = client

    async def fetch(self) -> SourceResult:
        if self._client is None:
            log.info("dexscreener source: primary pipeline owns fetch — supplement no-op")
            return SourceResult(source_name=self.name, notes="primary_noop")
        # Optional peek (used when explicitly run as supplement with client)
        try:
            items = await self._client.fetch_token_profiles_latest()
        except Exception as exc:  # noqa: BLE001
            log.warning("dexscreener supplement fetch failed: %s", exc)
            return SourceResult(source_name=self.name, notes=f"error:{exc}")
        tokens = [x for x in items if isinstance(x, dict)]
        return SourceResult(
            source_name=self.name,
            tokens=tokens,
            notes=f"profiles_latest={len(tokens)}",
        )
