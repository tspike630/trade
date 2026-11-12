"""
News sidechannel — low-cost RSS observe-only pipeline.

Strictly parallel to JEV / decision / paper / live:
  - writes ONLY to independent news_events.db (never dexscreener.db)
  - never touches decision_log, jev_signals, paper/live ledgers
  - ingest still writes onchain_status=pending; the read-only verifier
    does not relabel a row until a calibrated config exists

CLI:
  python -m sources.news --once
  python -m sources.news --once --db /path/to/news_events.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional
import html
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from logging_setup import get_logger, setup_logging

try:
    from config import USER_AGENT
except ImportError:  # pragma: no cover
    USER_AGENT = "dexscreener-crawler/news-sidechannel"

log = get_logger("sources.news")


def repo_root() -> Path:
    """Repository root for both ``sources/news.py`` and ``crawler/sources/news.py``."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "decision_engine").is_dir() and (candidate / "AGENTS.md").is_file():
            return candidate
    return here.parents[1]


def default_db_path() -> str:
    """``NEWS_EVENTS_DB_PATH`` or ``<repo>/news_events.db``. Never the crawler package dir."""
    env = os.environ.get("NEWS_EVENTS_DB_PATH", "").strip()
    if env:
        return env
    return str(repo_root() / "news_events.db")


_ROOT = repo_root()
DEFAULT_DB_PATH = default_db_path()

# ---------------------------------------------------------------------------
# Source tiers (hardcoded table; override via --sources / env later)
# ---------------------------------------------------------------------------

TIER_WHITELIST = "whitelist"
TIER_RUMOR = "rumor"
ONCHAIN_STATUSES = ("pending", "aligned", "divergent", "none")

# Public RSS only. DefiLlama main site has no RSS; DL News (DefiLlama News) does.
SOURCE_CATALOG: list[dict[str, Any]] = [
    {
        "id": "coindesk",
        "name": "CoinDesk",
        "tier": TIER_WHITELIST,
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "enabled": True,
    },
    {
        "id": "theblock",
        "name": "The Block",
        "tier": TIER_WHITELIST,
        "url": "https://www.theblock.co/rss.xml",
        "enabled": True,
    },
    {
        "id": "cointelegraph",
        "name": "Cointelegraph",
        "tier": TIER_WHITELIST,
        "url": "https://cointelegraph.com/rss",
        "enabled": True,
    },
    {
        "id": "dlnews",
        "name": "DL News (DefiLlama News)",
        "tier": TIER_WHITELIST,
        "url": "https://www.dlnews.com/arc/outboundfeeds/rss/",
        "enabled": True,
    },
    {
        "id": "decrypt",
        "name": "Decrypt",
        "tier": TIER_WHITELIST,
        "url": "https://decrypt.co/feed",
        "enabled": True,
    },
    {
        "id": "fed_monetary",
        "name": "Federal Reserve Monetary Policy",
        "tier": TIER_WHITELIST,
        "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "enabled": True,
    },
    {
        "id": "fed_speeches",
        "name": "Federal Reserve Speeches",
        "tier": TIER_WHITELIST,
        "url": "https://www.federalreserve.gov/feeds/speeches.xml",
        "enabled": True,
    },
    {
        "id": "tg_binance_announcements",
        "name": "Binance Announcements (Public Telegram)",
        "tier": TIER_WHITELIST,
        "kind": "telegram_web",
        "url": "https://t.me/s/binance_announcements",
        "enabled": True,
    },
    {
        "id": "tg_bwenews",
        "name": "BWEnews Wire (Public Telegram)",
        "tier": TIER_WHITELIST,
        "kind": "telegram_web",
        "url": "https://t.me/s/BWEnews",
        "enabled": True,
    },
    # Rumor / lower-confidence — observe only; never promote without on-chain align
    {
        "id": "newsbtc",
        "name": "NewsBTC",
        "tier": TIER_RUMOR,
        "url": "https://www.newsbtc.com/feed/",
        "enabled": True,
    },
]

REQUEST_TIMEOUT_SEC = 20.0
MAX_ITEMS_PER_FEED = 40


