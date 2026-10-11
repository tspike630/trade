"""API key check shared with api_server.

Compares ``X-API-Key`` / the login field to ``API_SECRET`` in constant time.
The key is never logged, stored, or returned.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets

from dashboard.settings import api_secret

_SECRET_ENV = (
    "API_SECRET",
    "LIVE_EXCHANGE_API_KEY",
    "LIVE_EXCHANGE_API_SECRET",
    "LIVE_TRADING_CONFIRM",
    "GROK_API_KEY",
    "XAI_API_KEY",
)


def sensitive_values() -> list[str]:
    found: list[str] = []
    for name in _SECRET_ENV:
        raw = os.environ.get(name, "").strip()
        # Skip short values so a confirm phrase of "true" cannot blank unrelated logs.
        if len(raw) >= 8:
            found.append(raw)
    found.sort(key=len, reverse=True)
    return found


def scrub(message: str) -> str:
    cleaned = message
    for value in sensitive_values():
        if value in cleaned:
            cleaned = cleaned.replace(value, "[redacted]")
    return cleaned


class SecretFilter(logging.Filter):
    """Drop known secret values if they ever reach a log line."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = scrub(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def verify_api_key(provided: str | None) -> bool:
    secret = api_secret()
    if not secret or not provided:
        return False
    try:
        return secrets.compare_digest(provided, secret)
    except (TypeError, ValueError):
        provided_digest = hashlib.sha256(provided.encode("utf-8", errors="replace")).digest()
        secret_digest = hashlib.sha256(secret.encode("utf-8", errors="replace")).digest()
        return secrets.compare_digest(provided_digest, secret_digest)
