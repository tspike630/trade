"""Pre-stop anomaly confirmation for paper marks.

Problem: DexScreener occasionally returns a single bad quote (e.g. bzc9… 0.052
while the real price is ~0.15), which fires a stop-loss. Rule:

* A quote more than DROP_PCT (30%) below the previous accepted quote is
  "suspect". It is not used as a mark until CONFIRM_N (2) consecutive distinct
  suspect quotes have been seen (the triggering one counts).
* If liquidity crashed by more than LIQ_CRASH_PCT (80%) vs the previous
  accepted quote at the same time (real rug), accept immediately.
* A normal quote resets the pending suspicion.

Distinct quotes are identified by ``quote_id`` (recorded_at, or the price
itself if unknown), so re-reading the same stale snapshot every tick does not
count as a second confirmation.

Shadow book: on by default. Main paper book: env PAPER_STOP_CONFIRM_ENABLED
(default 0, off). State is in-process (resets on restart: first quote after
restart is accepted as baseline).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Hashable

DROP_PCT = 30.0
LIQ_CRASH_PCT = 80.0
CONFIRM_N = 2


def main_book_enabled() -> bool:
    return os.environ.get("PAPER_STOP_CONFIRM_ENABLED", "0").strip().lower() in ("1", "true", "yes")


@dataclass
class _State:
    last_px: float
    last_liq: float | None
    pending: list[Hashable] = field(default_factory=list)


class StopPriceConfirmer:
    def __init__(
        self,
        *,
        drop_pct: float = DROP_PCT,
        liq_crash_pct: float = LIQ_CRASH_PCT,
        confirm_n: int = CONFIRM_N,
    ) -> None:
        self.drop = float(drop_pct) / 100.0
        self.liq_crash = float(liq_crash_pct) / 100.0
        self.confirm_n = max(int(confirm_n), 1)
        self._s: dict[Hashable, _State] = {}
        self.events: list[dict[str, Any]] = []

    def _accept(self, key, px, liq, verdict):
        st = self._s.get(key)
        if st is None:
            self._s[key] = _State(px, liq)
        else:
            st.last_px = px
            if liq is not None:
                st.last_liq = liq
            st.pending.clear()
        return px, verdict

    def check(
        self,
        key: Hashable,
        px: float | None,
        liq: float | None = None,
        quote_id: Hashable | None = None,
    ) -> tuple[float | None, str]:
        """Return (mark_or_None, verdict). None => hold this tick, do not exit."""
        if px is None or px <= 0:
            return None, "no_price"
        st = self._s.get(key)
        if st is None:
            return self._accept(key, px, liq, "baseline")
        if px >= st.last_px * (1.0 - self.drop):
            return self._accept(key, px, liq, "ok")
        if liq is not None and st.last_liq and liq <= st.last_liq * (1.0 - self.liq_crash):
            self.events.append({"key": key, "px": px, "liq": liq, "verdict": "rug_immediate"})
            return self._accept(key, px, liq, "rug_immediate")
        qid = quote_id if quote_id is not None else px
        if qid not in st.pending:
            st.pending.append(qid)
        if len(st.pending) >= self.confirm_n:
            self.events.append({"key": key, "px": px, "liq": liq, "verdict": "confirmed"})
            return self._accept(key, px, liq, "confirmed")
        self.events.append({"key": key, "px": px, "liq": liq, "verdict": "suspect_hold"})
        return None, "suspect_hold"

    def forget(self, key: Hashable) -> None:
        self._s.pop(key, None)


def latest_quote(
    conn: sqlite3.Connection, chain_id: str, pair_address: str
) -> dict[str, Any] | None:
    """Newest pair_history row (price, liquidity, recorded_at)."""
    row = conn.execute(
        """
        SELECT price_usd, liquidity_usd, recorded_at FROM pair_history
        WHERE chain_id=? AND pair_address=? AND price_usd IS NOT NULL
        ORDER BY recorded_at DESC LIMIT 1
        """,
        (chain_id, pair_address),
    ).fetchone()
    if row is None:
        return None
    return {"price_usd": row[0], "liquidity_usd": row[1], "recorded_at": row[2]}