NEWS_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_tier TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    summary TEXT,
    raw_json TEXT,
    onchain_status TEXT NOT NULL DEFAULT 'pending',
    notes TEXT,
    UNIQUE(url)
);
CREATE INDEX IF NOT EXISTS idx_news_events_published
    ON news_events (published_at);
CREATE INDEX IF NOT EXISTS idx_news_events_source
    ON news_events (source);
CREATE INDEX IF NOT EXISTS idx_news_events_tier
    ON news_events (source_tier);
CREATE INDEX IF NOT EXISTS idx_news_events_status
    ON news_events (onchain_status);
CREATE INDEX IF NOT EXISTS idx_news_events_fetched
    ON news_events (fetched_at);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "spm", "ref_src", "ref_url"})
_TG_POST_RE = re.compile(r'<div[^>]+data-post="([^"]+)"[^>]*>', re.IGNORECASE)
_TG_TEXT_RE = re.compile(r'<div[^>]+class="[^"]*\btgme_widget_message_text\b[^"]*"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)
_TG_TIME_RE = re.compile(r'<time[^>]+datetime="([^"]+)"', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def normalize_url(url: str) -> str:
    """Strip fragments, trailing slashes, and UTM/marketing query noise for idempotent URL dedup."""
    if not url:
        return ""
    u = url.strip()
    try:
        p = urlparse(u)
        path = p.path.rstrip("/") or "/"
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if not (k.lower().startswith("utm_") or k.lower() in _TRACKING_PARAMS)]
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(kept, doseq=True), ""))
    except Exception:  # noqa: BLE001
        return u.rstrip("/")


def parse_telegram_web_html(content: bytes, *, source_id: str, tier: str) -> list["NewsItem"]:
    """Parse public Telegram preview HTML (https://t.me/s/<channel>) preserving exact UTC timestamps."""
    parts = _TG_POST_RE.split(content.decode("utf-8", errors="replace"))
    if len(parts) < 3:
        return []
    out: list[NewsItem] = []
    for slug, block in reversed(list(zip(parts[1::2], parts[2::2]))[-MAX_ITEMS_PER_FEED:]):
        slug = slug.strip().strip("/")
        m_txt = _TG_TEXT_RE.search(block)
        if not slug or "/" not in slug or m_txt is None:
            continue
        plain = re.sub(r"\s+", " ", html.unescape(_HTML_TAG_RE.sub(" ", re.sub(r"<br\s*/?>", " ", m_txt.group(1), flags=re.I)))).strip()
        if not plain:
            continue
        m_time = _TG_TIME_RE.search(block)
        published = parse_feed_datetime(m_time.group(1)) if m_time else None
        link = normalize_url(f"https://t.me/{slug}")
        out.append(NewsItem(
            source=source_id, source_tier=tier, title=plain[:180], url=link,
            published_at=published, summary=plain[:2000],
            raw={"post": slug, "link": link, "published": published},
        ))
    return out


def parse_feed_datetime(value: str | None) -> str | None:
    """Best-effort → UTC ISO Z. Accepts RFC 2822 and ISO-8601."""
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    # ISO-ish
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass
    # RFC 2822 (RSS pubDate)
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, IndexError):
        return None


def _local(tag: str) -> str:
    """Strip XML namespace: {ns}tag → tag."""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(el: ET.Element, *names: str) -> str | None:
    want = set(names)
    for child in el:
        if _local(child.tag) in want:
            text = (child.text or "").strip()
            if text:
                return text
            # Atom: title may have nested markup; use itertext
            joined = "".join(child.itertext()).strip()
            if joined:
                return joined
    return None


def _child_link(el: ET.Element) -> str | None:
    # RSS <link>text</link>
    for child in el:
        if _local(child.tag) == "link":
            href = (child.get("href") or "").strip()
            if href:
                return href
            text = (child.text or "").strip()
            if text:
                return text
    # Atom alternate
    for child in el:
        if _local(child.tag) == "link":
            rel = (child.get("rel") or "alternate").lower()
            href = (child.get("href") or "").strip()
            if href and rel in ("alternate", ""):
                return href
    return None


