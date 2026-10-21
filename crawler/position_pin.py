"""Pin open paper positions to the hot tier (snapshot + enrich cadence).

Read-only: opens paper DBs with sqlite mode=ro. Missing/unusable DBs are skipped
with a WARNING; per-DB outcome is kept in ``last_status``.
Toggle with PIN_OPEN_POSITIONS_HOT (default 1; 0 = original behaviour).
Result cached ~60s so each cycle reads the DBs at most once.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from logging_setup import get_logger

log = get_logger("position_pin")

BASE_DIR = Path(__file__).resolve().parent
PAPER_DBS = (
    "paper_trading.db",
    "paper_trading_c_exit.db",
    "paper_trading_c_exit_shadow010.db",
)
CACHE_TTL_SEC = 60.0

_cache: dict = {"at": 0.0, "pairs": frozenset(), "tokens": frozenset()}


def pin_enabled() -> bool:
    return os.environ.get("PIN_OPEN_POSITIONS_HOT", "1").strip() not in ("0", "false", "False", "")


REQUIRED_COLS = ("status", "chain_id")
ADDR_COLS = ("pair_address", "token_address")  # at least one required

# Per-DB status of the most recent uncached load: {db_name: int open rows | 'missing'
# | 'no_table' | 'missing_cols' | 'error'}. Consumers (e.g. jev pin_load_failed)
# should use last_failed_count() / last_failed().
last_status: dict = {}
_startup_logged = False


class _DbUnusable(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _read_db(path: Path) -> tuple[set, set, int]:
    """Return (pairs, tokens, open_rows). Raises _DbUnusable for missing/no_table/missing_cols."""
    pairs: set = set()
    tokens: set = set()
    if not path.exists():
        raise _DbUnusable("missing", f"file not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        if not cols:
            raise _DbUnusable("no_table", "no positions table")
        missing = [c for c in REQUIRED_COLS if c not in cols]
        if not any(c in cols for c in ADDR_COLS):
            missing.append("pair_address|token_address")
        if missing:
            raise _DbUnusable("missing_cols", f"positions missing columns: {','.join(missing)}")
        pcol = "pair_address" if "pair_address" in cols else "NULL"
        tcol = "token_address" if "token_address" in cols else "NULL"
        n = 0
        for chain, pair, tok in conn.execute(
            f"SELECT chain_id, {pcol}, {tcol} FROM positions WHERE status = 'open'"
        ):
            n += 1
            if pair:
                pairs.add((chain, str(pair)))
                pairs.add((chain, str(pair).lower()))
            if tok:
                tokens.add((chain, str(tok)))
                tokens.add((chain, str(tok).lower()))
    finally:
        conn.close()
    return pairs, tokens, n


def last_failed() -> dict:
    """DBs whose last load was not usable: {db_name: code}."""
    return {k: v for k, v in last_status.items() if not isinstance(v, int)}


def last_failed_count() -> int:
    return len(last_failed())


def status_summary(status: dict | None = None) -> str:
    st = last_status if status is None else status
    return " ".join(f"{k}={v}" for k, v in st.items())


def load_pins(*, force: bool = False, base_dir: Path | None = None, dbs=PAPER_DBS):
    """Return (pairs, tokens) sets of (chain_id, address) for open positions.

    Per-DB outcome of each uncached load is stored in ``last_status``.
    """
    global _startup_logged
    if not pin_enabled():
        return frozenset(), frozenset()
    now = time.monotonic()
    if not force and base_dir is None and now - _cache["at"] < CACHE_TTL_SEC and _cache["at"]:
        return _cache["pairs"], _cache["tokens"]
    root = base_dir or BASE_DIR
    first = not _startup_logged
    if first:
        _startup_logged = True
        for name in dbs:
            log.info("position_pin startup db=%s %s", name, "found" if (root / name).exists() else "missing")
    pairs: set = set()
    tokens: set = set()
    status: dict = {}
    for name in dbs:
        try:
            p, t, n = _read_db(root / name)
            pairs |= p
            tokens |= t
            status[name] = n
        except _DbUnusable as exc:
            status[name] = exc.code
            log.warning("position_pin db unusable db=%s reason=%s", name, exc.reason)
        except Exception as exc:  # noqa: BLE001
            status[name] = "error"
            log.warning("position_pin read failed db=%s err=%s", name, exc)
    last_status.clear()
    last_status.update(status)
    log.info("position_pin loaded open_rows %s failed=%d", status_summary(status), last_failed_count())
    fp, ft = frozenset(pairs), frozenset(tokens)
    if base_dir is None:
        _cache.update(at=now, pairs=fp, tokens=ft)
    return fp, ft


def is_pinned(chain_id: str, token_address: str | None = None, pair_address: str | None = None) -> bool:
    if not pin_enabled():
        return False
    pairs, tokens = load_pins()
    if token_address and ((chain_id, token_address) in tokens or (chain_id, token_address.lower()) in tokens):
        return True
    if pair_address and ((chain_id, pair_address) in pairs or (chain_id, pair_address.lower()) in pairs):
        return True
    return False


def pinned_count() -> int:
    """Distinct open-position tokens/pairs (by pair) currently pinned."""
    pairs, _ = load_pins()
    return len({(c, a.lower()) for c, a in pairs})
