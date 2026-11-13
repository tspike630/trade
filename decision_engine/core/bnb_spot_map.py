"""Optional DEX hot-token ∩ Binance listing map for ``live_bnb_boll_fade``.

The primary book is ``binance_indicators``. ``binance_klines`` is only the
in-process fallback when that table has not been created. This module is
the bypass for ``LIVE_BNB_FACTOR_SOURCE=dex_map``, or for ``auto`` when
neither CEX feed is available. It does not recompute tech_boll factors;
mapped rows keep the DEX fields they arrived with. Rows that do not
resolve to a listed spot ``BASEUSDT`` market are dropped so a Solana mint
is never written as the fill id.

Listing membership is read from local SQLite only (``cex_listing_events``
and the ccxt side tables). No exchange client and no API key. An empty or
tiny Binance spot USDT set stays fail-closed.

Base tickers are read from ``tokens.raw_json`` when that blob has one, then
from ``pairs.raw_json`` ``baseToken.symbol`` (the live token blob often has
only url/chainId/tokenAddress).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

from logging_setup import get_logger

log = get_logger("decision_engine.bnb_spot_map")

BINANCE_VENUE = "binance"
BINANCE_MARKET = "spot"
BINANCE_QUOTE = "USDT"
CEX_UNIVERSE = "binance_spot_usdt"

_BASE_RE_MIN = 2
_BASE_RE_MAX = 20
_NOT_A_BASE = frozenset(
    {"USDT", "USD", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP"}
)
_ROW_SYMBOL_KEYS = ("base_asset", "base_symbol", "base", "symbol", "token_symbol")
_LISTING_TABLES = ("cex_listing_events", "ccxt_listings", "ccxt_ohlcv", "ccxt_ticks")
_STAMP_KEYS = (
    "binance_symbol",
    "ccxt_symbol",
    "venue",
    "market",
    "quote_asset",
    "base_asset",
    "universe",
    "factor_source",
    "kline_interval",
    "source_chain_id",
    "source_token_address",
)


def _is_base_ticker(text: str) -> bool:
    if len(text) < _BASE_RE_MIN or len(text) > _BASE_RE_MAX:
        return False
    return text.isalnum() and text.isupper()


def normalize_spot_base(raw: str) -> str | None:
    """Return the base asset of a spot USDT name, or None.

    Accepts ``SOL``, ``sol/usdt``, ``SOL-USDT``, ``SOLUSDT``.
    Rejects derivatives (``:``), non-USDT quotes, and quote-asset tickers.
    """
    text = raw.strip().upper()
    if not text or ":" in text:
        return None
    for sep in ("/", "-", "_"):
        if sep in text:
            base, _, quote = text.partition(sep)
            if quote != BINANCE_QUOTE:
                return None
            text = base
            break
    else:
        suffix = BINANCE_QUOTE
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    if text in _NOT_A_BASE or not _is_base_ticker(text):
        return None
    return text


def classify_listed_symbol(
    symbol: str | None,
    base: str | None = None,
    quote: str | None = None,
) -> str | None:
    """Canonical Binance spot id ``BASEUSDT``, or None when not spot USDT."""
    sym = (symbol or "").strip().upper()
    if not sym or ":" in sym:
        return None
    b = (base or "").strip().upper()
    q = (quote or "").strip().upper()
    if b or q:
        if q != BINANCE_QUOTE or not _is_base_ticker(b) or b in _NOT_A_BASE:
            return None
        allowed = {
            f"{b}/{BINANCE_QUOTE}",
            f"{b}{BINANCE_QUOTE}",
            f"{b}-{BINANCE_QUOTE}",
            f"{b}_{BINANCE_QUOTE}",
        }
        if sym not in allowed:
            return None
        return f"{b}{BINANCE_QUOTE}"
    base_only = normalize_spot_base(sym)
    if base_only is None:
        return None
    return f"{base_only}{BINANCE_QUOTE}"


def _base_from_mapping(raw: dict[str, Any]) -> str | None:
    for key in ("symbol", "token_symbol"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            base = normalize_spot_base(value)
            if base is not None:
                return base
    for key in ("baseToken", "base_token", "token"):
        value = raw.get(key)
        if isinstance(value, dict):
            symbol = value.get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                base = normalize_spot_base(symbol)
                if base is not None:
                    return base
    return None


def base_from_row(row: dict[str, Any], looked_up: str | None = None) -> str | None:
    for key in _ROW_SYMBOL_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            base = normalize_spot_base(value)
            if base is not None:
                return base
    if looked_up:
        return normalize_spot_base(looked_up)
    return None


def _canonical_listed(listed_symbols: set[str]) -> set[str]:
    out: set[str] = set()
    for item in listed_symbols:
        spot_id = classify_listed_symbol(str(item))
        if spot_id is not None:
            out.add(spot_id)
    return out


def map_hot_tokens_to_binance_spot(
    rows: list[dict[str, Any]],
    *,
    listed_symbols: set[str],
    symbol_by_token: dict[tuple[str, str], str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Copy rows whose base is a listed Binance spot USDT market.

    Identity on the copy is the CEX symbol. Factor fields are left as they
    arrived. The first hot-row for a given ``BASEUSDT`` wins.
    """
    listed = _canonical_listed(listed_symbols)
    lookup = symbol_by_token or {}
    stats = {
        "scanned": len(rows),
        "mapped": 0,
        "skipped_no_symbol": 0,
        "skipped_unlisted": 0,
        "skipped_duplicate": 0,
    }
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = (str(row.get("chain_id") or ""), str(row.get("token_address") or ""))
        base = base_from_row(row, lookup.get(key))
        if base is None:
            stats["skipped_no_symbol"] += 1
            continue
        spot_id = f"{base}{BINANCE_QUOTE}"
        if spot_id not in listed:
            stats["skipped_unlisted"] += 1
            continue
        if spot_id in seen:
            stats["skipped_duplicate"] += 1
            continue
        seen.add(spot_id)
        ccxt_symbol = f"{base}/{BINANCE_QUOTE}"
        copy = dict(row)
        copy["source_chain_id"] = row.get("chain_id")
        copy["source_token_address"] = row.get("token_address")
        copy["chain_id"] = BINANCE_VENUE
        copy["token_address"] = spot_id
        copy["primary_pair_address"] = ccxt_symbol
        copy["binance_symbol"] = spot_id
        copy["ccxt_symbol"] = ccxt_symbol
        copy["venue"] = BINANCE_VENUE
        copy["market"] = BINANCE_MARKET
        copy["quote_asset"] = BINANCE_QUOTE
        copy["base_asset"] = base
        copy["universe"] = CEX_UNIVERSE
        mapped.append(copy)
        stats["mapped"] += 1
    return mapped, stats


