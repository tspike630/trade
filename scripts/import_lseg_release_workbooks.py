#!/usr/bin/env python3
"""Import lesg crypto release-clock workbooks into a new acceptance database.

Does not open or rewrite the 2026-11-10 CityU snapshot. ``Period`` stays the
observation label. ``Local Time`` is Hong Kong wall time. An empty Local Time
is not filled from the date. Rows that disagree on clock or relevance for the
same event identity are conflicts and are not release clocks.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SNAPSHOT_SHA256 = "949dedc4a30ebe2f0b13a43c6cc6f79862af88b0e9b066cae0fa772f541c0ff4"
HKT = ZoneInfo("Asia/Hong_Kong")
_EXCEL_EPOCH = datetime(1899, 12, 30)
_HIGH_IMPACT = re.compile(
    r"\b(FOMC|Powell|Interest Rate|Policy Statement|Press Conference|Lagarde|Ueda|Minutes|Nonfarm|CPI)\b",
    re.IGNORECASE,
)
# A policy-decision release, not a speech that merely mentions monetary policy.
_POLICY_DECISION = re.compile(
    r"(?:"
    r"announces (?:its |a )?(?:rate|policy) decision\b"
    r"|announces decision of its\b"
    r"|concludes (?:two-day |its )?policy meeting, announces\b"
    r"|publication of the monetary policy decisions\b"
    r"|publication of the account of the (?:ecb )?monetary policy meeting\b"
    r")",
    re.IGNORECASE,
)


def is_high_impact_event(name: str) -> bool:
    text = name or ""
    return _HIGH_IMPACT.search(text) is not None or _POLICY_DECISION.search(text) is not None

_ECON_FILES = (
    "lesg_crypto__Economic Events.xlsx",
    "lesg_crypto__Economic Events (1).xlsx",
    "lesg_crypto__Economic Events (2).xlsx",
)
_CB_FILE = "lesg_crypto__Central Bank and Political Events (1).xlsx"
_ECOCAL_FILE = "lesg_creat__US_CN_EU_JP_ECOCAL_SmartEconomics.xlsx"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refuse_snapshot_overwrite(path: Path) -> None:
    if path.is_file() and _sha256(path) == SNAPSHOT_SHA256:
        raise RuntimeError(f"refusing to overwrite the 2026-11-10 snapshot at {path}")


def _column_index(ref: str) -> int:
    column = 0
    index = 0
    while index < len(ref) and ref[index].isalpha():
        column = column * 26 + (ord(ref[index].upper()) - 64)
        index += 1
    return column - 1


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    shared: list[str] = []
    with workbook.open("xl/sharedStrings.xml") as handle:
        for _, elem in ET.iterparse(handle, events=("end",)):
            if elem.tag.endswith("}si"):
                shared.append("".join(node.text or "" for node in elem.iter() if node.tag.endswith("}t")))
                elem.clear()
    return shared


def read_xlsx_sheet(path: Path, sheet_name: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as workbook:
        shared = _shared_strings(workbook)
        book = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        rid_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        target = None
        for sheet in book.iter():
            if sheet.tag.endswith("}sheet") and sheet.attrib.get("name") == sheet_name:
                rid = next(value for key, value in sheet.attrib.items() if key.endswith("}id"))
                raw = rid_map[rid].lstrip("/")
                target = raw if raw.startswith("xl/") else "xl/" + raw
                break
        if target is None:
            raise ValueError(f"{path.name} has no sheet {sheet_name}")
        rows: list[list[str]] = []
        with workbook.open(target) as handle:
            for _, elem in ET.iterparse(handle, events=("end",)):
                if not elem.tag.endswith("}row"):
                    continue
                cells: dict[int, str] = {}
                for cell in elem:
                    if not cell.tag.endswith("}c"):
                        continue
                    ref = cell.attrib.get("r", "")
                    position = _column_index(ref) if ref else len(cells)
                    kind = cell.attrib.get("t")
                    text = ""
                    for child in cell.iter():
                        if child.tag.endswith("}v") and child.text:
                            text = shared[int(child.text)] if kind == "s" else child.text
                    cells[position] = text
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(index, "") for index in range(width)])
                elem.clear()
        return rows


def excel_datetime(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "T" in text or (len(text) >= 10 and text[4] == "-"):
        return text[:19]
    serial = float(text)
    return (_EXCEL_EPOCH + timedelta(days=serial)).strftime("%Y-%m-%dT%H:%M:%S")


def hkt_clock_to_utc(local_time: str) -> tuple[str, int, str]:
    local = datetime.strptime(local_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=HKT)
    utc = local.astimezone(timezone.utc)
    return (
        utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        int(utc.timestamp() * 1000),
        local.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _rows_by_header(path: Path, sheet: str) -> list[dict[str, str]]:
    table = read_xlsx_sheet(path, sheet)
    header = table[0]
    index = {name: position for position, name in enumerate(header)}
    out: list[dict[str, str]] = []
    for row in table[1:]:
        def cell(name: str) -> str:
            position = index[name]
            return row[position] if position < len(row) else ""

        out.append({name: cell(name).strip() for name in header})
    return out


def _merge(groups: dict[tuple, list[tuple[str, dict[str, str]]]], time_key: str) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for identity, items in groups.items():
        clocks = {(item[time_key] or "") for _, item in items}
        populated = {value for value in clocks if value}
        relevances = {(item.get("Relevance") or "").strip() for _, item in items if "Relevance" in item}
        sources = tuple(sorted({source for source, _ in items}))
        if len(populated) > 1 or len(relevances) > 1:
            status = "conflict"
            chosen = ""
        elif len(populated) == 1:
            status = "release"
            chosen = next(iter(populated))
        else:
            status = "absent"
            chosen = ""
        sample = items[0][1]
        merged.append(
            {
                "identity": identity,
                "status": status,
                "local_time": chosen,
                "sources": sources,
                "sample": sample,
                "relevance": next(iter(relevances)) if len(relevances) == 1 else "",
            }
        )
    return merged


def load_economic(source_dir: Path) -> list[dict[str, object]]:
    groups: dict[tuple, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for name in _ECON_FILES:
        for row in _rows_by_header(source_dir / name, "Economic Events"):
            start = excel_datetime(row.get("Local Start Date", ""))
            local_time = excel_datetime(row.get("Local Time", ""))
            identity = (
                row.get("RIC", ""),
                row.get("Indicator Name", ""),
                row.get("Period", ""),
                row.get("Country/Region", ""),
                start[:10],
            )
            stored = dict(row)
            stored["Local Time"] = local_time
            stored["Local Start Date"] = start
            groups[identity].append((name, stored))
    return _merge(groups, "Local Time")


def load_central_bank(source_dir: Path) -> list[dict[str, object]]:
    groups: dict[tuple, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for row in _rows_by_header(source_dir / _CB_FILE, "Central Bank and Political Even"):
        start = excel_datetime(row.get("Local Start Date", ""))
        local_time = excel_datetime(row.get("Local Time", ""))
        identity = (row.get("Country/Region", ""), row.get("Event Name", ""), start[:10])
        stored = dict(row)
        stored["Local Time"] = local_time
        stored["Local Start Date"] = start
        groups[identity].append((_CB_FILE, stored))
    return _merge(groups, "Local Time")


def build_acceptance_db(source_dir: Path, out_path: Path) -> dict[str, int]:
    refuse_snapshot_overwrite(out_path)
    economic = load_economic(source_dir)
    central = load_central_bank(source_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        refuse_snapshot_overwrite(out_path)
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(
            """
            CREATE TABLE macro_events (
                event_id TEXT PRIMARY KEY,
                country TEXT,
                indicator_name TEXT,
                ric TEXT,
                relevance INTEGER,
                period TEXT,
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
            CREATE TABLE release_event (
                event_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                clock_status TEXT NOT NULL,
                observation_period TEXT,
                release_date_hkt TEXT,
                release_local_hkt TEXT,
                utc_ts TEXT,
                relevance TEXT,
                sources TEXT NOT NULL
            );
            CREATE TABLE coverage_scope (
                country TEXT,
                event_class TEXT,
                resolved_n INTEGER,
                unresolved_n INTEGER,
                first_utc TEXT,
                last_utc TEXT,
                note TEXT
            );
            CREATE TABLE source_file (
                name TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                role TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO source_file VALUES (?, ?, ?)",
            (_ECOCAL_FILE, _sha256(source_dir / _ECOCAL_FILE), "excluded_date_only"),
        )
        for name in _ECON_FILES:
            conn.execute(
                "INSERT INTO source_file VALUES (?, ?, ?)",
                (name, _sha256(source_dir / name), "economic_release_clock"),
            )
        conn.execute(
            "INSERT INTO source_file VALUES (?, ?, ?)",
            (_CB_FILE, _sha256(source_dir / _CB_FILE), "central_bank_release_clock"),
        )
        counts = {"release": 0, "absent": 0, "conflict": 0, "macro": 0, "central_bank": 0}
        for kind, rows in (("economic", economic), ("central_bank", central)):
            for item in rows:
                status = str(item["status"])
                counts[status] += 1
                local_time = str(item["local_time"] or "")
                utc_ts = ""
                utc_ms = None
                local_hkt = ""
                if status == "release":
                    utc_ts, utc_ms, local_hkt = hkt_clock_to_utc(local_time)
                identity = item["identity"]
                key = "|".join(str(part) for part in identity)
                sample = item["sample"]
                assert isinstance(sample, dict)
                period = sample.get("Period", "") if kind == "economic" else ""
                sources = item["sources"]
                assert isinstance(sources, tuple)
                conn.execute(
                    """
                    INSERT INTO release_event (
                        event_key, kind, clock_status, observation_period,
                        release_date_hkt, release_local_hkt, utc_ts, relevance, sources
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        kind,
                        status,
                        period,
                        str(identity[-1])[:10],
                        local_hkt,
                        utc_ts or None,
                        item["relevance"],
                        ",".join(sources),
                    ),
                )
                if status != "release" or utc_ms is None:
                    continue
                if kind == "economic":
                    conn.execute(
                        """
                        INSERT INTO macro_events (
                            event_id, country, indicator_name, ric, relevance, period,
                            utc_ts, utc_ms, local_time_hkt
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            identity[3],
                            identity[1],
                            identity[0],
                            int(float(item["relevance"])) if item["relevance"] != "" else None,
                            identity[2],
                            utc_ts,
                            utc_ms,
                            local_hkt,
                        ),
                    )
                    counts["macro"] += 1
                else:
                    name = str(identity[1])
                    conn.execute(
                        """
                        INSERT INTO central_bank_events (
                            cb_id, country, event_name, utc_ts, utc_ms, local_time_hkt, is_high_impact
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            identity[0],
                            name,
                            utc_ts,
                            utc_ms,
                            local_hkt,
                            1 if is_high_impact_event(name) else 0,
                        ),
                    )
                    counts["central_bank"] += 1
        note = (
            "Proven only in the open gap between two consecutive resolved clocks "
            "of the same country and class. Not a continuous calendar."
        )
        conn.execute(
            """
            INSERT INTO coverage_scope (
                country, event_class, resolved_n, unresolved_n, first_utc, last_utc, note
            )
            SELECT country, 'rel3', COUNT(*), 0, MIN(utc_ts), MAX(utc_ts), ?
            FROM macro_events
            WHERE relevance = 3
            GROUP BY country
            """,
            (note,),
        )
        conn.execute(
            """
            INSERT INTO coverage_scope (
                country, event_class, resolved_n, unresolved_n, first_utc, last_utc, note
            )
            SELECT country, 'cb_high', COUNT(*), 0, MIN(utc_ts), MAX(utc_ts), ?
            FROM central_bank_events
            WHERE is_high_impact = 1
            GROUP BY country
            """,
            (note,),
        )
        conn.commit()
    finally:
        conn.close()
    refuse_snapshot_overwrite(out_path)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    counts = build_acceptance_db(args.sources, args.out)
    print(
        "counts "
        + " ".join(f"{key}={value}" for key, value in counts.items())
    )
    print(f"sha256 {_sha256(args.out)}")


if __name__ == "__main__":
    main()
