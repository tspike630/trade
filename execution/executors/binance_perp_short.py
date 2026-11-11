"""Gated Binance USDT-M Perpetual Short Adapter (`binance_perp_short.py`).

Executes ``open_short`` and ``close_short`` signals emitted by
``decision_engine.core.perp_short_alpha_engine`` with strict institutional
fail-closed safety invariants:

1. **Isolated Margin & Low-Leverage Ceiling**:
   - Rejects Cross-Margin requests; enforces ``margin_type == 'ISOLATED'``.
   - Clamps leverage to ``[1, MAX_PERP_SHORT_LEVERAGE]`` (default ``1x``, hard ceiling ``2x``).
2. **Mandatory Protective Stop-Loss Above Entry**:
   - Every ``open_short`` MUST supply ``stop_price > entry_price > take_profit_price > 0``
     with stop distance within ``[0.3%, 3.5%]``; unprotected naked shorts are rejected.
3. **20 USDT Minimum Notional & Duplicate-Symbol Guard**:
   - Rejects orders below ``20.0 USDT`` notional and blocks opening a second short on a
     ``perp_symbol`` that already has an open short in the ledger, while always permitting
     ``close_short`` (including anti-squeeze emergency closes).
4. **Shadow Dry-Run Default**:
   - Stays in ``dry_run`` shadow ledger mode unless ``BINANCE_PERP_SHORT_LIVE_ENABLED=1``
     AND ``LIVE_TRADING_DISABLED != 1`` are explicitly set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

PERP_SHORT_LIVE_ENV = "BINANCE_PERP_SHORT_LIVE_ENABLED"
LIVE_DISABLED_ENV = "LIVE_TRADING_DISABLED"
MAX_LEVERAGE_ENV = "MAX_PERP_SHORT_LEVERAGE"

MIN_PERP_NOTIONAL_USDT = 20.0
HARD_MAX_LEVERAGE = 2
MIN_STOP_GAP_PCT = 0.003  # 0.30% minimum stop distance above entry
MAX_STOP_GAP_PCT = 0.035  # 3.50% maximum stop distance above entry


def _finite(val: object) -> float | None:
    if isinstance(val, bool) or val is None or val == "":
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PerpShortExecutionReceipt:
    accepted: bool
    mode: str  # "dry_run" | "live_gated"
    action: str
    perp_symbol: str
    status: str
    reject_reason: str | None = None
    quantity: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    leverage: int = 1
    margin_type: str = "ISOLATED"
    closed_pnl_usdt: float | None = None


class BinancePerpShortAdapter:
    """Safe, ledger-backed adapter for 1x-2x Isolated USDT-M Perpetual Shorts."""

    def __init__(
        self,
        ledger_db_path: str | Path,
        *,
        dry_run: bool | None = None,
        book_usdt: float | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_db_path)
        self.book_usdt = (
            float(book_usdt)
            if book_usdt is not None and book_usdt > 0
            else (_finite(os.environ.get("BINANCE_SPOT_BOOK_USDT")) or 320.0)
        )
        live_env = os.environ.get(PERP_SHORT_LIVE_ENV, "").strip() == "1"
        kill_on = os.environ.get(LIVE_DISABLED_ENV, "1").strip() != "0"
        if dry_run is None:
            self.dry_run = not (live_env and not kill_on)
        else:
            self.dry_run = bool(dry_run) or kill_on
        self._init_ledger()

    def _init_ledger(self) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.ledger_path), timeout=10.0)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS perp_short_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    perp_symbol TEXT NOT NULL,
                    spot_symbol TEXT NOT NULL,
                    playbook TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    margin_type TEXT NOT NULL,
                    leverage INTEGER NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    notional_usdt REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_price REAL NOT NULL,
                    take_profit_price REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT,
                    exit_price REAL,
                    exit_reason TEXT,
                    realized_pnl_usdt REAL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def open_positions(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(str(self.ledger_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM perp_short_positions WHERE status = 'open' ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def execute_signal(self, signal: Mapping[str, Any], *, mark_price: float | None = None) -> PerpShortExecutionReceipt:
        action = str(signal.get("action") or "").strip().lower()
        perp_sym = str(signal.get("perp_symbol") or signal.get("binance_symbol") or "").strip().upper()
        spot_sym = str(signal.get("spot_symbol") or perp_sym).strip().upper()
        mode_str = "dry_run" if self.dry_run else "live_gated"

        if not perp_sym or not perp_sym.endswith("USDT"):
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action or "unknown",
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="invalid_perp_symbol",
            )

        if action == "close_short":
            return self._close_open_short(
                perp_sym,
                exit_price=mark_price or _finite(signal.get("entry_price")),
                reason=str(signal.get("reason") or "signal_close_short"),
            )

        if action != "open_short":
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="unsupported_short_action",
            )

        margin_type = str(signal.get("margin_type") or "ISOLATED").strip().upper()
        if margin_type != "ISOLATED":
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="cross_margin_forbidden",
            )

        cfg_max_lev = min(HARD_MAX_LEVERAGE, max(1, int(_finite(os.environ.get(MAX_LEVERAGE_ENV)) or 1)))
        req_lev = int(_finite(signal.get("leverage")) or 1)
        leverage = max(1, min(cfg_max_lev, req_lev))

        entry_px = _finite(signal.get("entry_price")) or mark_price
        stop_px = _finite(signal.get("stop_price"))
        tp_px = _finite(signal.get("take_profit_price"))
        if entry_px is None or entry_px <= 0:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="entry_price_missing",
            )
        if stop_px is None or stop_px <= entry_px:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="short_stop_must_be_above_entry",
            )
        if tp_px is None or tp_px <= 0 or tp_px >= entry_px:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="short_tp_must_be_below_entry",
            )

        stop_gap = (stop_px - entry_px) / entry_px
        if stop_gap < MIN_STOP_GAP_PCT - 1e-9:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="short_stop_too_tight",
            )
        if stop_gap > MAX_STOP_GAP_PCT + 1e-9:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="short_stop_too_wide",
            )

        notional = _finite(signal.get("notional_usdt"))
        if notional is None:
            pct = _finite(signal.get("size_pct")) or 0.0
            notional = pct * self.book_usdt * leverage
        if notional < MIN_PERP_NOTIONAL_USDT - 1e-9:
            return PerpShortExecutionReceipt(
                accepted=False,
                mode=mode_str,
                action=action,
                perp_symbol=perp_sym,
                status="rejected",
                reject_reason="notional_below_20_usdt_floor",
            )

        conn = sqlite3.connect(str(self.ledger_path), timeout=10.0)
        try:
            existing = conn.execute(
                "SELECT id FROM perp_short_positions WHERE perp_symbol = ? AND status = 'open' LIMIT 1",
                (perp_sym,),
            ).fetchone()
            if existing is not None:
                return PerpShortExecutionReceipt(
                    accepted=False,
                    mode=mode_str,
                    action=action,
                    perp_symbol=perp_sym,
                    status="rejected",
                    reject_reason="already_holding_open_short",
                )

            qty = math.floor((notional / entry_px) * 1e6) / 1e6
            if qty <= 0:
                return PerpShortExecutionReceipt(
                    accepted=False,
                    mode=mode_str,
                    action=action,
                    perp_symbol=perp_sym,
                    status="rejected",
                    reject_reason="quantity_rounded_to_zero",
                )

            conn.execute(
                """
                INSERT INTO perp_short_positions (
                    perp_symbol, spot_symbol, playbook, mode, status, margin_type,
                    leverage, order_type, quantity, notional_usdt, entry_price,
                    stop_price, take_profit_price, opened_at
                ) VALUES (?, ?, ?, ?, 'open', 'ISOLATED', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    perp_sym,
                    spot_sym,
                    str(signal.get("playbook") or "perp_short"),
                    mode_str,
                    leverage,
                    str(signal.get("order_type") or "LIMIT_MAKER"),
                    qty,
                    round(qty * entry_px, 4),
                    entry_px,
                    stop_px,
                    tp_px,
                    _utc_iso(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return PerpShortExecutionReceipt(
            accepted=True,
            mode=mode_str,
            action="open_short",
            perp_symbol=perp_sym,
            status="opened_with_protective_sl_tp",
            quantity=qty,
            entry_price=entry_px,
            stop_price=stop_px,
            take_profit_price=tp_px,
            leverage=leverage,
            margin_type="ISOLATED",
        )

    def evaluate_protective_exits(self, mark_prices: Mapping[str, float]) -> list[PerpShortExecutionReceipt]:
        """Trigger protective stop-loss or take-profit exits against current mark prices."""
        receipts: list[PerpShortExecutionReceipt] = []
        for pos in self.open_positions():
            sym = str(pos["perp_symbol"])
            mark = _finite(mark_prices.get(sym))
            if mark is None or mark <= 0:
                continue
            if mark >= float(pos["stop_price"]):
                receipts.append(self._close_open_short(sym, exit_price=mark, reason="stop_loss"))
            elif mark <= float(pos["take_profit_price"]):
                receipts.append(self._close_open_short(sym, exit_price=mark, reason="take_profit"))
        return receipts

    def _close_open_short(
        self,
        perp_sym: str,
        *,
        exit_price: float | None,
        reason: str,
    ) -> PerpShortExecutionReceipt:
        mode_str = "dry_run" if self.dry_run else "live_gated"
        conn = sqlite3.connect(str(self.ledger_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM perp_short_positions WHERE perp_symbol = ? AND status = 'open' ORDER BY id ASC LIMIT 1",
                (perp_sym,),
            ).fetchone()
            if row is None:
                return PerpShortExecutionReceipt(
                    accepted=False,
                    mode=mode_str,
                    action="close_short",
                    perp_symbol=perp_sym,
                    status="no_open_short",
                    reject_reason="no_open_short_to_close",
                )
            px_out = exit_price if exit_price is not None and exit_price > 0 else float(row["entry_price"])
            qty = float(row["quantity"])
            # Short PnL: (entry_price - exit_price) * quantity
            pnl = round((float(row["entry_price"]) - px_out) * qty, 6)
            conn.execute(
                """
                UPDATE perp_short_positions
                SET status = 'closed', closed_at = ?, exit_price = ?, exit_reason = ?, realized_pnl_usdt = ?
                WHERE id = ?
                """,
                (_utc_iso(), px_out, reason, pnl, int(row["id"])),
            )
            conn.commit()
            return PerpShortExecutionReceipt(
                accepted=True,
                mode=mode_str,
                action="close_short",
                perp_symbol=perp_sym,
                status="closed",
                quantity=qty,
                entry_price=float(row["entry_price"]),
                closed_pnl_usdt=pnl,
            )
        finally:
            conn.close()