def stamp_bnb_spot_signals(
    signals: list[dict[str, Any]],
    mapped_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep signals whose identity is a mapped Binance spot id and copy CEX fields.

    A signal that still carries a DEX mint is dropped.
    """
    by_symbol = {
        str(row.get("token_address") or ""): row
        for row in mapped_rows
        if row.get("venue") == BINANCE_VENUE
    }
    kept: list[dict[str, Any]] = []
    for sig in signals:
        token = str(sig.get("token_address") or "")
        meta = by_symbol.get(token)
        if meta is None or str(sig.get("chain_id") or "") != BINANCE_VENUE:
            continue
        for key in _STAMP_KEYS:
            if meta.get(key) is not None:
                sig[key] = meta.get(key)
        sig["pair_address"] = meta.get("ccxt_symbol")
        kept.append(sig)
    return kept


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def symbols_from_connection(conn: sqlite3.Connection) -> set[str]:
    """Binance spot USDT ids present on this connection. Missing tables → empty."""
    try:
        names = _table_names(conn)
    except sqlite3.Error as exc:
        log.warning("listing table scan failed: %s", exc)
        return set()
    found: set[str] = set()
    for table in _LISTING_TABLES:
        if table not in names:
            continue
        cols = _columns(conn, table)
        if "symbol" not in cols:
            continue
        where = ""
        if "exchange" in cols:
            where = " WHERE lower(exchange) = 'binance'"
        base_sql = "base" if "base" in cols else "NULL"
        quote_sql = "quote" if "quote" in cols else "NULL"
        try:
            rows = conn.execute(
                f"SELECT DISTINCT symbol, {base_sql}, {quote_sql} FROM {table}{where}"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning("listing read %s failed: %s", table, exc)
            continue
        for symbol, base, quote in rows:
            spot_id = classify_listed_symbol(
                None if symbol is None else str(symbol),
                None if base is None else str(base),
                None if quote is None else str(quote),
            )
            if spot_id is not None:
                found.add(spot_id)
    return found


def _ro_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/") + "?mode=ro"


def load_binance_spot_usdt_symbols(
    conn: sqlite3.Connection,
    extra_paths: Sequence[Path] | None = None,
) -> set[str]:
    """Union spot USDT ids from ``conn`` and any extra SQLite files that exist."""
    found = symbols_from_connection(conn)
    for path in extra_paths or ():
        if not path.is_file():
            continue
        try:
            side = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            log.warning("listing db open failed path=%s err=%s", path, exc)
            continue
        try:
            found |= symbols_from_connection(side)
        finally:
            side.close()
    return found


def default_listing_db_paths(indicator_db: Path) -> list[Path]:
    """Side databases that may hold Binance listings. The indicator db is not repeated."""
    repo = Path(__file__).resolve().parents[2]
    candidates = (
        indicator_db.parent / "ccxt.db",
        repo / "ccxt.db",
        repo / "crawler" / "ccxt.db",
        repo / "data" / "prior" / "db" / "prior.db",
        repo / "crawler" / "data" / "prior" / "db" / "prior.db",
    )
    indicator = indicator_db.resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved == indicator or resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _parse_raw_object(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
    return None


def _base_from_raw_json(raw: Any) -> str | None:
    parsed = _parse_raw_object(raw)
    if parsed is None:
        return None
    return _base_from_mapping(parsed)


def _base_from_pair_raw(raw: Any, token_address: str) -> str | None:
    """Base from ``pairs.raw_json`` ``baseToken.symbol`` only.

    ``quoteToken.symbol`` is never the base. When ``baseToken.address`` is
    present it must be this token, so a pair stored on the other side is skipped.
    """
    parsed = _parse_raw_object(raw)
    if parsed is None:
        return None
    base_token = parsed.get("baseToken")
    if not isinstance(base_token, dict):
        base_token = parsed.get("base_token")
    if not isinstance(base_token, dict):
        return None
    address = base_token.get("address")
    if isinstance(address, str) and address.strip() and address.strip() != token_address:
        return None
    symbol = base_token.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    return normalize_spot_base(symbol)


def _select_in_chunks(
    conn: sqlite3.Connection,
    *,
    select_sql: str,
    column: str,
    keys: list[tuple[str, str]],
) -> list[Any]:
    """Run ``select_sql`` with ``{predicate}`` over ``(chain_id, column)`` chunks."""
    if column not in {"pair_address", "token_address"}:
        raise ValueError(f"unexpected pair lookup column: {column}")
    out: list[Any] = []
    chunk_size = 80
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start : start + chunk_size]
        predicate = " OR ".join(f"(chain_id = ? AND {column} = ?)" for _ in chunk)
        params: list[str] = []
        for left, right in chunk:
            params.extend((left, right))
        out.extend(conn.execute(select_sql.format(predicate=predicate), params).fetchall())
    return out


def _fill_bases_from_tokens(
    conn: sqlite3.Connection,
    keys: list[tuple[str, str]],
    found: dict[tuple[str, str], str],
) -> int:
    try:
        names = _table_names(conn)
    except sqlite3.Error as exc:
        log.warning("token symbol table scan failed: %s", exc)
        return 0
    if "tokens" not in names:
        return 0
    cols = _columns(conn, "tokens")
    if not {"chain_id", "token_address", "raw_json"} <= cols:
        return 0
    added = 0
    try:
        fetched = _select_in_chunks(
            conn,
            select_sql=(
                "SELECT chain_id, token_address, raw_json FROM tokens WHERE {predicate}"
            ),
            column="token_address",
            keys=keys,
        )
    except sqlite3.Error as exc:
        log.warning("token symbol lookup failed: %s", exc)
        return 0
    for chain_id, token_address, raw_json in fetched:
        base = _base_from_raw_json(raw_json)
        if base is None:
            continue
        found[(str(chain_id), str(token_address))] = base
        added += 1
    return added


def _fill_bases_from_pairs(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    found: dict[tuple[str, str], str],
) -> int:
    """Fill bases still missing after ``tokens.raw_json``.

    Prefer ``primary_pair_address``. Otherwise the first pair row for that
    token whose ``baseToken`` is this token.
    """
    pending: list[dict[str, Any]] = []
    seen_pending: set[tuple[str, str]] = set()
    for row in rows:
        chain = str(row.get("chain_id") or "")
        token = str(row.get("token_address") or "")
        key = (chain, token)
        if not chain or not token or key in found or key in seen_pending:
            continue
        seen_pending.add(key)
        pending.append(row)
    if not pending:
        return 0
    try:
        names = _table_names(conn)
    except sqlite3.Error as exc:
        log.warning("pair symbol table scan failed: %s", exc)
        return 0
    if "pairs" not in names:
        return 0
    cols = _columns(conn, "pairs")
    if not {"chain_id", "pair_address", "raw_json"} <= cols:
        return 0

    pair_keys: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for row in pending:
        pair = str(row.get("primary_pair_address") or "").strip()
        chain = str(row.get("chain_id") or "")
        if not pair:
            continue
        key = (chain, pair)
        if key not in seen_pairs:
            seen_pairs.add(key)
            pair_keys.append(key)
    raw_by_pair: dict[tuple[str, str], Any] = {}
    if pair_keys:
        try:
            fetched = _select_in_chunks(
                conn,
                select_sql=(
                    "SELECT chain_id, pair_address, raw_json FROM pairs WHERE {predicate}"
                ),
                column="pair_address",
                keys=pair_keys,
            )
        except sqlite3.Error as exc:
            log.warning("pair symbol lookup failed: %s", exc)
            fetched = []
        for chain_id, pair_address, raw_json in fetched:
            raw_by_pair[(str(chain_id), str(pair_address))] = raw_json

    added = 0
    still: list[tuple[str, str]] = []
    for row in pending:
        chain = str(row.get("chain_id") or "")
        token = str(row.get("token_address") or "")
        pair = str(row.get("primary_pair_address") or "").strip()
        base = _base_from_pair_raw(raw_by_pair.get((chain, pair)), token)
        if base is not None:
            found[(chain, token)] = base
            added += 1
        else:
            still.append((chain, token))
    if not still or "token_address" not in cols:
        return added
    try:
        fetched = _select_in_chunks(
            conn,
            select_sql=(
                "SELECT chain_id, token_address, raw_json FROM pairs "
                "WHERE {predicate} ORDER BY pair_address"
            ),
            column="token_address",
            keys=still,
        )
    except sqlite3.Error as exc:
        log.warning("pair symbol token lookup failed: %s", exc)
        return added
    grouped: dict[tuple[str, str], list[Any]] = {}
    for chain_id, token_address, raw_json in fetched:
        grouped.setdefault((str(chain_id), str(token_address)), []).append(raw_json)
    for chain, token in still:
        if (chain, token) in found:
            continue
        for raw_json in grouped.get((chain, token), ()):
            base = _base_from_pair_raw(raw_json, token)
            if base is None:
                continue
            found[(chain, token)] = base
            added += 1
            break
    return added


def load_token_bases(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """``(chain_id, token_address) → base``.

    Order: ``tokens.raw_json`` when it has a ticker, else
    ``pairs.raw_json`` ``baseToken.symbol`` for ``primary_pair_address``
    (then any pair stored for that token).
    """
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        chain = str(row.get("chain_id") or "")
        token = str(row.get("token_address") or "")
        if not chain or not token:
            continue
        key = (chain, token)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        return {}
    found: dict[tuple[str, str], str] = {}
    from_tokens = _fill_bases_from_tokens(conn, keys, found)
    from_pairs = _fill_bases_from_pairs(conn, rows, found)
    log.info(
        "token_bases keys=%s from_tokens=%s from_pairs=%s unresolved=%s",
        len(keys),
        from_tokens,
        from_pairs,
        len(keys) - len(found),
    )
    return found
