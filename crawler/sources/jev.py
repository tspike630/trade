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
    JEV_MIN_GAP_SEC,
    JEV_PIN_MAX_SHARE,
    JEV_RAW_MAX_CHARS,
    JEV_REFRESH_SHARE,
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

# Append-only history: one row per successful fetch.
JEV_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS jev_signals_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    stance TEXT,
    jev_score REAL,
    observed_score REAL,
    coverage_pct REAL,
    score_kind TEXT,
    analyzed_at TEXT,
    tier TEXT
);
CREATE INDEX IF NOT EXISTS idx_jev_hist_token
    ON jev_signals_history (chain_id, token_address, fetched_at);
CREATE INDEX IF NOT EXISTS idx_jev_hist_fetched
    ON jev_signals_history (fetched_at);
"""
_HISTORY_COLS = {
    "chain_id": "TEXT", "token_address": "TEXT", "fetched_at": "TEXT",
    "stance": "TEXT", "jev_score": "REAL", "observed_score": "REAL",
    "coverage_pct": "REAL", "score_kind": "TEXT", "analyzed_at": "TEXT",
    "tier": "TEXT",
}
# Nullable columns added to jev_signals (existing columns keep their meaning).
_SIGNAL_EXTRA_COLS = {"last_error": "TEXT", "last_error_at": "TEXT"}

HARD_MAX_PER_MIN = 6
HARD_MIN_GAP_SEC = 10.0


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
    """Sliding 60s window + minimum gap between calls (even pacing).

    Hard caps: <= HARD_MAX_PER_MIN per 60s and >= HARD_MIN_GAP_SEC between calls.
    """

    def __init__(self, max_per_min: int = 6, min_gap_sec: float = HARD_MIN_GAP_SEC):
        self.max_per_min = max(1, min(int(max_per_min), HARD_MAX_PER_MIN))
        self.min_gap_sec = max(float(min_gap_sec), HARD_MIN_GAP_SEC)
        self._ts: deque[float] = deque()
        self._last: float | None = None

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
        if self._last is not None:
            gap = self.min_gap_sec - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
                slept += gap
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
        now = time.monotonic()
        self._ts.append(now)
        self._last = now


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _missing_ts_with_stance(row: dict[str, Any]) -> bool:
    """Defense-in-depth: a stance row must carry fetched_at or analyzed_at."""
    return not _blank(row.get("stance")) and _blank(row.get("fetched_at")) and _blank(
        row.get("analyzed_at")
    )


class JevStore:
    """Own sqlite connection; WRITE only jev_signals; READ tokens for hot list."""

    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self.ensure_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JevStore":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        self._conn.executescript(JEV_SIGNALS_DDL)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(jev_signals)")}
        for c, t in _SIGNAL_EXTRA_COLS.items():
            if c not in cols:
                self._conn.execute(f"ALTER TABLE jev_signals ADD COLUMN {c} {t}")
        # Reuse/align an existing jev_signals_history if one is already there.
        self._conn.executescript(
            "\n".join(
                ln for ln in JEV_HISTORY_DDL.splitlines()
                if not ln.startswith("CREATE INDEX") and "ON jev_signals_history" not in ln
            )
        )
        hcols = {r[1] for r in self._conn.execute("PRAGMA table_info(jev_signals_history)")}
        for c, t in _HISTORY_COLS.items():
            if c not in hcols:
                self._conn.execute(f"ALTER TABLE jev_signals_history ADD COLUMN {c} {t}")
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_jev_hist_token
                ON jev_signals_history (chain_id, token_address, fetched_at);
            CREATE INDEX IF NOT EXISTS idx_jev_hist_fetched
                ON jev_signals_history (fetched_at);
            """
        )
        self._conn.commit()

    def mark_error(self, chain_id: str, token_address: str, error: str, at: str) -> None:
        """Keep a successful row intact; only stamp last_error / last_error_at."""
        self._conn.execute(
            "UPDATE jev_signals SET last_error = ?, last_error_at = ? "
            "WHERE chain_id = ? AND token_address = ?",
            (error, at, norm_chain(chain_id), norm_address(token_address)),
        )
        self._conn.commit()

    def append_history(self, row: dict[str, Any], tier: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO jev_signals_history (
                chain_id, token_address, fetched_at, stance, jev_score,
                observed_score, coverage_pct, score_kind, analyzed_at, tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                norm_chain(row["chain_id"]), norm_address(row["token_address"]),
                row.get("fetched_at") or utc_now_iso(), row.get("stance"),
                row.get("jev_score"), row.get("observed_score"),
                row.get("coverage_pct"), row.get("score_kind"),
                row.get("analyzed_at"), tier,
            ),
        )
        self._conn.commit()

    def count_history(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM jev_signals_history").fetchone()[0]
        )

    def upsert_signal(self, row: dict[str, Any]) -> bool:
        chain_id = norm_chain(row["chain_id"])
        token_address = norm_address(row["token_address"])
        if _missing_ts_with_stance(row):
            log.warning(
                "jev refuse write: stance set but fetched_at/analyzed_at both empty "
                "chain_id=%s token_address=%s",
                chain_id,
                token_address,
            )
            return False
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
        for extra in _SIGNAL_EXTRA_COLS:
            if extra in row:
                cols.append(extra)
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
        return True

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
        self,
        *,
        limit: int,
        interval_sec: int,
        refresh_share: float | None = None,
        pins: set[tuple[str, str]] | None = None,
        pin_max_share: float | None = None,
    ) -> list[dict[str, Any]]:
        """
        Three tiers per pass (each row carries ``tier``):
          1. pin     - open-position tokens (position_pin), oldest fetched first
          2. refresh - hot tokens fetched before, oldest fetched_at first
                       (only if older than interval_sec)
          3. new     - hot tokens never fetched, most recently seen first
        Remaining slots after pins split refresh/new by refresh_share
        (default JEV_REFRESH_SHARE=0.5); unused slots spill to the other tier.
        """
        limit = max(1, min(int(limit), 200))
        share = JEV_REFRESH_SHARE if refresh_share is None else float(refresh_share)
        share = min(1.0, max(0.0, share))
        pshare = JEV_PIN_MAX_SHARE if pin_max_share is None else float(pin_max_share)
        cutoff_seen = datetime.fromtimestamp(
            time.time() - HOT_SEEN_WINDOW_SEC, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_before = datetime.fromtimestamp(
            time.time() - max(60, int(interval_sec)), tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if pins is None:
            pins = _load_pinned_tokens()
        out: list[dict[str, Any]] = []
        taken: set[tuple[str, str]] = set()

        # Tier 1: pinned positions (not required to be hot)
        pin_rows = []
        for chain, addr in {(norm_chain(c), norm_address(a)) for c, a in pins}:
            r = self._conn.execute(
                "SELECT fetched_at FROM jev_signals WHERE chain_id=? AND token_address=?",
                (chain, addr),
            ).fetchone()
            fa = r["fetched_at"] if r else None
            if fa is not None and fa >= stale_before:
                continue
            pin_rows.append({"chain_id": chain, "token_address": addr,
                             "last_seen_at": None, "jev_fetched_at": fa, "tier": "pin"})
        pin_rows.sort(key=lambda d: (d["jev_fetched_at"] is not None, d["jev_fetched_at"] or ""))
        pin_cap = max(1, int(limit * max(0.0, min(1.0, pshare)))) if pin_rows else 0
        for d in pin_rows[:pin_cap]:
            out.append(d)
            taken.add((d["chain_id"], d["token_address"]))

        remaining = limit - len(out)
        if remaining <= 0:
            return out
        fetch_n = remaining + len(taken)
        refresh = [
            dict(r, tier="refresh")
            for r in self._conn.execute(
                """
                SELECT t.chain_id, t.token_address, t.last_seen_at,
                       j.fetched_at AS jev_fetched_at
                FROM tokens t
                JOIN jev_signals j
                  ON j.chain_id = t.chain_id AND j.token_address = t.token_address
                WHERE t.has_pairs = 1
                  AND t.last_seen_at IS NOT NULL
                  AND t.last_seen_at >= ?
                  AND j.fetched_at < ?
                ORDER BY j.fetched_at ASC, t.last_seen_at DESC
                LIMIT ?
                """,
                (cutoff_seen, stale_before, fetch_n),
            ).fetchall()
            if (r["chain_id"], r["token_address"]) not in taken
        ]
        new = [
            dict(r, tier="new")
            for r in self._conn.execute(
                """
                SELECT t.chain_id, t.token_address, t.last_seen_at,
                       NULL AS jev_fetched_at
                FROM tokens t
                LEFT JOIN jev_signals j
                  ON j.chain_id = t.chain_id AND j.token_address = t.token_address
                WHERE t.has_pairs = 1
                  AND t.last_seen_at IS NOT NULL
                  AND t.last_seen_at >= ?
                  AND j.fetched_at IS NULL
                ORDER BY t.last_seen_at DESC
                LIMIT ?
                """,
                (cutoff_seen, fetch_n),
            ).fetchall()
            if (r["chain_id"], r["token_address"]) not in taken
        ]
        n_refresh = int(round(remaining * share))
        n_new = remaining - n_refresh
        pick_r = refresh[:n_refresh]
        pick_n = new[:n_new]
        spare = remaining - len(pick_r) - len(pick_n)
        if spare > 0 and len(pick_r) < len(refresh):
            extra = refresh[len(pick_r): len(pick_r) + spare]
            pick_r += extra
            spare -= len(extra)
        if spare > 0 and len(pick_n) < len(new):
            pick_n += new[len(pick_n): len(pick_n) + spare]
        return out + pick_r + pick_n


def _load_pinned_tokens(status: dict[str, Any] | None = None) -> set[tuple[str, str]]:
    """Open-position tokens via position_pin (optional module; failures -> empty).

    On import/load failure logs a WARNING and, if ``status`` is given, sets
    ``status["failed"] = True`` so callers can count pin_load_failed.
    """
    try:
        import position_pin  # type: ignore

        _pairs, tokens = position_pin.load_pins()
        failed_dbs = getattr(position_pin, "last_failed_count", lambda: 0)()
        if failed_dbs and status is not None:
            # per-DB failures from position_pin (missing/no_table/missing_cols/error)
            status["failed"] = True
            status["error"] = f"failed_dbs={failed_dbs}"
            status["dbs"] = dict(getattr(position_pin, "last_status", {}) or {})
        return {(norm_chain(c), norm_address(a)) for c, a in tokens}
    except Exception as exc:  # noqa: BLE001
        log.warning("position_pin load failed; pins skipped this round: %r", exc)
        if status is not None:
            status["failed"] = True
            status["error"] = repr(exc)
        return set()


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
        self.budget = MinuteBudget(max_per_min, JEV_MIN_GAP_SEC)
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
    tier: str | None = None,
) -> dict[str, Any]:
    """Fetch one token and upsert jev_signals. Exceptions → error row.

    On error, an existing successful row is NOT overwritten; only
    last_error/last_error_at are stamped. Successes append to history.
    """
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
    if row.get("error"):
        err_at = row.get("fetched_at") or utc_now_iso()
        try:
            existing = store.get_signal(chain_id, token_address)
        except sqlite3.OperationalError:
            existing = None
        if existing and existing.get("error") is None and existing.get("fetched_at"):
            try:
                store.mark_error(chain_id, token_address, str(row["error"]), err_at)
            except sqlite3.OperationalError as exc:
                log.warning("jev mark_error failed %s/%s err=%s",
                            chain_id, token_address[:12], type(exc).__name__)
            row["preserved"] = True
            return row
        row["last_error"] = row["error"]
        row["last_error_at"] = err_at
        _upsert_signal_with_retry(store, row, chain_id, token_address)
        return row
    _upsert_signal_with_retry(store, row, chain_id, token_address)
    try:
        store.append_history(row, tier)
    except sqlite3.OperationalError as exc:
        log.warning("jev history append failed %s/%s err=%s",
                    chain_id, token_address[:12], type(exc).__name__)
    return row


def _upsert_signal_with_retry(
    store: JevStore,
    row: dict[str, Any],
    chain_id: str,
    token_address: str,
) -> None:
    """Retry locked writes, then append the token to the pending file."""
    last_name = "error"
    for attempt in range(1, 4):
        try:
            store.upsert_signal(row)
            return
        except sqlite3.OperationalError as exc:
            last_name = type(exc).__name__
            log.warning(
                "jev upsert failed %s/%s attempt=%s err=%s",
                chain_id,
                token_address[:12],
                attempt,
                last_name,
            )
            if attempt < 3:
                time.sleep(0.25 * attempt)
    try:
        path = Path(JEV_PENDING_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"chain_id": chain_id, "token_address": token_address}) + "\n"
            )
        log.error(
            "jev upsert failed; stashed pending %s/%s err=%s",
            chain_id,
            token_address[:12],
            last_name,
        )
    except OSError as exc:
        log.error(
            "jev upsert failed and stash failed %s/%s err=%s",
            chain_id,
            token_address[:12],
            type(exc).__name__,
        )


