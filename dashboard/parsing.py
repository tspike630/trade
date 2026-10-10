"""Pure parsers shared with the crawler's review.data helpers.

No SQLite and no I/O. Display text is passed through :func:`redact`.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from dashboard.auth import scrub

STRENGTH_RE = re.compile(r"jev_strength=([0-9.]+)")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|api[_-]?secret|secret|password|private[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|authorization)\b\s*[=:]\s*\S+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|password|private[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|authorization|credential)"
)


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def redact(text: str | None) -> str:
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    cleaned = _BEARER_RE.sub("Bearer [redacted]", raw)
    cleaned = _SECRET_ASSIGN_RE.sub(lambda match: f"{match.group(1)}=[redacted]", cleaned)
    return scrub(cleaned)


def clip(text: str | None, limit: int) -> str:
    cleaned = redact(text).replace("\n", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 1:
        return "…"
    return cleaned[: limit - 1] + "…"


def extract_jev_strength(raw: dict[str, Any], reason: str | None) -> float | None:
    """Same order as review.data._extract_jev_strength."""
    extras = raw.get("extras")
    if isinstance(extras, dict):
        value = safe_float(extras.get("jev_strength"))
        if value is not None:
            return value
    value = safe_float(raw.get("jev_strength"))
    if value is not None:
        return value
    match = STRENGTH_RE.search(reason or "")
    if match:
        return safe_float(match.group(1))
    return None


def extract_coverage(raw: dict[str, Any]) -> float | None:
    sources: list[Any] = []
    extras = raw.get("extras")
    if isinstance(extras, dict):
        sources.append(extras)
    sources.append(raw)
    for source in sources:
        for key in ("coverage_pct", "coverage", "jev_coverage"):
            value = safe_float(source.get(key))
            if value is not None:
                return value
    return None


def extract_liquidity_usd(raw: dict[str, Any]) -> float | None:
    liquidity = raw.get("liquidity")
    if isinstance(liquidity, dict):
        return safe_float(liquidity.get("usd"))
    value = safe_float(liquidity)
    if value is not None:
        return value
    extras = raw.get("extras")
    if isinstance(extras, dict):
        return safe_float(extras.get("liquidity_usd"))
    return None


def extract_pair(raw: dict[str, Any]) -> str | None:
    pair = raw.get("pair_address")
    if isinstance(pair, str) and pair.strip():
        return pair.strip()
    return None


def extract_symbol(raw: dict[str, Any]) -> str | None:
    for key in ("symbol", "token_symbol"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return clip(value.strip(), 32)
    for key in ("baseToken", "base_token", "token"):
        value = raw.get(key)
        if isinstance(value, dict):
            symbol = value.get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                return clip(symbol.strip(), 32)
    return None


def bucket_key(errors: str | None) -> str:
    """First segment of validation_errors, matching review.data.summarize_execution."""
    err = redact(errors).strip()
    if not err:
        return "(empty)"
    key = err.split(";")[0].split("\n")[0].strip()
    if not key:
        return "(empty)"
    if len(key) > 80:
        key = key[:77] + "..."
    return key


def receipt_summary(receipt: dict[str, Any] | None, limit: int = 160) -> str:
    if not isinstance(receipt, dict) or not receipt:
        return "—"
    preferred = (
        "status",
        "ok",
        "error",
        "message",
        "reason",
        "tx",
        "signature",
        "order_id",
        "filled",
        "side",
    )
    parts: list[str] = []
    used: set[str] = set()
    for key in preferred:
        if key not in receipt or _SECRET_KEY_RE.search(key):
            continue
        rendered = _short_value(receipt.get(key))
        if not rendered:
            continue
        parts.append(f"{key}={rendered}")
        used.add(key)
    if not parts:
        for key, value in receipt.items():
            if key in used or _SECRET_KEY_RE.search(str(key)):
                continue
            rendered = _short_value(value)
            if not rendered:
                continue
            parts.append(f"{key}={rendered}")
            if len(parts) >= 3:
                break
    if not parts:
        return "—"
    return clip(" · ".join(parts), limit)


def _short_value(value: Any) -> str:
    if value is None or value in ("", [], {}):
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    return clip(text, 60)
