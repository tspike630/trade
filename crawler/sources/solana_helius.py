"""
P1.2 Helius Solana logsSubscribe listener for new-pair / mint hints.

Without HELIUS_API_KEY: TokenSource is a documented no-op.
With key: background WebSocket logsSubscribe → parse → pending_enrich (+ upsert).

INTEL_LOOP I-1: a confirmed getTransaction also labels initializeMint mintAuthority
as dev and, when HOLDER_SNAPSHOT_ENABLED (default ON), snapshots top holders via
the same HTTP RPC. Unknown deployer or a failed holder RPC writes nothing.

INTEL_LOOP I-3: when BOT_MARKERS_ENABLED (default ON), that same getTransaction
is scanned for bot-like fills. No extra RPC. Missing fields write no bot_suspect
label. The flag does not skip orders.

Default program IDs are well-known public addresses (Raydium / Pump.fun / Meteora / Orca).
Confirm against current on-chain deployments before production — override via HELIUS_PROGRAM_IDS.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from alerts import emit_alert
from config import (
    BOT_MARKERS_ENABLED,
    DB_PATH,
    HELIUS_API_KEY,
    HELIUS_DISCONNECT_ALERT_AFTER,
    HELIUS_HTTP_URL,
    HELIUS_MENTIONS,
    HELIUS_PING_INTERVAL_SEC,
    HELIUS_PROGRAM_IDS,
    HELIUS_RECONNECT_MAX_SEC,
    HELIUS_WS_URL,
    HOLDER_SNAPSHOT_ENABLED,
)
from logging_setup import get_logger, redact_secrets
from sources.base import SourceResult, TokenSource
from sources.bot_markers import apply_bot_observations, extract_bot_observations
from sources.holder_snapshot import fetch_holder_snapshot
from store import norm_address

log = get_logger("sources.helius")

# Public program IDs (document sources; treat as configurable defaults)
# Raydium AMM v4: https://docs.raydium.io
# Raydium CPMM / CLMM: Raydium docs / common explorers
# Pump.fun: commonly cited bonding-curve program
# Meteora DLMM / dynamic pools: Meteora docs
# Orca Whirlpool: Orca docs
# Sources (2026-10): docs.raydium.io/reference/program-addresses ;
# docs.solanatracker.io/guides/pumpfun-program ; pinax-network/substreams-solana-idls ;
# docs.rs/solana-protocols (Meteora DLMM). Re-confirm before production.
DEFAULT_HELIUS_PROGRAM_IDS: list[tuple[str, str]] = [
    ("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8", "Raydium AMM v4"),
    ("CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK", "Raydium CLMM"),
    ("CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C", "Raydium CPMM"),
    ("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "Pump.fun bonding curve"),
    ("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA", "PumpSwap AMM (post-grad)"),
    ("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo", "Meteora DLMM"),
    ("Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB", "Meteora pools"),
    ("whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc", "Orca Whirlpool"),
]

_MINT_INIT_TYPES = frozenset({"initializemint", "initializemint2"})
_TRANSFER_TYPES = frozenset({"transfer", "transferchecked"})
DEV_LABEL_SOURCE = "helius:initializeMint"
TRANSFER_EDGE_SOURCE = "helius:getTransaction"

# Explicit mint fields only. A bare pubkey on an "initialize" line is not a mint.
_MINT_FIELD_RE = re.compile(
    r"(?i)(?:\b(?:base|quote|token)[_ ]?mint\b|\bmint\b)"
    r"[\"'\s:=]+([1-9A-HJ-NP-Za-km-z]{32,44})"
)
_SYSTEMISH = {
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "ComputeBudget111111111111111111111111111111",
    "SysvarRent111111111111111111111111111111111",
    # Common quote / wrapped assets (noise for "new mint" hints)
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def helius_enabled() -> bool:
    return bool(HELIUS_API_KEY)


def default_ws_url(api_key: str | None = None) -> str:
    key = api_key or HELIUS_API_KEY or ""
    if HELIUS_WS_URL:
        return HELIUS_WS_URL
    return f"wss://mainnet.helius-rpc.com/?api-key={key}"


def default_http_url(api_key: str | None = None) -> str:
    key = api_key or HELIUS_API_KEY or ""
    if HELIUS_HTTP_URL:
        return HELIUS_HTTP_URL
    return f"https://mainnet.helius-rpc.com/?api-key={key}"


def resolve_program_ids() -> list[str]:
    raw = HELIUS_PROGRAM_IDS or HELIUS_MENTIONS
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return [pid for pid, _label in DEFAULT_HELIUS_PROGRAM_IDS]


def parse_logs_notification(message: dict[str, Any]) -> dict[str, Any] | None:
    """
    Normalize a logsNotification JSON-RPC message.
    Returns {signature, err, logs, subscription} or None.
    """
    if not isinstance(message, dict):
        return None
    if message.get("method") not in ("logsNotification", None) and "params" not in message:
        # allow fixture that is just params.result
        pass
    params = message.get("params")
    if params is None and "result" in message:
        # bare result wrapper
        value = message.get("result")
        if isinstance(value, dict) and "value" in value:
            params = {"result": value}
        elif isinstance(value, dict) and "signature" in value:
            params = {"result": {"value": value}}
    if not isinstance(params, dict):
        return None
    result = params.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    sig = value.get("signature")
    if not sig:
        return None
    return {
        "signature": str(sig),
        "err": value.get("err"),
        "logs": value.get("logs") if isinstance(value.get("logs"), list) else [],
        "subscription": params.get("subscription"),
        "slot": (result.get("context") or {}).get("slot")
        if isinstance(result.get("context"), dict)
        else None,
    }


def extract_mints_from_logs(logs: list[str], *, program_ids: list[str] | None = None) -> list[str]:
    """
    Fallback mint hints from log text.

    Requires a whitelisted program id somewhere in the batch, then only
    addresses that sit in an explicit mint field (mint / baseMint / quoteMint).
    Random pubkeys on initialize/pool lines are ignored.
    """
    text_lines = [line for line in logs if isinstance(line, str)]
    if program_ids:
        blob = "\n".join(text_lines)
        if not any(pid in blob for pid in program_ids):
            return []
    skip = set(_SYSTEMISH)
    if program_ids:
        skip.update(program_ids)
    found: list[str] = []
    seen: set[str] = set()
    for line in text_lines:
        for mint in _MINT_FIELD_RE.findall(line):
            if mint in skip or mint in seen:
                continue
            seen.add(mint)
            found.append(mint)
    return found


def format_get_tx_failure(exc: BaseException) -> str:
    """Safe getTransaction error text: status or type name, never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"status={exc.response.status_code}"
    return type(exc).__name__


