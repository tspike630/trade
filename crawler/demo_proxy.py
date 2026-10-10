"""Demonstrate proxy pool load / LRU / bad-cooldown without real proxies."""

from __future__ import annotations

import time

from proxy_pool import ProxyPool, ProxyState, load_proxy_urls


def main() -> None:
    urls = load_proxy_urls(
        proxy_list="http://p1.example:8080,http://user:secret@p2.example:8080",
        proxy_file="proxies_missing.txt",
    )
    assert len(urls) == 2
    pool = ProxyPool(
        proxies=[ProxyState(url=u) for u in urls],
        enabled=True,
        cooldown_sec=1,
    )
    a = pool.next_proxy()
    b = pool.next_proxy()
    assert {a, b} == set(urls)
    pool.report_bad(a, reason="timeout")
    c = pool.next_proxy()
    assert c == b
    snap = pool.snapshot()
    assert snap["enabled"] is True
    assert snap["count"] == 2
    assert "secret" not in str(snap)
    time.sleep(1.1)
    d = pool.next_proxy()
    assert d in urls
    off = ProxyPool.from_config(force_enable=False)
    assert off.enabled is False
    assert off.next_proxy() is None
    print(f"PROXY_DEMO_OK urls={len(urls)} snapshot_healthy={snap['healthy']}")


if __name__ == "__main__":
    main()