def run_pass(
    store: JevStore,
    client: JevClient,
    *,
    force: bool = False,
    limit: int | None = None,
    tokens: list[tuple[str, str]] | None = None,
    sleep_fn=time.sleep,
) -> dict[str, int]:
    """
    Fetch JEV for hot tokens (and optional pending/explicit list).
    force=True ignores freshness interval (still rate-limited).
    """
    stats = {"seen": 0, "ok": 0, "err": 0, "skip": 0, "preserved": 0,
             "ok_pin": 0, "ok_refresh": 0, "ok_new": 0, "s429": 0,
             "pin_load_failed": 0}
    lim = limit if limit is not None else JEV_HOT_LIMIT
    queue: list[tuple[str, str]] = []
    tiers: dict[str, str] = {}

    if tokens:
        queue.extend(tokens)
    else:
        if JEV_ON_NEW_LISTING:
            queue.extend(drain_pending())
        if force:
            for t in store.list_hot_tokens(limit=lim):
                queue.append((t["chain_id"], t["token_address"]))
        else:
            pin_status: dict[str, Any] = {}
            pins = _load_pinned_tokens(status=pin_status)
            if pin_status.get("failed"):
                stats["pin_load_failed"] += 1
            for t in store.list_due_hot(
                limit=lim, interval_sec=JEV_HOT_INTERVAL_SEC, pins=pins
            ):
                queue.append((t["chain_id"], t["token_address"]))
                tiers[f"{norm_chain(t['chain_id'])}|{norm_address(t['token_address'])}"] = t["tier"]

    # de-dupe preserving order
    seen_keys: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for chain, addr in queue:
        k = f"{norm_chain(chain)}|{norm_address(addr)}"
        if k in seen_keys:
            continue
        seen_keys.add(k)
        uniq.append((norm_chain(chain), norm_address(addr)))

    consec_429 = 0
    for chain_id, token_address in uniq[:lim] if not tokens else uniq:
        stats["seen"] += 1
        tier = tiers.get(f"{chain_id}|{token_address}")
        row = fetch_and_store(store, client, chain_id, token_address, tier=tier)
        if row.get("error"):
            stats["err"] += 1
            if row.get("preserved"):
                stats["preserved"] += 1
            if row.get("error") == "status:429":
                consec_429 += 1
                stats["s429"] += 1
                pause = min(600.0, 60.0 * (2 ** (consec_429 - 1)))
                log.warning("jev consecutive 429 x%s; pausing %.0fs", consec_429, pause)
                sleep_fn(pause)
            else:
                consec_429 = 0
            log.info(
                "jev stored error %s/%s err=%s",
                chain_id,
                token_address[:12],
                row.get("error"),
            )
        else:
            consec_429 = 0
            stats["ok"] += 1
            if tier in ("pin", "refresh", "new"):
                stats[f"ok_{tier}"] += 1
            log.info(
                "jev ok %s/%s score=%s coverage=%s stance=%s",
                chain_id,
                token_address[:12],
                row.get("jev_score"),
                row.get("coverage_pct"),
                row.get("stance"),
            )
    return stats