def choose_mint_hints(
    tx_mints: list[str] | None,
    log_mints: list[str] | None,
) -> tuple[list[str], bool]:
    """Prefer getTransaction mints. Log-regex hits do not emit new_listing."""
    confirmed = list(tx_mints or [])
    if confirmed:
        return confirmed[:5], True
    hints = list(log_mints or [])
    if hints:
        return hints[:5], False
    return [], False


def extract_mints_from_transaction(tx: dict[str, Any]) -> list[str]:
    """Extract mint addresses from getTransaction JSON (jsonParsed / meta balances)."""
    mints: list[str] = []
    seen: set[str] = set()

    def _add(m: str | None) -> None:
        if not m or m in seen or m in _SYSTEMISH:
            return
        seen.add(m)
        mints.append(m)

    meta = tx.get("meta") if isinstance(tx, dict) else None
    if isinstance(meta, dict):
        for key in ("preTokenBalances", "postTokenBalances"):
            bal = meta.get(key)
            if isinstance(bal, list):
                for b in bal:
                    if isinstance(b, dict):
                        _add(b.get("mint"))

    message = (tx.get("transaction") or {}).get("message") if isinstance(tx, dict) else None
    if isinstance(message, dict):
        for ix in message.get("instructions") or []:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed")
            if isinstance(parsed, dict):
                info = parsed.get("info") or {}
                if isinstance(info, dict):
                    for k in ("mint", "tokenMint", "baseMint", "quoteMint"):
                        _add(info.get(k))
            # inner instructions elsewhere
        # account keys sometimes include mints — skip bulk noise

    inner = meta.get("innerInstructions") if isinstance(meta, dict) else None
    if isinstance(inner, list):
        for block in inner:
            for ix in (block or {}).get("instructions") or []:
                if not isinstance(ix, dict):
                    continue
                parsed = ix.get("parsed")
                if isinstance(parsed, dict):
                    info = parsed.get("info") or {}
                    if isinstance(info, dict):
                        for k in ("mint", "tokenMint", "mintAddress"):
                            _add(info.get(k))
    return mints


async def rpc_get_transaction(
    signature: str,
    *,
    http_url: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any] | None:
    url = http_url or default_http_url()
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed",
            },
        ],
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    result = data.get("result")
    return result if isinstance(result, dict) else None


