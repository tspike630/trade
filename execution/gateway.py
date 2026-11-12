"""Execution gateway Step A/B/C: decision_log → validate → PM → optional PaperExecutor.

Step A (default): validate + ledger receipt only (executed=0).
Step B: when EXECUTION_PAPER_ENABLED / paper_enabled, validated buy/sell → PaperExecutor.
Step C: PositionManager after validation, before executor (PM_ENABLED=0 → pass-through).
Pre-trade risk (EXECUTION_RISK_ENABLED=0 default) runs after PM and before the executor.
Purpose gate (P2-38): by default only decision purpose ``live_candidate`` may
reach validation or a fill. ``sample``, NULL/missing, and any other value are
rejected with executed=0 and the cursor still advances. Peer runners may pass
``accept_purposes`` (CLI ``--accept-purpose`` / env ``EXECUTION_ACCEPT_PURPOSE``)
to allow a single alternate purpose such as ``race_*`` or ``shadow_entry_ih_ge1h``
for that process only; other purposes stay rejected. Paper / PM / risk stay
default off. Never calls LiveExecutor. Main decision DB is read-only.

``live_bnb_boll_fade`` and ``live_bnb_boll_classic`` (only when that one
purpose is on this process's allowlist) are handed to ``BinanceSpotAdapter``
instead of PaperExecutor. The adapter refuses a live BUY while the
kill-switch file is present or ``BINANCE_SPOT_LIVE_ENABLED`` is off. A
completed BUY requires a verified native OCO. ``mode=dry_run`` never
submits. No other purpose can reach that adapter. Each arm may hold several
symbols. The same symbol is refused when the other arm already has it open.
A sealed USDT free balance is reduced by priced holds on either arm before
the buy.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from logging_setup import get_logger

from execution.config import (
    EXECUTION_DAILY_LOSS_USD,
    EXECUTION_EVENTS_ENABLED,
    EXECUTION_MAX_OPEN,
    EXECUTION_RISK_ENABLED,
)
from execution.binance_account_snapshot import default_live_bnb_adapter
from execution.executors.binance_spot_gate import (
    REST_BOOK,
    consumer_read_cursor,
    is_live_bnb_spot_purpose,
    sole_live_bnb_purpose,
    spot_account_uid,
)
from execution.executors.binance_spot import (
    EXECUTOR_NAME as BINANCE_SPOT_EXECUTOR,
    BinanceSpotAdapter,
    held_position_spec,
    kill_switch_active,
    public_exchange_error,
)
from execution.live_bnb_dispatch import (
    CatalogProbe,
    dispatch_live_bnb,
    parent_queue_view,
)
from execution.ledger import (
    ExecutionLedger,
    _utc,
    buy_reservation_lock,
    load_ledger_registry,
    read_ledger_account_uid,
    read_ledger_membership,
    read_peer_spot_blocks,
    read_spot_buy_blocks_readonly,
)
from execution.order_events import init_events
from execution.risk import run_all
import execution.risk.limits  # noqa: F401 — register checkers on import

log = get_logger("execution.gateway")


def _required_peer_books(
    current: str | Path,
    snapshot_uid: str | None,
    accept_purposes: frozenset[str] | set[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Required books from the purpose map. A one-ledger list is incomplete.

    Every purpose in ``required_purposes`` is checked, including carriers
    this process does not accept. Zero rows are empty only after the schema
    and the binding uid match the snapshot. A snapshot with no uid cannot
    prove the binding.
    """
    if not snapshot_uid:
        return [], "ledger_account_unbound"
    registry, error = load_ledger_registry()
    if error or registry is None:
        return [], error or "ledger_registry_unmapped"
    current_path = Path(current).expanduser().resolve()
    stored, digest, stored_error = read_ledger_membership(current_path)
    if stored_error or stored is None or digest is None:
        return [], stored_error or "ledger_registry_unbound"
    file_books = {purpose: str(path) for purpose, path in registry.items()}
    if file_books != stored or current_path.as_posix() not in set(stored.values()):
        return [], "ledger_registry_incomplete"
    accepted = {str(item).strip() for item in (accept_purposes or set()) if str(item).strip()}
    if any(item not in stored for item in accepted):
        return [], "ledger_registry_incomplete"
    peer_rows: list[dict[str, Any]] = []
    for path_text in stored.values():
        path = Path(path_text)
        uid, uid_error = read_ledger_account_uid(path)
        if uid_error:
            return [], uid_error
        if uid != snapshot_uid:
            return [], "ledger_account_mismatch"
        peer_books, peer_digest, peer_error = read_ledger_membership(path)
        if peer_error or peer_digest != digest or peer_books != stored:
            return [], peer_error or "ledger_registry_incomplete"
        if path == current_path:
            continue
        rows, read_error = read_peer_spot_blocks(path)
        if read_error:
            return [], read_error
        peer_rows.extend(rows)
    return peer_rows, None

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
# decision_log.purpose (P2-38). Only live_candidate is paper-eligible.
# Comparison is case-sensitive after strip; decision writes these literals.
PURPOSE_LIVE_CANDIDATE = "live_candidate"
PURPOSE_SAMPLE = "sample"
PURPOSE_REJECT_SAMPLE = "purpose_sample"
PURPOSE_REJECT_MISSING = "purpose_missing"
PURPOSE_REJECT_INVALID = "purpose_invalid"
PURPOSE_REJECT_SHADOW = "purpose_shadow"  # shadow_* (e.g. shadow_010) never reaches the main book
_DECISION_LOG_COLUMNS = (
    "id",
    "ts",
    "strategy_id",
    "action",
    "chain_id",
    "token_address",
    "size_pct",
    "reason",
    "risk_passed",
    "mode",
    "raw_json",
)
# A claimed row younger than this is treated as in-flight (another worker).
# Older claims with no paper position are resumed. Claims that already have a
# position are finalized without a second fill.
CLAIM_STALE_SEC = 60.0


