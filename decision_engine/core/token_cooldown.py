"""Per-token buy re-emit cooldown + stable signal_id.

After a written buy for (chain_id, token_address), further buys for that token
are suppressed (not written) for ``cooldown_min`` minutes. State is rebuilt on
startup from ``decision_log`` in the output DB (last buy ts per token), so a
restart does not reset it. ``cooldown_min=0`` disables suppression; rows are
still labelled.

Every buy row that is written gets extras:
- ``signal_id``: ``"<chain>:<token>:<first_ts>"``; first_ts = ts of the first
  buy of the current episode (an episode ends after ``window`` minutes without
  a written buy for the token).
- ``is_first``: True when this row starts a new episode.
- ``dup_of``: signal_id of the episode it repeats (None when is_first).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.token_cooldown")

DEFAULT_TOKEN_COOLDOWN_MIN = 60.0
_LABEL_WINDOW_MIN_WHEN_DISABLED = 60.0


def _parse_ts(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(sig: dict[str, Any]) -> tuple[str, str]:
    return (str(sig.get("chain_id") or ""), str(sig.get("token_address") or "").lower())


class TokenCooldown:
    def __init__(self, cooldown_min: float = DEFAULT_TOKEN_COOLDOWN_MIN):
        self.cooldown_min = max(0.0, float(cooldown_min or 0.0))
        self.window_sec = 60.0 * (self.cooldown_min or _LABEL_WINDOW_MIN_WHEN_DISABLED)
        # key -> (last_buy_dt, episode_signal_id)
        self._state: dict[tuple[str, str], tuple[datetime, str]] = {}
        self.suppressed = 0

    @property
    def enabled(self) -> bool:
        return self.cooldown_min > 0

    def load_from_db(self, db_path: str) -> int:
        """Rebuild state from decision_log buys (read-only). Returns tokens loaded."""
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return 0
        try:
            rows = conn.execute(
                "SELECT chain_id, token_address, ts, raw_json FROM decision_log "
                "WHERE action='buy' ORDER BY ts, id"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for chain, token, ts, raw in rows:
            dt = _parse_ts(ts)
            if dt is None:
                continue
            sid = None
            try:
                sid = ((json.loads(raw or "{}").get("extras") or {}).get("signal_id"))
            except (ValueError, AttributeError):
                sid = None
            self._observe({"chain_id": chain, "token_address": token}, dt, sid)
        log.info("token_cooldown loaded tokens=%s cooldown_min=%s", len(self._state), self.cooldown_min)
        return len(self._state)

    def _observe(self, sig: dict[str, Any], now: datetime, known_sid: str | None = None) -> tuple[bool, str, str | None]:
        key = _key(sig)
        prev = self._state.get(key)
        if prev is not None and (now - prev[0]).total_seconds() < self.window_sec:
            sid = prev[1]
            self._state[key] = (now, sid)
            return False, sid, sid
        sid = known_sid or f"{key[0]}:{key[1]}:{_iso(now)}"
        self._state[key] = (now, sid)
        return True, sid, None

    def filter(self, signals: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for sig in signals:
            if str(sig.get("action") or "").lower() != "buy":
                out.append(sig)
                continue
            ts = _parse_ts(sig.get("ts")) or now
            prev = self._state.get(_key(sig))
            if self.enabled and prev is not None and (ts - prev[0]).total_seconds() < self.cooldown_min * 60:
                self.suppressed += 1
                log.info(
                    "token cooldown skip chain=%s token=%s age=%.0fs need=%.0fs dup_of=%s",
                    sig.get("chain_id"), str(sig.get("token_address") or "")[:16],
                    (ts - prev[0]).total_seconds(), self.cooldown_min * 60, prev[1])
                continue
            is_first, sid, dup = self._observe(sig, ts)
            sig = dict(sig)
            sig.setdefault("ts", _iso(ts))
            sig["signal_id"] = sid
            sig["is_first"] = is_first
            sig["dup_of"] = dup
            out.append(sig)
        return out
