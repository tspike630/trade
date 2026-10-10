"""
P1.2 Helius Solana logsSubscribe listener for new-pair / mint hints.

Without HELIUS_API_KEY: TokenSource is a documented no-op.
With key: background WebSocket logsSubscribe → parse → pending_enrich (+ upsert).

Default program IDs are well-known public addresses (Raydium / Pump.fun / Meteora / Orca).
Confirm against current on-chain deployments before production — override via HELIUS_PROGRAM_IDS.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from typing import Any, Callable

import httpx

from config import (
    DB_PATH,
    HELIUS_API_KEY,
    HELIUS_DISCONNECT_ALERT_AFTER,
    HELIUS_HTTP_URL,
    HELIUS_MENTIONS,
    HELIUS_PING_INTERVAL_SEC,
    HELIUS_PROGRAM_IDS,
    HELIUS_RECONNECT_MAX_SEC,
    HELIUS_WS_URL,
)
from logging_setup import get_logger
from sources.base import SourceResult, TokenSource

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

# Solana base58 pubkey-ish (32–44 chars, no 0/O/I/l)
_BASE58_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
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
    Best-effort mint hints from log lines.
    Looks for base58 pubkeys that are not system/token programs or subscribed program IDs.
    """
    skip = set(_SYSTEMISH)
    if program_ids:
        skip.update(program_ids)
    found: list[str] = []
    seen: set[str] = set()
    for line in logs:
        if not isinstance(line, str):
            continue
        # Prefer lines that look like create/initialize
        interesting = any(
            k in line.lower()
            for k in ("initialize", "create", "mint", "pool", "bonding")
        )
        for m in _BASE58_RE.findall(line):
            if m in skip or m in seen:
                continue
            # Heuristic: keep interesting-line candidates first; still collect others
            if interesting or "Program log" in line:
                seen.add(m)
                found.append(m)
    return found


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


def enqueue_solana_mints(
    store: Any,
    mints: list[str],
    *,
    signature: str | None = None,
    emit_new_listing: bool = True,
    cycle_id: str = "helius",
) -> int:
    """Upsert tokens + pending_enrich. Returns number of newly inserted tokens."""
    from alerts import emit_alert
    from store import norm_address

    inserted = 0
    for mint in mints:
        addr = norm_address(mint)
        raw = {
            "chainId": "solana",
            "tokenAddress": mint,
            "description": f"helius logsSubscribe hint sig={signature or '-'}",
            "source_hint": "helius",
        }
        result = store.upsert_token("solana", addr, raw, "source:helius")
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
                    log.error("helius new_listing alert failed: %s", exc)
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()

    def start_background(self) -> None:
        if not self.api_key:
            log.warning("HeliusListener.start_background skipped: no HELIUS_API_KEY")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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
                log.error(
                    "helius session error streak=%s err=%s",
                    self._fail_streak,
                    exc,
                )
                if self._fail_streak >= HELIUS_DISCONNECT_ALERT_AFTER:
                    self._admin_alert_disconnect(str(exc))
                await asyncio.sleep(min(backoff, HELIUS_RECONNECT_MAX_SEC))
                backoff = min(backoff * 2, HELIUS_RECONNECT_MAX_SEC)

    def _admin_alert_disconnect(self, err: str) -> None:
        try:
            from alerts_telegram import telegram_enabled
            from store import Store

            if not telegram_enabled():
                return
            with Store(self.db_path) as store:
                from alerts import emit_alert

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
                            f"streak={self._fail_streak}: {err[:400]}"
                        ),
                        "payload": {"component": "helius_listener"},
                    },
                    cycle_id="helius-listener",
                )
        except Exception as exc:  # noqa: BLE001
            log.error("helius admin alert failed: %s", exc)

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
        from store import Store

        parsed = parse_logs_notification(msg)
        if not parsed or parsed.get("err"):
            return
        sig = parsed["signature"]
        logs = parsed.get("logs") or []
        mints = extract_mints_from_logs(logs, program_ids=self.program_ids)

        if not mints:
            try:
                tx = await rpc_get_transaction(sig, http_url=default_http_url(self.api_key))
                if tx:
                    mints = extract_mints_from_transaction(tx)
            except Exception as exc:  # noqa: BLE001
                log.warning("helius getTransaction failed sig=%s err=%s", sig[:16], exc)

        if not mints:
            log.debug("helius no mints for sig=%s", sig[:16])
            return

        # Cap noise: at most a few mints per tx
        mints = mints[:5]
        with Store(self.db_path) as store:
            n = enqueue_solana_mints(
                store, mints, signature=sig, cycle_id="helius-listen"
            )
        log.info(
            "helius enqueued sig=%s mints=%s newly_inserted=%s",
            sig[:16],
            len(mints),
            n,
        )


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
