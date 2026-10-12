"""Execution gateway Step A/B/C: decision_log → validate → PM → optional PaperExecutor.

Step A (default): validate + ledger receipt only (executed=0).
Step B: when EXECUTION_PAPER_ENABLED / paper_enabled, validated buy/sell → PaperExecutor.
Step C: PositionManager after validation, before executor (PM_ENABLED=0 → pass-through).
Pre-trade risk (EXECUTION_RISK_ENABLED=0 default) runs after PM and before the executor.
Never calls LiveExecutor. Main decision DB is read-only.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from logging_setup import get_logger

from execution.config import EXECUTION_MAX_OPEN, EXECUTION_RISK_ENABLED
from execution.ledger import ExecutionLedger, _utc
from execution.risk import run_all
import execution.risk.limits  # noqa: F401 — register checkers on import

log = get_logger("execution.gateway")

REQUIRED_FIELDS = (
    "strategy_id",
    "action",
    "chain_id",
    "token_address",
    "pair_address",
    "size_pct",
)
ALLOWED_ACTIONS = frozenset({"buy", "sell"})
STEP_A_EXECUTOR = "step_a_noop"
PAPER_EXECUTOR = "paper"
PM_GATE = "position_manager"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_decision_db(explicit: str | Path | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        p = Path(str(explicit)).expanduser()
        return p if p.is_absolute() else (Path.cwd() / p).resolve()
    from decision_engine.config import DB_PATH

    return Path(DB_PATH).resolve()


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_decision_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Normalize a decision_log row.

    pair_address MUST come from json.loads(raw_json)["pair_address"].
    extras live under raw["extras"].
    """
    d = dict(row)
    raw: dict[str, Any] = {}
    raw_json = d.get("raw_json")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                raw = parsed
        except (json.JSONDecodeError, TypeError):
            raw = {}

    pair_address = raw.get("pair_address")
    extras = raw.get("extras") if isinstance(raw.get("extras"), dict) else {}

    # Prefer table columns for core fields; fall back to raw_json
    strategy_id = d.get("strategy_id") or raw.get("strategy_id")
    action = d.get("action") or raw.get("action")
    chain_id = d.get("chain_id") if d.get("chain_id") is not None else raw.get("chain_id")
    token_address = (
        d.get("token_address")
        if d.get("token_address") is not None
        else raw.get("token_address")
    )
    size_pct = d.get("size_pct") if d.get("size_pct") is not None else raw.get("size_pct")
    reason = d.get("reason") if d.get("reason") is not None else raw.get("reason")
    mode = d.get("mode") or raw.get("mode")

    return {
        "decision_log_id": int(d["id"]),
        "ts": d.get("ts"),
        "strategy_id": strategy_id,
        "action": action,
        "chain_id": chain_id,
        "token_address": token_address,
        "pair_address": pair_address,
        "size_pct": size_pct,
        "reason": reason,
        "mode": mode,
        "risk_passed": d.get("risk_passed"),
        "extras": extras,
        "raw": raw,
    }


def validate_signal(sig: dict[str, Any]) -> list[str]:
    """Return list of validation errors (empty = ok)."""
    errors: list[str] = []

    for key in REQUIRED_FIELDS:
        val = sig.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            errors.append(f"missing_required:{key}")

    action = sig.get("action")
    if action is not None and str(action).strip():
        if str(action).strip().lower() not in ALLOWED_ACTIONS:
            errors.append(f"invalid_action:{action}")

    size = sig.get("size_pct")
    if size is not None:
        try:
            f = float(size)
            if not (0.0 < f <= 1.0):
                errors.append(f"invalid_size_pct:{size}")
        except (TypeError, ValueError):
            errors.append(f"invalid_size_pct:{size}")

    return errors


@dataclass
class ProcessStats:
    processed: int = 0
    validation_ok: int = 0
    validation_fail: int = 0
    pm_rejected: int = 0
    executed_ok: int = 0
    executed_fail: int = 0
    last_decision_id: int | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)


