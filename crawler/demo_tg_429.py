#!/usr/bin/env python3
"""Dry-run: parse retry_after + 429 retry/ban/queue — NEVER hits real Telegram."""

from __future__ import annotations

import json
from typing import Callable

import httpx

from alerts_telegram import (
    TelegramAlerter,
    parse_retry_after,
    reset_telegram_alerter_for_tests,
)


def test_parse_retry_after() -> None:
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 48693",
        "parameters": {"retry_after": 48693},
    }
    assert parse_retry_after(body) == 48693.0
    assert parse_retry_after(json.dumps(body)) == 48693.0
    assert parse_retry_after({"ok": False, "description": "retry after 12"}) == 12.0
    # httpx response mock
    req = httpx.Request("POST", "https://api.telegram.org/botT/sendMessage")
    resp = httpx.Response(429, json=body, request=req)
    assert parse_retry_after(resp) == 48693.0
    resp2 = httpx.Response(
        429,
        headers={"Retry-After": "7"},
        text="no json",
        request=req,
    )
    assert parse_retry_after(resp2) == 7.0
    print("parse_retry_after OK")


class _ScriptedTransport(httpx.BaseTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]):
        self.handler = handler
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.handler(request)


def test_429_max_retries_and_ban() -> None:
    """Short retry_after: sleep capped + at most TG_MAX_RETRIES HTTP calls."""
    sleeps: list[float] = []
    body_429 = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 1",
        "parameters": {"retry_after": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=body_429, request=request)

    transport = _ScriptedTransport(handler)
    import alerts_telegram as mod

    old = (
        mod.TG_429_SLEEP_CAP_SEC,
        mod.TG_MAX_RETRIES,
        mod.TG_RATE_MIN_INTERVAL_SEC,
        mod.TG_RATE_PER_MIN,
    )
    mod.TG_429_SLEEP_CAP_SEC = 2.0
    mod.TG_MAX_RETRIES = 3
    mod.TG_RATE_MIN_INTERVAL_SEC = 0.0
    mod.TG_RATE_PER_MIN = 1000
    try:
        alerter = TelegramAlerter(
            token="000:TEST",
            transport=transport,
            sleep_fn=lambda s: sleeps.append(s),
        )
        # Bypass queue/chat resolve: call _send_with_retries directly
        ok = alerter._send_with_retries(  # noqa: SLF001
            "-1003",
            "hi",
            alert_key="t|short",
            alert_type="price_change",
        )
        assert ok is False
        assert transport.calls == 3, transport.calls  # exactly max retries
        assert sleeps and max(sleeps) <= 2.0 + 1e-6, sleeps
        assert alerter.stats()["retries"] == 3
        assert alerter.stats()["dropped_429"] == 1
        print(
            f"429 short-retry OK calls={transport.calls} sleeps={sleeps} stats={alerter.stats()}"
        )
    finally:
        (
            mod.TG_429_SLEEP_CAP_SEC,
            mod.TG_MAX_RETRIES,
            mod.TG_RATE_MIN_INTERVAL_SEC,
            mod.TG_RATE_PER_MIN,
        ) = old


def test_ban_skips_without_extra_http() -> None:
    sleeps: list[float] = []
    body_429 = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 5000",
        "parameters": {"retry_after": 5000},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=body_429, request=request)

    transport = _ScriptedTransport(handler)
    import alerts_telegram as mod

    old_cap = mod.TG_429_SLEEP_CAP_SEC
    mod.TG_429_SLEEP_CAP_SEC = 0.05
    try:
        # Use explicit chat via monkeypatch resolve — set env-less by sending
        # through send_message after binding gate
        alerter = TelegramAlerter(
            token="000:TEST",
            transport=transport,
            sleep_fn=lambda s: sleeps.append(s),
        )
        # First send to known chat
        alerter.send_message("-1001", "hi")
        calls_after_first = transport.calls
        # Second should hit ban and skip without many more calls
        alerter.send_message("-1001", "hi2")
        # At most one more attempt that aborts on ban, or zero new if ban check first
        assert transport.calls - calls_after_first <= 1, transport.calls
        print(
            f"ban skip OK total_calls={transport.calls} delta={transport.calls - calls_after_first} stats={alerter.stats()}"
        )
    finally:
        mod.TG_429_SLEEP_CAP_SEC = old_cap


