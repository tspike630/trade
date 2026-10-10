"""
P1.3 X (Twitter) API v2 recent-search social sentiment source.

Without X_BEARER_TOKEN: documented no-op.
With bearer: search recent posts for watched tokens (early + hot), persist
since_id / mention counts, emit social_spike / sentiment_anomaly via alerts.
Never logs the bearer token.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from config import (
    HOT_SEEN_WINDOW_SEC,
    USER_AGENT,
    X_API_BASE,
    X_BEARER_TOKEN,
    X_DAILY_BUDGET_REQUESTS,
    X_DUAL_SIGNAL_LOOKBACK_SEC,
    X_EARLY_WATCH_LIMIT,
    X_EARLY_WATCH_SEC,
    X_HOURLY_BUDGET_REQUESTS,
    X_HOT_WATCH_LIMIT,
    X_MENTION_SPIKE_MIN,
    X_MENTION_SPIKE_RATIO,
    X_RATE_CAPACITY,
    X_RATE_REFILL_PER_SEC,
    X_SEARCH_MAX_RESULTS,
)
from logging_setup import get_logger
from sources.base import SourceResult, TokenSource

log = get_logger("sources.x")

# Solana-ish / EVM-ish address in tweet text (best-effort discovery)
_ADDR_RE = re.compile(
    r"\b(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b"
)
_SYMBOL_SAFE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,14}$")


def x_enabled() -> bool:
    return bool(X_BEARER_TOKEN)


def _redact(s: str) -> str:
    """Strip anything that looks like a bearer token from error strings."""
    if not s:
        return s
    out = s
    if X_BEARER_TOKEN and X_BEARER_TOKEN in out:
        out = out.replace(X_BEARER_TOKEN, "***")
    out = re.sub(r"(?i)bearer\s+[A-Za-z0-9_\-%.=]+", "Bearer ***", out)
    return out


def build_search_query(symbol: str | None, token_address: str) -> str:
    """Build a recent-search query (≤512 chars). Prefer $SYMBOL + address."""
    parts: list[str] = []
    if symbol and _SYMBOL_SAFE.match(symbol):
        parts.append(f'("${symbol}" OR {symbol})')
    # Addresses: quote if needed; Solana base58 is fine raw
    addr = token_address.strip()
    if addr:
        if len(addr) >= 8:
            parts.append(addr)
    if not parts:
        parts.append(addr or "crypto")
    core = " OR ".join(parts)
    # Prefer originals; keep query short
    q = f"({core}) -is:retweet"
    return q[:512]


def parse_search_response(data: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize tweets/search/recent JSON.
    Returns {tweets, newest_id, oldest_id, result_count, texts}.
    """
    if not isinstance(data, dict):
        return {
            "tweets": [],
            "newest_id": None,
            "oldest_id": None,
            "result_count": 0,
            "texts": [],
        }
    tweets = data.get("data") if isinstance(data.get("data"), list) else []
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    texts = []
    for tw in tweets:
        if isinstance(tw, dict) and tw.get("text"):
            texts.append(str(tw["text"]))
    return {
        "tweets": tweets,
        "newest_id": meta.get("newest_id"),
        "oldest_id": meta.get("oldest_id"),
        "result_count": int(meta.get("result_count") or len(tweets)),
        "texts": texts,
    }


