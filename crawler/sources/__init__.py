"""Pluggable multi-source adapters (Phase 5 + P1.2 Helius + P1.3 X)."""

from __future__ import annotations

from typing import Any

from logging_setup import get_logger
from sources.base import SourceResult, TokenSource
from sources.dexscreener import DexScreenerSource
from sources.solana_helius import HeliusSolanaSource
from sources.stub_solana_logs import StubSolanaLogsSource
from sources.stub_x import StubXSource
from sources.x_api import XApiSource

log = get_logger("sources")

_REGISTRY: dict[str, type[TokenSource]] = {
    "dexscreener": DexScreenerSource,
    "helius": HeliusSolanaSource,
    "solana_helius": HeliusSolanaSource,
    "x": XApiSource,
    "x_api": XApiSource,
    "solana_stub": StubSolanaLogsSource,
    "x_stub": StubXSource,  # legacy offline stub
}


def available_source_names() -> list[str]:
    return sorted(_REGISTRY.keys())


def build_sources(names: list[str], **kwargs: Any) -> list[TokenSource]:
    out: list[TokenSource] = []
    for name in names:
        key = name.strip().lower()
        if not key:
            continue
        cls = _REGISTRY.get(key)
        if cls is None:
            log.warning("unknown source %s — skip", name)
            continue
        out.append(cls(**kwargs))
    return out


def parse_sources_arg(value: str | None) -> list[str]:
    if not value:
        return ["dexscreener"]
    return [p.strip() for p in value.split(",") if p.strip()]


__all__ = [
    "SourceResult",
    "TokenSource",
    "build_sources",
    "parse_sources_arg",
    "available_source_names",
]
