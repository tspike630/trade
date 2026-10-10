"""
Stub Solana logsSubscribe feed.

Offline fixture stub. Live Helius logsSubscribe is in sources/solana_helius.py
(--helius-listen / --sources helius when HELIUS_API_KEY is set).
This stub NEVER requires an API key: it optionally reads fixtures/solana_logs_sample.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from config import SOLANA_LOGS_FIXTURE
from logging_setup import get_logger
from sources.base import SourceResult, TokenSource

log = get_logger("sources.solana_stub")


class StubSolanaLogsSource(TokenSource):
    name = "solana_stub"

    def __init__(self, fixture_path: str | Path | None = None, **_kwargs: Any):
        self.fixture_path = Path(fixture_path or SOLANA_LOGS_FIXTURE)

    async def fetch(self) -> SourceResult:
        if not self.fixture_path.is_file():
            log.info(
                "solana_stub: no fixture at %s — no-op "
                "(document: wire logsSubscribe → mint list → upsert tokens)",
                self.fixture_path,
            )
            return SourceResult(
                source_name=self.name,
                notes="no_fixture",
            )
        try:
            data = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("solana_stub: bad fixture %s: %s", self.fixture_path, exc)
            return SourceResult(source_name=self.name, notes=f"fixture_error:{exc}")

        tokens: list[dict[str, Any]] = []
        if isinstance(data, dict):
            items = data.get("tokens") or data.get("mints") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for item in items:
            if isinstance(item, str):
                tokens.append(
                    {
                        "chainId": "solana",
                        "tokenAddress": item,
                        "description": "from solana_stub fixture",
                    }
                )
            elif isinstance(item, dict):
                addr = item.get("tokenAddress") or item.get("mint") or item.get("address")
                if not addr:
                    continue
                tokens.append(
                    {
                        "chainId": item.get("chainId") or "solana",
                        "tokenAddress": addr,
                        "description": item.get("description") or "from solana_stub fixture",
                        "links": item.get("links") or [],
                    }
                )

        log.info("solana_stub: loaded %s token(s) from %s", len(tokens), self.fixture_path)
        return SourceResult(
            source_name=self.name,
            tokens=tokens,
            notes=f"fixture={self.fixture_path} count={len(tokens)}",
        )
