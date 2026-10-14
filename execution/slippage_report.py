"""Read-only I-3 slippage report. Does not place or skip orders.

    python -m execution.slippage_report
    python -m execution.slippage_report --ledger execution_ledger.db --paper paper_trading.db --crawler-db dexscreener.db --json

``slippage_1h`` aggregates the recorded ``slippage`` (else a cost derived from
``mid_price`` and ``fill_price``) over the last hour, and the same stats in
UTC day buckets. Sources, in order:

- ``execution_log.receipt_json`` paper fills (``exec_result`` / its ``meta``)
- paper ``fills`` joined to ``positions.meta_json`` for buys, which already
  store ``mid_price`` and ``slippage``. A sell fill is used only when that
  fill's own note JSON carries the fields. Entry meta is not reused as the
  exit slip. Tick closes that never stored mid/slippage are skipped.

``bot_trade_share`` prefers collection #81 ``bot_fill_observations`` in the
crawler DB (``--crawler-db``, else ``CRAWLER_DB`` / ``DB_PATH``, else
``dexscreener.db``). The share matches ``Store.bot_share_inputs``: parsed
fills in the 1h ``recorded_at`` window are the denominator, and rows whose
``bot_reason`` is non-empty are the numerator. A missing table or an empty
window is a data gap (share is null), not a human label and not zero. Receipt
tags (``bot_tag`` / ``is_bot`` / ``bot_label`` / ``suspicious_bot`` / ``bot``)
stay a secondary fallback and do not replace that headline. This module does
not import the crawler, does not classify millisecond repeats, intervals, or
gas auctions, and does not call PositionManager or the gateway.
``high_bot_skip.wired`` stays false.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from execution.ledger import resolve_ledger_path  # noqa: E402

BOT_TAG_KEYS = ("bot_tag", "is_bot", "bot_label", "suspicious_bot", "bot")
_BOT_TRUE = {"1", "true", "yes", "y", "bot"}
_BOT_FALSE = {"0", "false", "no", "n", "human"}
_LABEL_KEYS = {"bot_tag", "bot_label", "suspicious_bot"}

BOT_GAP_NOTE = (
    "主源是采集 #81 bot_fill_observations："
    "bot_reason 非空为分子，1h 窗内已解析成交为分母（与 bot_share_inputs 同义）。"
    "表缺失或空表 = data gap，份额为 null，不当成人类，也不写成 0。"
    "份额 0 只表示窗内没有 bot_reason，不是人类标签，也不是跳单。"
    "receipt 的 bot_tag / is_bot / bot_label / suspicious_bot / bot 只作次要 fallback。"
    "不实现毫秒级 bot 模式识别（属采集）。"
    "高 bot 占比先记录，对拍后再 R2/R3，不自动跳单，不接 PM/gateway。"
)
# Same numerator as crawler Store.bot_share_inputs. The report adds a closed
# recorded_at window and rolls the predicate up globally and per token.
_BOT_REASON_HITS = (
    "SUM(CASE WHEN bot_reason IS NOT NULL AND bot_reason != '' THEN 1 ELSE 0 END)"
)
HIGH_BOT_SKIP_NOTE = "先记录，对拍后再 R2/R3。不接 PM/gateway，不自动跳单。"

_LEDGER_WANTED = (
    "ts",
    "decision_log_id",
    "action",
    "executed",
    "receipt_json",
    *BOT_TAG_KEYS,
)
_FILL_WANTED = ("id", "position_id", "side", "price", "created_at", "note", *BOT_TAG_KEYS)
_POSITION_WANTED = ("id", "meta_json", *BOT_TAG_KEYS)


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > 1e12:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(str(value).strip(), fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "sell"}:
        return text
    return "unknown"


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def percentile(values: list[float], pct: float) -> float | None:
    """Linear percentile on the inclusive range. Empty input is None."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1 or pct <= 0:
        return xs[0]
    if pct >= 100:
        return xs[-1]
    rank = (len(xs) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p90": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": sum(values) / len(values),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "min": min(values),
        "max": max(values),
    }