def extract_addresses_from_texts(texts: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for m in _ADDR_RE.findall(text or ""):
            key = m.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(m)
    return found


def detect_spike(
    prev: int,
    last: int,
    *,
    ratio: float = X_MENTION_SPIKE_RATIO,
    minimum: int = X_MENTION_SPIKE_MIN,
) -> tuple[bool, str]:
    """
    Velocity spike: last >= minimum and last >= prev * ratio (or prev==0 and last>=minimum).
    Returns (is_spike, kind) where kind is social_spike | sentiment_anomaly | "".
    """
    prev = max(0, int(prev))
    last = max(0, int(last))
    if last < minimum:
        return False, ""
    if prev <= 0:
        # First non-trivial burst
        if last >= minimum * 2:
            return True, "social_spike"
        return False, ""
    if last >= prev * ratio:
        # Extreme multiple → sentiment_anomaly; otherwise social_spike
        if last >= prev * max(ratio * 2, 6):
            return True, "sentiment_anomaly"
        return True, "social_spike"
    return False, ""


class TokenBucket:
    """Simple thread-unsafe token bucket (single async fetch cycle)."""

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = max(1.0, float(capacity))
        self.refill_per_sec = max(0.01, float(refill_per_sec))
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.updated
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.updated = now

    def try_consume(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class BudgetFuse:
    """Hard daily/hourly request caps persisted in SQLite."""

    def __init__(self, store: Any):
        self.store = store

    @staticmethod
    def day_bucket(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return f"day:{now.strftime('%Y-%m-%d')}"

    @staticmethod
    def hour_bucket(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return f"hour:{now.strftime('%Y-%m-%dT%H')}"

    def remaining(self) -> tuple[int, int]:
        day_left = X_DAILY_BUDGET_REQUESTS - self.store.get_x_budget(self.day_bucket())
        hour_left = X_HOURLY_BUDGET_REQUESTS - self.store.get_x_budget(self.hour_bucket())
        return max(0, day_left), max(0, hour_left)

    def allow(self) -> bool:
        d, h = self.remaining()
        return d > 0 and h > 0

    def record(self) -> None:
        self.store.bump_x_budget(self.day_bucket(), 1)
        self.store.bump_x_budget(self.hour_bucket(), 1)


async def search_recent(
    query: str,
    *,
    bearer: str,
    since_id: str | None = None,
    max_results: int = X_SEARCH_MAX_RESULTS,
    timeout: float = 20.0,
    base_url: str = X_API_BASE,
) -> dict[str, Any]:
    """
    GET /2/tweets/search/recent. Raises httpx.HTTPError on transport failure.
    Does not log Authorization.
    """
    max_results = max(10, min(int(max_results), 100))  # API min 10 on many tiers
    params: dict[str, str] = {
        "query": query,
        "max_results": str(max_results),
        "tweet.fields": "created_at,lang,public_metrics",
    }
    if since_id:
        params["since_id"] = since_id
    url = f"{base_url}/tweets/search/recent"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, params=params, headers=headers)
    if resp.status_code == 429:
        raise RuntimeError("x_api rate limited (429)")
    if resp.status_code >= 400:
        # Never include request headers in message
        raise RuntimeError(
            f"x_api HTTP {resp.status_code}: {_redact(resp.text[:200])}"
        )
    return resp.json()


class XApiSource(TokenSource):
    """
    Cycle hook: refresh watch lists, search within budget, emit social alerts.
    Returns SourceResult with any newly discovered token addresses from tweets.
    """

    name = "x"

    def __init__(self, store: Any | None = None, **_kwargs: Any):
        self._store = store
        self._bucket = TokenBucket(X_RATE_CAPACITY, X_RATE_REFILL_PER_SEC)

    async def fetch(self) -> SourceResult:
        if not x_enabled():
            log.info(
                "x: X_BEARER_TOKEN unset — no-op "
                "(would search recent posts for early/hot watched tokens)"
            )
            return SourceResult(source_name=self.name, notes="no_bearer")

        store = self._store
        if store is None:
            log.warning("x: no store bound — skip")
            return SourceResult(source_name=self.name, notes="no_store")

        fuse = BudgetFuse(store)
        day_left, hour_left = fuse.remaining()
        if day_left <= 0 or hour_left <= 0:
            log.warning(
                "x: budget fuse tripped day_left=%s hour_left=%s — skip cycle",
                day_left,
                hour_left,
            )
            return SourceResult(
                source_name=self.name,
                notes=f"budget_fuse day={day_left} hour={hour_left}",
            )

        enrolled = self._enroll_watches(store)
        watches = store.list_x_watch(limit=X_EARLY_WATCH_LIMIT + X_HOT_WATCH_LIMIT)
        searched = 0
        spikes = 0
        discovered: list[dict[str, Any]] = []
        errors = 0

        for w in watches:
            if not fuse.allow():
                log.warning("x: budget exhausted mid-cycle after %s searches", searched)
                break
            if not self._bucket.try_consume(1.0):
                log.info("x: token bucket empty — stop this cycle")
                break
            try:
                n_new, spike, found = await self._search_one(store, fuse, w)
                searched += 1
                if spike:
                    spikes += 1
                for addr in found:
                    # Only enqueue if looks like a different token on same chain
                    if addr.lower() == w["token_address"]:
                        continue
                    discovered.append(
                        {
                            "chainId": w["chain_id"],
                            "tokenAddress": addr,
                            "description": f"x mention near {w['token_address'][:8]}…",
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.error("x: search failed token=%s err=%s", w["token_address"][:12], _redact(str(exc)))

        # Dedup discovered
        uniq: dict[tuple[str, str], dict[str, Any]] = {}
        for item in discovered:
            key = (item["chainId"].lower(), item["tokenAddress"].lower())
            uniq[key] = item
        tokens_out = list(uniq.values())[:20]

        # Enqueue discovered into pending_enrich
        for item in tokens_out:
            try:
                from store import norm_address, norm_chain

                store.upsert_token(
                    norm_chain(item["chainId"]),
                    norm_address(item["tokenAddress"]),
                    item,
                    "source:x",
                )
                store.add_pending_enrich(
                    norm_chain(item["chainId"]),
                    norm_address(item["tokenAddress"]),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("x: enqueue discovered failed: %s", exc)

        notes = (
            f"enrolled={enrolled} searched={searched} spikes={spikes} "
            f"discovered={len(tokens_out)} errors={errors} "
            f"budget_day={fuse.remaining()[0]} budget_hour={fuse.remaining()[1]}"
        )
        log.info("x: cycle done %s", notes)
        return SourceResult(source_name=self.name, tokens=tokens_out, notes=notes)

    def _enroll_watches(self, store: Any) -> int:
        """Mode A: recent listings → early; Mode B: hot-tier → hot."""
        n = 0
        # Early
        for tok in store.list_recent_tokens(
            within_sec=X_EARLY_WATCH_SEC, limit=X_EARLY_WATCH_LIMIT
        ):
            sym = store.guess_token_symbol(tok["chain_id"], tok["token_address"])
            q = build_search_query(sym, tok["token_address"])
            store.upsert_x_watch(
                tok["chain_id"],
                tok["token_address"],
                symbol=sym,
                query_key=q,
                mode="early",
            )
            n += 1
        # Hot (may overwrite mode to hot for recently seen with pairs)
        for tok in store.list_recent_tokens(
            within_sec=HOT_SEEN_WINDOW_SEC, limit=X_HOT_WATCH_LIMIT
        ):
            if tok.get("has_pairs") != 1:
                continue
            sym = store.guess_token_symbol(tok["chain_id"], tok["token_address"])
            q = build_search_query(sym, tok["token_address"])
            store.upsert_x_watch(
                tok["chain_id"],
                tok["token_address"],
                symbol=sym,
                query_key=q,
                mode="hot",
            )
            n += 1
        return n

    async def _search_one(
        self, store: Any, fuse: BudgetFuse, watch: dict[str, Any]
    ) -> tuple[int, bool, list[str]]:
        query = watch.get("query_key") or build_search_query(
            watch.get("symbol"), watch["token_address"]
        )
        since_id = watch.get("since_id") or None
        assert X_BEARER_TOKEN  # gated by x_enabled
        raw = await search_recent(
            query,
            bearer=X_BEARER_TOKEN,
            since_id=since_id,
            max_results=X_SEARCH_MAX_RESULTS,
        )
        fuse.record()
        parsed = parse_search_response(raw)
        count = parsed["result_count"]
        newest = parsed["newest_id"]
        updated = store.update_x_watch_search(
            watch["chain_id"],
            watch["token_address"],
            since_id=newest,
            mention_count_last=count,
            bump_total=count,
        )
        prev = int((updated or {}).get("mention_count_prev") or 0)
        last = int((updated or {}).get("mention_count_last") or count)
        is_spike, kind = detect_spike(prev, last)
        found_addrs = extract_addresses_from_texts(parsed["texts"])

        if is_spike and kind:
            self._emit_social_alert(
                store,
                watch,
                kind=kind,
                prev=prev,
                last=last,
            )
        return count, is_spike, found_addrs

    def _emit_social_alert(
        self,
        store: Any,
        watch: dict[str, Any],
        *,
        kind: str,
        prev: int,
        last: int,
    ) -> None:
        from alerts import emit_alert

        chain = watch["chain_id"]
        token = watch["token_address"]
        dual = False
        for price_type in ("price_change", "volume_h24_jump", "liquidity_drop"):
            if store.has_recent_alert(
                price_type,
                chain_id=chain,
                token_address=token,
                within_sec=X_DUAL_SIGNAL_LOOKBACK_SEC,
            ):
                dual = True
                break
            # also check any pair for this token
            if store.has_recent_alert(
                price_type,
                chain_id=chain,
                within_sec=X_DUAL_SIGNAL_LOOKBACK_SEC,
            ):
                # weaker: only if token matches — has_recent_alert needs token;
                # pair-level: scan recent alerts
                pass

        # Pair-aware dual: look at recent alerts list
        if not dual:
            try:
                for a in store.list_alerts_api(limit=50):
                    if a.get("type") not in (
                        "price_change",
                        "volume_h24_jump",
                        "liquidity_drop",
                    ):
                        continue
                    if (a.get("token_address") or "").lower() != token.lower():
                        continue
                    if (a.get("chain_id") or "").lower() != chain.lower():
                        continue
                    dual = True
                    break
            except Exception:  # noqa: BLE001
                pass

        severity = "high" if dual or kind == "sentiment_anomaly" else "medium"
        payload = {
            "prev_mentions": prev,
            "last_mentions": last,
            "ratio": (last / prev) if prev else None,
            "symbol": watch.get("symbol"),
            "mode": watch.get("mode"),
            "dual_price_social": dual,
            "high_priority": dual,
            "source": "x",
        }
        msg = (
            f"X {kind}: {chain}/{token[:8]}… mentions {prev}→{last}"
            + (" [DUAL price+social]" if dual else "")
        )
        try:
            emit_alert(
                store,
                {
                    "type": kind,
                    "severity": severity,
                    "chain_id": chain,
                    "token_address": token,
                    "pair_address": "",
                    "message": msg,
                    "payload": payload,
                },
                cycle_id="x-social",
            )
        except Exception as exc:  # noqa: BLE001
            log.error("x: emit_alert failed: %s", exc)


# Alias kept for clarity in docs
XSocialSource = XApiSource
