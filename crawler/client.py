"""DexScreener async HTTP client with retries + optional proxy pool (Phase 5)."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from config import (
    BASE_URL,
    BATCH_SIZE,
    MAX_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    RETRY_JITTER_RATIO,
    RETRY_STATUS_CODES,
    USER_AGENT,
)
from logging_setup import get_logger
from proxy_pool import ProxyPool

log = get_logger("client")


class AsyncDexScreenerClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        cycle_id: str | None = None,
        proxy_pool: ProxyPool | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.cycle_id = cycle_id or "-"
        self.timeout = timeout
        self.proxy_pool = proxy_pool
        self._headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        # Direct (no proxy) client — used when pool disabled
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=self._headers,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncDexScreenerClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def _sleep_backoff(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and retry_after > 0:
            delay = min(retry_after, RETRY_BACKOFF_MAX_SECONDS)
        else:
            delay = min(
                RETRY_BACKOFF_BASE_SECONDS * (2**attempt),
                RETRY_BACKOFF_MAX_SECONDS,
            )
        jitter = delay * RETRY_JITTER_RATIO
        delay = max(0.0, delay + random.uniform(-jitter, jitter))
        await asyncio.sleep(delay)
        return delay

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float | None:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
        for key in ("X-RateLimit-Reset", "RateLimit-Reset"):
            val = resp.headers.get(key)
            if not val:
                continue
            try:
                reset = float(val)
                now = time.time()
                if reset > now:
                    return reset - now
                if reset < 3600:
                    return reset
            except ValueError:
                continue
        return None

    async def _raw_get(self, path: str, proxy: str | None) -> httpx.Response:
        if proxy:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._headers,
                follow_redirects=True,
                proxy=proxy,
            ) as proxied:
                return await proxied.get(path)
        return await self._client.get(path)

    async def _get_json(
        self,
        path: str,
        *,
        endpoint: str,
        chain: str | None = None,
        batch_size: int | None = None,
    ) -> Any:
        last_exc: Exception | None = None
        total_tries = self.max_retries + 1
        use_pool = bool(self.proxy_pool and self.proxy_pool.enabled)

        for attempt in range(total_tries):
            proxy = self.proxy_pool.next_proxy() if use_pool else None
            t0 = time.monotonic()
            try:
                resp = await self._raw_get(path, proxy)
                duration_ms = int((time.monotonic() - t0) * 1000)
                status = resp.status_code

                if status in (403, 429) and proxy and self.proxy_pool:
                    self.proxy_pool.report_bad(proxy, reason=f"http_{status}")

                if status in RETRY_STATUS_CODES:
                    retry_after = self._parse_retry_after(resp)
                    if attempt < self.max_retries:
                        delay = await self._sleep_backoff(attempt, retry_after)
                        log.warning(
                            "retry cycle_id=%s endpoint=%s chain=%s batch_size=%s "
                            "status=%s attempt=%s/%s delay=%.2fs duration_ms=%s proxy=%s",
                            self.cycle_id,
                            endpoint,
                            chain or "-",
                            batch_size if batch_size is not None else "-",
                            status,
                            attempt + 1,
                            total_tries,
                            delay,
                            duration_ms,
                            "yes" if proxy else "no",
                        )
                        continue
                    log.error(
                        "exhausted retries cycle_id=%s endpoint=%s status=%s attempts=%s",
                        self.cycle_id,
                        endpoint,
                        status,
                        total_tries,
                    )
                    resp.raise_for_status()

                resp.raise_for_status()
                if proxy and self.proxy_pool:
                    self.proxy_pool.report_success(proxy)
                log.info(
                    "ok cycle_id=%s endpoint=%s chain=%s batch_size=%s status=%s "
                    "duration_ms=%s proxy=%s",
                    self.cycle_id,
                    endpoint,
                    chain or "-",
                    batch_size if batch_size is not None else "-",
                    status,
                    duration_ms,
                    "yes" if proxy else "no",
                )
                return resp.json()

            except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_exc = exc
                if proxy and self.proxy_pool:
                    self.proxy_pool.report_bad(proxy, reason=type(exc).__name__)
                if attempt < self.max_retries:
                    delay = await self._sleep_backoff(attempt)
                    log.warning(
                        "retry cycle_id=%s endpoint=%s error=%s attempt=%s/%s delay=%.2fs proxy=%s",
                        self.cycle_id,
                        endpoint,
                        type(exc).__name__,
                        attempt + 1,
                        total_tries,
                        delay,
                        "yes" if proxy else "no",
                    )
                    continue
                log.error(
                    "exhausted retries cycle_id=%s endpoint=%s error=%s attempts=%s",
                    self.cycle_id,
                    endpoint,
                    type(exc).__name__,
                    total_tries,
                )
                raise

            except httpx.HTTPStatusError:
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError(f"request failed without exception: {path}")

    async def fetch_token_profiles_latest(self) -> list[dict[str, Any]]:
        data = await self._get_json(
            "/token-profiles/latest/v1", endpoint="token-profiles/latest"
        )
        return data if isinstance(data, list) else []

    async def fetch_token_profiles_recent_updates(self) -> list[dict[str, Any]]:
        data = await self._get_json(
            "/token-profiles/recent-updates/v1",
            endpoint="token-profiles/recent-updates",
        )
        return data if isinstance(data, list) else []

    async def fetch_community_takeovers_latest(self) -> list[dict[str, Any]]:
        data = await self._get_json(
            "/community-takeovers/latest/v1",
            endpoint="community-takeovers/latest",
        )
        return data if isinstance(data, list) else []

    async def fetch_tokens_batch(
        self, chain_id: str, token_addresses: list[str]
    ) -> list[dict[str, Any]]:
        if not token_addresses:
            return []
        if len(token_addresses) > BATCH_SIZE:
            raise ValueError(
                f"batch size {len(token_addresses)} exceeds limit {BATCH_SIZE}"
            )
        joined = ",".join(token_addresses)
        path = f"/tokens/v1/{chain_id}/{joined}"
        data = await self._get_json(
            path,
            endpoint="tokens/v1",
            chain=chain_id,
            batch_size=len(token_addresses),
        )
        return data if isinstance(data, list) else []


DexScreenerClient = AsyncDexScreenerClient
