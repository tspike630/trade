#!/usr/bin/env python3
"""Apply official clocks onto a copy of the workbook acceptance database.

The workbook file and the 2026-11-10 CityU snapshot are not modified.
A row is filled only when its event key matches and the evidence file
contains the quoted clock. Conflicts are not overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.import_lseg_release_workbooks import SNAPSHOT_SHA256, is_high_impact_event

FETCHED_AT = "2026-11-19T13:52:00Z"
_HEADER = re.compile(r"^([a-z_]+):\s*(.*)$")
_COUNCIL_CLOCK = re.compile(
    r"(\d{1,2})\s+November\s+(\d{4})\s+\((\d{2}):(\d{2})\)"
)
_BOJ_CLOCK = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+at\s+(\d{1,2}):(\d{2})"
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blocks(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    shared: dict[str, str] = {}
    current: dict[str, str] = {}
    body: list[str] = []
    keys = {"event_key", "source_url", "fetched_at_utc", "release_tz", "decision", "quote"}

    def flush() -> None:
        if current.get("event_key"):
            current["body"] = "\n".join(body)
            blocks.append(dict(current))

    for line in text.splitlines():
        match = _HEADER.match(line.strip())
        if match and match.group(1) in keys:
            name, value = match.group(1), match.group(2).strip()
            if name == "event_key":
                flush()
                body = []
                current = dict(shared)
            if name in {"fetched_at_utc", "release_tz", "decision"}:
                shared[name] = value
            current[name] = value
        else:
            body.append(line)
    flush()
    return blocks


def _clock_from_quote(quote: str, tz_name: str, release_date: str) -> tuple[str, int, str]:
    zone = ZoneInfo(tz_name)
    year, month, day = (int(part) for part in release_date.split("-"))
    council = _COUNCIL_CLOCK.search(quote)
    boj = _BOJ_CLOCK.search(quote)
    if council:
        if int(council.group(1)) != day or int(council.group(2)) != year:
            raise ValueError(f"council date does not match {release_date}")
        hour, minute = int(council.group(3)), int(council.group(4))
    elif boj:
        if _MONTHS[boj.group(1).lower()] != month or int(boj.group(2)) != day:
            raise ValueError(f"statement date does not match {release_date}")
        hour, minute = int(boj.group(3)), int(boj.group(4))
    else:
        raise ValueError(f"no clock in quote: {quote}")
    local = datetime(year, month, day, hour, minute, tzinfo=zone)
    utc = local.astimezone(ZoneInfo("UTC"))
    hkt = local.astimezone(ZoneInfo("Asia/Hong_Kong"))
    return (
        utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        int(utc.timestamp() * 1000),
        hkt.strftime("%Y-%m-%d %H:%M:%S"),
    )


def load_adopted(overlay_dir: Path) -> list[dict[str, object]]:
    adopted: list[dict[str, object]] = []
    for path in sorted(overlay_dir.glob("*.txt")):
        if path.name == "not_adopted.txt":
            continue
        for block in _blocks(path.read_text(encoding="utf-8")):
            if block.get("decision") != "adopt":
                continue
            if block.get("fetched_at_utc") != FETCHED_AT:
                raise ValueError(f"unexpected fetch time in {path.name}")
            quote = block.get("quote") or ""
            if quote not in block.get("body", "") and quote not in path.read_text(encoding="utf-8"):
                raise ValueError(f"quote missing from {path.name}")
            event_key = block["event_key"]
            release_date = event_key.rsplit("|", 1)[-1]
            utc_ts, utc_ms, local_hkt = _clock_from_quote(quote, block["release_tz"], release_date)
            adopted.append(
                {
                    "event_key": event_key,
                    "source_url": block["source_url"],
                    "fetched_at_utc": block["fetched_at_utc"],
                    "release_tz": block["release_tz"],
                    "quote": quote,
                    "evidence_sha256": _sha256(path),
                    "utc_ts": utc_ts,
                    "utc_ms": utc_ms,
                    "local_hkt": local_hkt,
                }
            )
    if len(adopted) != 9:
        raise ValueError(f"expected 9 adopted clocks, parsed {len(adopted)}")
    return adopted


def apply_overlay(source_db: Path, overlay_dir: Path, out_path: Path) -> dict[str, int]:
    if _sha256(source_db) == SNAPSHOT_SHA256 or (out_path.exists() and _sha256(out_path) == SNAPSHOT_SHA256):
        raise RuntimeError("refusing to overwrite the 2026-11-10 snapshot")
    adopted = load_adopted(overlay_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.resolve() != source_db.resolve():
        shutil.copy2(source_db, out_path)
    conn = sqlite3.connect(out_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clock_supplement (
                event_key TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                source_url TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                release_tz TEXT NOT NULL,
                quote TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                utc_ts TEXT NOT NULL
            )
            """
        )
        filled = 0
        for row in adopted:
            current = conn.execute(
                "SELECT kind, clock_status FROM release_event WHERE event_key = ?",
                (row["event_key"],),
            ).fetchone()
            if current is None or current[1] != "absent":
                raise ValueError(f"overlay target is not an absent row: {row['event_key']}")
            kind = current[0]
            conn.execute(
                """
                UPDATE release_event
                SET clock_status = 'release', release_local_hkt = ?, utc_ts = ?
                WHERE event_key = ?
                """,
                (row["local_hkt"], row["utc_ts"], row["event_key"]),
            )
            if kind == "economic":
                parts = str(row["event_key"]).split("|")
                conn.execute(
                    """
                    INSERT INTO macro_events (
                        event_id, country, indicator_name, ric, relevance, period,
                        utc_ts, utc_ms, local_time_hkt
                    ) VALUES (?, 'Japan', ?, ?, 3, ?, ?, ?, ?)
                    """,
                    (
                        row["event_key"], parts[1], parts[0], parts[2],
                        row["utc_ts"], row["utc_ms"], row["local_hkt"],
                    ),
                )
            else:
                name = str(row["event_key"]).split("|")[1]
                conn.execute(
                    """
                    INSERT INTO central_bank_events (
                        cb_id, country, event_name, utc_ts, utc_ms, local_time_hkt, is_high_impact
                    ) VALUES (?, 'Euro Zone', ?, ?, ?, ?, ?)
                    """,
                    (
                        row["event_key"], name, row["utc_ts"], row["utc_ms"], row["local_hkt"],
                        1 if is_high_impact_event(name) else 0,
                    ),
                )
            conn.execute(
                """
                INSERT INTO clock_supplement (
                    event_key, decision, source_url, fetched_at_utc, release_tz,
                    quote, evidence_sha256, utc_ts
                ) VALUES (?, 'adopt', ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"], row["source_url"], row["fetched_at_utc"], row["release_tz"],
                    row["quote"], row["evidence_sha256"], row["utc_ts"],
                ),
            )
            filled += 1
        conn.commit()
    finally:
        conn.close()
    if _sha256(source_db) == SNAPSHOT_SHA256:
        raise RuntimeError("source database changed")
    return {"adopted": filled}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    counts = apply_overlay(args.source_db, args.overlay, args.out)
    print(f"adopted={counts['adopted']}")
    print(f"sha256 {_sha256(args.out)}")


if __name__ == "__main__":
    main()
