"""Pin open paper positions to the hot tier (snapshot + enrich cadence).

Read-only: opens paper DBs with sqlite mode=ro. Missing DBs are skipped.
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


def _read_db(path: Path) -> tuple[set, set]:
    pairs: set = set()
    tokens: set = set()
    if not path.exists():
        return pairs, tokens
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        if not cols or "status" not in cols or "chain_id" not in cols:
            return pairs, tokens
        pcol = "pair_address" if "pair_address" in cols else "NULL"
        tcol = "token_address" if "token_address" in cols else "NULL"
        for chain, pair, tok in conn.execute(
            f"SELECT chain_id, {pcol}, {tcol} FROM positions WHERE status = 'open'"
        ):
            if pair:
                pairs.add((chain, str(pair)))
                pairs.add((chain, str(pair).lower()))
            if tok:
                tokens.add((chain, str(tok)))
                tokens.add((chain, str(tok).lower()))
    finally:
        conn.close()
    return pairs, tokens


def load_pins(*, force: bool = False, base_dir: Path | None = None, dbs=PAPER_DBS):
    """Return (pairs, tokens) sets of (chain_id, address) for open positions."""
    if not pin_enabled():
        return frozenset(), frozenset()
    now = time.monotonic()
    if not force and base_dir is None and now - _cache["at"] < CACHE_TTL_SEC and _cache["at"]:
        return _cache["pairs"], _cache["tokens"]
    pairs: set = set()
    tokens: set = set()
    for name in dbs:
        try:
            p, t = _read_db((base_dir or BASE_DIR) / name)
            pairs |= p
            tokens |= t
        except Exception as exc:  # noqa: BLE001
            log.warning("position_pin read failed db=%s err=%s", name, exc)
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
