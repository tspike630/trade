"""Optional proxy pool — DEFAULT OFF. P0: background health + eviction."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from config import (
    DB_PATH,
    PROXY_AVAILABILITY_ALERT_RATIO,
    PROXY_BAD_COOLDOWN_SEC,
    PROXY_FILE,
    PROXY_HEALTH_INTERVAL_SEC,
    PROXY_HEALTH_PROBE_URL,
    PROXY_LIST,
    PROXY_PROBE_FAIL_EVICT,
    REQUEST_TIMEOUT_SECONDS,
    USE_PROXY,
)
from logging_setup import get_logger

log = get_logger("proxy_pool")


@dataclass
class ProxyState:
    url: str
    last_used: float = 0.0
    bad_until: float = 0.0
    failures: int = 0  # request-path failures (cooldown)
    successes: int = 0
    consecutive_probe_failures: int = 0
    evicted: bool = False


@dataclass
class ProxyPool:
    proxies: list[ProxyState] = field(default_factory=list)
    cooldown_sec: float = PROXY_BAD_COOLDOWN_SEC
    enabled: bool = False
    health_interval_sec: float = PROXY_HEALTH_INTERVAL_SEC
    probe_url: str = PROXY_HEALTH_PROBE_URL
    probe_fail_evict: int = PROXY_PROBE_FAIL_EVICT
    availability_alert_ratio: float = PROXY_AVAILABILITY_ALERT_RATIO
    _rr: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _health_thread: threading.Thread | None = field(default=None, repr=False)
    _stop_health: threading.Event = field(default_factory=threading.Event, repr=False)
    _low_alert_emitted: bool = False

    @classmethod
    def from_config(cls, *, force_enable: bool | None = None) -> "ProxyPool":
        urls = load_proxy_urls()
        enable = USE_PROXY if force_enable is None else force_enable
        if enable and not urls:
            log.warning("USE_PROXY set but no proxies found in PROXY_LIST / %s", PROXY_FILE)
            enable = False
        if not enable:
            if urls:
                log.info(
                    "proxy pool loaded count=%s but DISABLED (set USE_PROXY=1 to enable); "
                    "prefer batch/tier optimizations first",
                    len(urls),
                )
            else:
                log.info("proxy pool empty/disabled (default)")
            return cls(proxies=[], enabled=False)
        pool = cls(
            proxies=[ProxyState(url=u) for u in urls],
            enabled=True,
        )
        log.warning(
            "proxy pool ENABLED count=%s cooldown=%ss health_interval=%ss "
            "evict_after=%s probe_fails — last resort",
            len(pool.proxies),
            pool.cooldown_sec,
            pool.health_interval_sec,
            pool.probe_fail_evict,
        )
        pool.start_health_loop()
        return pool

    def __len__(self) -> int:
        with self._lock:
            return sum(1 for p in self.proxies if not p.evicted)

    def active_proxies(self) -> list[ProxyState]:
        with self._lock:
            return [p for p in self.proxies if not p.evicted]

    def next_proxy(self) -> str | None:
        """LRU among healthy non-evicted proxies."""
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            alive = [p for p in self.proxies if not p.evicted]
            if not alive:
                return None
            healthy = [p for p in alive if p.bad_until <= now]
            candidates = healthy or alive
            candidates.sort(key=lambda p: p.last_used)
            chosen = candidates[0]
            chosen.last_used = now
            return chosen.url

    def report_success(self, url: str) -> None:
        with self._lock:
            p = self._find_unlocked(url)
            if p and not p.evicted:
                p.successes += 1
                p.bad_until = 0.0
                p.consecutive_probe_failures = 0

    def report_bad(self, url: str, *, reason: str = "") -> None:
        with self._lock:
            p = self._find_unlocked(url)
            if not p or p.evicted:
                return
            p.failures += 1
            p.bad_until = time.monotonic() + self.cooldown_sec
            log.warning(
                "proxy marked bad url=%s reason=%s cooldown=%ss failures=%s",
                _redact(url),
                reason or "-",
                self.cooldown_sec,
                p.failures,
            )

    def _find_unlocked(self, url: str) -> ProxyState | None:
        for p in self.proxies:
            if p.url == url:
                return p
        return None

    def availability_ratio(self) -> float | None:
        with self._lock:
            total = sum(1 for p in self.proxies if not p.evicted)
            if total == 0:
                return None
            now = time.monotonic()
            healthy = sum(
                1 for p in self.proxies if not p.evicted and p.bad_until <= now
            )
            return healthy / total

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            active = [p for p in self.proxies if not p.evicted]
            healthy = sum(1 for p in active if p.bad_until <= now)
            return {
                "enabled": self.enabled,
                "count": len(active),
                "evicted": sum(1 for p in self.proxies if p.evicted),
                "healthy": healthy,
                "availability": (healthy / len(active)) if active else None,
                "proxies": [
                    {
                        "url": _redact(p.url),
                        "failures": p.failures,
                        "successes": p.successes,
                        "probe_fails": p.consecutive_probe_failures,
                        "bad": p.bad_until > now,
                        "evicted": p.evicted,
                    }
                    for p in self.proxies
                ],
            }

    # --- P0 health loop ---

    def start_health_loop(self) -> None:
        if not self.enabled or not self.proxies:
            return
        if self._health_thread and self._health_thread.is_alive():
            return
        self._stop_health.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop,
            name="proxy-health",
            daemon=True,
        )
        self._health_thread.start()
        log.info(
            "proxy health loop started interval=%ss probe=%s",
            self.health_interval_sec,
            self.probe_url,
        )

    def stop_health_loop(self) -> None:
        self._stop_health.set()
        if self._health_thread and self._health_thread.is_alive():
            self._health_thread.join(timeout=5.0)

    def _health_loop(self) -> None:
        # first probe shortly after start
        while not self._stop_health.wait(self.health_interval_sec):
            try:
                self.run_health_probes()
            except Exception as exc:  # noqa: BLE001
                log.error("proxy health loop error: %s", exc)

    def run_health_probes(self) -> dict[str, Any]:
        """
        Probe all non-evicted proxies (incl. those in cooldown).
        3 consecutive probe failures → permanent eviction.
        Low availability → WARNING + alerts table.
        """
        results: dict[str, Any] = {"probed": 0, "ok": 0, "fail": 0, "evicted": 0}
        with self._lock:
            targets = list(self.proxies)
        for p in targets:
            if p.evicted:
                continue
            results["probed"] += 1
            ok = self._probe_one(p.url)
            with self._lock:
                if p.evicted:
                    continue
                if ok:
                    results["ok"] += 1
                    p.consecutive_probe_failures = 0
                    p.bad_until = 0.0
                else:
                    results["fail"] += 1
                    p.consecutive_probe_failures += 1
                    p.bad_until = time.monotonic() + self.cooldown_sec
                    if p.consecutive_probe_failures >= self.probe_fail_evict:
                        p.evicted = True
                        results["evicted"] += 1
                        log.error(
                            "proxy permanently evicted url=%s consecutive_probe_fails=%s",
                            _redact(p.url),
                            p.consecutive_probe_failures,
                        )
        self._maybe_alert_low_availability()
        log.info(
            "proxy health probes probed=%s ok=%s fail=%s evicted_now=%s availability=%s",
            results["probed"],
            results["ok"],
            results["fail"],
            results["evicted"],
            self.availability_ratio(),
        )
        return results

    def _probe_one(self, url: str) -> bool:
        try:
            with httpx.Client(
                proxy=url,
                timeout=min(15.0, REQUEST_TIMEOUT_SECONDS),
                follow_redirects=True,
            ) as client:
                resp = client.get(self.probe_url)
                return resp.status_code < 500
        except Exception as exc:  # noqa: BLE001
            log.warning("proxy probe failed url=%s err=%s", _redact(url), type(exc).__name__)
            return False

    def _maybe_alert_low_availability(self) -> None:
        ratio = self.availability_ratio()
        if ratio is None:
            return
        if ratio >= self.availability_alert_ratio:
            self._low_alert_emitted = False
            return
        log.warning(
            "proxy pool availability low ratio=%.2f threshold=%.2f",
            ratio,
            self.availability_alert_ratio,
        )
        # emit at most once until recovery (cooldown via flag + alerts table dedupe)
        if self._low_alert_emitted:
            return
        try:
            from store import Store

            with Store(DB_PATH) as store:
                store.insert_alert(
                    alert_type="proxy_pool_low",
                    severity="high",
                    message=(
                        f"proxy pool availability {ratio:.0%} "
                        f"below threshold {self.availability_alert_ratio:.0%}"
                    ),
                    payload={
                        "availability": ratio,
                        "threshold": self.availability_alert_ratio,
                        "snapshot": self.snapshot(),
                    },
                    delivered=True,
                )
                store.bump_counter("alerts_emitted")
            self._low_alert_emitted = True
        except Exception as exc:  # noqa: BLE001
            log.error("failed to write proxy_pool_low alert: %s", exc)


def load_proxy_urls(
    proxy_list: str | None = None,
    proxy_file: str | Path | None = None,
) -> list[str]:
    urls: list[str] = []
    raw = proxy_list if proxy_list is not None else PROXY_LIST
    if raw:
        urls.extend(u.strip() for u in raw.split(",") if u.strip())
    path = Path(proxy_file if proxy_file is not None else PROXY_FILE)
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _redact(url: str) -> str:
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[-1]
            return f"{scheme}://***@{rest}"
    return url