def classify_bot_value(key: str, value: Any) -> str | None:
    """``bot`` / ``not_bot`` when a tag is present. None when the field is absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bot" if value else "not_bot"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return "bot"
        if value == 0:
            return "not_bot"
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in _BOT_TRUE:
            return "bot"
        if text in _BOT_FALSE:
            return "not_bot"
        if key in _LABEL_KEYS:
            return "bot"
    return None


def read_bot_tag(*bags: Any) -> str | None:
    """First present reserved tag. Does not infer a bot from timing or size."""
    for key in BOT_TAG_KEYS:
        for bag in bags:
            if not isinstance(bag, dict) or key not in bag:
                continue
            mark = classify_bot_value(key, bag.get(key))
            if mark is not None:
                return mark
    return None


def _cost(side: str, mid: float | None, fill: float | None, stored: float | None) -> float | None:
    if stored is not None:
        return stored
    if mid is None or fill is None or mid <= 0:
        return None
    if side == "sell":
        return (mid - fill) / mid
    return (fill - mid) / mid


def _slippage_from_objects(
    objects: list[dict[str, Any]],
    *,
    side: str,
    fill_fallback: float | None = None,
) -> dict[str, Any] | None:
    for obj in objects:
        stored = _num(obj.get("slippage"))
        mid = _num(obj.get("mid_price"))
        fill = _num(obj.get("fill_price"))
        if fill is None:
            fill = fill_fallback
        if stored is None and (mid is None or fill is None):
            continue
        slip = _cost(side if side != "unknown" else _side(obj.get("side")), mid, fill, stored)
        if slip is None:
            continue
        resolved = side if side != "unknown" else _side(obj.get("side"))
        return {
            "slippage": slip,
            "mid_price": mid,
            "fill_price": fill,
            "side": resolved,
        }
    return None


def _filled_receipt(row: sqlite3.Row, payload: dict[str, Any] | None, exec_result: dict[str, Any] | None) -> bool:
    """``executed`` is authoritative when the column is present.

    A receipt body can still say ``ok`` on a row the ledger marked unfilled.
    Older extracts that omit the column fall back to the receipt status.
    """
    keys = row.keys()
    if "executed" in keys and row["executed"] is not None:
        return row["executed"] in (1, "1", True)
    if isinstance(payload, dict) and payload.get("status") == "paper_executed":
        return True
    return bool(isinstance(exec_result, dict) and exec_result.get("ok") is True)


def _observation(
    *,
    ts: datetime,
    side: str,
    slippage: float,
    mid_price: float | None,
    fill_price: float | None,
    source: str,
    decision_log_id: str | None,
    bot: str | None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "side": side,
        "slippage": slippage,
        "mid_price": mid_price,
        "fill_price": fill_price,
        "source": source,
        "decision_log_id": decision_log_id,
        "bot": bot,
    }


def load_receipt_observations(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]] | None:
    """None when the file is absent. Counts are zero when the table is absent."""
    counts = {"rows": 0, "skipped_missing_fields": 0, "skipped_not_filled": 0}
    if not path.is_file():
        return None
    conn = _connect_ro(path)
    try:
        if "execution_log" not in _tables(conn):
            return [], counts
        have = _columns(conn, "execution_log")
        cols = [name for name in _LEDGER_WANTED if name in have]
        if "ts" not in cols or "receipt_json" not in cols:
            return [], counts
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM execution_log ORDER BY rowid ASC"
        ).fetchall()
    finally:
        conn.close()
    counts["rows"] = len(rows)
    found: list[dict[str, Any]] = []
    for row in rows:
        payload = _as_dict(row["receipt_json"])
        exec_result = payload.get("exec_result") if isinstance(payload, dict) else None
        if not isinstance(exec_result, dict):
            exec_result = None
        objects: list[dict[str, Any]] = []
        if exec_result is not None:
            objects.append(exec_result)
            meta = _as_dict(exec_result.get("meta"))
            if meta is not None:
                objects.append(meta)
        if isinstance(payload, dict):
            objects.append(payload)
            top_meta = _as_dict(payload.get("meta"))
            if top_meta is not None:
                objects.append(top_meta)
        side = _side(row["action"] if "action" in row.keys() else None)
        if side == "unknown" and exec_result is not None:
            side = _side(exec_result.get("side"))
        parsed = _slippage_from_objects(objects, side=side)
        filled = _filled_receipt(row, payload, exec_result)
        if parsed is None:
            if filled:
                counts["skipped_missing_fields"] += 1
            continue
        if not filled:
            counts["skipped_not_filled"] += 1
            continue
        ts = _parse_ts(row["ts"])
        if ts is None:
            counts["skipped_missing_fields"] += 1
            continue
        decision = None
        if "decision_log_id" in row.keys() and row["decision_log_id"] is not None:
            decision = str(row["decision_log_id"]).strip() or None
        column_bag = {key: row[key] for key in BOT_TAG_KEYS if key in row.keys()}
        bags: list[Any] = [column_bag, *objects]
        found.append(
            _observation(
                ts=ts,
                side=str(parsed["side"]),
                slippage=float(parsed["slippage"]),
                mid_price=parsed["mid_price"],
                fill_price=parsed["fill_price"],
                source="receipt",
                decision_log_id=decision,
                bot=read_bot_tag(*bags),
            )
        )
    return found, counts


def load_paper_observations(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]] | None:
    """None when the file is absent.

    Buy fills use ``positions.meta_json`` mid/slippage, which PaperExecutor
    already writes. Sell fills do not inherit that entry slip.
    """
    counts = {"rows": 0, "skipped_missing_fields": 0, "skipped_not_filled": 0}
    if not path.is_file():
        return None
    conn = _connect_ro(path)
    try:
        names = _tables(conn)
        if "fills" not in names:
            return [], counts
        fill_have = _columns(conn, "fills")
        fill_cols = [name for name in _FILL_WANTED if name in fill_have]
        if "created_at" not in fill_cols or "price" not in fill_cols:
            return [], counts
        fills = conn.execute(
            f"SELECT {', '.join(fill_cols)} FROM fills ORDER BY rowid ASC"
        ).fetchall()
        positions: dict[str, sqlite3.Row] = {}
        if "positions" in names:
            pos_have = _columns(conn, "positions")
            pos_cols = [name for name in _POSITION_WANTED if name in pos_have]
            if "id" in pos_cols:
                for pos in conn.execute(
                    f"SELECT {', '.join(pos_cols)} FROM positions"
                ).fetchall():
                    positions[str(pos["id"])] = pos
    finally:
        conn.close()
    counts["rows"] = len(fills)
    found: list[dict[str, Any]] = []
    for row in fills:
        side = _side(row["side"] if "side" in row.keys() else None)
        note = _as_dict(row["note"]) if "note" in row.keys() else None
        pos = None
        if "position_id" in row.keys() and row["position_id"] is not None:
            pos = positions.get(str(row["position_id"]))
        meta = _as_dict(pos["meta_json"]) if pos is not None and "meta_json" in pos.keys() else None
        objects: list[dict[str, Any]] = []
        if note is not None:
            objects.append(note)
        # Entry meta belongs to the buy. A sell note that is only an exit
        # reason ("stop_loss") does not carry mid/slippage and is skipped.
        if side == "buy" and meta is not None:
            objects.append(meta)
        fill_price = _num(row["price"])
        parsed = _slippage_from_objects(objects, side=side, fill_fallback=fill_price)
        if parsed is None:
            counts["skipped_missing_fields"] += 1
            continue
        ts = _parse_ts(row["created_at"])
        if ts is None:
            counts["skipped_missing_fields"] += 1
            continue
        decision = None
        if meta is not None and meta.get("decision_log_id") is not None:
            decision = str(meta.get("decision_log_id")).strip() or None
        column_bag = {key: row[key] for key in BOT_TAG_KEYS if key in row.keys()}
        pos_bag = {}
        if pos is not None:
            pos_bag = {key: pos[key] for key in BOT_TAG_KEYS if key in pos.keys()}
        found.append(
            _observation(
                ts=ts,
                side=str(parsed["side"]),
                slippage=float(parsed["slippage"]),
                mid_price=parsed["mid_price"],
                fill_price=parsed["fill_price"],
                source="paper_fill",
                decision_log_id=decision,
                bot=read_bot_tag(column_bag, note, pos_bag, meta),
            )
        )
    return found, counts


def dedupe(
    receipts: list[dict[str, Any]],
    paper: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Receipt wins when the same decision and side is also a paper fill."""
    claimed: set[tuple[str, str]] = set()
    for row in receipts:
        decision = row.get("decision_log_id")
        if decision:
            claimed.add((str(decision), str(row["side"])))
    kept = list(receipts)
    dropped = 0
    for row in paper:
        decision = row.get("decision_log_id")
        key = (str(decision), str(row["side"])) if decision else None
        if key is not None and key in claimed:
            dropped += 1
            continue
        kept.append(row)
    return kept, dropped


