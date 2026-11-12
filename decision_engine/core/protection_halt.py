"""Decision-side halt while a live arm's inventory is unprotected.

Source of truth is the execution ledger. Decision reads it and does not
write it.

Tables (execution ``ledger.py``):

- ``binance_spot_positions`` — protective book. Columns used:
  ``purpose``, ``symbol``, ``status``, ``entry_order_id``, ``protection``,
  ``oco_order_list_id``, ``oco_above_order_id``, ``oco_below_order_id``.
- ``canonical_positions`` — canonical hold. Columns used:
  ``purpose``, ``symbol``, ``status``, ``entry_order_id``.

The admit/block rule is ``execution.executors.binance_spot_gate.buy_exposure_block``.
Decision does not keep a second copy of that rule. One arm may hold several
symbols. A healthy open blocks another BUY of that same symbol only. An
unsafe row blocks every new BUY on that purpose:

- ``oco_failed``, ``oco_unknown``
- ``held_naked``, ``qty_unconfirmed``
- a non-terminal canonical row with no entry order id
- ``open`` that is not a verified exchange OCO
- any other non-terminal protective status

Execution clears the halt by moving the row to a terminal status
(``closed``, ``closed_dust``, ``oco_filled``) or to a verified protected
open. Decision does not clear it.

Handoff path, first hit wins:

1. ``DECISION_EXECUTION_LEDGER``
2. ``EXECUTION_LEDGER_PATH``
3. ``EXECUTION_LEDGER_DB``

For ``live_bnb_boll_fade`` and ``live_bnb_boll_classic``, a configured path
that is missing, unreadable, or short a required table or column fails
closed (``protection_status_unknown``): no new BUY. An unconfigured path
does not invent a book. Paper purposes are not halted.

Still execution-owned, and not done here: revalidate the OCO against the
fill price (the ledger entry is still the signal price), keep the Binance
HTTP code and message on the reject, and flatten a naked position when the
OCO fails.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from execution.executors.binance_spot_gate import (
    account_unresolved_halt,
    buy_exposure_block,
)

_LEDGER_ENV = (
    "DECISION_EXECUTION_LEDGER",
    "EXECUTION_LEDGER_PATH",
    "EXECUTION_LEDGER_DB",
)
# Not a listed symbol. A healthy open of a real name does not match it, so
# the purpose-wide probe does not treat 一臂多仓 as a halt.
_PROBE_SYMBOL = "ZZZHALTPROBEUSDT"

_POSITION_COLUMNS = (
    "purpose",
    "symbol",
    "status",
    "entry_order_id",
    "protection",
    "oco_order_list_id",
    "oco_above_order_id",
    "oco_below_order_id",
)
_CANONICAL_COLUMNS = (
    "purpose",
    "symbol",
    "status",
    "entry_order_id",
)


@dataclass(frozen=True)
class ProtectionBook:
    """Read-only snapshot. ``configured`` is false when no ledger path is set."""

    configured: bool
    positions: tuple[dict[str, Any], ...]
    canonical: tuple[dict[str, Any], ...]
    error: str | None

    @staticmethod
    def unwired() -> ProtectionBook:
        return ProtectionBook(False, (), (), None)

    @staticmethod
    def failed(error: str) -> ProtectionBook:
        return ProtectionBook(True, (), (), error)

    @staticmethod
    def ready(
        positions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        canonical: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    ) -> ProtectionBook:
        return ProtectionBook(True, tuple(positions), tuple(canonical), None)


def resolve_ledger_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Ledger path from the handoff env. ``None`` when none of the keys is set."""
    env = os.environ if environ is None else environ
    for key in _LEDGER_ENV:
        raw = env.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return Path(text).expanduser()
    return None


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


_READ_SQL = {
    "binance_spot_positions": (
        "SELECT purpose, symbol, status, entry_order_id, protection, "
        "oco_order_list_id, oco_above_order_id, oco_below_order_id "
        "FROM binance_spot_positions"
    ),
    "canonical_positions": (
        "SELECT purpose, symbol, status, entry_order_id FROM canonical_positions"
    ),
}
_PRAGMA_SQL = {
    "binance_spot_positions": "PRAGMA table_info(binance_spot_positions)",
    "canonical_positions": "PRAGMA table_info(canonical_positions)",
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str] | None:
    pragma = _PRAGMA_SQL.get(table)
    if pragma is None:
        return None
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if found is None:
        return None
    info = conn.execute(pragma).fetchall()
    return {str(item[1]) for item in info}


_OPTIONAL_POSITION_COLUMNS = (
    "id",
    "decision_log_id",
    "quantity",
    "stop_price",
    "take_profit_price",
    "entry_price",
    "exit_reason",
    "created_at",
    "updated_at",
)


def _read_table(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str | None]:
    sql = _READ_SQL.get(table)
    if sql is None:
        return [], f"protection_table_missing:{table}"
    have = _table_columns(conn, table)
    if have is None:
        return [], f"protection_table_missing:{table}"
    for name in columns:
        if name not in have:
            return [], f"protection_column_missing:{table}.{name}"
    if table == "binance_spot_positions":
        extra = [col for col in _OPTIONAL_POSITION_COLUMNS if col in have and col not in columns]
        if extra:
            select_cols = ", ".join((*columns, *extra))
            sql = f"SELECT {select_cols} FROM binance_spot_positions"
    rows = conn.execute(sql).fetchall()
    return [dict(row) for row in rows], None


def read_protection_book(path: str | Path) -> ProtectionBook:
    """Read both books. Any failure is ``configured`` with ``error`` set.

    Does not create the file and does not write.
    """
    try:
        uri = _ro_uri(Path(path).expanduser())
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (OSError, sqlite3.Error) as exc:
        return ProtectionBook.failed(f"ledger_unreadable:{exc.__class__.__name__}")
    try:
        conn.row_factory = sqlite3.Row
        positions, pos_error = _read_table(conn, "binance_spot_positions", _POSITION_COLUMNS)
        if pos_error:
            return ProtectionBook.failed(pos_error)
        canonical, can_error = _read_table(conn, "canonical_positions", _CANONICAL_COLUMNS)
        if can_error:
            return ProtectionBook.failed(can_error)
        return ProtectionBook.ready(positions, canonical)
    except sqlite3.Error as exc:
        return ProtectionBook.failed(f"ledger_unreadable:{exc.__class__.__name__}")
    finally:
        conn.close()


def purpose_halt_reason(purpose: str, book: ProtectionBook) -> str | None:
    """Purpose-wide reason to emit no new BUY. ``None`` when the arm may buy.

    A healthy open of a real symbol is not a purpose halt (another symbol
    may still be emitted). The per-signal call blocks that same symbol.
    An unreadable configured book is ``protection_status_unknown``.
    """
    if not book.configured:
        return None
    if book.error:
        return "protection_status_unknown"
    unresolved = account_unresolved_halt(list(book.positions) + list(book.canonical))
    if unresolved:
        return unresolved
    return buy_exposure_block(
        list(book.positions),
        list(book.canonical),
        purpose=purpose,
        symbol=_PROBE_SYMBOL,
    )