def test_long_ban_no_sleep_storm() -> None:
    sleeps: list[float] = []
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 48693",
        "parameters": {"retry_after": 48693},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=body, request=request)

    transport = _ScriptedTransport(handler)
    import alerts_telegram as mod

    old_cap = mod.TG_429_SLEEP_CAP_SEC
    mod.TG_429_SLEEP_CAP_SEC = 30.0
    try:
        alerter = TelegramAlerter(
            token="000:TEST", transport=transport, sleep_fn=lambda s: sleeps.append(s)
        )
        ok = alerter.send_message("-1009", "storm")
        assert ok is False
        assert transport.calls == 1, transport.calls
        assert sleeps == [], sleeps  # must not sleep 30s on long ban
        # second message: no HTTP
        alerter.send_message("-1009", "storm2")
        assert transport.calls == 1
        print(f"long-ban fast-drop OK stats={alerter.stats()}")
    finally:
        mod.TG_429_SLEEP_CAP_SEC = old_cap



def test_ban_log_throttle() -> None:
    """First ban-drop WARN full; subsequent ≤1/min summary; no ERROR spam."""
    import logging
    from alerts_telegram import TelegramAlerter

    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 5000",
        "parameters": {"retry_after": 5000},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=body, request=request)

    transport = _ScriptedTransport(handler)
    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    lg = logging.getLogger("dexscreener.alerts.telegram")
    cap = _Cap()
    lg.addHandler(cap)
    old_level = lg.level
    lg.setLevel(logging.INFO)
    try:
        import alerts_telegram as mod
        old_cap = mod.TG_429_SLEEP_CAP_SEC
        mod.TG_429_SLEEP_CAP_SEC = 30.0
        alerter = TelegramAlerter(
            token="000:TEST", transport=transport, sleep_fn=lambda s: None
        )
        # First long-ban drop
        alerter.send_message("-42", "a")
        # Many more drops while banned (no HTTP)
        for i in range(5):
            alerter.send_message("-42", f"b{i}")
        warns = [r for r in records if r.levelno == logging.WARNING]
        errors = [r for r in records if r.levelno == logging.ERROR]
        msgs = [r.getMessage() for r in warns]
        assert any("ban drop (first)" in m for m in msgs), msgs
        # Without advancing 60s, should NOT spam summary for every drop
        assert sum(1 for m in msgs if "ban drop (first)" in m) == 1
        assert sum(1 for m in msgs if "ban drop (summary)" in m) == 0
        assert not errors, [e.getMessage() for e in errors]
        # Force summary by rewinding last_ban_warn_ts
        gate = alerter._gate("-42")  # noqa: SLF001
        gate.last_ban_warn_ts = 0.0
        alerter.send_message("-42", "c")
        warns2 = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        assert any("ban drop (summary)" in m and "dropped=" in m for m in warns2), warns2
        print(f"ban log throttle OK warns={len(warns2)} http={transport.calls}")
        mod.TG_429_SLEEP_CAP_SEC = old_cap
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)


def test_success_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {}}, request=request)

    transport = _ScriptedTransport(handler)
    alerter = TelegramAlerter(token="000:TEST", transport=transport, sleep_fn=lambda s: None)
    assert alerter.send_message("-1002", "ok") is True
    assert transport.calls == 1
    print("success path OK")


def main() -> int:
    reset_telegram_alerter_for_tests()
    test_parse_retry_after()
    test_429_max_retries_and_ban()
    test_ban_skips_without_extra_http()
    test_long_ban_no_sleep_storm()
    test_ban_log_throttle()
    test_success_path()
    print("TG_429_DEMO_OK (no real Telegram calls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
