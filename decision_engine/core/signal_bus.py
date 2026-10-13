"""Standard signal structure + dry-run sink (log + decision_log table)."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from logging_setup import get_logger

log = get_logger("decision_engine.signal_bus")

# Matches sqlite3.connect(timeout=30). One extra attempt after a short pause.
_BUSY_TIMEOUT_MS = 30000
_PUBLISH_RETRY_SLEEP_SEC = 0.2

DECISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    action TEXT NOT NULL,
    chain_id TEXT,
    token_address TEXT,
    size_pct REAL,
    reason TEXT,
    risk_passed INTEGER,
    mode TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log (ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_strategy ON decision_log (strategy_id);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_signal(sig: dict[str, Any], *, mode: str = "dry_run") -> dict[str, Any]:
    """Ensure required keys; fill defaults."""
    out = {
        "strategy_id": str(sig.get("strategy_id") or "unknown"),
        "action": str(sig.get("action") or "noop").lower(),
        "chain_id": sig.get("chain_id"),
        "token_address": sig.get("token_address"),
        "pair_address": sig.get("pair_address"),
        "size_pct": float(sig["size_pct"]) if sig.get("size_pct") is not None else None,
        "reason": sig.get("reason") or "",
        "ts": sig.get("ts") or utc_now_iso(),
        "risk_passed": bool(sig.get("risk_passed", False)),
        "mode": mode,
    }
    # preserve extras in raw
    extras = {k: v for k, v in sig.items() if k not in out}
    if extras:
        out["_extras"] = extras
    return out


class SignalBus:
    """
    Collect candidate signals; in dry-run emit structured logs and write decision_log
    via a dedicated RW connection (separate from indicator RO adapter).
    """

    def __init__(self, db_path: str | Path, *, dry_run: bool = True):
        self.db_path = Path(db_path).resolve()
        self.dry_run = dry_run
        self.mode = "dry_run" if dry_run else "live_stub"
        self._rw: sqlite3.Connection | None = None
        self._emitted: list[dict[str, Any]] = []
        self._ensure_rw()

    def _ensure_rw(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._rw = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._rw.row_factory = sqlite3.Row
        self._rw.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._rw.executescript(DECISION_LOG_DDL)
        self._rw.commit()
        log.info("signal_bus decision_log ready db=%s mode=%s", self.db_path, self.mode)

    def close(self) -> None:
        if self._rw is not None:
            self._rw.close()
            self._rw = None

    def __enter__(self) -> "SignalBus":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def emitted(self) -> list[dict[str, Any]]:
        return list(self._emitted)

    def publish(self, signal: dict[str, Any]) -> dict[str, Any] | None:
        norm = normalize_signal(signal, mode=self.mode)
        log.info(
            "SIGNAL strategy=%s action=%s chain=%s token=%s size_pct=%s risk=%s reason=%s mode=%s",
            norm["strategy_id"],
            norm["action"],
            norm["chain_id"],
            (norm["token_address"] or "")[:16],
            norm["size_pct"],
            norm["risk_passed"],
            norm["reason"],
            norm["mode"],
        )
        if not self._persist(norm):
            return None
        self._emitted.append(norm)
        return norm

    def publish_many(self, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sig in signals:
            norm = self.publish(sig)
            if norm is not None:
                out.append(norm)
        return out

    def _persist(self, norm: dict[str, Any]) -> bool:
        """Write one row. On failure, retry once and warn — do not abort the batch."""
        try:
            self._write_log(norm)
            return True
        except sqlite3.Error as exc:
            log.warning(
                "decision_log publish failed; retrying once strategy=%s action=%s token=%s err=%s",
                norm.get("strategy_id"),
                norm.get("action"),
                (norm.get("token_address") or "")[:16],
                exc,
            )
            time.sleep(_PUBLISH_RETRY_SLEEP_SEC)
            try:
                self._write_log(norm)
                return True
            except sqlite3.Error as exc2:
                log.warning(
                    "decision_log publish dropped after retry strategy=%s action=%s token=%s err=%s",
                    norm.get("strategy_id"),
                    norm.get("action"),
                    (norm.get("token_address") or "")[:16],
                    exc2,
                )
                return False

    def _write_log(self, norm: dict[str, Any]) -> None:
        assert self._rw is not None
        raw = {k: v for k, v in norm.items() if k != "_extras"}
        if "_extras" in norm:
            raw["extras"] = norm["_extras"]
        try:
            self._rw.execute(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, chain_id, token_address, size_pct,
                     reason, risk_passed, mode, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm["ts"],
                    norm["strategy_id"],
                    norm["action"],
                    norm.get("chain_id"),
                    norm.get("token_address"),
                    norm.get("size_pct"),
                    norm.get("reason"),
                    1 if norm.get("risk_passed") else 0,
                    norm["mode"],
                    json.dumps(raw, ensure_ascii=False, default=str),
                ),
            )
            self._rw.commit()
        except sqlite3.Error:
            try:
                self._rw.rollback()
            except sqlite3.Error:
                pass
            raise