@dataclass
class NewsItem:
    source: str
    source_tier: str
    title: str
    url: str
    published_at: str | None
    summary: str | None
    raw: dict[str, Any]


def parse_rss_xml(content: bytes, *, source_id: str, tier: str) -> list[NewsItem]:
    """Parse RSS 2.0 or Atom with stdlib ElementTree (no feedparser)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        log.warning("xml parse failed source=%s: %s", source_id, exc)
        return []

    root_local = _local(root.tag).lower()
    items: list[ET.Element] = []

    if root_local == "rss" or root_local == "rdf":
        for el in root.iter():
            if _local(el.tag).lower() == "item":
                items.append(el)
    elif root_local == "feed":
        for el in list(root):
            if _local(el.tag).lower() == "entry":
                items.append(el)
    else:
        # Unknown wrapper — collect any item/entry descendants
        for el in root.iter():
            if _local(el.tag).lower() in ("item", "entry"):
                items.append(el)

    out: list[NewsItem] = []
    for el in items[:MAX_ITEMS_PER_FEED]:
        title = _child_text(el, "title") or ""
        link = _child_link(el) or _child_text(el, "guid", "id") or ""
        link = normalize_url(link)
        if not title or not link:
            continue
        published = parse_feed_datetime(
            _child_text(el, "pubDate", "published", "updated", "date")
        )
        summary = _child_text(el, "description", "summary", "content", "encoded")
        if summary and len(summary) > 2000:
            summary = summary[:2000] + "…"
        raw = {
            "title": title,
            "link": link,
            "published": published,
            "summary_len": len(summary or ""),
        }
        out.append(
            NewsItem(
                source=source_id,
                source_tier=tier,
                title=title[:500],
                url=link,
                published_at=published,
                summary=summary,
                raw=raw,
            )
        )
    return out


class NewsStore:
    """Independent SQLite for news_events — zero intrusion into main crawler DB."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.executescript(NEWS_EVENTS_DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NewsStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def upsert_item(self, item: NewsItem, *, fetched_at: str) -> bool:
        """
        Insert if URL unseen. Returns True if a new row was written.
        Idempotent on url (UNIQUE).
        """
        try:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO news_events (
                    source, source_tier, title, url,
                    published_at, fetched_at, summary, raw_json,
                    onchain_status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL)
                """,
                (
                    item.source,
                    item.source_tier,
                    item.title,
                    item.url,
                    item.published_at,
                    fetched_at,
                    item.summary,
                    json.dumps(item.raw, ensure_ascii=False),
                ),
            )
            self._conn.commit()
            return cur.rowcount > 0
        except sqlite3.Error as exc:
            log.warning("upsert failed url=%s: %s", item.url[:80], exc)
            self._conn.rollback()
            return False

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM news_events").fetchone()
        return int(row["n"]) if row else 0

    def recent(self, limit: int = 5) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT id, source, source_tier, title, url, published_at, onchain_status
                FROM news_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def enabled_sources(filter_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    want = {s.strip().lower() for s in (filter_ids or []) if s and s.strip()}
    out: list[dict[str, Any]] = []
    for src in SOURCE_CATALOG:
        if not src.get("enabled", True):
            continue
        if want and src["id"] not in want:
            continue
        out.append(src)
    return out


def fetch_feed(
    client: httpx.Client,
    src: dict[str, Any],
) -> tuple[list[NewsItem], Optional[str]]:
    """
    Fetch + parse one feed. On network/HTTP/parse failure return ([], error).
    Never raises — graceful degrade per source.
    """
    url = src["url"]
    sid = src["id"]
    try:
        resp = client.get(url)
        if resp.status_code != 200:
            return [], f"http_{resp.status_code}"
        if src.get("kind") == "telegram_web":
            items = parse_telegram_web_html(resp.content, source_id=sid, tier=src["tier"])
        else:
            items = parse_rss_xml(resp.content, source_id=sid, tier=src["tier"])
        if not items:
            return [], "empty_or_unparsed"
        return items, None
    except httpx.TimeoutException:
        return [], "timeout"
    except httpx.HTTPError as exc:
        return [], f"http_error:{type(exc).__name__}"
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected fetch error source=%s", sid)
        return [], f"error:{type(exc).__name__}:{exc}"


def run_once(
    *,
    db_path: str | None = None,
    source_ids: Optional[list[str]] = None,
    timeout_sec: float = REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    """
    Pull all enabled feeds once; upsert into news_events.
    Returns summary stats. Never writes trading signals.
    """
    if not db_path:
        db_path = default_db_path()
    sources = enabled_sources(source_ids)
    fetched_at = utc_now_iso()
    stats: dict[str, Any] = {
        "fetched_at": fetched_at,
        "sources_ok": 0,
        "sources_fail": 0,
        "items_seen": 0,
        "items_written": 0,
        "items_dup": 0,
        "failures": {},
        "db_path": str(Path(db_path).resolve()),
        "db_total": 0,
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }

    with NewsStore(db_path) as store, httpx.Client(
        timeout=timeout_sec,
        follow_redirects=True,
        headers=headers,
    ) as client:
        for src in sources:
            sid = src["id"]
            items, err = fetch_feed(client, src)
            if err:
                stats["sources_fail"] += 1
                stats["failures"][sid] = err
                log.warning("news feed degrade source=%s err=%s", sid, err)
                continue
            stats["sources_ok"] += 1
            for item in items:
                stats["items_seen"] += 1
                if store.upsert_item(item, fetched_at=fetched_at):
                    stats["items_written"] += 1
                else:
                    stats["items_dup"] += 1
            # polite gap between hosts
            time.sleep(0.15)
        stats["db_total"] = store.count()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "News sidechannel RSS pull (observe-only → news_events.db). "
            "Does not touch decision/JEV/paper/live."
        )
    )
    parser.add_argument("--once", action="store_true", help="Single pull of all enabled feeds")
    parser.add_argument("--loop", action="store_true", help="Poll continuously every --interval seconds")
    parser.add_argument("--interval", type=float, default=60.0, help="Loop interval seconds (default 60, min 5)")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default NEWS_EVENTS_DB_PATH or <repo>/news_events.db)",
    )
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated source ids (default: all enabled in SOURCE_CATALOG)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=REQUEST_TIMEOUT_SEC,
        help=f"Per-feed HTTP timeout seconds (default {REQUEST_TIMEOUT_SEC})",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print SOURCE_CATALOG and exit",
    )
    args = parser.parse_args(argv)

    setup_logging()

    if args.list_sources:
        for src in SOURCE_CATALOG:
            print(
                f"{src['id']:16s} tier={src['tier']:10s} "
                f"enabled={src.get('enabled', True)!s:5s} {src['url']}"
            )
        return 0

    if not args.once and not args.loop:
        parser.error("specify --once or --loop")

    source_ids = None
    if args.sources:
        source_ids = [p.strip() for p in args.sources.split(",") if p.strip()]

    if args.loop:
        iv = max(5.0, float(args.interval))
        while True:
            try:
                st = run_once(db_path=args.db, source_ids=source_ids, timeout_sec=args.timeout)
                print(f"news --loop written={st['items_written']} seen={st['items_seen']} dup={st['items_dup']} ok={st['sources_ok']} fail={st['sources_fail']} total={st['db_total']}", flush=True)
            except Exception as exc:
                log.exception("news loop error: %s", exc)
            time.sleep(iv)
    stats = run_once(db_path=args.db, source_ids=source_ids, timeout_sec=args.timeout)
    # Acceptance: print write count clearly
    print(
        "news --once "
        f"written={stats['items_written']} "
        f"seen={stats['items_seen']} "
        f"dup={stats['items_dup']} "
        f"ok_sources={stats['sources_ok']} "
        f"fail_sources={stats['sources_fail']} "
        f"db_total={stats['db_total']} "
        f"db={stats['db_path']}"
    )
    if stats["failures"]:
        print(f"failures={json.dumps(stats['failures'], ensure_ascii=False)}")
    log.info("news --once %s", stats)
    # Exit 0 if at least one source ok OR nothing to do; 1 only if all failed
    if stats["sources_ok"] == 0 and stats["sources_fail"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
