#!/usr/bin/env python3
"""Build an acceptance calendar from saved BLS and Fed pages.

The 2026-11-10 CityU snapshot stays untouched. Observation periods stay in
their own column. A release instant exists only when the saved page states
a clock. Eastern Time for the BLS tables is taken from the November 2026
selected-release note, and only after that note's October rows match the
series pages.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SNAPSHOT_SHA256 = "949dedc4a30ebe2f0b13a43c6cc6f79862af88b0e9b066cae0fa772f541c0ff4"
FETCHED_AT_UTC = "2026-11-19T13:20:00Z"
EASTERN = ZoneInfo("America/New_York")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_ROW = re.compile(
    r"\|\s*([A-Za-z]+ \d{4})\s*\|\s*"
    r"([A-Za-z]+\.?\s+\d{1,2},\s+\d{4})\s*\|\s*"
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*\|"
)
_HEADER = re.compile(r"^([a-z_]+):\s*(.*)$")


@dataclass(frozen=True)
class ReleaseEvent:
    source_url: str
    fetched_at_utc: str
    indicator: str
    observation_period: str
    release_local: str
    release_tz: str
    zone_basis: str
    utc_ts: str
    utc_ms: int
    local_time_hkt: str
    relevance: int | None
    central_bank: bool


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refuse_snapshot_overwrite(path: Path) -> None:
    if path.is_file() and _sha256(path) == SNAPSHOT_SHA256:
        raise RuntimeError(f"refusing to overwrite the 2026-11-10 snapshot at {path}")


def _headers(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _HEADER.match(line.strip())
        if match is None:
            if found:
                break
            continue
        found[match.group(1)] = match.group(2).strip()
    return found


def _us_date(text: str) -> datetime:
    match = re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s+(\d{4})", text.strip())
    if match is None:
        raise ValueError(f"unparsed US date: {text}")
    month = _MONTHS.get(match.group(1)[:3].lower())
    if month is None:
        raise ValueError(f"unparsed month: {text}")
    return datetime(int(match.group(3)), month, int(match.group(2)))


def _clock(text: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([AP]M)", text.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError(f"unparsed clock: {text}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3).upper()
    if hour < 1 or hour > 12 or minute > 59:
        raise ValueError(f"clock out of range: {text}")
    if meridiem == "AM":
        hour = 0 if hour == 12 else hour
    elif hour != 12:
        hour += 12
    return hour, minute


def _stamp(day: datetime, hour: int, minute: int, zone: ZoneInfo) -> tuple[datetime, datetime]:
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    return local.astimezone(ZoneInfo("UTC")), local.astimezone(HONG_KONG)


def _event(
    *,
    source_url: str,
    fetched_at_utc: str,
    indicator: str,
    observation_period: str,
    local_dt: datetime,
    utc_dt: datetime,
    hkt_dt: datetime,
    zone_basis: str,
    relevance: int | None,
    central_bank: bool,
) -> ReleaseEvent:
    if utc_dt.tzinfo is None or local_dt.tzinfo is None:
        raise ValueError("release clock has no timezone")
    if observation_period and re.fullmatch(r"\d{4}-\d{2}-\d{2}", observation_period):
        raise ValueError("observation period must not be stored as a release date")
    return ReleaseEvent(
        source_url=source_url,
        fetched_at_utc=fetched_at_utc,
        indicator=indicator,
        observation_period=observation_period,
        release_local=local_dt.strftime("%Y-%m-%d %H:%M"),
        release_tz="America/New_York",
        zone_basis=zone_basis,
        utc_ts=utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        utc_ms=int(utc_dt.timestamp() * 1000),
        local_time_hkt=hkt_dt.strftime("%Y-%m-%d %H:%M:%S"),
        relevance=relevance,
        central_bank=central_bank,
    )


def _bls_rows(text: str, zone_basis: str) -> list[ReleaseEvent]:
    header = _headers(text)
    if header.get("kind") != "bls_release_table":
        raise ValueError("not a BLS release table")
    if header.get("fetched_at_utc") != FETCHED_AT_UTC:
        raise ValueError("unexpected fetch timestamp")
    indicator = header["indicator"]
    relevance = int(header["relevance"])
    rows: list[ReleaseEvent] = []
    for period, release_date, release_time in _ROW.findall(text):
        day = _us_date(release_date)
        hour, minute = _clock(release_time)
        utc_dt, hkt_dt = _stamp(day, hour, minute, EASTERN)
        local_dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)
        rows.append(
            _event(
                source_url=header["source_url"],
                fetched_at_utc=header["fetched_at_utc"],
                indicator=indicator,
                observation_period=period,
                local_dt=local_dt,
                utc_dt=utc_dt,
                hkt_dt=hkt_dt,
                zone_basis=zone_basis,
                relevance=relevance,
                central_bank=False,
            )
        )
    if len(rows) != 14:
        raise ValueError(f"{indicator} parsed {len(rows)} rows, expected 14")
    return rows


def _require_october_match(series: list[ReleaseEvent], note: str, label: str) -> None:
    needle = f"{label} for October 2026"
    if needle not in note or "08:30 AM" not in note:
        raise ValueError(f"November BLS note does not corroborate {label}")
    october = [row for row in series if row.observation_period == "October 2026"]
    if len(october) != 1:
        raise ValueError(f"{label} October 2026 row missing")
    if "08:30" not in october[0].release_local:
        raise ValueError(f"{label} October clock is not 08:30 Eastern")


def load_public_releases(source_dir: Path) -> list[ReleaseEvent]:
    note_path = source_dir / "bls_november_2026_selected.txt"
    note = note_path.read_text(encoding="utf-8")
    if "All times on calendar are Eastern Time." not in note:
        raise ValueError("BLS Eastern Time note missing")
    zone_basis = (
        "https://www.bls.gov/schedule/2026/11_sched_list.htm states "
        "All times on calendar are Eastern Time. Applied to the Release Time "
        "column after the October 2026 rows matched this note."
    )
    cpi = _bls_rows((source_dir / "bls_cpi_schedule.txt").read_text(encoding="utf-8"), zone_basis)
    employment = _bls_rows(
        (source_dir / "bls_empsit_schedule.txt").read_text(encoding="utf-8"),
        zone_basis,
    )
    ppi = _bls_rows((source_dir / "bls_ppi_schedule.txt").read_text(encoding="utf-8"), zone_basis)
    _require_october_match(cpi, note, "Consumer Price Index")
    _require_october_match(employment, note, "Employment Situation")
    _require_october_match(ppi, note, "Producer Price Index")

    statement_text = (source_dir / "fed_fomc_20261028.txt").read_text(encoding="utf-8")
    if "Released October 28, 2026 at 2:00 p.m." not in statement_text:
        raise ValueError("FOMC statement clock missing")
    statement_header = _headers(statement_text)
    statement_day = datetime(2026, 10, 28)
    statement_utc, statement_hkt = _stamp(statement_day, 14, 0, EASTERN)
    statement = _event(
        source_url=statement_header["source_url"],
        fetched_at_utc=statement_header["fetched_at_utc"],
        indicator="FOMC statement",
        observation_period="October 27-28, 2026 meeting",
        local_dt=datetime(2026, 10, 28, 14, 0, tzinfo=EASTERN),
        utc_dt=statement_utc,
        hkt_dt=statement_hkt,
        zone_basis="Meeting page says 2:00 p.m. on October 28, 2026, read as America/New_York.",
        relevance=None,
        central_bank=True,
    )

    minutes_text = (source_dir / "fed_minutes_20261118.txt").read_text(encoding="utf-8")
    if "For release at 2:00 p.m. EST" not in minutes_text or "November 18, 2026" not in minutes_text:
        raise ValueError("FOMC minutes clock missing")
    minutes_header = _headers(minutes_text)
    minutes_day = datetime(2026, 11, 18)
    minutes_utc, minutes_hkt = _stamp(minutes_day, 14, 0, EASTERN)
    minutes = _event(
        source_url=minutes_header["source_url"],
        fetched_at_utc=minutes_header["fetched_at_utc"],
        indicator="FOMC minutes",
        observation_period="October 27-28, 2026 meeting",
        local_dt=datetime(2026, 11, 18, 14, 0, tzinfo=EASTERN),
        utc_dt=minutes_utc,
        hkt_dt=minutes_hkt,
        zone_basis="Press release says For release at 2:00 p.m. EST on November 18, 2026.",
        relevance=None,
        central_bank=True,
    )
    return [*cpi, *employment, *ppi, statement, minutes]


def build_acceptance_db(source_dir: Path, out_path: Path) -> dict[str, int]:
    refuse_snapshot_overwrite(out_path)
    events = load_public_releases(source_dir)
    if out_path.exists():
        refuse_snapshot_overwrite(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(
            """
            CREATE TABLE macro_events (
                event_id TEXT PRIMARY KEY,
                country TEXT,
                indicator_name TEXT,
                relevance INTEGER,
                utc_ts TEXT,
                utc_ms INTEGER,
                local_time_hkt TEXT
            );
            CREATE TABLE central_bank_events (
                cb_id TEXT PRIMARY KEY,
                country TEXT,
                event_name TEXT,
                utc_ts TEXT,
                utc_ms INTEGER,
                local_time_hkt TEXT,
                is_high_impact INTEGER
            );
            CREATE TABLE release_provenance (
                event_key TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                fetched_at_utc TEXT NOT NULL,
                observation_period TEXT NOT NULL,
                release_local TEXT NOT NULL,
                release_tz TEXT NOT NULL,
                zone_basis TEXT NOT NULL,
                utc_ts TEXT NOT NULL
            );
            """
        )
        macro_n = 0
        cb_n = 0
        for event in events:
            key = f"{event.indicator}|{event.observation_period}|{event.utc_ts}"
            conn.execute(
                """
                INSERT INTO release_provenance (
                    event_key, source_url, fetched_at_utc, observation_period,
                    release_local, release_tz, zone_basis, utc_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    event.source_url,
                    event.fetched_at_utc,
                    event.observation_period,
                    event.release_local,
                    event.release_tz,
                    event.zone_basis,
                    event.utc_ts,
                ),
            )
            if event.central_bank:
                conn.execute(
                    """
                    INSERT INTO central_bank_events (
                        cb_id, country, event_name, utc_ts, utc_ms, local_time_hkt, is_high_impact
                    ) VALUES (?, 'United States', ?, ?, ?, ?, 1)
                    """,
                    (key, event.indicator, event.utc_ts, event.utc_ms, event.local_time_hkt),
                )
                cb_n += 1
            else:
                conn.execute(
                    """
                    INSERT INTO macro_events (
                        event_id, country, indicator_name, relevance, utc_ts, utc_ms, local_time_hkt
                    ) VALUES (?, 'United States', ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        event.indicator,
                        event.relevance,
                        event.utc_ts,
                        event.utc_ms,
                        event.local_time_hkt,
                    ),
                )
                macro_n += 1
        conn.commit()
    finally:
        conn.close()
    refuse_snapshot_overwrite(out_path)
    return {"macro_events": macro_n, "central_bank_events": cb_n, "provenance": macro_n + cb_n}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    counts = build_acceptance_db(args.sources, args.out)
    print(f"wrote {args.out}")
    print(
        "counts "
        f"macro_events={counts['macro_events']} "
        f"central_bank_events={counts['central_bank_events']} "
        f"provenance={counts['provenance']}"
    )
    print(f"sha256 {_sha256(args.out)}")


if __name__ == "__main__":
    main()
