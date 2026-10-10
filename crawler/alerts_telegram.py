"""Telegram delivery channel for alerts (P1.1 + 429 storm protection).

Detection/cooldown stay in alerts.py. This module:
- Parses Telegram retry_after on 429; sleeps capped; max TG_MAX_RETRIES then drop
- Per-chat rate limits (~1 msg/s, ~20/min) + ban_until from long retry_after
- Priority queue: high (new_listing/cto/admin system) preferred; drop/merge low when full
- First failure WARN; escalate ERROR only on repeated fail for same alert key
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from heapq import heappop, heappush
from typing import Any

import httpx

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_CHAT_ID_ADMIN,
    TELEGRAM_CHAT_ID_ALERT,
    TELEGRAM_CHAT_ID_MAIN,
    TELEGRAM_TIMEOUT_SEC,
    TG_429_SLEEP_CAP_SEC,
    TG_MAX_RETRIES,
    TG_QUEUE_DROP_LOW,
    TG_QUEUE_MAX,
    TG_RATE_MIN_INTERVAL_SEC,
    TG_RATE_PER_MIN,
)
from logging_setup import get_logger

log = get_logger("alerts.telegram")

# Routing buckets
_MAIN_TYPES = frozenset({"new_listing", "cto_new"})
_ALERT_TYPES = frozenset({
    "price_change", "volume_h24_jump", "liquidity_drop",
    "social_spike", "sentiment_anomaly", "arb_opportunity",
})
_ADMIN_TYPES = frozenset({"proxy_pool_low", "system_error", "pipeline_error", "heartbeat_fail"})
_HIGH_PRIORITY_TYPES = _MAIN_TYPES | _ADMIN_TYPES


def telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def resolve_chat_id(alert_type: str) -> str | None:
    """Map alert type → chat id; fall back to TELEGRAM_CHAT_ID."""
    if alert_type in _MAIN_TYPES:
        return TELEGRAM_CHAT_ID_MAIN or TELEGRAM_CHAT_ID
    if alert_type in _ALERT_TYPES:
        return TELEGRAM_CHAT_ID_ALERT or TELEGRAM_CHAT_ID
    return TELEGRAM_CHAT_ID_ADMIN or TELEGRAM_CHAT_ID


def alert_priority(alert_type: str) -> int:
    """Lower number = higher priority (heapq)."""
    if alert_type in _ADMIN_TYPES:
        return 0
    if alert_type in _MAIN_TYPES:
        return 1
    if alert_type in ("social_spike", "sentiment_anomaly"):
        return 2
    return 3  # price/volume/liq/arb — low


def parse_retry_after(resp_or_body: Any) -> float | None:
    """
    Extract retry_after seconds from a Telegram Bot API response.
    Accepts httpx.Response, dict, or raw JSON/text body.
    Prefers parameters.retry_after; falls back to 'retry after N' in description.
    """
    status = None
    data: Any = None
    text = ""

    if isinstance(resp_or_body, httpx.Response):
        status = resp_or_body.status_code
        text = resp_or_body.text or ""
        try:
            data = resp_or_body.json()
        except Exception:  # noqa: BLE001
            data = None
        # Also honor Retry-After header if present
        hdr = resp_or_body.headers.get("Retry-After") or resp_or_body.headers.get("retry-after")
        if hdr:
            try:
                return float(hdr)
            except ValueError:
                pass
    elif isinstance(resp_or_body, dict):
        data = resp_or_body
        text = json.dumps(resp_or_body)
    elif isinstance(resp_or_body, (str, bytes)):
        text = resp_or_body.decode() if isinstance(resp_or_body, bytes) else resp_or_body
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    else:
        return None

    if isinstance(data, dict):
        params = data.get("parameters") or {}
        if isinstance(params, dict) and params.get("retry_after") is not None:
            try:
                return float(params["retry_after"])
            except (TypeError, ValueError):
                pass
        desc = str(data.get("description") or "")
        m = re.search(r"retry after\s+(\d+(?:\.\d+)?)", desc, re.I)
        if m:
            return float(m.group(1))

    m = re.search(r"retry after\s+(\d+(?:\.\d+)?)", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r'"retry_after"\s*:\s*(\d+(?:\.\d+)?)', text)
    if m:
        return float(m.group(1))

    if status == 429:
        return 1.0  # minimal backoff if 429 but no number
    return None


def _dex_token_url(chain: str, token: str) -> str:
    return f"https://dexscreener.com/{chain}/{token}"


def _dex_pair_url(chain: str, pair: str) -> str:
    return f"https://dexscreener.com/{chain}/{pair}"


def _fmt_num(v: Any) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
        return f"{f:.6g}"
    except (TypeError, ValueError):
        return escape(str(v))


def format_alert_html(alert: dict[str, Any], *, cycle_id: str = "-") -> str:
    """Build Telegram HTML body for an alert dict."""
    atype = alert.get("type") or "unknown"
    severity = alert.get("severity") or "info"
    chain = alert.get("chain_id") or ""
    token = alert.get("token_address") or ""
    pair = alert.get("pair_address") or ""
    payload = alert.get("payload") or {}
    message = alert.get("message") or ""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    if atype in ("new_listing", "cto_new"):
        title = "🆕 New listing" if atype == "new_listing" else "🏴 CTO"
        link = _dex_token_url(chain, token) if chain and token else ""
        desc = payload.get("description") or ""
        lines = [
            f"<b>{escape(title)}</b> · <code>{escape(severity)}</code>",
            f"Chain: <code>{escape(chain)}</code>",
            f"Token: <code>{escape(token)}</code>",
        ]
        if desc:
            lines.append(f"Desc: {escape(str(desc)[:200])}")
        if link:
            lines.append(f'<a href="{escape(link)}">Open on DexScreener</a>')
        lines.append(f"cycle=<code>{escape(cycle_id)}</code> · {ts}")
        if message:
            lines.append(escape(message)[:300])
        return "\n".join(lines)

    if atype == "arb_opportunity":
        payload = alert.get("payload") or {}
        buy = payload.get("buy_venue") or {}
        sell = payload.get("sell_venue") or {}
        lines = [
            f"<b>🔀 Arb opportunity (signal only)</b> · <code>{escape(severity)}</code>",
            f"Key: <code>{escape(str(payload.get('key')))}</code>",
            f"Spread: <b>{escape(str(payload.get('spread_pct')))}%</b>",
            f"Buy: <code>{escape(str(buy.get('chain_id')))}</code> @ {_fmt_num(buy.get('price'))}",
            f"Sell: <code>{escape(str(sell.get('chain_id')))}</code> @ {_fmt_num(sell.get('price'))}",
            "Not executable — research / bridge risk / fees apply.",
            f"cycle=<code>{escape(cycle_id)}</code> · {ts}",
        ]
        return "\n".join(lines)

    if atype in ("social_spike", "sentiment_anomaly"):
        title = "📣 Social spike" if atype == "social_spike" else "⚡ Sentiment anomaly"
        link = _dex_token_url(chain, token) if chain and token else ""
        dual = payload.get("dual_price_social") or payload.get("high_priority")
        lines = [
            f"<b>{escape(title)}</b> · <code>{escape(severity)}</code>",
            f"Chain: <code>{escape(chain)}</code>",
            f"Token: <code>{escape(token)}</code>",
            f"Mentions: {_fmt_num(payload.get('prev_mentions'))} → "
            f"<b>{_fmt_num(payload.get('last_mentions'))}</b>",
        ]
        if payload.get("symbol"):
            lines.append(f"Symbol: <code>{escape(str(payload.get('symbol')))}</code>")
        if dual:
            lines.append("<b>DUAL price + social — high priority</b>")
        if link:
            lines.append(f'<a href="{escape(link)}">Open on DexScreener</a>')
        lines.append(f"cycle=<code>{escape(cycle_id)}</code> · {ts}")
        if message:
            lines.append(escape(message)[:300])
        return "\n".join(lines)

    if atype in _ALERT_TYPES:
        title_map = {
            "price_change": "📈 Price change",
            "volume_h24_jump": "📊 Volume h24",
            "liquidity_drop": "💧 Liquidity drop",
        }
        title = title_map.get(atype, atype)
        link = _dex_pair_url(chain, pair) if chain and pair else (
            _dex_token_url(chain, token) if chain and token else ""
        )
        pct = payload.get("change_pct", payload.get("drop_pct"))
        lines = [
            f"<b>{escape(title)}</b> · <code>{escape(severity)}</code>",
            f"Chain: <code>{escape(chain)}</code>",
            f"Pair: <code>{escape(pair)}</code>",
            f"Token: <code>{escape(token)}</code>",
        ]
        if pct is not None:
            lines.append(f"Change: <b>{escape(str(pct))}%</b>")
        if "old_priceUsd" in payload or "new_priceUsd" in payload:
            lines.append(
                f"priceUsd: {_fmt_num(payload.get('old_priceUsd'))} → "
                f"{_fmt_num(payload.get('new_priceUsd'))}"
            )
        if "old_volume_h24" in payload or "new_volume_h24" in payload:
            lines.append(
                f"volume.h24: {_fmt_num(payload.get('old_volume_h24'))} → "
                f"{_fmt_num(payload.get('new_volume_h24'))}"
            )
        if "old_liquidity_usd" in payload or "new_liquidity_usd" in payload:
            lines.append(
                f"liquidity: {_fmt_num(payload.get('old_liquidity_usd'))} → "
                f"{_fmt_num(payload.get('new_liquidity_usd'))}"
            )
        if link:
            lines.append(f'<a href="{escape(link)}">Open pair on DexScreener</a>')
        lines.append(f"cycle=<code>{escape(cycle_id)}</code> · {ts}")
        return "\n".join(lines)

    lines = [
        f"<b>🛠 Admin / {escape(atype)}</b> · <code>{escape(severity)}</code>",
        f"cycle=<code>{escape(cycle_id)}</code>",
        f"time={ts}",
        escape(message)[:1500] if message else "(no message)",
    ]
    if chain or token or pair:
        lines.append(
            f"chain=<code>{escape(chain)}</code> "
            f"token=<code>{escape(token)}</code> "
            f"pair=<code>{escape(pair)}</code>"
        )
    return "\n".join(lines)


@dataclass
class _ChatGate:
    """Per-chat ban + sliding-window rate limit."""

    ban_until: float = 0.0  # time.monotonic()
    last_send: float = 0.0
    send_times: deque = field(default_factory=deque)  # monotonic timestamps in last 60s
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Ban-period LOG throttle (not send throttle)
    last_ban_warn_ts: float = 0.0
    ban_dropped_count: int = 0
    ban_retry_after: float = 0.0  # last retry_after that set the ban
    ban_first_alert_key: str = ""
    ban_was_active: bool = False  # for ban-clear INFO

    def banned_remaining(self) -> float:
        return max(0.0, self.ban_until - time.monotonic())

    def set_ban(self, retry_after_sec: float) -> None:
        # Cap stored ban to avoid overflow weirdness but honor long Telegram bans
        # (we skip sends until ban clears — we do NOT sleep the whole duration).
        wait = max(0.0, float(retry_after_sec))
        self.ban_until = max(self.ban_until, time.monotonic() + wait)
        self.ban_retry_after = max(self.ban_retry_after, wait)
        self.ban_was_active = True

    def wait_for_slot(self, *, max_wait: float = 5.0) -> bool:
        """
        Block briefly to respect ~1 msg/s and ~20/min.
        Returns False if chat is banned or cannot get a slot within max_wait.
        """
        deadline = time.monotonic() + max_wait
        while True:
            with self.lock:
                now = time.monotonic()
                if now < self.ban_until:
                    return False
                while self.send_times and now - self.send_times[0] > 60.0:
                    self.send_times.popleft()
                need_wait = 0.0
                if self.last_send > 0:
                    need_wait = max(
                        need_wait, TG_RATE_MIN_INTERVAL_SEC - (now - self.last_send)
                    )
                if len(self.send_times) >= TG_RATE_PER_MIN:
                    need_wait = max(
                        need_wait, 60.0 - (now - self.send_times[0]) + 0.01
                    )
                if need_wait <= 0:
                    return True
                if now + need_wait > deadline:
                    return False
                sleep_for = min(need_wait, deadline - now)
            time.sleep(max(0.0, sleep_for))

    def record_send(self) -> None:
        now = time.monotonic()
        self.last_send = now
        self.send_times.append(now)


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int
    chat_id: str = field(compare=False)
    text: str = field(compare=False)
    alert_key: str = field(compare=False)
    alert_type: str = field(compare=False)
    merge_key: str = field(compare=False)
    merged_count: int = field(default=1, compare=False)


class TelegramAlerter:
    """Bot API sendMessage with 429 backoff, per-chat rate limit, and priority queue."""

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = TELEGRAM_TIMEOUT_SEC,
        transport: httpx.BaseTransport | None = None,
        sleep_fn=time.sleep,
    ):
        self.token = token if token is not None else TELEGRAM_BOT_TOKEN
        self.timeout = timeout
        self._transport = transport  # for unit tests / demos
        self._sleep = sleep_fn
        self._admin_fail_notified = False
        self._gates: dict[str, _ChatGate] = {}
        self._gates_lock = threading.Lock()
        self._fail_counts: dict[str, int] = {}
        self._queue: list[_QueueItem] = []
        self._queue_lock = threading.Lock()
        self._seq = 0
        self._dropped_low = 0
        self._merged_low = 0
        self._stats = {
            "sent": 0,
            "dropped_429": 0,
            "dropped_ban": 0,
            "dropped_queue": 0,
            "retries": 0,
        }

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _gate(self, chat_id: str) -> _ChatGate:
        with self._gates_lock:
            g = self._gates.get(chat_id)
            if g is None:
                g = _ChatGate()
                self._gates[chat_id] = g
            return g

    def _log_fail(self, alert_key: str, msg: str, *args: Any) -> None:
        n = self._fail_counts.get(alert_key, 0) + 1
        self._fail_counts[alert_key] = n
        if n <= 1:
            log.warning(msg, *args)
        else:
            log.error(msg + " (repeated=%s)", *args, n)

    def _note_ban_drop(
        self,
        gate: _ChatGate,
        chat_id: str,
        alert_key: str,
        *,
        remaining: float,
        policy: str,
    ) -> None:
        """Throttle ban-period WARN logs: first full, then ≤1/min summary per chat."""
        gate.ban_dropped_count += 1
        now = time.monotonic()
        if gate.ban_dropped_count == 1:
            gate.ban_first_alert_key = alert_key
            gate.last_ban_warn_ts = now
            log.warning(
                "telegram ban drop (first) chat=%s ban_remaining=%.0fs "
                "retry_after=%.0fs policy=%s first_alert_key=%s",
                chat_id,
                remaining,
                gate.ban_retry_after or remaining,
                policy,
                alert_key,
            )
            return
        if now - gate.last_ban_warn_ts >= 60.0:
            gate.last_ban_warn_ts = now
            log.warning(
                "telegram ban drop (summary) chat=%s dropped=%s "
                "last_alert_key=%s ban_remaining=%.0fs",
                chat_id,
                gate.ban_dropped_count,
                alert_key,
                remaining,
            )

    def _maybe_log_ban_clear(self, gate: _ChatGate, chat_id: str) -> None:
        if not gate.ban_was_active:
            return
        if gate.banned_remaining() > 0:
            return
        log.info(
            "telegram ban ended chat=%s — resume under ~1 msg/s and ~%s/min "
            "(dropped_during_ban=%s)",
            chat_id,
            TG_RATE_PER_MIN,
            gate.ban_dropped_count,
        )
        gate.ban_was_active = False
        gate.ban_dropped_count = 0
        gate.ban_first_alert_key = ""
        gate.ban_retry_after = 0.0
        gate.last_ban_warn_ts = 0.0

    def send_alert(self, alert: dict[str, Any], *, cycle_id: str = "-") -> bool:
        if not self.enabled:
            return False
        atype = str(alert.get("type") or "")
        chat_id = resolve_chat_id(atype)
        if not chat_id:
            log.error(
                "telegram skip: no chat_id for type=%s "
                "(set TELEGRAM_CHAT_ID_* or TELEGRAM_CHAT_ID)",
                alert.get("type"),
            )
            return False
        text = format_alert_html(alert, cycle_id=cycle_id)
        alert_key = (
            f"{atype}|{alert.get('chain_id')}|{alert.get('token_address')}|"
            f"{alert.get('pair_address')}|{cycle_id}"
        )
        merge_key = (
            f"{atype}|{alert.get('chain_id')}|{alert.get('pair_address') or alert.get('token_address')}"
        )
        pri = alert_priority(atype)

        gate = self._gate(chat_id)
        self._maybe_log_ban_clear(gate, chat_id)
        banned = gate.banned_remaining()
        if banned > 0:
            # While Telegram ban is active, do not hammer — drop low, queue/skip high briefly
            if pri >= 3:
                self._stats["dropped_ban"] += 1
                self._note_ban_drop(
                    gate,
                    chat_id,
                    alert_key,
                    remaining=banned,
                    policy="drop_low_pri_during_ban",
                )
                return None  # expected skip — no outer WARN
            # high priority: still try via queue (may wait), but if ban >> sleep cap, drop
            if banned > TG_429_SLEEP_CAP_SEC * 2:
                self._stats["dropped_ban"] += 1
                self._note_ban_drop(
                    gate,
                    chat_id,
                    alert_key,
                    remaining=banned,
                    policy="drop_high_pri_long_ban",
                )
                return None  # expected skip — no outer WARN

        # Enqueue then drain one (keeps rate limits without unbounded backlog)
        self._enqueue(chat_id, text, alert_key, atype, merge_key, pri)
        return self._drain_one(preferred_chat=chat_id)

    def _enqueue(
        self,
        chat_id: str,
        text: str,
        alert_key: str,
        alert_type: str,
        merge_key: str,
        priority: int,
    ) -> None:
        with self._queue_lock:
            # merge low-priority same merge_key already queued
            if priority >= 3:
                for item in self._queue:
                    if item.merge_key == merge_key and item.priority >= 3:
                        item.merged_count += 1
                        self._merged_low += 1
                        return
            while len(self._queue) >= TG_QUEUE_MAX:
                if not TG_QUEUE_DROP_LOW:
                    # drop newest low if must
                    break
                # drop lowest priority (highest priority number) from queue
                worst_i = max(range(len(self._queue)), key=lambda i: (self._queue[i].priority, -self._queue[i].seq))
                if self._queue[worst_i].priority <= priority and priority < 3:
                    # queue full of equal/higher — drop incoming low only
                    if priority >= 3:
                        self._stats["dropped_queue"] += 1
                        self._dropped_low += 1
                        log.warning(
                            "telegram queue full — drop low type=%s merged_pending=%s",
                            alert_type,
                            self._dropped_low,
                        )
                        return
                dropped = self._queue.pop(worst_i)
                self._stats["dropped_queue"] += 1
                self._dropped_low += 1
                log.warning(
                    "telegram queue full — drop type=%s pri=%s (queue_max=%s)",
                    dropped.alert_type,
                    dropped.priority,
                    TG_QUEUE_MAX,
                )
                # rebuild heap order
                heap = list(self._queue)
                self._queue.clear()
                for it in heap:
                    heappush(self._queue, it)
            self._seq += 1
            heappush(
                self._queue,
                _QueueItem(
                    priority=priority,
                    seq=self._seq,
                    chat_id=chat_id,
                    text=text,
                    alert_key=alert_key,
                    alert_type=alert_type,
                    merge_key=merge_key,
                ),
            )

    def _drain_one(self, *, preferred_chat: str | None = None) -> bool:
        with self._queue_lock:
            if not self._queue:
                return False
            item = heappop(self._queue)
        if item.merged_count > 1:
            # annotate merged
            item.text = (
                item.text
                + f"\n<i>(+{item.merged_count - 1} similar suppressed in queue)</i>"
            )
        return self._send_with_retries(
            item.chat_id,
            item.text,
            alert_key=item.alert_key,
            alert_type=item.alert_type,
        )

    def send_message(self, chat_id: str, text: str, *, parse_mode: str = "HTML") -> bool:
        return self._send_with_retries(
            chat_id, text, alert_key=f"raw|{chat_id}", alert_type="raw", parse_mode=parse_mode
        )

    def _send_with_retries(
        self,
        chat_id: str,
        text: str,
        *,
        alert_key: str,
        alert_type: str,
        parse_mode: str = "HTML",
    ) -> bool:
        if not self.token:
            return False
        gate = self._gate(chat_id)
        max_attempts = max(1, TG_MAX_RETRIES)

        for attempt in range(1, max_attempts + 1):
            banned = gate.banned_remaining()
            if banned > TG_429_SLEEP_CAP_SEC:
                self._stats["dropped_ban"] += 1
                self._note_ban_drop(
                    gate,
                    chat_id,
                    alert_key,
                    remaining=banned,
                    policy="abort_send_long_ban",
                )
                return None  # expected skip — no outer WARN
            if banned > 0:
                self._sleep(min(banned, TG_429_SLEEP_CAP_SEC))
                # Honored short ban wait — clear so this attempt can proceed
                # (monotonic advances in prod; tests stub sleep).
                with gate.lock:
                    gate.ban_until = min(gate.ban_until, time.monotonic())

            slot_wait = max(0.5, min(5.0, max(TG_RATE_MIN_INTERVAL_SEC * 3, 0.5)))
            if not gate.wait_for_slot(max_wait=slot_wait):
                # rate limited — requeue soft drop for low pri
                if alert_priority(alert_type) >= 3:
                    self._stats["dropped_queue"] += 1
                    self._log_fail(
                        alert_key,
                        "telegram rate-limit skip type=%s chat=%s",
                        alert_type,
                        chat_id,
                    )
                    return False
                # brief wait for high pri
                self._sleep(TG_RATE_MIN_INTERVAL_SEC)

            ok, retry_after, status = self._post_once(chat_id, text, parse_mode=parse_mode)
            if ok:
                gate.record_send()
                self._stats["sent"] += 1
                self._fail_counts.pop(alert_key, None)
                self._maybe_log_ban_clear(gate, chat_id)
                return True

            if status == 429:
                ra = retry_after if retry_after is not None else 1.0
                gate.set_ban(ra)
                self._stats["retries"] += 1
                # Long flood-wait (hours): do NOT sleep/retry — set ban and drop now.
                # Short retry_after: sleep capped, retry up to TG_MAX_RETRIES.
                if ra > TG_429_SLEEP_CAP_SEC:
                    self._stats["dropped_429"] += 1
                    self._note_ban_drop(
                        gate,
                        chat_id,
                        alert_key,
                        remaining=ra,
                        policy="429_long_ban_no_retry",
                    )
                    return None  # expected skip after setting ban
                sleep_for = min(ra, TG_429_SLEEP_CAP_SEC)
                self._log_fail(
                    alert_key,
                    "telegram 429 type=%s attempt=%s/%s retry_after=%.0fs "
                    "sleep=%.1fs",
                    alert_type,
                    attempt,
                    max_attempts,
                    ra,
                    sleep_for,
                )
                if attempt >= max_attempts:
                    self._stats["dropped_429"] += 1
                    log.error(
                        "telegram short-retry exhausted after %s x 429 "
                        "type=%s chat=%s retry_after=%.0fs alert_key=%s",
                        max_attempts,
                        alert_type,
                        chat_id,
                        ra,
                        alert_key,
                    )
                    return False
                self._sleep(sleep_for)
                continue

            # non-429 failure
            self._log_fail(
                alert_key,
                "telegram send failed type=%s status=%s attempt=%s/%s",
                alert_type,
                status,
                attempt,
                max_attempts,
            )
            if attempt >= max_attempts:
                return False
            self._sleep(min(2.0 * attempt, TG_429_SLEEP_CAP_SEC))

        return False

    def _post_once(
        self, chat_id: str, text: str, *, parse_mode: str = "HTML"
    ) -> tuple[bool, float | None, int | None]:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            client_kwargs: dict[str, Any] = {"timeout": self.timeout}
            if self._transport is not None:
                client_kwargs["transport"] = self._transport
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                )
            if resp.status_code == 429:
                return False, parse_retry_after(resp), 429
            if resp.status_code >= 300:
                log.warning(
                    "telegram sendMessage failed status=%s body=%s",
                    resp.status_code,
                    resp.text[:300],
                )
                return False, None, resp.status_code
            data = resp.json()
            if not data.get("ok"):
                # Telegram sometimes returns 200 with ok=false + retry_after
                ra = parse_retry_after(data)
                err_code = data.get("error_code")
                if err_code == 429 or ra is not None:
                    return False, ra or 1.0, 429
                log.warning("telegram API not ok: %s", str(data)[:300])
                return False, None, err_code if isinstance(err_code, int) else 400
            return True, None, 200
        except Exception as exc:  # noqa: BLE001
            self._log_fail("transport", "telegram sendMessage error: %s", exc)
            return False, None, None

    def _notify_admin_send_failure(self, alert: dict[str, Any], *, cycle_id: str) -> None:
        if self._admin_fail_notified:
            return
        admin = TELEGRAM_CHAT_ID_ADMIN or TELEGRAM_CHAT_ID
        if not admin or not self.token:
            return
        # Don't notify admin if that chat is banned — would worsen 429
        if self._gate(admin).banned_remaining() > 0:
            return
        self._admin_fail_notified = True
        msg = (
            f"<b>🛠 Telegram delivery failure</b>\n"
            f"Failed to deliver type=<code>{escape(str(alert.get('type')))}</code>\n"
            f"cycle=<code>{escape(cycle_id)}</code>\n"
            f"(further Telegram errors suppressed this process)"
        )
        try:
            self.send_message(admin, msg)
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict[str, Any]:
        bans = {
            cid: round(g.banned_remaining(), 1)
            for cid, g in self._gates.items()
            if g.banned_remaining() > 0
        }
        return {
            **self._stats,
            "queue_len": len(self._queue),
            "dropped_low_total": self._dropped_low,
            "merged_low_total": self._merged_low,
            "bans": bans,
        }


_default_alerter: TelegramAlerter | None = None


def get_telegram_alerter() -> TelegramAlerter:
    global _default_alerter
    if _default_alerter is None:
        _default_alerter = TelegramAlerter()
    return _default_alerter


def reset_telegram_alerter_for_tests() -> None:
    """Clear singleton (demos / unit tests)."""
    global _default_alerter
    _default_alerter = None
