"""Step B PaperExecutor — route validated buy/sell into PaperLedger.

Main crawler DB is opened read-only. Failures return {ok: False, error: ...}
with no retries. Does not touch live trading.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from logging_setup import get_logger
from paper_trade import PaperLedger, latest_price
from store import norm_address, norm_chain

log = get_logger("execution.paper_executor")

DEFAULT_TP_PCT = 10.0
DEFAULT_SL_PCT = 5.0
DEFAULT_MAX_HOLD_MIN = 60.0
EXECUTOR_NAME = "paper"


def _env_paper_db_default() -> str:
    import os

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


def slippage_rate(liquidity_usd: float | None) -> float:
    """Liquidity tiers → slip fraction (not percent points).

    <50k → 2%, 50k~500k → 1%, ≥500k → 0.3%, unknown → 2%.
    """
    if liquidity_usd is None:
        return 0.02
    try:
        liq = float(liquidity_usd)
    except (TypeError, ValueError):
        return 0.02
    if liq < 50_000:
        return 0.02
    if liq < 500_000:
        return 0.01
    return 0.003


def liquidity_usd_from_raw(raw: dict[str, Any] | None) -> float | None:
    if not raw or not isinstance(raw, dict):
        return None
    li = raw.get("liquidity")
    if isinstance(li, dict):
        val = li.get("usd")
    elif isinstance(li, (int, float)):
        val = li
    else:
        val = None
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


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


def tp_sl_hold_from_signal(sig: dict[str, Any]) -> tuple[float, float, float]:
    extras = sig.get("extras") if isinstance(sig.get("extras"), dict) else {}
    raw = sig.get("raw") if isinstance(sig.get("raw"), dict) else {}
    sources = [sig, extras, raw]
    tp = _pick_float(sources, ("tp_pct", "tp"), DEFAULT_TP_PCT)
    sl = _pick_float(sources, ("sl_pct", "sl"), DEFAULT_SL_PCT)
    hold = _pick_float(sources, ("max_hold_min", "max_hold"), DEFAULT_MAX_HOLD_MIN)
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
    ) -> None:
        if crawler_db is None:
            from config import DB_PATH

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
        size_pct = float(sig["size_pct"])
        mid, slip, liq = self._mid_and_slip(chain_id, pair_address)
        if mid is None or mid <= 0:
            return {"ok": False, "error": "no_price", "executor": EXECUTOR_NAME}

        fill_price = mid * (1.0 + slip)
        eq = self.ledger.equity()
        equity_usd = float(eq["equity_usd"])
        requested_usd = size_pct * equity_usd
        cash = float(self.ledger.get_cash())
        truncated = False
        size_usd = requested_usd
        if size_usd > cash:
            size_usd = cash
            truncated = True
        if size_usd <= 0:
            return {
                "ok": False,
                "error": "insufficient_cash",
                "executor": EXECUTOR_NAME,
                "cash_usd": cash,
                "requested_usd": requested_usd,
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
                "size_pct": size_pct,
                "equity_usd": equity_usd,
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
            "tp_pct": tp,
            "sl_pct": sl,
            "max_hold_min": hold,
            "meta": meta,
            "equity": self.ledger.equity(),
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
    ) -> dict[str, Any] | None:
        opens = self.ledger.list_open()
        chain_id = norm_chain(chain_id)
        tok = norm_address(token_address) if token_address else None
        pair = norm_address(pair_address) if pair_address else None
        if tok:
            for p in opens:
                pt = p.get("token_address")
                if (
                    norm_chain(str(p["chain_id"])) == chain_id
                    and pt
                    and norm_address(str(pt)) == tok
                ):
                    return p
        if pair:
            for p in opens:
                if (
                    norm_chain(str(p["chain_id"])) == chain_id
                    and norm_address(str(p["pair_address"])) == pair
                ):
                    return p
        return None

    def _sell(self, sig: dict[str, Any]) -> dict[str, Any]:
        chain_id = norm_chain(str(sig["chain_id"]))
        pair_address = (
            norm_address(str(sig["pair_address"])) if sig.get("pair_address") else None
        )
        token_address = (
            norm_address(str(sig["token_address"])) if sig.get("token_address") else None
        )
        pos = self._find_open(
            chain_id=chain_id,
            token_address=token_address,
            pair_address=pair_address,
        )
        if pos is None:
            return {
                "ok": False,
                "error": "no_open_position",
                "executor": EXECUTOR_NAME,
                "chain_id": chain_id,
                "token_address": token_address,
                "pair_address": pair_address,
            }

        pos_pair = norm_address(str(pos["pair_address"]))
        mid, slip, liq = self._mid_and_slip(chain_id, pos_pair)
        if mid is None or mid <= 0:
            return {"ok": False, "error": "no_price", "executor": EXECUTOR_NAME}

        fill_price = mid * (1.0 - slip)
        closed = self.ledger.close_position(
            pos["id"],
            price=fill_price,
            reason="signal_sell",
        )
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
            "match": (
                "chain+token"
                if token_address
                and pos.get("token_address")
                and norm_address(str(pos["token_address"])) == token_address
                else "chain+pair"
            ),
            "meta": self._meta(sig),
            "equity": self.ledger.equity(),
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
        """Apply TP/SL/max_hold on open positions (uses crawler RO prices)."""
        from paper_trade import tick as paper_tick

        return paper_tick(self.ledger, self.store)