# Floor on the inter-round tail sleep so --interval 0 with no due tokens
# cannot busy-loop (each pass would otherwise return immediately).
JEV_MIN_ROUND_SLEEP_SEC = 30


def _tail_sleep_sec(interval: int | float | None) -> int:
    try:
        val = int(interval or 0)
    except (TypeError, ValueError):
        val = 0
    return max(JEV_MIN_ROUND_SLEEP_SEC, val)


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
                t0 = time.monotonic()
                stats = run_pass(store, client, force=False, limit=limit)
                log.info(
                    "jev pass ok=%s err=%s seen=%s rows=%s remaining_budget=%s "
                    "pin=%s refresh=%s new=%s pin_load_failed=%s preserved=%s s429=%s history=%s dur=%.0fs",
                    stats["ok"],
                    stats["err"],
                    stats["seen"],
                    store.count_signals(),
                    client.budget.remaining(),
                    stats["ok_pin"], stats["ok_refresh"], stats["ok_new"],
                    stats["pin_load_failed"], stats["preserved"], stats["s429"], store.count_history(),
                    time.monotonic() - t0,
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("jev pass crashed: %s", exc)
            # Even pacing lives in MinuteBudget; this is only a tail wait.
            time.sleep(_tail_sleep_sec(interval))


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
        help=f"Tail sleep seconds between passes (default {JEV_HOT_INTERVAL_SEC}; floored at {JEV_MIN_ROUND_SLEEP_SEC}s)",
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