def _window(rows: list[dict[str, Any]], *, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if start <= row["ts"] <= end]


def _by_side(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        side: summarize([float(row["slippage"]) for row in rows if row["side"] == side])
        for side in ("buy", "sell")
    }


def bot_trade_share(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Receipt-tag fallback. Untagged rows are a gap, not 'not a bot'.

    Collection #81 is the headline when a crawler DB is passed. This result
    does not replace that share.
    """
    tagged = [row for row in rows if row.get("bot") in {"bot", "not_bot"}]
    bot_n = sum(1 for row in tagged if row["bot"] == "bot")
    not_bot_n = sum(1 for row in tagged if row["bot"] == "not_bot")
    untagged = len(rows) - len(tagged)
    if not rows:
        status = "no_samples"
        share = None
    elif not tagged:
        status = "data_gap"
        share = None
    else:
        status = "partial" if untagged else "observed"
        share = bot_n / len(tagged)
    return {
        "status": status,
        "share": share,
        "window": "1h",
        "source": "receipt_tag",
        "tagged_n": len(tagged),
        "bot_n": bot_n,
        "not_bot_n": not_bot_n,
        "untagged_n": untagged,
        "pattern_recognition": False,
        "note": BOT_GAP_NOTE,
    }


def _crawler_share_base(since_recorded_at: str, until_recorded_at: str) -> dict[str, Any]:
    return {
        "status": "data_gap",
        "share": None,
        "window": "1h",
        "source": "bot_fill_observations",
        "available": False,
        "total_fills": None,
        "bot_fills": None,
        "by_token": [],
        "since_recorded_at": since_recorded_at,
        "until_recorded_at": until_recorded_at,
        "tagged_n": 0,
        "bot_n": 0,
        "not_bot_n": 0,
        "untagged_n": 0,
        "pattern_recognition": False,
        "note": BOT_GAP_NOTE,
        "error": None,
        "gap": "no_table",
    }


def load_bot_fill_share(
    path: Path,
    *,
    since_recorded_at: str,
    until_recorded_at: str,
) -> dict[str, Any]:
    """Global and per-token share from ``bot_fill_observations``. Read-only.

    Does not import ``crawler.store``. A missing file, missing table, or empty
    window leaves ``share`` null. Rows with a null or empty ``bot_reason`` stay
    in the denominator.
    """
    base = _crawler_share_base(since_recorded_at, until_recorded_at)
    if not path.is_file():
        return {**base, "error": "missing crawler db"}
    try:
        conn = _connect_ro(path)
    except sqlite3.Error as exc:
        return {**base, "error": type(exc).__name__}
    try:
        if "bot_fill_observations" not in _tables(conn):
            return {**base, "error": "missing table bot_fill_observations"}
        have = set(_columns(conn, "bot_fill_observations"))
        needed = {"chain_id", "token_address", "recorded_at", "bot_reason"}
        missing = sorted(needed - have)
        if missing:
            return {**base, "error": "missing column " + ", ".join(missing)}
        params = (since_recorded_at, until_recorded_at)
        where = "WHERE recorded_at >= ? AND recorded_at <= ?"
        row = conn.execute(
            f"SELECT COUNT(*) AS total, {_BOT_REASON_HITS} AS bot "
            f"FROM bot_fill_observations {where}",
            params,
        ).fetchone()
        grouped = conn.execute(
            f"SELECT chain_id, token_address, COUNT(*) AS total, {_BOT_REASON_HITS} AS bot "
            f"FROM bot_fill_observations {where} "
            "GROUP BY chain_id, token_address "
            "ORDER BY chain_id, token_address",
            params,
        ).fetchall()
    except sqlite3.Error as exc:
        return {**base, "error": type(exc).__name__}
    finally:
        conn.close()
    total = int(row["total"] or 0)
    bot_fills = int(row["bot"] or 0)
    by_token: list[dict[str, Any]] = []
    for item in grouped:
        token_total = int(item["total"] or 0)
        token_bot = int(item["bot"] or 0)
        by_token.append(
            {
                "chain_id": item["chain_id"],
                "token_address": item["token_address"],
                "total_fills": token_total,
                "bot_fills": token_bot,
                "share": None if token_total == 0 else token_bot / token_total,
            }
        )
    if total == 0:
        return {
            **base,
            "available": True,
            "total_fills": 0,
            "bot_fills": 0,
            "share": None,
            "status": "data_gap",
            "gap": "empty",
            "by_token": [],
            "error": None,
        }
    return {
        **base,
        "available": True,
        "total_fills": total,
        "bot_fills": bot_fills,
        "share": bot_fills / total,
        "status": "observed",
        "gap": None,
        "by_token": by_token,
        "tagged_n": total,
        "bot_n": bot_fills,
        "not_bot_n": total - bot_fills,
        "untagged_n": 0,
        "error": None,
    }


def resolve_bot_trade_share(
    rows: list[dict[str, Any]],
    *,
    crawler_db: Path | None,
    since_recorded_at: str,
    until_recorded_at: str,
) -> dict[str, Any]:
    """Prefer #81 observations. Receipt tags are nested and do not set the share."""
    fallback = bot_trade_share(rows)
    if crawler_db is None:
        return fallback
    primary = load_bot_fill_share(
        crawler_db,
        since_recorded_at=since_recorded_at,
        until_recorded_at=until_recorded_at,
    )
    primary["receipt_fallback"] = {
        "status": fallback["status"],
        "share": fallback["share"],
        "tagged_n": fallback["tagged_n"],
        "bot_n": fallback["bot_n"],
        "not_bot_n": fallback["not_bot_n"],
        "untagged_n": fallback["untagged_n"],
        "source": "receipt_tag",
    }
    return primary


def _source_block(path: Path | None, present: bool | None, rows: int) -> dict[str, Any]:
    return {
        "path": None if path is None else str(path),
        "present": bool(present),
        "rows": rows,
    }


def build_report(
    *,
    ledger: Path | None,
    paper: Path | None,
    as_of: datetime | None = None,
    window_hours: float = 1.0,
    daily_days: int = 7,
    crawler_db: Path | None = None,
) -> dict[str, Any]:
    end = as_of or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    end = end.astimezone(timezone.utc)
    hour_start = end - timedelta(hours=float(window_hours))
    day_start = end - timedelta(days=int(daily_days))

    receipt_rows: list[dict[str, Any]] = []
    paper_rows: list[dict[str, Any]] = []
    skipped_missing = 0
    skipped_not_filled = 0
    ledger_present = False
    ledger_scanned = 0
    paper_present = False
    paper_scanned = 0
    if ledger is not None:
        loaded = load_receipt_observations(ledger)
        if loaded is None:
            ledger_present = False
        else:
            ledger_present = True
            receipt_rows, counts = loaded
            ledger_scanned = counts["rows"]
            skipped_missing += counts["skipped_missing_fields"]
            skipped_not_filled += counts["skipped_not_filled"]
    if paper is not None:
        loaded_paper = load_paper_observations(paper)
        if loaded_paper is None:
            paper_present = False
        else:
            paper_present = True
            paper_rows, counts = loaded_paper
            paper_scanned = counts["rows"]
            skipped_missing += counts["skipped_missing_fields"]
            skipped_not_filled += counts["skipped_not_filled"]

    pool, deduped = dedupe(receipt_rows, paper_rows)
    in_hour = _window(pool, start=hour_start, end=end)
    in_days = _window(pool, start=day_start, end=end)
    buckets: dict[str, list[float]] = {}
    for row in in_days:
        buckets.setdefault(row["ts"].strftime("%Y-%m-%d"), []).append(float(row["slippage"]))
    daily = []
    for day in sorted(buckets):
        stats = summarize(buckets[day])
        stats["date"] = day
        daily.append(stats)
    hour_stats = summarize([float(row["slippage"]) for row in in_hour])
    hour_stats["by_side"] = _by_side(in_hour)
    since_recorded_at = hour_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    until_recorded_at = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    bot = resolve_bot_trade_share(
        in_hour,
        crawler_db=crawler_db,
        since_recorded_at=since_recorded_at,
        until_recorded_at=until_recorded_at,
    )
    crawler_rows = bot.get("total_fills")
    return {
        "report": "slippage_1h",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window_hours": float(window_hours),
        "daily_days": int(daily_days),
        "rejects_orders": False,
        "high_bot_skip": {
            "wired": False,
            "pm_gateway": False,
            "note": HIGH_BOT_SKIP_NOTE,
        },
        "sources": {
            "ledger": _source_block(ledger, ledger_present, ledger_scanned),
            "paper": _source_block(paper, paper_present, paper_scanned),
            "crawler": _source_block(
                crawler_db,
                crawler_db is not None and crawler_db.is_file(),
                crawler_rows if isinstance(crawler_rows, int) else 0,
            ),
        },
        "samples": {
            "used_1h": len(in_hour),
            "used_daily": len(in_days),
            "from_receipt": sum(1 for row in pool if row["source"] == "receipt"),
            "from_paper_fill": sum(1 for row in pool if row["source"] == "paper_fill"),
            "deduped": deduped,
            "skipped_missing_fields": skipped_missing,
            "skipped_not_filled": skipped_not_filled,
        },
        "slippage_1h": hour_stats,
        "daily": daily,
        "bot_trade_share": bot,
    }


def _fmt(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}"


def _fmt_stats(prefix: str, stats: dict[str, Any]) -> str:
    return (
        f"{prefix}: n={stats['n']} mean={_fmt(stats['mean'])} "
        f"p50={_fmt(stats['p50'])} p90={_fmt(stats['p90'])} "
        f"min={_fmt(stats['min'])} max={_fmt(stats['max'])}"
    )


def format_report(report: dict[str, Any]) -> str:
    lines = [
        "slippage_1h (read-only; does not place or skip orders)",
        f"as_of={report['as_of']} window_hours={report['window_hours']} "
        f"daily_days={report['daily_days']} rejects_orders={report['rejects_orders']}",
    ]
    for name in ("ledger", "paper", "crawler"):
        src = report["sources"].get(name)
        if not isinstance(src, dict):
            continue
        lines.append(f"{name}: present={src['present']} rows={src['rows']} path={src['path']}")
    lines.append(_fmt_stats("1h", report["slippage_1h"]))
    for side, stats in report["slippage_1h"]["by_side"].items():
        lines.append(_fmt_stats(f"  {side}", stats))
    lines.append("daily:")
    if not report["daily"]:
        lines.append("  (none)")
    for row in report["daily"]:
        lines.append(_fmt_stats(f"  {row['date']}", row))
    bot = report["bot_trade_share"]
    fills = ""
    if bot.get("total_fills") is not None:
        fills = f" total_fills={bot['total_fills']} bot_fills={bot['bot_fills']}"
    lines.append(
        f"bot_trade_share: status={bot['status']} share={_fmt(bot['share'])} "
        f"source={bot.get('source', 'receipt_tag')} "
        f"tagged_n={bot['tagged_n']} bot_n={bot['bot_n']} untagged_n={bot['untagged_n']} "
        f"pattern_recognition={bot['pattern_recognition']}{fills}"
    )
    if bot.get("error"):
        lines.append(f"  error={bot['error']}")
    token_rows = bot.get("by_token") or []
    for row in token_rows[:8]:
        lines.append(
            f"  token {row['chain_id']} {row['token_address']}: "
            f"share={_fmt(row['share'])} bot_fills={row['bot_fills']} "
            f"total_fills={row['total_fills']}"
        )
    if len(token_rows) > 8:
        lines.append(f"  by_token_omitted={len(token_rows) - 8}")
    fallback = bot.get("receipt_fallback")
    if isinstance(fallback, dict):
        lines.append(
            f"  receipt_fallback: status={fallback['status']} "
            f"share={_fmt(fallback['share'])} (secondary)"
        )
    lines.append(f"  {bot['note']}")
    skip = report["high_bot_skip"]
    lines.append(
        f"high_bot_skip: wired={str(skip['wired']).lower()} "
        f"pm_gateway={str(skip['pm_gateway']).lower()} {skip['note']}"
    )
    samples = report["samples"]
    lines.append(
        "samples: "
        f"used_1h={samples['used_1h']} from_receipt={samples['from_receipt']} "
        f"from_paper_fill={samples['from_paper_fill']} deduped={samples['deduped']} "
        f"skipped_missing_fields={samples['skipped_missing_fields']} "
        f"skipped_not_filled={samples['skipped_not_filled']}"
    )
    return "\n".join(lines) + "\n"


def _default_paper() -> Path | None:
    env = os.environ.get("EXECUTION_PAPER_DB", "").strip() or os.environ.get(
        "PAPER_DB_PATH", ""
    ).strip()
    if not env:
        return None
    path = Path(env).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _resolve_cli_path(text: str | None) -> Path | None:
    if text is None or not str(text).strip():
        return None
    path = Path(str(text)).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def default_crawler_db() -> Path:
    """``CRAWLER_DB``, else ``DB_PATH``, else ``dexscreener.db`` (crawler config)."""
    env = os.environ.get("CRAWLER_DB", "").strip() or os.environ.get("DB_PATH", "").strip()
    resolved = _resolve_cli_path(env or "dexscreener.db")
    if resolved is None:
        return (Path.cwd() / "dexscreener.db").resolve()
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only slippage_1h report. bot_trade_share reads collection #81 "
            "bot_fill_observations and does not skip orders."
        )
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help="execution_ledger.db (read-only). Default: EXECUTION_LEDGER_PATH or the ledger next to the decision DB.",
    )
    parser.add_argument(
        "--paper",
        default=None,
        help="paper DB (read-only). Default: EXECUTION_PAPER_DB or PAPER_DB_PATH when set.",
    )
    parser.add_argument(
        "--crawler-db",
        default=None,
        help=(
            "crawler SQLite with bot_fill_observations (read-only). "
            "Default: CRAWLER_DB, else DB_PATH, else dexscreener.db."
        ),
    )
    parser.add_argument("--hours", type=float, default=1.0, help="lookback hours for slippage_1h (default 1)")
    parser.add_argument("--days", type=int, default=7, help="UTC day buckets lookback (default 7)")
    parser.add_argument("--as-of", default=None, help="window end, ISO-8601 UTC (default: now)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        print("error: --hours must be > 0", file=sys.stderr)
        return 2
    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        return 2
    ledger = _resolve_cli_path(args.ledger)
    if ledger is None and args.ledger is None:
        ledger = resolve_ledger_path(None)
    paper = _resolve_cli_path(args.paper)
    if paper is None and args.paper is None:
        paper = _default_paper()
    crawler_db = _resolve_cli_path(args.crawler_db)
    if crawler_db is None:
        crawler_db = default_crawler_db()
    as_of = _parse_ts(args.as_of) if args.as_of else None
    if args.as_of and as_of is None:
        print("error: --as-of is not a timestamp", file=sys.stderr)
        return 2
    try:
        report = build_report(
            ledger=ledger,
            paper=paper,
            as_of=as_of,
            window_hours=args.hours,
            daily_days=args.days,
            crawler_db=crawler_db,
        )
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(format_report(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
