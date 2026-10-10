"""
JEV Token Intelligence enricher (Step 2).

NOT a TokenSource that invents tokens — enriches existing tokens via
GET https://jevs.fun/v1/score/{contract}?chain=...

Auth (optional): set JEV_API_KEY. JEV_AUTH_MODE selects how it is sent:
  - header (default): X-API-Key
  - bearer: Authorization: Bearer …
  - query: api_key=… (redact_url strips from logs)

Empty key → public unauthenticated scans (unchanged). Never log the key.
Writes only to independent table `jev_signals`. Rate-limited (default ≤7/min).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from config import (
    DB_PATH,
    HOT_SEEN_WINDOW_SEC,
    JEV_API_KEY,
    JEV_AUTH_MODE,
    JEV_BASE_URL,
    JEV_ENABLED,
    JEV_HOT_INTERVAL_SEC,
    JEV_HOT_LIMIT,
    JEV_MAX_PER_MIN,
    JEV_ON_NEW_LISTING,
    JEV_PENDING_PATH,
    JEV_RAW_MAX_CHARS,
    JEV_REQUEST_TIMEOUT_SEC,
    USER_AGENT,
)
from logging_setup import get_logger, setup_logging
from store import norm_address, norm_chain

log = get_logger("sources.jev")

_ROOT = Path(__file__).resolve().parents[1]

JEV_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS jev_signals (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    jev_score REAL,
    observed_score REAL,
    coverage_pct REAL,
    score_kind TEXT,
    stance TEXT,
    flags_json TEXT,
    metrics_json TEXT,
    analyzed_at TEXT,
    fetched_at TEXT NOT NULL,
    raw TEXT,
    latency_ms REAL,
    cached INTEGER,
    error TEXT,
    PRIMARY KEY (chain_id, token_address)
);
CREATE INDEX IF NOT EXISTS idx_jev_signals_fetched
    ON jev_signals (fetched_at);
CREATE INDEX IF NOT EXISTS idx_jev_signals_score
    ON jev_signals (jev_score);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact_url(url: str) -> str:
    """Strip query secrets (api_key/token/key/secret/auth) for safe logs."""
    if not url:
        return url
    out = re.sub(
        r"(?i)([?&](?:api[_-]?key|token|key|secret|auth)=)[^&]*",
        r"\1***",
        url,
    )
    return out


class MinuteBudget:
    """Sliding 60s window; blocks when count >= max_per_min."""

    def __init__(self, max_per_min: int = 7):
        self.max_per_min = max(1, int(max_per_min))
        self._ts: deque[float] = deque()

    def _prune(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        while self._ts and now - self._ts[0] >= 60.0:
            self._ts.popleft()

    def remaining(self) -> int:
        self._prune()
        return max(0, self.max_per_min - len(self._ts))

    def wait_if_needed(self) -> float:
        """Sleep until a slot opens; return seconds slept."""
        slept = 0.0
        while True:
            self._prune()
            if len(self._ts) < self.max_per_min:
                return slept
            wait = 60.0 - (time.monotonic() - self._ts[0]) + 0.05
            wait = max(0.05, min(wait, 60.0))
            time.sleep(wait)
            slept += wait

    def record(self) -> None:
        self._prune()
        self._ts.append(time.monotonic())


class JevStore:
    """Own sqlite connection; WRITE only jev_signals; READ tokens for hot list."""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self.ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JevStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        self._conn.executescript(JEV_SIGNALS_DDL)
        self._conn.commit()

    def upsert_signal(self, row: dict[str, Any]) -> None:
        chain_id = norm_chain(row["chain_id"])
        token_address = norm_address(row["token_address"])
        fetched_at = row.get("fetched_at") or utc_now_iso()
        cols = [
            "jev_score",
            "observed_score",
            "coverage_pct",
            "score_kind",
            "stance",
            "flags_json",
            "metrics_json",
            "analyzed_at",
            "fetched_at",
            "raw",
            "latency_ms",
            "cached",
            "error",
        ]
        values = []
        for c in cols:
            if c == "fetched_at":
                values.append(fetched_at)
            elif c == "cached":
                v = row.get("cached")
                if v is None:
                    values.append(None)
                else:
                    values.append(1 if v else 0)
            else:
                values.append(row.get(c))
        self._conn.execute(
            f"""
            INSERT INTO jev_signals (
                chain_id, token_address, {", ".join(cols)}
            ) VALUES (?, ?, {", ".join("?" for _ in cols)})
            ON CONFLICT(chain_id, token_address) DO UPDATE SET
                {", ".join(f"{c}=excluded.{c}" for c in cols)}
            """,
            (chain_id, token_address, *values),
        )
        self._conn.commit()

    def get_signal(self, chain_id: str, token_address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """
            SELECT * FROM jev_signals
            WHERE chain_id = ? AND token_address = ?
            """,
            (norm_chain(chain_id), norm_address(token_address)),
        ).fetchone()
        return dict(row) if row else None

    def count_signals(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM jev_signals").fetchone()[0])

    def list_hot_tokens(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """
        Hot tier: has_pairs=1 and last_seen within HOT_SEEN_WINDOW_SEC.
        Prefer never-fetched, then oldest fetched_at.
        """
        limit = max(1, min(int(limit), 200))
        cutoff_epoch = time.time() - HOT_SEEN_WINDOW_SEC
        # Compare ISO strings lexicographically (our format is sortable)
        cutoff = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows = self._conn.execute(
            """
            SELECT t.chain_id, t.token_address, t.last_seen_at,
                   j.fetched_at AS jev_fetched_at
            FROM tokens t
            LEFT JOIN jev_signals j
              ON j.chain_id = t.chain_id AND j.token_address = t.token_address
            WHERE t.has_pairs = 1
              AND t.last_seen_at IS NOT NULL
              AND t.last_seen_at >= ?
            ORDER BY
                CASE WHEN j.fetched_at IS NULL THEN 0 ELSE 1 END ASC,
                j.fetched_at ASC,
                t.last_seen_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_due_hot(
        self, *, limit: int, interval_sec: int
    ) -> list[dict[str, Any]]:
        """Hot tokens missing signal or fetched_at older than interval."""
        limit = max(1, min(int(limit), 200))
        cutoff_seen = datetime.fromtimestamp(
            time.time() - HOT_SEEN_WINDOW_SEC, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_before = datetime.fromtimestamp(
            time.time() - max(60, int(interval_sec)), tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._conn.execute(
            """
            SELECT t.chain_id, t.token_address, t.last_seen_at,
                   j.fetched_at AS jev_fetched_at
            FROM tokens t
            LEFT JOIN jev_signals j
              ON j.chain_id = t.chain_id AND j.token_address = t.token_address
            WHERE t.has_pairs = 1
              AND t.last_seen_at IS NOT NULL
              AND t.last_seen_at >= ?
              AND (j.fetched_at IS NULL OR j.fetched_at < ?)
            ORDER BY
                CASE WHEN j.fetched_at IS NULL THEN 0 ELSE 1 END ASC,
                j.fetched_at ASC,
                t.last_seen_at DESC
            LIMIT ?
            """,
            (cutoff_seen, stale_before, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def _stance_from_payload(data: dict[str, Any]) -> str | None:
    verdict = data.get("investment_verdict")
    if isinstance(verdict, dict):
        s = verdict.get("stance")
        if s is not None:
            return str(s)
    return None


def parse_score_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize JEV JSON into jev_signals row fields (no chain/address)."""
    if not isinstance(data, dict):
        return {"error": "non-object response"}
    flags = data.get("flags")
    metrics = data.get("metrics")
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > JEV_RAW_MAX_CHARS:
        raw = raw[: JEV_RAW_MAX_CHARS] + "…[truncated]"
    return {
        "jev_score": _as_float(data.get("jev_score")),
        "observed_score": _as_float(data.get("observed_score")),
        "coverage_pct": _as_float(data.get("coverage_pct")),
        "score_kind": data.get("score_kind"),
        "stance": _stance_from_payload(data),
        "flags_json": json.dumps(flags, ensure_ascii=False) if flags is not None else None,
        "metrics_json": (
            json.dumps(metrics, ensure_ascii=False)[: JEV_RAW_MAX_CHARS]
            if metrics is not None
            else None
        ),
        "analyzed_at": data.get("analyzed_at"),
        "raw": raw,
        "latency_ms": _as_float(data.get("latency_ms")),
        "cached": bool(data.get("cached")) if "cached" in data else None,
        "error": None,
    }



# Well-known casings (DB lowercases Solana base58 which breaks some APIs)
_WELL_KNOWN_ADDR = {
    ("solana", "so11111111111111111111111111111111111111112"):
        "So11111111111111111111111111111111111111112",
}


def resolve_api_address(store: "JevStore", chain_id: str, token_address: str) -> str:
    """
    Recover original casing for API calls.
    EVM chains accept lowercase; Solana base58 is case-sensitive and our DB
    stores lowercased PKs — pull original from tokens/pairs.raw_json when present.
    """
    chain_id = norm_chain(chain_id)
    token_address = token_address.strip()
    known = _WELL_KNOWN_ADDR.get((chain_id, norm_address(token_address)))
    if known:
        return known
    if chain_id in ("ethereum", "eth", "base", "bsc", "arbitrum", "polygon", "optimism"):
        return token_address  # lowercase fine for EVM
    # Prefer tokens.raw_json tokenAddress / address fields
    try:
        row = store._conn.execute(
            """
            SELECT raw_json FROM tokens
            WHERE chain_id = ? AND token_address = ?
            """,
            (chain_id, norm_address(token_address)),
        ).fetchone()
        if row and row["raw_json"]:
            data = json.loads(row["raw_json"])
            if isinstance(data, dict):
                for key in ("tokenAddress", "address", "token_address"):
                    v = data.get(key)
                    if isinstance(v, str) and v.strip().lower() == norm_address(token_address):
                        return v.strip()
                # nested url token profiles sometimes use url path only
        # pairs.raw_json baseToken.address / quoteToken.address
        prow = store._conn.execute(
            """
            SELECT raw_json FROM pairs
            WHERE chain_id = ? AND token_address = ?
            ORDER BY updated_at DESC LIMIT 1
            """,
            (chain_id, norm_address(token_address)),
        ).fetchone()
        if prow and prow["raw_json"]:
            data = json.loads(prow["raw_json"])
            if isinstance(data, dict):
                for side in ("baseToken", "quoteToken"):
                    tok = data.get(side)
                    if isinstance(tok, dict):
                        v = tok.get("address")
                        if isinstance(v, str) and v.strip().lower() == norm_address(token_address):
                            return v.strip()
                pa = data.get("pairAddress")
                # also check tokenAddress field on pair
                v = data.get("tokenAddress")
                if isinstance(v, str) and v.strip().lower() == norm_address(token_address):
                    return v.strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("resolve_api_address fallback: %s", exc)
    return token_address


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class JevClient:
    """HTTP client with exponential backoff on 429/5xx and per-minute budget."""

    def __init__(
        self,
        *,
        base_url: str = JEV_BASE_URL,
        max_per_min: int = JEV_MAX_PER_MIN,
        timeout: float = JEV_REQUEST_TIMEOUT_SEC,
        api_key: str | None = None,
        auth_mode: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.budget = MinuteBudget(max_per_min)
        self.timeout = timeout
        # Empty / unset → public mode (no auth headers or query params)
        key = (JEV_API_KEY if api_key is None else api_key) or ""
        self.api_key = key.strip()
        mode = (JEV_AUTH_MODE if auth_mode is None else auth_mode) or "header"
        mode = str(mode).strip().lower()
        self.auth_mode = mode if mode in ("header", "bearer", "query") else "header"
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self.api_key and self.auth_mode == "header":
            headers["X-API-Key"] = self.api_key
        elif self.api_key and self.auth_mode == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        # query mode attaches api_key per-request in fetch_score (never log key)
        self._client = httpx.Client(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JevClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def fetch_score(
        self, contract: str, chain: str, *, max_attempts: int = 5
    ) -> dict[str, Any]:
        """
        GET /v1/score/{contract}?chain=...
        Attaches auth when JEV_API_KEY is set (header/bearer/query per JEV_AUTH_MODE).
        Returns parsed fields + optional error. Records budget only on attempt.
        """
        chain = norm_chain(chain)
        contract = contract.strip()  # keep original case for URL; store lowercases
        url = f"{self.base_url}/v1/score/{contract}"
        params: dict[str, str] = {"chain": chain}
        if self.api_key and self.auth_mode == "query":
            params["api_key"] = self.api_key
        last_err: str | None = None
        for attempt in range(max_attempts):
            self.budget.wait_if_needed()
            self.budget.record()
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_err = f"http_error:{type(exc).__name__}"
                log.warning(
                    "jev request error %s attempt=%s: %s",
                    redact_url(str(url)),
                    attempt + 1,
                    last_err,
                )
                time.sleep(min(60.0, (2**attempt) + 0.1))
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = f"status:{resp.status_code}"
                # 503 may mean incomplete — still backoff
                backoff = min(60.0, (2**attempt) * 1.5)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        backoff = max(backoff, float(retry_after))
                    except ValueError:
                        pass
                log.warning(
                    "jev backoff %s status=%s sleep=%.1fs",
                    redact_url(str(resp.url)),
                    resp.status_code,
                    backoff,
                )
                time.sleep(backoff)
                continue

            if resp.status_code != 200:
                last_err = f"status:{resp.status_code}"
                log.warning(
                    "jev non-200 %s status=%s",
                    redact_url(str(resp.url)),
                    resp.status_code,
                )
                return {
                    "error": last_err,
                    "raw": (resp.text or "")[:500],
                    "fetched_at": utc_now_iso(),
                }

            try:
                data = resp.json()
            except json.JSONDecodeError:
                return {
                    "error": "invalid_json",
                    "raw": (resp.text or "")[:500],
                    "fetched_at": utc_now_iso(),
                }
            parsed = parse_score_response(data)
            parsed["fetched_at"] = utc_now_iso()
            return parsed

        return {"error": last_err or "exhausted_retries", "fetched_at": utc_now_iso()}


def drain_pending(path: str | Path = JEV_PENDING_PATH) -> list[tuple[str, str]]:
    """Read and clear JEV_ON_NEW_LISTING pending queue."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("jev pending read failed: %s", exc)
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        chain = norm_chain(str(o.get("chain_id") or ""))
        addr = norm_address(str(o.get("token_address") or ""))
        if not chain or not addr:
            continue
        key = f"{chain}|{addr}"
        if key in seen:
            continue
        seen.add(key)
        out.append((chain, addr))
    try:
        p.write_text("", encoding="utf-8")
    except OSError:
        pass
    return out


def fetch_and_store(
    store: JevStore,
    client: JevClient,
    chain_id: str,
    token_address: str,
) -> dict[str, Any]:
    """Fetch one token and upsert jev_signals. Exceptions → error row."""
    chain_id = norm_chain(chain_id)
    api_addr = resolve_api_address(store, chain_id, token_address)
    token_address = norm_address(token_address)
    try:
        parsed = client.fetch_score(api_addr, chain_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("jev fetch crashed %s/%s: %s", chain_id, token_address[:12], exc)
        parsed = {"error": f"exception:{type(exc).__name__}", "fetched_at": utc_now_iso()}
    row = {
        "chain_id": chain_id,
        "token_address": token_address,
        **parsed,
    }
    try:
        store.upsert_signal(row)
    except Exception as exc:  # noqa: BLE001
        log.warning("jev upsert failed %s/%s: %s", chain_id, token_address[:12], exc)
    return row


def run_pass(
    store: JevStore,
    client: JevClient,
    *,
    force: bool = False,
    limit: int | None = None,
    tokens: list[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """
    Fetch JEV for hot tokens (and optional pending/explicit list).
    force=True ignores freshness interval (still rate-limited).
    """
    stats = {"seen": 0, "ok": 0, "err": 0, "skip": 0}
    lim = limit if limit is not None else JEV_HOT_LIMIT
    queue: list[tuple[str, str]] = []

    if tokens:
        queue.extend(tokens)
    else:
        if JEV_ON_NEW_LISTING:
            queue.extend(drain_pending())
        if force:
            for t in store.list_hot_tokens(limit=lim):
                queue.append((t["chain_id"], t["token_address"]))
        else:
            for t in store.list_due_hot(limit=lim, interval_sec=JEV_HOT_INTERVAL_SEC):
                queue.append((t["chain_id"], t["token_address"]))

    # de-dupe preserving order
    seen_keys: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for chain, addr in queue:
        k = f"{norm_chain(chain)}|{norm_address(addr)}"
        if k in seen_keys:
            continue
        seen_keys.add(k)
        uniq.append((norm_chain(chain), norm_address(addr)))

    for chain_id, token_address in uniq[:lim] if not tokens else uniq:
        stats["seen"] += 1
        row = fetch_and_store(store, client, chain_id, token_address)
        if row.get("error"):
            stats["err"] += 1
            log.info(
                "jev stored error %s/%s err=%s",
                chain_id,
                token_address[:12],
                row.get("error"),
            )
        else:
            stats["ok"] += 1
            log.info(
                "jev ok %s/%s score=%s coverage=%s stance=%s",
                chain_id,
                token_address[:12],
                row.get("jev_score"),
                row.get("coverage_pct"),
                row.get("stance"),
            )
    return stats


def run_loop(
    *,
    db_path: str = DB_PATH,
    interval_sec: int | None = None,
    limit: int | None = None,
) -> None:
    interval = interval_sec if interval_sec is not None else JEV_HOT_INTERVAL_SEC
    log.info(
        "jev --loop interval=%ss max_per_min=%s hot_limit=%s db=%s",
        interval,
        JEV_MAX_PER_MIN,
        limit or JEV_HOT_LIMIT,
        db_path,
    )
    with JevStore(db_path) as store, JevClient() as client:
        while True:
            try:
                stats = run_pass(store, client, force=False, limit=limit)
                log.info(
                    "jev pass ok=%s err=%s seen=%s rows=%s remaining_budget=%s",
                    stats["ok"],
                    stats["err"],
                    stats["seen"],
                    store.count_signals(),
                    client.budget.remaining(),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("jev pass crashed: %s", exc)
            time.sleep(max(30, int(interval)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JEV Token Intelligence enricher (hot-tier only)"
    )
    parser.add_argument("--once", action="store_true", help="Single due/hot pass")
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --once: ignore freshness; fetch up to --limit hot tokens",
    )
    parser.add_argument("--loop", action="store_true", help="Loop forever")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"Loop sleep seconds (default {JEV_HOT_INTERVAL_SEC})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"Max tokens per pass (default {JEV_HOT_LIMIT})",
    )
    parser.add_argument("--db", default=None, help=f"SQLite path (default {DB_PATH})")
    parser.add_argument(
        "--token",
        action="append",
        default=[],
        help="Explicit chain:address (repeatable), e.g. solana:So1111...",
    )
    parser.add_argument(
        "--force-enabled",
        action="store_true",
        help="Run even if JEV_ENABLED=0",
    )
    args = parser.parse_args(argv)

    setup_logging()
    if not JEV_ENABLED and not args.force_enabled:
        log.warning("JEV_ENABLED=0; pass --force-enabled to run")
        return 2

    if not args.once and not args.loop and not args.token:
        parser.error("specify --once, --loop, and/or --token")

    db_path = args.db or DB_PATH
    explicit: list[tuple[str, str]] = []
    for item in args.token:
        if ":" not in item:
            parser.error(f"--token needs chain:address, got {item!r}")
        chain, addr = item.split(":", 1)
        explicit.append((chain.strip(), addr.strip()))

    if args.loop:
        run_loop(db_path=db_path, interval_sec=args.interval, limit=args.limit)
        return 0

    with JevStore(db_path) as store, JevClient() as client:
        stats = run_pass(
            store,
            client,
            force=bool(args.force) or bool(explicit),
            limit=args.limit,
            tokens=explicit or None,
        )
        log.info(
            "jev --once %s rows=%s",
            stats,
            store.count_signals(),
        )
        return 0 if stats["err"] == 0 or stats["ok"] > 0 else 1


if __name__ == "__main__":
    # Allow `python -m sources.jev`
    raise SystemExit(main())