def _risk_rejected_event_detail(risk_reasons: list[dict[str, Any]]) -> dict[str, Any]:
    """``order_events`` detail for a pre-trade risk reject.

    ``reason`` is the checker key (``daily_loss`` stays exact). Date, pnl, and
    the configured limit stay under ``detail`` when the checker supplied them.
    ``stage=risk`` plus ``reason=daily_loss`` maps to ``risk_rejected:daily_loss``.
    """
    payload: dict[str, Any] = {
        "stage": "risk",
        "reason": ";".join(str(item.get("reason") or "") for item in risk_reasons),
    }
    for item in risk_reasons:
        extra = item.get("detail")
        if str(item.get("reason") or "") == "daily_loss" and isinstance(extra, dict):
            payload["detail"] = extra
            break
    return payload


_BINANCE_ORDER_SECRET_KEYS = frozenset(
    {"signature", "api_key", "api_secret", "secret"}
)


def _binance_public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Ledger copy of a spot-adapter result. Secrets never land in the receipt."""
    order = result.get("order")
    if isinstance(order, dict):
        public_order = {
            key: value
            for key, value in order.items()
            if key not in _BINANCE_ORDER_SECRET_KEYS
        }
    else:
        public_order = None
    published: dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "dry_run": bool(result.get("dry_run")),
        "executed": int(result.get("executed") or 0),
        "submitted": bool(result.get("submitted")),
        "error": result.get("error"),
        "executor": result.get("executor"),
        "symbol": result.get("symbol"),
        "order": public_order,
        "exchange_order_id": result.get("exchange_order_id"),
        "gates": result.get("gates") if isinstance(result.get("gates"), dict) else {},
        "intent": result.get("intent") if isinstance(result.get("intent"), dict) else None,
        "exit_plan": (
            result.get("exit_plan") if isinstance(result.get("exit_plan"), dict) else None
        ),
        "filled_qty": (
            result.get("filled_qty")
            if isinstance(result.get("filled_qty"), str)
            else None
        ),
        "sellable_qty": (
            result.get("sellable_qty")
            if isinstance(result.get("sellable_qty"), str)
            else None
        ),
        "size_plan": (
            result.get("size_plan") if isinstance(result.get("size_plan"), dict) else None
        ),
        "consumer_allowed": result.get("consumer_allowed") is True,
        "arm_position": (
            result.get("arm_position") if isinstance(result.get("arm_position"), dict) else None
        ),
        "oco_fields": (
            result.get("oco_fields") if isinstance(result.get("oco_fields"), dict) else None
        ),
    }
    preflight = result.get("oco_preflight")
    if isinstance(preflight, dict):
        published["oco_preflight"] = dict(preflight)
    for key in ("protection_state", "manual_action"):
        value = result.get(key)
        if isinstance(value, str) and value:
            published[key] = value
    incident = result.get("protection_incident")
    if isinstance(incident, dict):
        published["protection_incident"] = {
            "state": incident.get("state"),
            "store": incident.get("store"),
            "event": incident.get("event"),
            "delivered": False,
            "transport": "none",
            "integrations_required": incident.get("integrations_required"),
        }
    exchange_error = public_exchange_error(result.get("exchange_error"))
    if exchange_error is not None:
        published["exchange_error"] = exchange_error
    return published


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


def _decision_log_column_names(conn: sqlite3.Connection) -> set[str]:
    """Column names from PRAGMA. Empty if the table is missing."""
    rows = conn.execute("PRAGMA table_info(decision_log)").fetchall()
    return {str(row[1]) for row in rows}


def read_decision_purpose(
    row: dict[str, Any], raw: dict[str, Any] | None
) -> str | None:
    """Purpose used by the gate.

    Prefer ``decision_log.purpose`` when the SELECT included that column (the
    key is present even when the value is NULL). NULL or blank stays missing
    and does not fall through to ``raw_json`` — historical NULL rows are not
    executable. When the column is absent, use top-level
    ``raw_json["purpose"]``. Strip whitespace only; do not case-fold.
    """
    if "purpose" in row:
        value = row.get("purpose")
    else:
        value = None if raw is None else raw.get("purpose")
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


PAPER_OPEN_PAUSED_REASON = "paper_open_paused"
PAPER_OPEN_PAUSE_FLAG = Path(__file__).resolve().parents[1] / "PAPER_OPEN_PAUSED"


def paper_open_paused(flag: str | Path | None = None) -> bool:
    """Main paper book: refuse new opens while the flag file exists.

    Flag path: env EXECUTION_PAPER_OPEN_PAUSE_FLAG or <repo>/PAPER_OPEN_PAUSED.
    Also honours EXECUTION_PAPER_OPEN_PAUSED=1. Checked on every decision, so
    creating/removing the file takes effect without restart. Closes / ticks are
    unaffected.
    """
    if _env_bool("EXECUTION_PAPER_OPEN_PAUSED", False):
        return True
    path = Path(flag or os.environ.get("EXECUTION_PAPER_OPEN_PAUSE_FLAG") or PAPER_OPEN_PAUSE_FLAG)
    return path.exists()


def purpose_reject_reason(
    purpose: str | None,
    *,
    accept_purposes: frozenset[str] | set[str] | None = None,
) -> str | None:
    """None when ``purpose`` is executable for this process. Otherwise a reject code.

    Default (``accept_purposes`` is None): only ``live_candidate`` passes — the
    main / baseline paper book gate (P2-38).

    Peer mode (``accept_purposes`` is a non-None set): only purposes in that set
    pass. ``live_candidate`` does *not* auto-pass unless listed. Used by isolated
    paper peers that consume ``race_*`` / ``shadow_entry_ih_ge1h`` into their own
    PaperLedger + execution ledger.

    ``purpose`` is already stripped by ``read_decision_purpose``. Blank is missing.
    """
    if purpose is None or purpose == "":
        return PURPOSE_REJECT_MISSING
    if accept_purposes is not None:
        if purpose in accept_purposes:
            return None
        if purpose == PURPOSE_SAMPLE:
            return PURPOSE_REJECT_SAMPLE
        if purpose.startswith("shadow_"):
            return PURPOSE_REJECT_SHADOW
        return PURPOSE_REJECT_INVALID
    if purpose == PURPOSE_LIVE_CANDIDATE:
        return None
    if purpose == PURPOSE_SAMPLE:
        return PURPOSE_REJECT_SAMPLE
    if purpose.startswith("shadow_"):
        return PURPOSE_REJECT_SHADOW
    return PURPOSE_REJECT_INVALID


def parse_decision_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    """Normalize a decision_log row.

    pair_address MUST come from json.loads(raw_json)["pair_address"].
    extras live under raw["extras"].
    purpose is resolved before any claim or fill (column, else raw_json).
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
        "purpose": read_decision_purpose(d, raw),
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
        accept_purposes: frozenset[str] | set[str] | tuple[str, ...] | list[str] | None = None,
        binance_spot: BinanceSpotAdapter | None = None,
        mcp_catalog: Iterable[str] | None = None,
        mcp_transport: CatalogProbe | None = None,
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
        # None → default live_candidate-only gate. Non-None set → peer allowlist.
        if accept_purposes is None:
            self.accept_purposes: frozenset[str] | None = None
        else:
            self.accept_purposes = frozenset(
                str(p).strip() for p in accept_purposes if str(p).strip()
            )
        self._binance_spot = binance_spot
        self._binance_injected = binance_spot is not None
        self._mcp_catalog = tuple(mcp_catalog) if mcp_catalog else None
        self._mcp_transport = mcp_transport
        # Order-event table lives on the execution ledger only. Flag off → no write.
        if EXECUTION_EVENTS_ENABLED:
            init_events(self.ledger.path)

    def _get_paper(self) -> Any:
        if self._paper_executor is not None:
            return self._paper_executor
        if not self.paper_enabled:
            return None
        from execution.executors.paper_executor import PaperExecutor

        self._paper_executor = PaperExecutor(
            paper_db=self.paper_db,
            crawler_db=self.decision_db,
            observe_db=self.ledger.path,
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
            columns = list(_DECISION_LOG_COLUMNS)
            # Older decision DBs have no purpose column. SELECT must not name
            # it or the read fails; purpose then comes from raw_json only.
            if "purpose" in _decision_log_column_names(conn):
                columns.append("purpose")
            sql = (
                "SELECT "
                + ", ".join(columns)
                + " FROM decision_log WHERE id > ? ORDER BY id ASC"
            )
            params: list[Any] = [int(since_id)]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            return list(conn.execute(sql, params).fetchall())
        finally:
            conn.close()

    def _step_label(self) -> str:
        if self.pm_enabled and self.paper_enabled:
            return "B+C"
        if self.pm_enabled:
            return "C"
        if self.paper_enabled:
            return "B"
        return "A"

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
        """After position_manager, before executor. Default off → no behavior change.

        SELL skips max_open inside the checker. Daily loss is passed through and
        stays off unless EXECUTION_DAILY_LOSS_USD is set. Its day bucket is the
        Asia/Hong_Kong date of closed_at; the limit comparison is unchanged.
        Missing book fails closed
        only when the risk gate is on. F-1 loss streaks are not a checker: the
        order path does not read EXECUTION_F1_CIRCUIT and does not reject on streak.
        """
        if not EXECUTION_RISK_ENABLED:
            return []
        ledger = self._risk_ledger(pm)
        if ledger is None:
            return [{"ok": False, "reason": "no_ledger"}]
        return run_all(
            ledger,
            sig,
            max_open_per_strategy=EXECUTION_MAX_OPEN,
            daily_loss_limit_usd=EXECUTION_DAILY_LOSS_USD,
        )

    def _status_events(
        self,
        *,
        sig: dict[str, Any],
        ok: bool,
        errors: list[str],
        executed: int,
        executor: str | None,
        exec_error: str | None,
        risk_reasons: list[dict[str, Any]] | None,
        submitted: bool,
        fill_receipt: dict[str, Any] | None,
    ) -> list[tuple[str, dict[str, Any] | None]]:
        """Status chain written in the same transaction as the receipt and cursor.

        ``new`` is first even though validation already ran. step_a_noop is not
        a submission. ``cancelled`` is reserved and not emitted here.
        """
        if not EXECUTION_EVENTS_ENABLED:
            return []
        events: list[tuple[str, dict[str, Any] | None]] = [
            ("new", {"decision_log_id": sig["decision_log_id"]}),
        ]
        if not ok:
            events.append(
                (
                    "rejected",
                    {"stage": "validation", "error": ";".join(errors)},
                )
            )
            return events
        events.append(("validated", {"validation_ok": True}))
        if risk_reasons:
            events.append(("rejected", _risk_rejected_event_detail(risk_reasons)))
            return events
        if executor == PM_GATE and exec_error:
            events.append(
                (
                    "rejected",
                    {"stage": "position_manager", "reason": exec_error},
                )
            )
            return events
        incident = (
            fill_receipt.get("protection_incident")
            if isinstance(fill_receipt, dict)
            else None
        )
        if isinstance(incident, dict) and incident.get("state"):
            events.append(
                (
                    "rejected",
                    {
                        "stage": "protection",
                        "error": exec_error,
                        "protection_state": fill_receipt.get("protection_state")
                        if isinstance(fill_receipt, dict)
                        else None,
                        "delivered": False,
                        "transport": "none",
                        "integrations_required": incident.get("integrations_required"),
                    },
                )
            )
            return events
        if submitted and executor is not None:
            events.append(("submitted", {"executor": executor}))
            if executed == 1:
                events.append(("filled", {"receipt": fill_receipt}))
            elif exec_error:
                events.append(
                    (
                        "rejected",
                        {"stage": "executor", "error": exec_error},
                    )
                )
        return events

    def _position_for_decision(self, decision_log_id: int) -> dict[str, Any] | None:
        paper = self._paper_executor if self._paper_executor is not None else None
        if paper is None and self.paper_enabled:
            paper = self._get_paper()
        if paper is None:
            return None
        ledger = getattr(paper, "ledger", None)
        finder = getattr(ledger, "find_by_decision_log_id", None)
        if not callable(finder):
            return None
        found = finder(decision_log_id)
        return found if isinstance(found, dict) else None

    def _claim_age_sec(self, status_updated_at: Any) -> float:
        if not status_updated_at:
            return CLAIM_STALE_SEC + 1.0
        text = str(status_updated_at).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return CLAIM_STALE_SEC + 1.0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()

    def _finalize(
        self,
        *,
        execution_log_id: int,
        sig: dict[str, Any],
        strategy_id: str,
        action: str | None,
        size_pct_f: float | None,
        ok: bool,
        errors: list[str],
        executed: int,
        executor: str | None,
        exec_error: str | None,
        risk_reasons: list[dict[str, Any]] | None,
        submitted: bool,
        exec_result: dict[str, Any] | None,
        pm_result: dict[str, Any] | None,
        receipt_body: dict[str, Any],
        order_status: str,
    ) -> dict[str, Any]:
        status_updated_at = _utc()
        events = self._status_events(
            sig=sig,
            ok=ok,
            errors=errors,
            executed=executed,
            executor=executor,
            exec_error=exec_error,
            risk_reasons=risk_reasons,
            submitted=submitted,
            fill_receipt=exec_result,
        )
        self.ledger.finalize_receipt(
            execution_log_id,
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
            status=order_status,
            events=events,
            advance_cursor=True,
            ts=status_updated_at,
        )
        out = {
            "execution_log_id": execution_log_id,
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

    def _duplicate_result(self, sig: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
        """Terminal row already exists. Advance cursor with MAX; do not fill again."""
        row = claim.get("row") or {}
        self.ledger.set_last_processed_id(int(sig["decision_log_id"]))
        return {
            "execution_log_id": claim["execution_log_id"],
            "decision_log_id": sig["decision_log_id"],
            "strategy_id": str(sig.get("strategy_id") or row.get("strategy_id") or "unknown"),
            "validation_ok": bool(row.get("validation_ok")),
            "validation_errors": [],
            "executed": 0,
            "duplicate": True,
            "already_executed": int(row.get("executed") or 0),
            "executor": row.get("executor"),
            "error": "duplicate_decision",
            "status": row.get("status") or "duplicate",
            "status_updated_at": row.get("status_updated_at"),
            "exec_result": None,
            "pm_check": None,
            "mode": sig.get("mode"),
            "action": sig.get("action"),
            "pair_address": sig.get("pair_address"),
            "receipt": None,
        }

    def _resume_claimed(
        self,
        sig: dict[str, Any],
        claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        """None → caller should execute. Otherwise a terminal/inflight result."""
        status = str(claim.get("status") or "")
        if status != "claimed":
            return self._duplicate_result(sig, claim)
        row = claim.get("row") or {}
        pos = self._position_for_decision(int(sig["decision_log_id"]))
        if pos is not None:
            return self._finalize_existing_position(sig, claim, pos)
        age = self._claim_age_sec(row.get("status_updated_at"))
        if age < CLAIM_STALE_SEC:
            return self._inflight_result(sig, claim)
        return None

    def _inflight_result(
        self, sig: dict[str, Any], claim: dict[str, Any]
    ) -> dict[str, Any]:
        """Fresh claim belongs to an in-flight worker. Do not fill or skip it."""
        row = claim.get("row") or {}
        return {
            "execution_log_id": claim["execution_log_id"],
            "decision_log_id": sig["decision_log_id"],
            "strategy_id": str(sig.get("strategy_id") or "unknown"),
            "validation_ok": False,
            "validation_errors": [],
            "executed": 0,
            "inflight": True,
            "executor": None,
            "error": "claim_inflight",
            "status": "claimed",
            "status_updated_at": row.get("status_updated_at"),
            "exec_result": None,
            "pm_check": None,
            "mode": sig.get("mode"),
            "action": sig.get("action"),
            "pair_address": sig.get("pair_address"),
            "receipt": None,
        }

    def _finalize_existing_position(
        self,
        sig: dict[str, Any],
        claim: dict[str, Any],
        pos: dict[str, Any],
    ) -> dict[str, Any]:
        """Paper fill already committed. Write the receipt; do not execute again."""
        action = sig.get("action")
        if isinstance(action, str):
            action = action.strip().lower()
        try:
            size_pct_f: float | None = (
                float(sig["size_pct"]) if sig.get("size_pct") is not None else None
            )
        except (TypeError, ValueError):
            size_pct_f = None
        strategy_id = str(sig.get("strategy_id") or "unknown")
        exec_result = {
            "ok": True,
            "executor": PAPER_EXECUTOR,
            "resumed": True,
            "position_id": pos.get("id"),
            "note": "position already existed for decision_log_id; skipped second fill",
        }
        step_label = "B" if self.paper_enabled else "A"
        receipt_body: dict[str, Any] = {
            "step": step_label,
            "decision_log_id": sig["decision_log_id"],
            "status": "paper_executed",
            "note": "resumed claimed decision; existing position; no second fill",
            "paper_enabled": self.paper_enabled,
            "pm_enabled": self.pm_enabled,
            "exec_result": exec_result,
            "resumed": True,
        }
        return self._finalize(
            execution_log_id=int(claim["execution_log_id"]),
            sig=sig,
            strategy_id=strategy_id,
            action=action,
            size_pct_f=size_pct_f,
            ok=True,
            errors=[],
            executed=1,
            executor=PAPER_EXECUTOR,
            exec_error=None,
            risk_reasons=None,
            submitted=True,
            exec_result=exec_result,
            pm_result=None,
            receipt_body=receipt_body,
            order_status="executed",
        )

    def _reject_exception(
        self,
        row: sqlite3.Row | dict[str, Any],
        sig: dict[str, Any] | None,
        exc: BaseException,
    ) -> dict[str, Any]:
        """Bad message → rejected receipt, then advance. Last resort re-raises."""
        if sig is not None:
            did = int(sig["decision_log_id"])
        else:
            did = int(dict(row)["id"])
        strategy_id = str((sig or {}).get("strategy_id") or "unknown")
        action = (sig or {}).get("action")
        if isinstance(action, str):
            action = action.strip().lower()
        chain_id = (sig or {}).get("chain_id")
        token_address = (sig or {}).get("token_address")
        pair_address = (sig or {}).get("pair_address")
        mode = (sig or {}).get("mode")
        reason = (sig or {}).get("reason")
        size_pct = (sig or {}).get("size_pct")
        try:
            size_pct_f: float | None = float(size_pct) if size_pct is not None else None
        except (TypeError, ValueError):
            size_pct_f = None
        err = f"exception:{type(exc).__name__}:{exc}"
        claim = self.ledger.claim_decision(
            decision_log_id=did,
            strategy_id=strategy_id,
            action=action if isinstance(action, str) else None,
            chain_id=None if chain_id is None else str(chain_id),
            token_address=None if token_address is None else str(token_address),
            pair_address=None if pair_address is None else str(pair_address),
            size_pct=size_pct_f,
            reason=None if reason is None else str(reason),
            mode=None if mode is None else str(mode),
        )
        if not claim["created"] and str(claim.get("status") or "") == "claimed":
            pos = self._position_for_decision(did)
            if pos is not None:
                return self._finalize_existing_position(
                    sig
                    or {
                        "decision_log_id": did,
                        "strategy_id": strategy_id,
                        "action": action,
                        "chain_id": chain_id,
                        "token_address": token_address,
                        "pair_address": pair_address,
                        "size_pct": size_pct,
                        "reason": reason,
                        "mode": mode,
                    },
                    claim,
                    pos,
                )
        if not claim["created"] and str(claim.get("status") or "") not in ("claimed", ""):
            self.ledger.set_last_processed_id(did)
            out = self._duplicate_result(
                sig
                or {
                    "decision_log_id": did,
                    "strategy_id": strategy_id,
                    "action": action,
                    "mode": mode,
                    "pair_address": pair_address,
                },
                claim,
            )
            out["error"] = err
            out["validation_ok"] = False
            return out
        stub = sig or {
            "decision_log_id": did,
            "strategy_id": strategy_id,
            "action": action,
            "chain_id": chain_id,
            "token_address": token_address,
            "pair_address": pair_address,
            "size_pct": size_pct,
            "reason": reason,
            "mode": mode,
        }
        receipt_body = {
            "step": "A",
            "decision_log_id": did,
            "status": "rejected",
            "note": "exception; recorded rejected and advanced",
            "error": err,
            "paper_enabled": self.paper_enabled,
            "pm_enabled": self.pm_enabled,
        }
        events: list[tuple[str, dict[str, Any] | None]] = []
        if EXECUTION_EVENTS_ENABLED:
            events = [
                ("new", {"decision_log_id": did}),
                ("rejected", {"stage": "exception", "error": err}),
            ]
        status_updated_at = _utc()
        self.ledger.finalize_receipt(
            int(claim["execution_log_id"]),
            decision_log_id=did,
            strategy_id=strategy_id,
            action=action if isinstance(action, str) else None,
            chain_id=None if chain_id is None else str(chain_id),
            token_address=None if token_address is None else str(token_address),
            pair_address=None if pair_address is None else str(pair_address),
            size_pct=size_pct_f,
            reason=None if reason is None else str(reason),
            mode=None if mode is None else str(mode),
            validation_ok=False,
            validation_errors=[err],
            executed=0,
            executor=STEP_A_EXECUTOR,
            receipt=receipt_body,
            status="rejected",
            events=events,
            advance_cursor=True,
            ts=status_updated_at,
        )
        return {
            "execution_log_id": claim["execution_log_id"],
            "decision_log_id": did,
            "strategy_id": strategy_id,
            "validation_ok": False,
            "validation_errors": [err],
            "executed": 0,
            "executor": STEP_A_EXECUTOR,
            "error": err,
            "status": "rejected",
            "status_updated_at": status_updated_at,
            "exec_result": None,
            "pm_check": None,
            "mode": mode,
            "action": action,
            "pair_address": pair_address,
            "receipt": receipt_body,
        }

    def _reject_purpose(self, sig: dict[str, Any], reason: str) -> dict[str, Any]:
        """Non-executable purpose. Receipt + cursor, no paper fill.

        Checked before claim on the executable path. The receipt itself still
        claims then finalizes in the same pattern as other rejected rows, so a
        crash cannot resume this id into a fill (a stale claim is finalized
        here instead of executed).
        """
        action = sig.get("action")
        if isinstance(action, str):
            action = action.strip().lower()
        size_pct = sig.get("size_pct")
        try:
            size_pct_f: float | None = float(size_pct) if size_pct is not None else None
        except (TypeError, ValueError):
            size_pct_f = None
        strategy_id = str(sig.get("strategy_id") or "unknown")
        chain_id = sig.get("chain_id")
        token_address = sig.get("token_address")
        pair_address = sig.get("pair_address")
        mode = sig.get("mode")
        signal_reason = sig.get("reason")
        claim = self.ledger.claim_decision(
            decision_log_id=int(sig["decision_log_id"]),
            strategy_id=strategy_id,
            action=action,
            chain_id=None if chain_id is None else str(chain_id),
            token_address=None if token_address is None else str(token_address),
            pair_address=None if pair_address is None else str(pair_address),
            size_pct=size_pct_f,
            reason=None if signal_reason is None else str(signal_reason),
            mode=None if mode is None else str(mode),
        )
        if not claim["created"]:
            status = str(claim.get("status") or "")
            if status != "claimed":
                out = self._duplicate_result(sig, claim)
                out["validation_ok"] = False
                out["error"] = reason
                return out
            row = claim.get("row") or {}
            if self._claim_age_sec(row.get("status_updated_at")) < CLAIM_STALE_SEC:
                return self._inflight_result(sig, claim)
        receipt_body: dict[str, Any] = {
            "step": self._step_label(),
            "decision_log_id": sig["decision_log_id"],
            "status": "rejected",
            "reason": reason,
            "error": reason,
            "purpose": sig.get("purpose"),
            "note": (
                "paper open paused (flag); not executed"
                if reason == PAPER_OPEN_PAUSED_REASON
                else "purpose gate; not executed"
            ),
            "paper_enabled": self.paper_enabled,
            "pm_enabled": self.pm_enabled,
        }
        events: list[tuple[str, dict[str, Any] | None]] = []
        if EXECUTION_EVENTS_ENABLED:
            events = [
                ("new", {"decision_log_id": sig["decision_log_id"]}),
                (
                    "rejected",
                    {
                        "stage": "purpose",
                        "error": reason,
                        "purpose": sig.get("purpose"),
                    },
                ),
            ]
        status_updated_at = _utc()
        self.ledger.finalize_receipt(
            int(claim["execution_log_id"]),
            decision_log_id=int(sig["decision_log_id"]),
            strategy_id=strategy_id,
            action=action,
            chain_id=None if chain_id is None else str(chain_id),
            token_address=None if token_address is None else str(token_address),
            pair_address=None if pair_address is None else str(pair_address),
            size_pct=size_pct_f,
            reason=None if signal_reason is None else str(signal_reason),
            mode=None if mode is None else str(mode),
            validation_ok=False,
            validation_errors=[reason],
            executed=0,
            executor=STEP_A_EXECUTOR,
            receipt=receipt_body,
            status="rejected",
            events=events,
            advance_cursor=True,
            ts=status_updated_at,
        )
        log.info(
            "purpose gate reject decision_log_id=%s purpose=%s reason=%s",
            sig["decision_log_id"],
            sig.get("purpose"),
            reason,
        )
        return {
            "execution_log_id": claim["execution_log_id"],
            "decision_log_id": sig["decision_log_id"],
            "strategy_id": strategy_id,
            "validation_ok": False,
            "validation_errors": [reason],
            "executed": 0,
            "executor": STEP_A_EXECUTOR,
            "error": reason,
            "status": "rejected",
            "status_updated_at": status_updated_at,
            "exec_result": None,
            "pm_check": None,
            "mode": mode,
            "action": action,
            "pair_address": pair_address,
            "purpose": sig.get("purpose"),
            "receipt": receipt_body,
        }

    def _arm_binance_position(
        self,
        sig: dict[str, Any],
        exec_result: dict[str, Any],
        receipt_body: dict[str, Any],
    ) -> None:
        held = held_position_spec(sig, exec_result)
        if held is None:
            return
        try:
            armed = self.ledger.arm_binance_spot_position(held)
            fill = exec_result.get("entry_fill")
            if isinstance(fill, dict) and armed.get("id") is not None:
                self.ledger.record_exchange_entry_origin(
                    int(armed["id"]),
                    symbol=str(armed.get("symbol") or ""),
                    placed=fill,
                )
                refreshed = self.ledger.binance_spot_position_by_id(int(armed["id"]))
                if isinstance(refreshed, dict):
                    armed = refreshed
            oco_fields = exec_result.get("oco_fields")
            if isinstance(oco_fields, dict) and oco_fields and armed.get("id") is not None:
                updated = self.ledger.update_binance_spot_position(int(armed["id"]), oco_fields)
                if isinstance(updated, dict):
                    armed = updated
        except Exception:  # noqa: BLE001 — a sent order must still leave a receipt
            log.exception(
                "binance spot position arm failed decision_log_id=%s",
                sig.get("decision_log_id"),
            )
            receipt_body["held_position_error"] = "arm_failed"
            return
        receipt_body["held_position"] = {
            "status": armed.get("status"),
            "quantity": armed.get("quantity"),
            "symbol": armed.get("symbol"),
            "protection": armed.get("protection"),
            "stop_price": armed.get("stop_price"),
            "take_profit_price": armed.get("take_profit_price"),
            "purpose": armed.get("purpose"),
            "residual_qty": armed.get("residual_qty"),
            "exit_filled_qty": armed.get("exit_filled_qty"),
        }
        exec_result["held_position"] = receipt_body["held_position"]

    def process_one(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        sig: dict[str, Any] | None = None
        try:
            sig = parse_decision_row(row)
            reason = purpose_reject_reason(
                sig.get("purpose"), accept_purposes=self.accept_purposes
            )
            if reason is not None:
                return self._reject_purpose(sig, reason)
            if (
                str(sig.get("action") or "").strip().lower() == "buy"
                and not is_live_bnb_spot_purpose(sig.get("purpose"))
                and paper_open_paused()
            ):
                return self._reject_purpose(sig, PAPER_OPEN_PAUSED_REASON)
            return self._process_one_claimed(sig)
        except Exception as exc:  # noqa: BLE001 — bad message must not wedge the cursor
            log.exception(
                "process_one failed decision_log_id=%s",
                None if sig is None else sig.get("decision_log_id"),
            )
            return self._reject_exception(row, sig, exc)

    def _process_one_claimed(self, sig: dict[str, Any]) -> dict[str, Any]:
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

        claim = self.ledger.claim_decision(
            decision_log_id=int(sig["decision_log_id"]),
            strategy_id=strategy_id,
            action=action,
            chain_id=None if sig.get("chain_id") is None else str(sig.get("chain_id")),
            token_address=(
                None
                if sig.get("token_address") is None
                else str(sig.get("token_address"))
            ),
            pair_address=(
                None if sig.get("pair_address") is None else str(sig.get("pair_address"))
            ),
            size_pct=size_pct_f,
            reason=None if sig.get("reason") is None else str(sig.get("reason")),
            mode=None if sig.get("mode") is None else str(sig.get("mode")),
        )
        if not claim["created"]:
            resumed = self._resume_claimed(sig, claim)
            if resumed is not None:
                return resumed
        exec_id = int(claim["execution_log_id"])

        executed = 0
        executor = STEP_A_EXECUTOR
        exec_result: dict[str, Any] | None = None
        exec_error: str | None = None
        risk_reasons: list[dict[str, Any]] | None = None
        submitted_to_executor = False

        step_label = self._step_label()

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
        elif (
            is_live_bnb_spot_purpose(sig.get("purpose"))
            and mode != "dry_run"
            and not self._binance_injected
            and os.environ.get("BINANCE_SPOT_PRODUCTION_BOOK", "").strip() != REST_BOOK
        ):
            active, kill_name = kill_switch_active()
            executor = BINANCE_SPOT_EXECUTOR
            executed = 0
            submitted_to_executor = False
            if active and action != "sell":
                exec_error = f"kill_switch:{kill_name}"
                receipt_body["status"] = "live_refused"
                receipt_body["note"] = "kill switch blocks a new buy"
                receipt_body["error"] = exec_error
            else:
                transport = self._mcp_transport
                if transport is None and self._mcp_catalog is not None:
                    transport = CatalogProbe(self._mcp_catalog)
                evaluation = dispatch_live_bnb(
                    sig,
                    self.ledger.mcp_book(),
                    transport,
                )
                calls = parent_queue_view(evaluation)
                exec_result = {
                    "reason": evaluation.get("reason"),
                    "idem": evaluation.get("idem"),
                    "parent_calls": calls,
                    "protection_tool_present": evaluation.get("protection_tool_present"),
                    "handshake": evaluation.get("handshake"),
                    "allow_buy": False,
                    "allow_sell": False,
                    "poll_naked_position": False,
                    "auto_sell": False,
                }
                receipt_body["status"] = "gate_skip"
                receipt_body["note"] = "live_bnb gate skip; no order"
                receipt_body["gate_reason"] = evaluation.get("reason")
                receipt_body["exec_result"] = exec_result
                log.info(
                    "live_bnb gate decision_log_id=%s action=%s reason=%s",
                    sig.get("decision_log_id"),
                    action,
                    evaluation.get("reason"),
                )
        elif is_live_bnb_spot_purpose(sig.get("purpose")):
            # CEX arm: not a paper fill, and not LiveExecutor. The adapter
            # itself keeps live newOrder behind the kill switch. A protected
            # open on this arm blocks only the same symbol. The other arm's
            # same symbol blocks too. Live buys share one lock so both arms
            # cannot spend the sealed free balance twice.
            adapter = self._binance_spot or default_live_bnb_adapter()
            self._binance_spot = adapter
            adapter.record_entry_origin = self.ledger.record_exchange_entry_origin
            live_buy = action == "buy" and mode != "dry_run"
            reserve = buy_reservation_lock(self.ledger.path) if live_buy else nullcontext()
            with reserve:
                if live_buy:
                    purpose = str(sig.get("purpose") or "")
                    own = self.ledger.list_binance_spot_buy_blocks(purpose=purpose)
                    own_ids = {row.get("id") for row in own}
                    same_book = [
                        row
                        for row in self.ledger.list_binance_spot_buy_blocks(purpose=None)
                        if row.get("id") not in own_ids
                    ]
                    other_book, peer_error = _required_peer_books(
                        self.ledger.path,
                        spot_account_uid(adapter.account_snapshot),
                        self.accept_purposes,
                    )
                    adapter.peer_book_error = peer_error
                    adapter.buy_blocks = own
                    adapter.peer_buy_blocks = same_book + other_book
                    book = self.ledger.mcp_book()
                    canonical = book.get("canonical_positions")
                    adapter.canonical_rows = canonical if isinstance(canonical, list) else []
                exec_result = _binance_public_result(adapter.execute(sig))
                if (
                    live_buy
                    and not exec_result.get("dry_run")
                    and exec_result.get("arm_position")
                ):
                    self._arm_binance_position(sig, exec_result, receipt_body)
            executor = BINANCE_SPOT_EXECUTOR
            submitted_to_executor = bool(exec_result.get("submitted"))
            receipt_body["step"] = "binance_spot"
            receipt_body["exec_result"] = exec_result
            reported = exec_result.get("exchange_error")
            if isinstance(reported, dict):
                receipt_body["exchange_error"] = reported
            if exec_result.get("dry_run"):
                executed = 0
                if exec_result.get("ok"):
                    receipt_body["status"] = "dry_run_acked"
                    receipt_body["note"] = (
                        "binance spot dry-run; newOrder not called"
                    )
                else:
                    exec_error = str(exec_result.get("error") or "binance_spot_refused")
                    receipt_body["status"] = "dry_run_rejected"
                    receipt_body["note"] = "binance spot dry-run rejected; newOrder not called"
                    receipt_body["error"] = exec_error
            elif (
                exec_result.get("consumer_allowed") is True
                and exec_result.get("ok")
                and int(exec_result.get("executed") or 0) == 1
            ):
                executed = 1
                receipt_body["status"] = "binance_spot_submitted"
                receipt_body["note"] = "binance spot buy covered by a verified native OCO"
            else:
                executed = 0
                exec_error = str(exec_result.get("error") or "binance_spot_refused")
                receipt_body["status"] = "live_refused"
                receipt_body["note"] = "binance spot live fill refused; no retry"
                receipt_body["error"] = exec_error
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
                risk_reasons = risk_rejected or None
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
                    submitted_to_executor = True
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

        return self._finalize(
            execution_log_id=exec_id,
            sig=sig,
            strategy_id=strategy_id,
            action=action,
            size_pct_f=size_pct_f,
            ok=ok,
            errors=errors,
            executed=executed,
            executor=executor,
            exec_error=exec_error,
            risk_reasons=risk_reasons,
            submitted=submitted_to_executor,
            exec_result=exec_result,
            pm_result=pm_result,
            receipt_body=receipt_body,
            order_status=order_status,
        )

    def process_pending(
        self,
        *,
        since_id: int | None = None,
        limit: int | None = None,
    ) -> ProcessStats:
        """Process decision_log rows with id > since_id (default: ledger cursor).

        ``since_id`` only changes which rows this call reads. The persisted
        cursor moves forward with MAX and is not rewound when since_id is lower.
        """
        persisted = self.ledger.get_last_processed_id()
        cursor = consumer_read_cursor(
            persisted=persisted,
            since_id=since_id,
            live_arm=sole_live_bnb_purpose(self.accept_purposes) is not None,
        )
        rows = self.fetch_pending(cursor, limit=limit)
        stats = ProcessStats()
        for row in rows:
            try:
                result = self.process_one(row)
            except Exception as exc:  # noqa: BLE001
                log.exception("process_pending row failed")
                try:
                    did = int(dict(row)["id"])
                    self.ledger.set_last_processed_id(did)
                except Exception:
                    log.exception("cannot advance past unreadable decision row")
                    break
                result = {
                    "decision_log_id": did,
                    "validation_ok": False,
                    "validation_errors": [f"exception:{type(exc).__name__}:{exc}"],
                    "executed": 0,
                    "executor": STEP_A_EXECUTOR,
                    "error": f"exception:{type(exc).__name__}:{exc}",
                    "status": "rejected",
                }
            if result.get("inflight"):
                # Do not process later ids: MAX(cursor) would skip this claim.
                stats.receipts.append(result)
                break
            stats.processed += 1
            stats.last_decision_id = result["decision_log_id"]
            if result.get("duplicate"):
                stats.receipts.append(result)
                continue
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
                result.get("executor") in (PAPER_EXECUTOR, BINANCE_SPOT_EXECUTOR)
                and result.get("validation_ok")
                and result.get("error")
            ):
                stats.executed_fail += 1
            stats.receipts.append(result)
        return stats

    def tick_binance_protective_exits(
        self,
        marks: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Full-position maintenance for this process's live spot arm.

        Fade and classic each scan only their own rows. Other purposes are
        not scanned. No mark fetch and no order when the book has nothing
        open. Callers that pass ``marks`` own the prices; a missing symbol
        is not filled in with a network read.
        """
        arm = sole_live_bnb_purpose(self.accept_purposes)
        if arm is None:
            return []
        positions = self.ledger.list_binance_spot_positions_for_exit(purpose=arm)
        if not positions:
            return []
        adapter = self._binance_spot or default_live_bnb_adapter()
        self._binance_spot = adapter
        adapter.record_entry_origin = self.ledger.record_exchange_entry_origin
        outcomes: list[dict[str, Any]] = []
        for row in positions:
            if row.get("status") == "qty_unconfirmed":
                qty = adapter.refresh_filled_qty(dict(row))
                if qty:
                    saved_qty = self.ledger.confirm_binance_spot_qty(int(row["id"]), qty)
                    if isinstance(saved_qty, dict) and saved_qty.get("status") == "open":
                        row = saved_qty
            symbol = str(row.get("symbol") or "")
            if marks is not None and symbol in marks:
                mark = marks.get(symbol)
            else:
                mark = None
            position_id = int(row["id"])

            def persist(fields: dict[str, Any], held_id: int = position_id) -> dict[str, Any] | None:
                return self.ledger.update_binance_spot_position(held_id, fields)

            outcome = adapter.maintain_held_position(dict(row), mark, persist=persist)
            book = str(outcome.get("book") or "")
            if book == "exit_submitted" or (
                book == "error_only" and outcome.get("error")
            ):
                saved = self.ledger.apply_binance_spot_exit(position_id, outcome)
            else:
                saved = self.ledger.binance_spot_position_by_id(position_id)
            if isinstance(saved, dict):
                outcome = dict(outcome)
                outcome["position_status"] = saved.get("status")
                outcome["protection"] = saved.get("protection")
                outcome["coverage"] = saved.get("protection")
            outcomes.append(outcome)
        return outcomes