class ExecutionGateway:
    """Consume decision_log incrementally; write receipts to ExecutionLedger."""

    def __init__(
        self,
        *,
        decision_db: str | Path | None = None,
        ledger: ExecutionLedger | None = None,
        ledger_path: str | Path | None = None,
        paper_enabled: bool | None = None,
        paper_db: str | Path | None = None,
        paper_executor: Any | None = None,
        position_manager: Any | None = None,
        pm_enabled: bool | None = None,
    ) -> None:
        self.decision_db = resolve_decision_db(decision_db)
        self._owns_ledger = ledger is None
        self.ledger = ledger or ExecutionLedger(ledger_path)
        if paper_enabled is None:
            paper_enabled = _env_bool("EXECUTION_PAPER_ENABLED", False)
        self.paper_enabled = bool(paper_enabled)
        self.paper_db = paper_db
        self._paper_executor = paper_executor
        self._owns_paper = False
        if pm_enabled is None:
            pm_enabled = _env_bool("PM_ENABLED", False)
        self.pm_enabled = bool(pm_enabled)
        self._position_manager = position_manager
        self._owns_pm = False

    def _get_paper(self) -> Any:
        if self._paper_executor is not None:
            return self._paper_executor
        if not self.paper_enabled:
            return None
        from execution.executors.paper_executor import PaperExecutor

        self._paper_executor = PaperExecutor(
            paper_db=self.paper_db,
            crawler_db=self.decision_db,
        )
        self._owns_paper = True
        return self._paper_executor

    def _get_pm(self) -> Any:
        """PositionManager: always constructable; PM_ENABLED=0 → pass-through."""
        if self._position_manager is not None:
            return self._position_manager
        from execution.position_manager import (
            PositionManager,
            PositionManagerConfig,
            resolve_paper_db_for_pm,
        )

        paper_db = self.paper_db or resolve_paper_db_for_pm()
        # Prefer live PaperExecutor ledger when paper is on (same book)
        ledger = None
        if self.paper_enabled:
            paper = self._get_paper()
            if paper is not None:
                ledger = paper.ledger
                paper_db = getattr(paper, "paper_db", paper_db)
        cfg = PositionManagerConfig.from_env()
        cfg.enabled = self.pm_enabled
        self._position_manager = PositionManager(
            ledger=ledger,
            paper_db=paper_db if ledger is None else None,
            config=cfg,
        )
        self._owns_pm = ledger is None and paper_db is not None
        return self._position_manager

    def close(self) -> None:
        if self._owns_pm and self._position_manager is not None:
            self._position_manager.close()
            self._position_manager = None
            self._owns_pm = False
        if self._owns_paper and self._paper_executor is not None:
            self._paper_executor.close()
            self._paper_executor = None
            self._owns_paper = False
        if self._owns_ledger:
            self.ledger.close()

    def __enter__(self) -> "ExecutionGateway":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def fetch_pending(
        self, since_id: int, *, limit: int | None = None
    ) -> list[sqlite3.Row]:
        conn = _open_ro(self.decision_db)
        try:
            sql = (
                "SELECT id, ts, strategy_id, action, chain_id, token_address, "
                "size_pct, reason, risk_passed, mode, raw_json "
                "FROM decision_log WHERE id > ? ORDER BY id ASC"
            )
            params: list[Any] = [int(since_id)]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def _risk_ledger(self, pm: Any) -> Any | None:
        """Paper book for pre-trade checks. Does not create a DB when no path is set."""
        if self._paper_executor is not None:
            return getattr(self._paper_executor, "ledger", None)
        if self.paper_enabled:
            paper = self._get_paper()
            if paper is not None:
                return getattr(paper, "ledger", None)
        ledger = getattr(pm, "ledger", None)
        if ledger is not None:
            return ledger
        ensure = getattr(pm, "_ensure_ledger", None)
        if callable(ensure):
            return ensure()
        return None

    def _pretrade_risk(self, pm: Any, sig: dict[str, Any]) -> list[dict[str, Any]]:
        """After position_manager, before executor. Default off → no behavior change."""
        if not EXECUTION_RISK_ENABLED:
            return []
        ledger = self._risk_ledger(pm)
        if ledger is None:
            return []
        return run_all(
            ledger,
            sig,
            max_open_per_strategy=EXECUTION_MAX_OPEN,
        )

    def process_one(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        sig = parse_decision_row(row)
        errors = validate_signal(sig)
        ok = len(errors) == 0
        mode = (sig.get("mode") or "").strip().lower()

        action = sig.get("action")
        if isinstance(action, str):
            action = action.strip().lower()
        size_pct = sig.get("size_pct")
        try:
            size_pct_f: float | None = float(size_pct) if size_pct is not None else None
        except (TypeError, ValueError):
            size_pct_f = None

        strategy_id = str(sig.get("strategy_id") or "unknown")

        executed = 0
        executor = STEP_A_EXECUTOR
        exec_result: dict[str, Any] | None = None
        exec_error: str | None = None

        step_label = "A"
        if self.paper_enabled:
            step_label = "B"
        if self.pm_enabled:
            step_label = "C" if not self.paper_enabled else "B+C"

        receipt_body: dict[str, Any] = {
            "step": step_label,
            "decision_log_id": sig["decision_log_id"],
            "mode": sig.get("mode"),
            "extras": sig.get("extras") or {},
            "pair_address_source": "raw_json.pair_address",
            "paper_enabled": self.paper_enabled,
            "pm_enabled": self.pm_enabled,
        }

        pm_result: dict[str, Any] | None = None

        if not ok:
            receipt_body["status"] = "validation_failed"
            receipt_body["note"] = "validation_failed; not executed"
        else:
            # Step C: PM gate after validation, before executor (buy only)
            pm = self._get_pm()
            check = pm.check_open(sig)
            pm_result = check.as_dict() if hasattr(check, "as_dict") else dict(check)
            receipt_body["pm_check"] = pm_result
            if not pm_result.get("allowed", True):
                executed = 0
                executor = PM_GATE
                exec_error = str(
                    pm_result.get("reason") or "pm_rejected:unknown"
                )
                receipt_body["status"] = "pm_rejected"
                receipt_body["note"] = "PositionManager denied; not executed"
                receipt_body["error"] = exec_error
            else:
                risk_rejected = self._pretrade_risk(pm, sig)
                if risk_rejected:
                    executed = 0
                    executor = "risk"
                    exec_error = ";".join(
                        str(r.get("reason") or "") for r in risk_rejected
                    )
                    receipt_body["status"] = "risk_rejected"
                    receipt_body["note"] = "pre-trade risk denied; not executed"
                    receipt_body["error"] = exec_error
                    receipt_body["risk"] = risk_rejected
                elif self.paper_enabled and action in ALLOWED_ACTIONS:
                    paper = self._get_paper()
                    assert paper is not None
                    executor = PAPER_EXECUTOR
                    exec_result = paper.execute(sig)
                    if exec_result.get("ok"):
                        executed = 1
                        receipt_body["status"] = "paper_executed"
                        receipt_body["note"] = "PaperExecutor ok"
                    else:
                        executed = 0
                        exec_error = str(exec_result.get("error") or "paper_failed")
                        receipt_body["status"] = "paper_failed"
                        receipt_body["note"] = "PaperExecutor returned ok=false; no retry"
                        receipt_body["error"] = exec_error
                    receipt_body["exec_result"] = exec_result
                else:
                    # Step A path (or paper off)
                    executor = STEP_A_EXECUTOR
                    executed = 0
                    if mode == "dry_run":
                        receipt_body["status"] = "dry_run_acked"
                    else:
                        receipt_body["status"] = "validated_not_executed"
                    receipt_body["note"] = (
                        "validate_and_ledger_only; PaperExecutor gated off"
                        if not self.paper_enabled
                        else "validated; action not buy/sell"
                    )

        if ok:
            order_status = "executed" if executed == 1 else "validated"
        else:
            order_status = "rejected"
        if exec_error:
            order_status = "rejected"
        status_updated_at = _utc()

        exec_id = self.ledger.append_receipt(
            decision_log_id=sig["decision_log_id"],
            strategy_id=strategy_id,
            action=action,
            chain_id=sig.get("chain_id"),
            token_address=sig.get("token_address"),
            pair_address=sig.get("pair_address"),
            size_pct=size_pct_f,
            reason=sig.get("reason"),
            mode=sig.get("mode"),
            validation_ok=ok,
            validation_errors=errors or None,
            executed=executed,
            executor=executor,
            receipt=receipt_body,
            ts=_utc(),
            status=order_status,
            status_updated_at=status_updated_at,
        )
        self.ledger.set_last_processed_id(sig["decision_log_id"])

        out = {
            "execution_log_id": exec_id,
            "decision_log_id": sig["decision_log_id"],
            "strategy_id": strategy_id,
            "validation_ok": ok,
            "validation_errors": errors,
            "executed": executed,
            "executor": executor,
            "error": exec_error,
            "status": order_status,
            "status_updated_at": status_updated_at,
            "exec_result": exec_result,
            "pm_check": pm_result,
            "mode": sig.get("mode"),
            "action": action,
            "pair_address": sig.get("pair_address"),
            "receipt": receipt_body,
        }
        log.info(
            "receipt decision_log_id=%s strategy=%s ok=%s executed=%s executor=%s mode=%s",
            sig["decision_log_id"],
            strategy_id,
            ok,
            executed,
            executor,
            sig.get("mode"),
        )
        return out

    def process_pending(
        self,
        *,
        since_id: int | None = None,
        limit: int | None = None,
    ) -> ProcessStats:
        """Process decision_log rows with id > since_id (default: ledger cursor)."""
        cursor = (
            int(since_id)
            if since_id is not None
            else self.ledger.get_last_processed_id()
        )
        rows = self.fetch_pending(cursor, limit=limit)
        stats = ProcessStats()
        for row in rows:
            result = self.process_one(row)
            stats.processed += 1
            stats.last_decision_id = result["decision_log_id"]
            if result["validation_ok"]:
                stats.validation_ok += 1
            else:
                stats.validation_fail += 1
            if result.get("executed"):
                stats.executed_ok += 1
            elif (
                result.get("executor") == PM_GATE
                and result.get("validation_ok")
                and result.get("error")
            ):
                stats.pm_rejected += 1
            elif (
                result.get("executor") == PAPER_EXECUTOR
                and result.get("validation_ok")
                and result.get("error")
            ):
                stats.executed_fail += 1
            stats.receipts.append(result)
        return stats
