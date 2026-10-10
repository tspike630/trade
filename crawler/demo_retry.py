"""Demonstrate async client retries on transient 503 then success."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from client import AsyncDexScreenerClient
from logging_setup import get_logger, setup_logging


class FlakyTransport(httpx.AsyncBaseTransport):
    def __init__(self, fail_times: int = 2):
        self.fail_times = fail_times
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.fail_times:
            return httpx.Response(503, request=request, text="unavailable")
        return httpx.Response(
            200,
            request=request,
            json=[{"chainId": "solana", "pairAddress": "demo", "baseToken": {"address": "abc"}}],
        )


async def _main() -> None:
    setup_logging()
    log = get_logger("demo_retry")
    transport = FlakyTransport(fail_times=2)
    client = AsyncDexScreenerClient(cycle_id="demo-retry", max_retries=4)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="https://api.dexscreener.com",
        transport=transport,
        timeout=5.0,
    )
    t0 = time.monotonic()
    data: list[Any] = await client.fetch_tokens_batch("solana", ["addr1", "addr2"])
    elapsed = time.monotonic() - t0
    await client.aclose()
    log.info(
        "demo_retry ok calls=%s pairs=%s elapsed=%.2fs",
        transport.calls,
        len(data),
        elapsed,
    )
    print(f"RETRY_DEMO_OK calls={transport.calls} pairs={len(data)} elapsed={elapsed:.2f}s")


if __name__ == "__main__":
    asyncio.run(_main())
