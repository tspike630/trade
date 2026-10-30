"""Step B PaperExecutor — route validated buy/sell into PaperLedger.

Main crawler DB is opened read-only. Failures return {ok: False, error: ...}
with no retries. Does not touch live trading.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path
from typing import Any

from config import DB_PATH
from logging_setup import get_logger
from paper_trade import PaperLedger, latest_price, tick as paper_tick
from store import fetch_latest_pair_price, fetch_pair_spot, norm_address, norm_chain

from execution.close_observe import record_closed_position
from execution.pricing import (
    buy_fill_price,
    liquidity_usd_from_raw,
    sell_fill_price,
    slippage_rate,
)
from execution.shadow_c3 import parse_meta, raise_peak

log = get_logger("execution.paper_executor")

DEFAULT_TP_PCT = 10.0
DEFAULT_SL_PCT = 5.0
DEFAULT_MAX_HOLD_MIN = 60.0
EXECUTOR_NAME = "paper"
PAPER_SIZE_MULT_ENV = "EXECUTION_PAPER_SIZE_MULT"
PAPER_TP_PCT_ENV = "EXECUTION_PAPER_TP_PCT"
PAPER_SL_PCT_ENV = "EXECUTION_PAPER_SL_PCT"
PAPER_MAX_HOLD_MIN_ENV = "EXECUTION_PAPER_MAX_HOLD_MIN"


def paper_size_mult() -> float:
    """Main-book paper buy notional multiplier. Unusable values stay at 1.0.

    Read on each buy so a process environment change is visible at the next
    fill. This does not reload a ``.env`` file by itself. Values that are
    unset, blank, non-numeric, non-finite, or ``<= 0`` apply ``1.0``.
    """
    raw = os.environ.get(PAPER_SIZE_MULT_ENV)
    if raw is None or not str(raw).strip():
        return 1.0
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value) or value <= 0:
        return 1.0
    return value


def _resolve_observe_db(observe_db: str | Path | None) -> Path | None:
    """Execution ledger for F-1 / C3 rows. None skips those writes.

    Peak updates still land on the paper position. The gateway passes its
    ledger path; a bare PaperExecutor does not guess a production ledger.
    """
    if observe_db is None or not str(observe_db).strip():
        return None
    path = Path(observe_db).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _env_paper_db_default() -> str:
    return (
        os.environ.get("EXECUTION_PAPER_DB", "").strip()
        or os.environ.get("PAPER_DB_PATH", "").strip()
        or "paper_trading.db"
    )


class _CrawlerRO:
    """Minimal read-only facade compatible with paper_trade.latest_price + get_pair_raw."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path).resolve()
        self.db_path = path
        self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "_CrawlerRO":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def get_pair_raw(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT raw_json FROM pairs WHERE chain_id = ? AND pair_address = ?",
            (norm_chain(chain_id), norm_address(pair_address)),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            return None

    def list_pair_history(
        self,
        chain_id: str,
        pair_address: str,
        *,
        limit: int = 200,
        since: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        sql = """
            SELECT id, chain_id, pair_address, token_address, recorded_at,
                   price_usd, volume_h24, volume_h1, liquidity_usd, txns_h24, tier
            FROM pair_history
            WHERE chain_id = ? AND pair_address = ?
        """
        params: list[Any] = [norm_chain(chain_id), norm_address(pair_address)]
        if since:
            sql += " AND recorded_at >= ?"
            params.append(since)
        sql += " ORDER BY recorded_at ASC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def latest_pair_price(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        return fetch_latest_pair_price(self._conn, chain_id, pair_address)

    def pair_spot(self, chain_id: str, pair_address: str) -> dict[str, Any] | None:
        return fetch_pair_spot(self._conn, chain_id, pair_address)


def _pick_float(sources: list[dict[str, Any]], keys: tuple[str, ...], default: float) -> float:
    for src in sources:
        if not isinstance(src, dict):
            continue
        for k in keys:
            if src.get(k) is None:
                continue
            try:
                return float(src[k])
            except (TypeError, ValueError):
                continue
    return float(default)


def _env_float_or(name: str, hardcoded: float) -> float:
    """Parse a positive-or-any finite env float; blank/bad → hardcoded default."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(hardcoded)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(hardcoded)
    if not math.isfinite(value):
        return float(hardcoded)
    return value


def paper_tp_sl_hold_defaults() -> tuple[float, float, float]:
    """Env-overridable paper TP/SL/hold defaults (peer exit synth).

    ``EXECUTION_PAPER_TP_PCT`` / ``EXECUTION_PAPER_SL_PCT`` /
    ``EXECUTION_PAPER_MAX_HOLD_MIN`` fill only when the signal omits the field.
    Unset or unusable env keeps hardcoded DEFAULT_TP_PCT / DEFAULT_SL_PCT /
    DEFAULT_MAX_HOLD_MIN (10 / 5 / 60). Does not touch SIZE_MULT / live / C-exit.
    """
    return (
        _env_float_or(PAPER_TP_PCT_ENV, DEFAULT_TP_PCT),
        _env_float_or(PAPER_SL_PCT_ENV, DEFAULT_SL_PCT),
        _env_float_or(PAPER_MAX_HOLD_MIN_ENV, DEFAULT_MAX_HOLD_MIN),
    )


def tp_sl_hold_from_signal(sig: dict[str, Any]) -> tuple[float, float, float]:
    extras = sig.get("extras") if isinstance(sig.get("extras"), dict) else {}
    raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
    sources = [sig, extras, raw]
    tp_default, sl_default, hold_default = paper_tp_sl_hold_defaults()
    tp = _pick_float(sources, ("tp_pct", "tp"), tp_default)
    sl = _pick_float(sources, ("sl_pct", "sl"), sl_default)
    hold = _pick_float(sources, ("max_hold_min", "max_hold"), hold_default)
    return tp, sl, hold


class PaperExecutor:
    """Buy/sell against PaperLedger using crawler prices (RO) + liquidity slippage."""

    def __init__(
        self,
        *,
        paper_db: str | Path | None = None,
        crawler_db: str | Path | None = None,
        ledger: PaperLedger | None = None,
        store: _CrawlerRO | None = None,
        observe_db: str | Path | None = None,
    ) -> None:
        if crawler_db is None:
            crawler_db = DB_PATH
        self.crawler_db = Path(crawler_db).resolve()
        self._owns_store = store is None
        self.store = store or _CrawlerRO(self.crawler_db)

        if ledger is not None:
            self._owns_ledger = False
            self.ledger = ledger
            self.paper_db = Path(ledger.path).resolve()
        else:
            self._owns_ledger = True
            self.paper_db = Path(paper_db or _env_paper_db_default()).expanduser()
            if not self.paper_db.is_absolute():
                self.paper_db = (Path.cwd() / self.paper_db).resolve()
            self.ledger = PaperLedger(self.paper_db)
        self.observe_db = _resolve_observe_db(observe_db)

    def close(self) -> None:
        if self._owns_ledger:
            self.ledger.close()
        if self._owns_store:
            self.store.close()

    def __enter__(self) -> "PaperExecutor":
        return self

    def __exit__(self, *a: object) -> None:
        self.close()

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        """Execute one validated signal. Never retries. Returns ok/error dict."""
        try:
            action = str(sig.get("action") or "").strip().lower()
            if action == "buy":
                return self._buy(sig)
            if action == "sell":
                return self._sell(sig)
            return {"ok": False, "error": f"unsupported_action:{action}", "executor": EXECUTOR_NAME}
        except Exception as exc:  # noqa: BLE001 — surface as receipt error, no retry
            log.exception(
                "paper execute failed decision_log_id=%s",
                sig.get("decision_log_id"),
            )
            return {
                "ok": False,
                "error": f"exception:{type(exc).__name__}:{exc}",
                "executor": EXECUTOR_NAME,
            }

    def _meta(self, sig: dict[str, Any]) -> dict[str, Any]:
        return {
            "strategy_id": str(sig.get("strategy_id") or "unknown"),
            "decision_log_id": sig.get("decision_log_id"),
        }

    def _position_strategy(self, pos: dict[str, Any]) -> str:
        meta_json = pos.get("meta_json")
        meta: dict[str, Any]
        if isinstance(meta_json, dict):
            meta = meta_json
        elif not meta_json:
            meta = {}
        else:
            try:
                parsed = json.loads(meta_json)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            meta = parsed if isinstance(parsed, dict) else {}
        return str(meta.get("strategy_id") or "").strip()

    def _marks(self) -> dict[str, float]:
        """Mid marks for open positions. Equity uses these, not slipped fills."""
        marks: dict[str, float] = {}
        for pos in self.ledger.list_open():
            mid = latest_price(self.store, pos["chain_id"], pos["pair_address"])
            if mid is None or mid <= 0:
                continue
            marks[f"{pos['chain_id']}|{pos['pair_address']}"] = mid
        return marks

    def _mid_and_slip(self, chain_id: str, pair_address: str) -> tuple[float | None, float, float | None]:
        mid = latest_price(self.store, chain_id, pair_address)
        raw = self.store.get_pair_raw(chain_id, pair_address)
        liq = liquidity_usd_from_raw(raw)
        slip = slippage_rate(liq)
        return mid, slip, liq

    def _buy(self, sig: dict[str, Any]) -> dict[str, Any]:
        chain_id = norm_chain(str(sig["chain_id"]))
        pair_address = norm_address(str(sig["pair_address"]))
        token_address = (
            norm_address(str(sig["token_address"]))
            if sig.get("token_address")
            else None
        )
        decision_log_id = sig.get("decision_log_id")
        if decision_log_id is not None:
            existing = self.ledger.find_by_decision_log_id(decision_log_id)
            if existing is not None:
                return {
                    "ok": False,
                    "error": "duplicate_decision_log_id",
                    "executor": EXECUTOR_NAME,
                    "position_id": existing.get("id"),
                    "decision_log_id": decision_log_id,
                }
        size_pct = float(sig["size_pct"])
        mid, slip, liq = self._mid_and_slip(chain_id, pair_address)
        if mid is None or mid <= 0:
            return {"ok": False, "error": "no_price", "executor": EXECUTOR_NAME}

        fill_price = buy_fill_price(mid, slip)
        eq = self.ledger.equity(self._marks())
        equity_usd = float(eq["equity_usd"])
        requested_usd_before_mult = size_pct * equity_usd
        mult = paper_size_mult()
        requested_usd = requested_usd_before_mult * mult
        cash = float(self.ledger.get_cash())
        truncated = False
        size_usd = requested_usd
        if size_usd > cash:
            size_usd = cash
            truncated = True
        size_audit = {
            "paper_size_mult": mult,
            "requested_usd_before_mult": requested_usd_before_mult,
        }
        if size_usd <= 0:
            return {
                "ok": False,
                "error": "insufficient_cash",
                "executor": EXECUTOR_NAME,
                "cash_usd": cash,
                "requested_usd": requested_usd,
                **size_audit,
            }

        tp, sl, hold = tp_sl_hold_from_signal(sig)
        meta = self._meta(sig)
        meta.update(
            {
                "mid_price": mid,
                "slippage": slip,
                "liquidity_usd": liq,
                "requested_usd": requested_usd,
                "truncated": truncated,
                **size_audit,
                "size_pct": size_pct,
                "equity_usd": equity_usd,
                "peak_price": mid,
            }
        )
        pid = self.ledger.open_position(
            chain_id=chain_id,
            pair_address=pair_address,
            price=fill_price,
            size_usd=size_usd,
            token_address=token_address,
            tp_pct=tp,
            sl_pct=sl,
            max_hold_min=hold,
            meta=meta,
        )
        out = {
            "ok": True,
            "executor": EXECUTOR_NAME,
            "side": "buy",
            "position_id": pid,
            "chain_id": chain_id,
            "pair_address": pair_address,
            "token_address": token_address,
            "mid_price": mid,
            "fill_price": fill_price,
            "slippage": slip,
            "liquidity_usd": liq,
            "size_usd": size_usd,
            "requested_usd": requested_usd,
            "truncated": truncated,
            **size_audit,
            "tp_pct": tp,
            "sl_pct": sl,
            "max_hold_min": hold,
            "meta": meta,
            "equity": self.ledger.equity(self._marks()),
        }
        log.info(
            "paper BUY pos=%s size_usd=%.4f fill=%.8g slip=%.4f truncated=%s",
            pid,
            size_usd,
            fill_price,
            slip,
            truncated,
        )
        return out

    def _find_open(
        self,
        *,
        chain_id: str,
        token_address: str | None,
        pair_address: str | None,
        strategy_id: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Match chain+token (else chain+pair) AND meta.strategy_id.

        Returns (position, match_label) or (None, error_code).
        error_code is strategy_mismatch when a different strategy holds the
        token/pair, else None (nothing open).
        """
        opens = self.ledger.list_open()
        chain_id = norm_chain(chain_id)
        tok = norm_address(token_address) if token_address else None
        pair = norm_address(pair_address) if pair_address else None
        want = str(strategy_id or "").strip()
        token_hits: list[tuple[dict[str, Any], bool]] = []
        pair_hits: list[tuple[dict[str, Any], bool]] = []
        for pos in opens:
            if norm_chain(str(pos["chain_id"])) != chain_id:
                continue
            same_strategy = self._position_strategy(pos) == want
            pos_tok = pos.get("token_address")
            token_ok = bool(
                tok and pos_tok and norm_address(str(pos_tok)) == tok
            )
            pair_ok = bool(
                pair and norm_address(str(pos["pair_address"])) == pair
            )
            if token_ok:
                token_hits.append((pos, same_strategy))
            elif pair_ok:
                pair_hits.append((pos, same_strategy))
        for pos, same in token_hits:
            if same:
                return pos, "chain+token+strategy"
        if token_hits:
            return None, "strategy_mismatch"
        for pos, same in pair_hits:
            if same:
                return pos, "chain+pair+strategy"
        if pair_hits:
            return None, "strategy_mismatch"
        return None, None

    def _sell(self, sig: dict[str, Any]) -> dict[str, Any]:
        chain_id = norm_chain(str(sig["chain_id"]))
        pair_address = (
            norm_address(str(sig["pair_address"])) if sig.get("pair_address") else None
        )
        token_address = (
            norm_address(str(sig["token_address"])) if sig.get("token_address") else None
        )
        strategy_id = str(sig.get("strategy_id") or "unknown").strip() or "unknown"
        pos, match_or_err = self._find_open(
            chain_id=chain_id,
            token_address=token_address,
            pair_address=pair_address,
            strategy_id=strategy_id,
        )
        if pos is None:
            error = match_or_err or "no_open_position"
            return {
                "ok": False,
                "error": error,
                "executor": EXECUTOR_NAME,
                "chain_id": chain_id,
                "token_address": token_address,
                "pair_address": pair_address,
                "strategy_id": strategy_id,
            }

        try:
            size_pct = float(sig["size_pct"])
        except (TypeError, ValueError):
            return {
                "ok": False,
                "error": "invalid_size_pct",
                "executor": EXECUTOR_NAME,
                "position_id": pos["id"],
            }
        # size_pct on a sell is the fraction of the open position to close.
        # Partial closes are not implemented; refuse rather than flatten the book.
        # A full close must send size_pct=1. Equity-fraction buys (0.05) are not
        # a close fraction — copying that onto a sell is a partial and is rejected.
        if size_pct < 1.0 - 1e-9:
            return {
                "ok": False,
                "error": "partial_sell_rejected",
                "executor": EXECUTOR_NAME,
                "position_id": pos["id"],
                "size_pct": size_pct,
                "message": (
                    "paper executor closes the full position only; "
                    "size_pct must be 1. Partial close is not implemented."
                ),
            }

        pos_pair = norm_address(str(pos["pair_address"]))
        mid, slip, liq = self._mid_and_slip(chain_id, pos_pair)
        if mid is None or mid <= 0:
            return {"ok": False, "error": "no_price", "executor": EXECUTOR_NAME}

        fill_price = sell_fill_price(mid, slip)
        self._note_peak(pos, mid)
        closed = self.ledger.close_position(
            pos["id"],
            price=fill_price,
            reason="signal_sell",
        )
        self._record_observe(str(pos["id"]), source="signal_sell", mid_price=mid)
        out = {
            "ok": True,
            "executor": EXECUTOR_NAME,
            "side": "sell",
            "position_id": pos["id"],
            "chain_id": chain_id,
            "pair_address": pos_pair,
            "token_address": pos.get("token_address") or token_address,
            "mid_price": mid,
            "fill_price": fill_price,
            "slippage": slip,
            "liquidity_usd": liq,
            "pnl_usd": closed.get("pnl_usd"),
            "exit_price": closed.get("exit_price"),
            "exit_reason": closed.get("reason"),
            "match": match_or_err,
            "size_pct": size_pct,
            "meta": self._meta(sig),
            "equity": self.ledger.equity(self._marks()),
            "closed": closed,
        }
        log.info(
            "paper SELL pos=%s pnl=%.6f fill=%.8g slip=%.4f",
            pos["id"],
            float(closed.get("pnl_usd") or 0.0),
            fill_price,
            slip,
        )
        return out

    def tick(self) -> list[dict[str, Any]]:
        """TP/SL/max_hold via paper_trade.tick → execution.exits.scan_exits.

        Close fill uses the same sell slippage as ``_sell``. This path does
        not append execution_log or order_events: decision_log_id is unique on
        the entry receipt, so a second receipt cannot share it. The close is
        recorded on the paper position (exit_reason) and the fills table.

        Only one tick writer may run against this paper DB (this method or
        paper_trade.py / paper_tick_loop.sh, not both).

        After each close, when ``observe_db`` is set, append F-1 streak and
        C3 shadow rows on that execution ledger. Those rows do not reject
        orders and do not change exit rules. Peak maintenance itself happens
        inside ``paper_tick`` for every open position.
        """
        actions = paper_tick(self.ledger, self.store)
        for closed in actions:
            pid = closed.get("id")
            if not pid:
                continue
            mid = closed.get("mid_price")
            try:
                mid_f = float(mid) if mid is not None else None
            except (TypeError, ValueError):
                mid_f = None
            self._record_observe(str(pid), source="tick", mid_price=mid_f)
        return actions

    def _note_peak(self, pos: dict[str, Any], price: float | None) -> None:
        """Raise meta peak before a signal sell. Failures do not block the close."""
        if price is None:
            return
        meta = parse_meta(pos.get("meta_json"))
        updated, changed = raise_peak(meta, price)
        if not changed:
            return
        merge = getattr(self.ledger, "merge_open_meta", None)
        if not callable(merge):
            return
        try:
            merge(str(pos["id"]), {"peak_price": updated["peak_price"]})
        except Exception:
            log.exception("peak update failed position_id=%s", pos.get("id"))

    def _record_observe(
        self,
        position_id: str,
        *,
        source: str,
        mid_price: float | None,
    ) -> None:
        if self.observe_db is None:
            return
        getter = getattr(self.ledger, "get_position", None)
        if not callable(getter):
            return
        row = getter(position_id)
        if not isinstance(row, dict):
            return
        try:
            record_closed_position(
                self.observe_db,
                row,
                source=source,
                mid_price=mid_price,
            )
        except Exception:
            log.exception("observe close record failed position_id=%s", position_id)