def known_program_ids() -> set[str]:
    """Program and quote addresses that are not a token deployer."""
    ids = {norm_address(item) for item in _SYSTEMISH}
    ids.update(norm_address(pid) for pid, _label in DEFAULT_HELIUS_PROGRAM_IDS)
    for pid in resolve_program_ids():
        ids.add(norm_address(pid))
    return ids


def _iter_parsed_infos(tx: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []

    def _walk(instructions: Any) -> None:
        if not isinstance(instructions, list):
            return
        for ix in instructions:
            if not isinstance(ix, dict):
                continue
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info")
            kind = parsed.get("type")
            if isinstance(info, dict) and isinstance(kind, str):
                found.append((kind, info))

    message = (tx.get("transaction") or {}).get("message") if isinstance(tx, dict) else None
    if isinstance(message, dict):
        _walk(message.get("instructions"))
    meta = tx.get("meta") if isinstance(tx, dict) else None
    if isinstance(meta, dict):
        inner = meta.get("innerInstructions")
        if isinstance(inner, list):
            for block in inner:
                if isinstance(block, dict):
                    _walk(block.get("instructions"))
    return found


def _ui_from_token_amount(info: dict[str, Any]) -> tuple[str | None, float | None]:
    token_amount = info.get("tokenAmount")
    if not isinstance(token_amount, dict):
        return None, None
    raw = token_amount.get("amount")
    amount_raw = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else None
    ui = token_amount.get("uiAmount")
    amount_ui = float(ui) if isinstance(ui, (int, float)) and not isinstance(ui, bool) else None
    return amount_raw or None, amount_ui


def extract_dev_authorities(tx: dict[str, Any]) -> dict[str, str]:
    """mint → mintAuthority for initializeMint, skipping programs and blanks.

    Fee payer, token-balance owner, and freezeAuthority are not treated as dev.
    A mint with no explicit mintAuthority stays unlabeled.
    """
    programs = known_program_ids()
    found: dict[str, str] = {}
    for kind, info in _iter_parsed_infos(tx):
        if kind.strip().lower() not in _MINT_INIT_TYPES:
            continue
        mint = info.get("mint")
        authority = info.get("mintAuthority")
        if not isinstance(mint, str) or not isinstance(authority, str):
            continue
        mint_n = norm_address(mint)
        auth_n = norm_address(authority)
        if not mint_n or not auth_n or mint_n == auth_n:
            continue
        if auth_n in programs or mint_n in programs:
            continue
        found.setdefault(mint_n, auth_n)
    return found


def extract_transfer_edges(
    tx: dict[str, Any],
    *,
    signature: str,
    recorded_at: str,
) -> list[dict[str, Any]]:
    """SPL transfer / transferChecked rows from a getTransaction we already fetched.

    A transfer with no mint, source, or destination is dropped. This is not a
    full transfer index; historical backfill stays Phase-2.
    """
    if not signature or not recorded_at:
        return []
    edges: list[dict[str, Any]] = []
    for kind, info in _iter_parsed_infos(tx):
        if kind.strip().lower() not in _TRANSFER_TYPES:
            continue
        mint = info.get("mint")
        source = info.get("source")
        dest = info.get("destination")
        if not all(isinstance(value, str) and value.strip() for value in (mint, source, dest)):
            continue
        if kind.strip().lower() == "transferchecked":
            amount_raw, amount_ui = _ui_from_token_amount(info)
        else:
            raw = info.get("amount")
            amount_raw = (
                str(raw).strip()
                if isinstance(raw, (str, int)) and not isinstance(raw, bool)
                else None
            )
            amount_ui = None
        edges.append(
            {
                "chain_id": "solana",
                "token_address": norm_address(str(mint)),
                "from_address": norm_address(str(source)),
                "to_address": norm_address(str(dest)),
                "amount_raw": amount_raw or None,
                "amount_ui": amount_ui,
                "signature": signature,
                "recorded_at": recorded_at,
                "source": TRANSFER_EDGE_SOURCE,
            }
        )
    return edges


def apply_helius_intel(store: Any, mints: list[str], intel: dict[str, Any] | None) -> None:
    """Write dev labels, holder snapshots, transfer edges, and bot markers.

    Labels and edges whose mint is not in this batch are ignored. whale_top10
    is only the owner wallet of a snapshot row, never a bare token account and
    never a known program id. Bot markers label only when a heuristic hits.
    """
    if not intel:
        return
    mint_set = {norm_address(mint) for mint in mints if str(mint).strip()}
    programs = known_program_ids()
    deployers = intel.get("deployers") or {}
    if isinstance(deployers, dict):
        for mint, authority in deployers.items():
            mint_n = norm_address(str(mint))
            if mint_n not in mint_set or not isinstance(authority, str) or not authority.strip():
                continue
            auth_n = norm_address(authority)
            if not auth_n or auth_n in programs:
                continue
            store.upsert_address_label(
                "solana",
                auth_n,
                "dev",
                token_mint=mint_n,
                source=DEV_LABEL_SOURCE,
            )
    snapshots = intel.get("snapshots") or {}
    if isinstance(snapshots, dict):
        for mint, snapshot in snapshots.items():
            mint_n = norm_address(str(mint))
            if mint_n not in mint_set or not isinstance(snapshot, dict):
                continue
            written = store.insert_holder_snapshot("solana", mint_n, snapshot)
            if written <= 0:
                continue
            source = str(snapshot.get("source") or "helius:getTokenLargestAccounts")
            stored_rows = store.list_holder_snapshot_rows(
                "solana",
                mint_n,
                snapshot_id=str(snapshot.get("snapshot_id") or ""),
            )
            for row in stored_rows:
                if row.get("address_kind") != "owner":
                    continue
                holder = row.get("holder_address")
                if not isinstance(holder, str) or not holder.strip():
                    continue
                if holder in programs:
                    continue
                store.upsert_address_label(
                    "solana",
                    holder,
                    "whale_top10",
                    token_mint=mint_n,
                    source=source,
                )
    transfers = intel.get("transfers") or []
    if isinstance(transfers, list):
        kept = []
        for edge in transfers:
            if not isinstance(edge, dict):
                continue
            token = norm_address(str(edge.get("token_address") or ""))
            if token in mint_set:
                kept.append(edge)
        if kept:
            store.insert_transfer_edges(kept)
    observations = intel.get("bot_observations")
    if isinstance(observations, list) and observations:
        apply_bot_observations(
            store,
            mints,
            observations,
            skip_addresses=programs,
        )


def enqueue_solana_mints(
    store: Any,
    mints: list[str],
    *,
    signature: str | None = None,
    emit_new_listing: bool = True,
    cycle_id: str = "helius",
    intel: dict[str, Any] | None = None,
) -> int:
    """Upsert tokens + pending_enrich. Returns number of newly inserted tokens.

    `intel` is best-effort I-1 fill from a confirmed getTransaction. A failure
    there does not undo the token row. Log-regex hints pass intel=None.
    """
    inserted = 0
    for mint in mints:
        addr = norm_address(mint)
        raw = {
            "chainId": "solana",
            "tokenAddress": mint,
            "description": f"helius logsSubscribe hint sig={signature or '-'}",
            "source_hint": "helius",
        }
        result = store.upsert_token("solana", addr, raw, "source:helius", clue=True)
        store.add_pending_enrich("solana", addr)
        if result == "inserted":
            inserted += 1
            if emit_new_listing:
                try:
                    emit_alert(
                        store,
                        {
                            "type": "new_listing",
                            "severity": "info",
                            "chain_id": "solana",
                            "token_address": addr,
                            "pair_address": "",
                            "message": f"helius new mint hint: solana/{addr}",
                            "payload": {
                                "description": raw["description"],
                                "signature": signature,
                                "source": "helius",
                            },
                        },
                        cycle_id=cycle_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error("helius new_listing alert failed: %s", type(exc).__name__)
    if intel:
        try:
            apply_helius_intel(store, mints, intel)
        except Exception as exc:  # noqa: BLE001
            log.error("helius intel fill failed: %s", type(exc).__name__)
    return inserted


class HeliusSolanaSource(TokenSource):
    """
    One-shot source adapter.
    Without API key: no-op.
    With key: does not open a long WS in fetch(); use HeliusListener / --helius-listen.
    Optionally drains fixtures for offline demos via HELIUS_FIXTURE path.
    """

    name = "helius"

    def __init__(self, store: Any | None = None, **_kwargs: Any):
        self._store = store

    async def fetch(self) -> SourceResult:
        if not helius_enabled():
            log.info(
                "helius: HELIUS_API_KEY unset — no-op "
                "(use --helius-listen with key for live logsSubscribe)"
            )
            return SourceResult(source_name=self.name, notes="no_api_key")
        log.info(
            "helius: fetch() is a marker — live listening is via --helius-listen "
            "(programs=%s)",
            len(resolve_program_ids()),
        )
        return SourceResult(
            source_name=self.name,
            notes=f"listen_mode_only programs={len(resolve_program_ids())}",
        )


class HeliusListener:
    """Background logsSubscribe client with reconnect + ping + admin alert."""

    def __init__(
        self,
        *,
        db_path: str = DB_PATH,
        api_key: str | None = None,
        stop_flag: Callable[[], bool] | None = None,
    ):
        self.db_path = db_path
        self.api_key = api_key or HELIUS_API_KEY
        self.stop_flag = stop_flag
        self.program_ids = resolve_program_ids()
        self._fail_streak = 0
        self._thread: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        # DB writes leave the WS receive loop. One writer thread owns Store.
        self._jobs: queue.Queue[tuple[Any, ...] | None] = queue.Queue(maxsize=1000)

    def start_background(self) -> None:
        if not self.api_key:
            log.warning("HeliusListener.start_background skipped: no HELIUS_API_KEY")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(
                target=self._writer_loop, name="helius-writer", daemon=True
            )
            self._writer.start()
        self._thread = threading.Thread(
            target=self._thread_main, name="helius-listener", daemon=True
        )
        self._thread.start()
        log.info("helius listener thread started programs=%s", len(self.program_ids))

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        if self._writer and self._writer.is_alive():
            self._writer.join(timeout=10.0)

    def _should_stop(self) -> bool:
        if self._stop.is_set():
            return True
        if self.stop_flag and self.stop_flag():
            return True
        return False

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_forever())
        finally:
            self._loop.close()

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._should_stop():
            try:
                await self._session()
                backoff = 1.0
                self._fail_streak = 0
            except Exception as exc:  # noqa: BLE001
                self._fail_streak += 1
                safe = redact_secrets(str(exc))
                log.error(
                    "helius session error streak=%s err=%s",
                    self._fail_streak,
                    safe,
                )
                if self._fail_streak >= HELIUS_DISCONNECT_ALERT_AFTER:
                    self._submit(("admin", safe[:400]))
                await asyncio.sleep(min(backoff, HELIUS_RECONNECT_MAX_SEC))
                backoff = min(backoff * 2, HELIUS_RECONNECT_MAX_SEC)

    def _submit(self, job: tuple[Any, ...]) -> None:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            log.warning("helius write queue full; dropped %s", job[0])

    def _writer_loop(self) -> None:
        from store import Store

        store: Store | None = None
        try:
            while True:
                if self._stop.is_set() and self._jobs.empty():
                    break
                try:
                    job = self._jobs.get(timeout=0.5)
                except queue.Empty:
                    continue
                if job is None:
                    break
                try:
                    if store is None:
                        store = Store(self.db_path)
                    self._apply_job(store, job)
                except Exception as exc:  # noqa: BLE001
                    log.error("helius writer failed err=%s", type(exc).__name__)
                    if store is not None:
                        try:
                            store.close()
                        except Exception:
                            pass
                        store = None
        finally:
            if store is not None:
                store.close()

    def _apply_job(self, store: Any, job: tuple[Any, ...]) -> None:
        kind = job[0]
        if kind == "mints":
            _, mints, sig, emit = job[:4]
            side = job[4] if len(job) > 4 else None
            n = enqueue_solana_mints(
                store,
                mints,
                signature=sig,
                emit_new_listing=emit,
                cycle_id="helius-listen",
                intel=side if isinstance(side, dict) else None,
            )
            log.info(
                "helius enqueued sig=%s mints=%s newly_inserted=%s emit_new_listing=%s",
                str(sig)[:16],
                len(mints),
                n,
                emit,
            )
            return
        if kind == "admin":
            self._emit_admin_alert(store, str(job[1]))
            return
        log.warning("helius writer ignored job %s", kind)

    def _emit_admin_alert(self, store: Any, err: str) -> None:
        try:
            from alerts_telegram import telegram_enabled

            if not telegram_enabled():
                return
            from alerts import emit_alert

            safe = redact_secrets(err)[:400]
            emit_alert(
                store,
                {
                    "type": "system_error",
                    "severity": "high",
                    "chain_id": "solana",
                    "token_address": "",
                    "pair_address": "",
                    "message": (
                        f"Helius WS disconnect/reconnect failures "
                        f"streak={self._fail_streak}: {safe}"
                    ),
                    "payload": {"component": "helius_listener"},
                },
                cycle_id="helius-listener",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("helius admin alert failed: %s", type(exc).__name__)

    async def _session(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package required for Helius listener") from exc

        ws_url = default_ws_url(self.api_key)
        log.info("helius connecting wss ... programs=%s", len(self.program_ids))
        async with websockets.connect(
            ws_url,
            ping_interval=HELIUS_PING_INTERVAL_SEC,
            ping_timeout=60,
            max_size=8 * 1024 * 1024,
        ) as ws:
            # Subscribe one mention stream per program
            for i, pid in enumerate(self.program_ids):
                sub = {
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [pid]},
                        {"commitment": "confirmed"},
                    ],
                }
                await ws.send(json.dumps(sub))
            log.info("helius subscribed programs=%s", len(self.program_ids))
            self._fail_streak = 0

            while not self._should_stop():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=HELIUS_PING_INTERVAL_SEC + 30)
                except asyncio.TimeoutError:
                    # force application-level keepalive if needed
                    try:
                        pong = await ws.ping()
                        await asyncio.wait_for(pong, timeout=10)
                    except Exception:
                        raise ConnectionError("helius idle ping failed")
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("method") != "logsNotification":
                    continue
                await self._handle_notification(msg)

    async def _handle_notification(self, msg: dict[str, Any]) -> None:
        parsed = parse_logs_notification(msg)
        if not parsed or parsed.get("err"):
            return
        sig = parsed["signature"]
        logs = parsed.get("logs") or []
        tx: dict[str, Any] | None = None
        tx_mints: list[str] = []
        try:
            tx = await rpc_get_transaction(sig, http_url=default_http_url(self.api_key))
            if tx:
                tx_mints = extract_mints_from_transaction(tx)
        except httpx.HTTPStatusError as exc:
            log.warning(
                "helius getTransaction failed sig=%s %s",
                sig[:16],
                format_get_tx_failure(exc),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "helius getTransaction failed sig=%s err=%s",
                sig[:16],
                format_get_tx_failure(exc),
            )

        log_mints = (
            []
            if tx_mints
            else extract_mints_from_logs(logs, program_ids=self.program_ids)
        )
        mints, emit = choose_mint_hints(tx_mints, log_mints)
        if not mints:
            log.debug("helius no mints for sig=%s", sig[:16])
            return
        if not emit:
            log.debug(
                "helius log-regex mint hints downgraded sig=%s n=%s",
                sig[:16],
                len(mints),
            )
        side = await self._intel_side(tx, mints, sig) if tx and emit else None
        self._submit(("mints", mints, sig, emit, side))

    async def _intel_side(
        self,
        tx: dict[str, Any],
        mints: list[str],
        signature: str,
    ) -> dict[str, Any]:
        """Dev label + transfers from the getTransaction already in hand.

        Holder RPC runs only when the flag is on, and only for these confirmed
        mints. A snapshot failure leaves that mint out of `snapshots`.
        Bot markers parse this same transaction when their flag is on. They
        do not call RPC. The flag off leaves `bot_observations` unset.
        """
        recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        side: dict[str, Any] = {
            "deployers": extract_dev_authorities(tx),
            "transfers": extract_transfer_edges(
                tx, signature=signature, recorded_at=recorded_at
            ),
            "snapshots": {},
        }
        if BOT_MARKERS_ENABLED:
            side["bot_observations"] = extract_bot_observations(
                tx,
                signature=signature,
                recorded_at=recorded_at,
                mints=mints,
                skip_addresses=known_program_ids(),
            )
        if not HOLDER_SNAPSHOT_ENABLED:
            return side
        http_url = default_http_url(self.api_key)

        async def _one(mint: str) -> tuple[str, dict[str, Any] | None]:
            try:
                snap = await fetch_holder_snapshot(
                    mint,
                    http_url=http_url,
                    enabled=True,
                    timeout=12.0,
                    recorded_at=recorded_at,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "helius holder snapshot failed mint=%s err=%s",
                    mint[:8],
                    type(exc).__name__,
                )
                return mint, None
            return mint, snap

        pairs = await asyncio.gather(*(_one(mint) for mint in mints))
        side["snapshots"] = {mint: snap for mint, snap in pairs if snap}
        return side


def start_helius_listener_background(
    *,
    db_path: str = DB_PATH,
    stop_flag: Callable[[], bool] | None = None,
) -> HeliusListener | None:
    if not helius_enabled():
        log.warning("--helius-listen requested but HELIUS_API_KEY unset")
        return None
    listener = HeliusListener(db_path=db_path, stop_flag=stop_flag)
    listener.start_background()
    return listener
