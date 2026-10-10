"""Demo P0 proxy eviction + low-availability alert without waiting on real TCP."""

from __future__ import annotations

from logging_setup import setup_logging
from proxy_pool import ProxyPool, ProxyState
from store import Store


def main() -> None:
    setup_logging()
    pool = ProxyPool(
        proxies=[
            ProxyState(url="http://dead1.example:9"),
            ProxyState(url="http://dead2.example:9"),
            ProxyState(url="http://dead3.example:9"),
        ],
        enabled=True,
        cooldown_sec=1,
        health_interval_sec=9999,
        probe_fail_evict=3,
        availability_alert_ratio=0.5,
        probe_url="https://example.invalid/",
    )

    # Instant failures — no network
    pool._probe_one = lambda url: False  # type: ignore[method-assign]

    before_alerts = 0
    with Store("dexscreener.db") as st:
        before_alerts = st.count_alerts()

    for i in range(3):
        r = pool.run_health_probes()
        print(f"round={i+1} results={r} availability={pool.availability_ratio()}")

    snap = pool.snapshot()
    assert snap["evicted"] == 3, snap
    assert snap["count"] == 0
    assert pool.next_proxy() is None

    with Store("dexscreener.db") as st:
        after = st.count_alerts()
        rows = st.list_alerts_api(alert_type="proxy_pool_low", limit=3)
    assert after >= before_alerts, "expected proxy_pool_low alert row"
    assert any(r["type"] == "proxy_pool_low" for r in rows)
    print(
        f"PROXY_HEALTH_DEMO_OK evicted={snap['evicted']} "
        f"alerts_delta={after - before_alerts} proxy_pool_low={len(rows)}"
    )


if __name__ == "__main__":
    main()
