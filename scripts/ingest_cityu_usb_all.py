#!/usr/bin/env python3
"""One-click Merger & Ingestor for all 24 CityU files in '/Volumes/NO NAME/lesg creat'.

Merges and cross-fills:
1. ``/tmp/trade_sync/data/lseg/lseg_macro.db`` (read directly by ``macro_circuit_breaker.py``):
   - ``cross_asset_bars``: 775,260 rows from ``lseg_cityu_core_refresh_bars.csv`` and
     ``lseg_cityu_incremental_bars.csv`` (with ``spread_bps`` and ``quote_imbalance``)
     PLUS ``.VIX``, ``.VXO``, ``.VXN`` from ``cityu_wrds_15_cboe_indices.csv`` to fill
     the LSEG index permission gap.
   - ``macro_events``: 58,863 rows from ``US_CN_EU_JP_ECOCAL_SmartEconomics.xlsx``
     (``US``, ``CN``, ``EU``, ``JP``, ``US_Auctions``) mapped to the exact schema of
     ``macro_circuit_breaker.py`` (``reuters_poll``, ``actual``, ``surprise``,
     ``predicted_surprise``, ``min_forecast``, ``max_forecast``).
   - ``central_bank_events``: Preserves existing rows and populates policy-rate
     decisions from ECOCAL.
2. ``/tmp/trade_sync/data/cityu/cityu_institutional.db``:
   - Ingests CFTC COT positioning, LSEG derived/substitute rates, ETF NAVs,
     Short Interest, Orbis Bank Focus liquidity fragility table, and WRDS
     OptionMetrics, TRACE convertible bonds, 13F, Form 4 Insiders, and CRSP tables.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import math
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

USB_DIR = Path("/Volumes/NO NAME/lesg creat")
REPO_ROOT = Path(__file__).resolve().parents[1]
LSEG_DB = REPO_ROOT / "data" / "lseg" / "lseg_macro.db"
INST_DB = REPO_ROOT / "data" / "cityu" / "cityu_institutional.db"

TIER1_KEYWORDS = (
    "cpi", "consumer price", "nonfarm", "non-farm", "payroll", "unemployment",
    "gdp", "pce", "fomc", "fed funds", "policy rate", "interest rate", "ism",
    "pmi", "retail sales", "ppi", "tankan", "lpr",
)


def _f(val: object) -> float | None:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _parse_utc_ms(ts_str: str) -> tuple[str, int] | None:
    s = (ts_str or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def iter_xlsx_rows(xlsx_path: Path, sheet_name: str):
    """Memory-efficient streaming reader for .xlsx worksheets using stdlib zipfile + xml."""
    with zipfile.ZipFile(xlsx_path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for _, elem in ET.iterparse(z.open("xl/sharedStrings.xml"), events=("end",)):
                if elem.tag.endswith("}si"):
                    t = "".join(x.text or "" for x in elem.iter() if x.tag.endswith("}t"))
                    shared.append(t)
                    elem.clear()
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_map = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        target = None
        for sh in wb.iter():
            if sh.tag.endswith("}sheet") and sh.attrib.get("name") == sheet_name:
                rid = [v for k, v in sh.attrib.items() if k.endswith("}id")][0]
                t_raw = rid_map[rid].lstrip("/")
                target = t_raw if t_raw.startswith("xl/") else "xl/" + t_raw
                break
        if target is None:
            return
        for _, elem in ET.iterparse(z.open(target), events=("end",)):
            if elem.tag.endswith("}row"):
                vals = []
                for c in elem:
                    if not c.tag.endswith("}c"):
                        continue
                    t_attr = c.attrib.get("t")
                    v_txt = ""
                    for ch in c.iter():
                        if ch.tag.endswith("}v") and ch.text:
                            v_txt = shared[int(ch.text)] if t_attr == "s" else ch.text
                        elif ch.tag.endswith("}t") and ch.text:
                            v_txt = ch.text
                    vals.append(v_txt)
                elem.clear()
                yield vals


def ingest_lseg_macro_db(conn: sqlite3.Connection) -> dict[str, int]:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cross_asset_bars (
            ric TEXT NOT NULL,
            interval TEXT NOT NULL,
            utc_ts TEXT NOT NULL,
            utc_ms INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            bid REAL,
            ask REAL,
            spread_bps REAL,
            bid_nummov REAL,
            ask_nummov REAL,
            quote_imbalance REAL,
            vwap REAL,
            PRIMARY KEY (ric, interval, utc_ms)
        );
        CREATE INDEX IF NOT EXISTS idx_cab_ric_int_ms ON cross_asset_bars(ric, interval, utc_ms DESC);

        CREATE TABLE IF NOT EXISTS macro_events (
            event_id TEXT PRIMARY KEY,
            country TEXT,
            indicator_name TEXT,
            ric TEXT,
            relevance INTEGER,
            utc_ts TEXT,
            utc_ms INTEGER,
            local_time_hkt TEXT,
            reuters_poll REAL,
            actual REAL,
            surprise REAL,
            predicted_surprise REAL,
            min_forecast REAL,
            max_forecast REAL,
            std_dev REAL,
            num_forecasts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_macro_ev_ms ON macro_events(relevance, utc_ms);

        CREATE TABLE IF NOT EXISTS central_bank_events (
            cb_id TEXT PRIMARY KEY,
            country TEXT,
            event_name TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            local_time_hkt TEXT,
            is_high_impact INTEGER DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_cb_ev_ms ON central_bank_events(is_high_impact, utc_ms);
        """
    )

    # 1. Ingest LSEG core & incremental bars
    bar_sql = """
        INSERT OR REPLACE INTO cross_asset_bars (
            ric, interval, utc_ts, utc_ms, open, high, low, close, volume,
            bid, ask, spread_bps, bid_nummov, ask_nummov, quote_imbalance, vwap
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    bar_count = 0
    for csv_name in ("lseg_cityu_core_refresh_bars.csv", "lseg_cityu_incremental_bars.csv"):
        p = USB_DIR / csv_name
        if not p.is_file():
            continue
        batch = []
        with p.open("r", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                ric = (row.get("ric") or "").strip()
                interval = (row.get("interval") or "").strip()
                parsed = _parse_utc_ms(row.get("Timestamp") or "")
                if not ric or not interval or parsed is None:
                    continue
                utc_ts, utc_ms = parsed
                op = _f(row.get("OPEN_PRC")) or _f(row.get("MID_OPEN")) or _f(row.get("OPEN_BID"))
                hi = _f(row.get("HIGH_1")) or _f(row.get("MID_HIGH")) or _f(row.get("BID_HIGH_1"))
                lo = _f(row.get("LOW_1")) or _f(row.get("MID_LOW")) or _f(row.get("BID_LOW_1"))
                cl = _f(row.get("TRDPRC_1")) or _f(row.get("MID_PRICE")) or _f(row.get("BID"))
                vol = _f(row.get("ACVOL_UNS")) or _f(row.get("NUM_MOVES"))
                bid = _f(row.get("BID"))
                ask = _f(row.get("ASK"))
                b_mov = _f(row.get("BID_NUMMOV"))
                a_mov = _f(row.get("ASK_NUMMOV"))
                vwap = _f(row.get("VWAP"))
                spread_bps = (
                    round(((ask - bid) / (0.5 * (bid + ask))) * 10_000.0, 4)
                    if (bid is not None and ask is not None and bid > 0 and ask >= bid)
                    else None
                )
                q_imb = (
                    round((b_mov - a_mov) / (b_mov + a_mov), 4)
                    if (b_mov is not None and a_mov is not None and (b_mov + a_mov) > 0)
                    else None
                )
                batch.append((ric, interval, utc_ts, utc_ms, op, hi, lo, cl, vol, bid, ask, spread_bps, b_mov, a_mov, q_imb, vwap))
                if len(batch) >= 15_000:
                    conn.executemany(bar_sql, batch)
                    bar_count += len(batch)
                    batch.clear()
        if batch:
            conn.executemany(bar_sql, batch)
            bar_count += len(batch)
        conn.commit()

    # 2. Cross-fill .VIX, .VXO, .VXN from WRDS CBOE Indices into cross_asset_bars!
    cboe_path = USB_DIR / "cityu_wrds_15_cboe_indices.csv"
    cboe_added = 0
    if cboe_path.is_file():
        batch = []
        with cboe_path.open("r", encoding="utf-8-sig", errors="replace") as f:
            for row in csv.DictReader(f):
                parsed = _parse_utc_ms(row.get("date") or "")
                if parsed is None:
                    continue
                utc_ts, utc_ms = parsed
                for ric_name, p_o, p_h, p_l, p_c in (
                    (".VIX", "vixo", "vixh", "vixl", "vix"),
                    (".VXO", "vxoo", "vxoh", "vxol", "vxo"),
                    (".VXN", "vxno", "vxnh", "vxnl", "vxn"),
                ):
                    cl = _f(row.get(p_c))
                    if cl is None:
                        continue
                    batch.append(
                        (
                            ric_name, "1d", utc_ts, utc_ms,
                            _f(row.get(p_o)) or cl,
                            _f(row.get(p_h)) or cl,
                            _f(row.get(p_l)) or cl,
                            cl, None, None, None, None, None, None, None, None,
                        )
                    )
        if batch:
            conn.executemany(bar_sql, batch)
            cboe_added = len(batch)
            conn.commit()

    # 3. Ingest US_CN_EU_JP_ECOCAL_SmartEconomics.xlsx into macro_events & central_bank_events
    ecocal_path = USB_DIR / "US_CN_EU_JP_ECOCAL_SmartEconomics.xlsx"
    ev_count = 0
    cb_count = 0
    if ecocal_path.is_file():
        ev_batch = []
        cb_batch = []
        for sheet in ("US", "CN", "EU", "JP", "US_Auctions"):
            it = iter_xlsx_rows(ecocal_path, sheet)
            hdr = next(it, None)
            if not hdr:
                continue
            for r in it:
                if len(r) < 6:
                    continue
                row_d = dict(zip(hdr, r + [""] * len(hdr)))
                dt_str = (row_d.get("date") or "").strip()
                parsed = _parse_utc_ms(dt_str)
                if parsed is None:
                    continue
                utc_ts, utc_ms = parsed
                region = (row_d.get("region") or sheet).strip()
                ind = (row_d.get("indicator") or "").strip()
                ticker = (row_d.get("ticker") or "").strip()
                actual = _f(row_d.get("actual"))
                poll = _f(row_d.get("median")) or _f(row_d.get("mean"))
                smart_ec = _f(row_d.get("smart_economics"))
                pred_surp = _f(row_d.get("predicted_surprise"))
                if pred_surp is None and smart_ec is not None and poll is not None:
                    pred_surp = round(smart_ec - poll, 6)
                surp = round(actual - poll, 6) if (actual is not None and poll is not None) else None
                min_f = _f(row_d.get("min"))
                max_f = _f(row_d.get("max"))
                std_f = _f(row_d.get("std_dev"))
                num_f = _f(row_d.get("num_forecasts"))

                ind_low = ind.lower()
                if sheet == "US_Auctions" or "auction" in ind_low:
                    rel = 2
                elif any(k in ind_low for k in TIER1_KEYWORDS):
                    rel = 3
                else:
                    rel = 2

                ev_id = f"{sheet}:{ticker}:{dt_str}"
                ev_batch.append(
                    (
                        ev_id, region, ind, ticker, rel, utc_ts, utc_ms, dt_str,
                        poll, actual, surp, pred_surp, min_f, max_f, std_f, num_f,
                    )
                )
                if any(k in ind_low for k in ("policy rate", "fed funds", "fomc", "lpr", "refinancing rate", "call rate")):
                    cb_batch.append((f"CB:{ev_id}", region, ind, utc_ts, utc_ms, dt_str, 1))

                if len(ev_batch) >= 10_000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO macro_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ev_batch,
                    )
                    ev_count += len(ev_batch)
                    ev_batch.clear()
        if ev_batch:
            conn.executemany(
                "INSERT OR REPLACE INTO macro_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ev_batch,
            )
            ev_count += len(ev_batch)
        if cb_batch:
            conn.executemany(
                "INSERT OR REPLACE INTO central_bank_events VALUES (?,?,?,?,?,?,?)",
                cb_batch,
            )
            cb_count += len(cb_batch)
        conn.commit()

    return {
        "lseg_bars": bar_count,
        "wrds_cboe_vix_bars_merged": cboe_added,
        "ecocal_macro_events": ev_count,
        "central_bank_events": cb_count,
    }


def ingest_csv_to_sqlite(conn: sqlite3.Connection, csv_path: Path, table_name: str) -> int:
    if not csv_path.is_file():
        return 0
    with csv_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        raw_headers = next(reader, [])
        if not raw_headers:
            return 0
        cols = []
        seen = set()
        for i, h in enumerate(raw_headers):
            c = "".join(ch if ch.isalnum() else "_" for ch in h.strip()).strip("_").lower() or f"col_{i}"
            if c in seen:
                c = f"{c}_{i}"
            seen.add(c)
            cols.append(c)
        conn.execute(f"DROP TABLE IF EXISTS {table_name}")
        col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
        conn.execute(f"CREATE TABLE {table_name} ({col_defs})")
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table_name} VALUES ({placeholders})"
        batch = []
        count = 0
        for row in reader:
            if not row:
                continue
            padded = (row + [""] * len(cols))[: len(cols)]
            batch.append(padded)
            if len(batch) >= 20_000:
                conn.executemany(sql, batch)
                count += len(batch)
                batch.clear()
        if batch:
            conn.executemany(sql, batch)
            count += len(batch)
        conn.commit()
    return count


def ingest_orbis_xlsx(conn: sqlite3.Connection, xlsx_path: Path) -> int:
    if not xlsx_path.is_file():
        return 0
    it = iter_xlsx_rows(xlsx_path, "银行明细")
    hdr = next(it, None)
    if not hdr:
        return 0
    cols = [f"col_{i}_{h[:10]}" for i, h in enumerate(hdr)]
    conn.execute("DROP TABLE IF EXISTS orbis_bank_liquidity_risk")
    col_defs = ", ".join(f'"{c}" TEXT' for c in cols)
    conn.execute(f"CREATE TABLE orbis_bank_liquidity_risk ({col_defs})")
    placeholders = ", ".join("?" for _ in cols)
    rows = [((r + [""] * len(cols))[: len(cols)]) for r in it if any(r)]
    conn.executemany(f"INSERT INTO orbis_bank_liquidity_risk VALUES ({placeholders})", rows)
    conn.commit()
    return len(rows)


def main() -> None:
    LSEG_DB.parent.mkdir(parents=True, exist_ok=True)
    INST_DB.parent.mkdir(parents=True, exist_ok=True)

    lseg_conn = sqlite3.connect(str(LSEG_DB))
    lseg_stats = ingest_lseg_macro_db(lseg_conn)
    lseg_conn.close()
    print(f"[LSEG MACRO DB] {LSEG_DB} -> {lseg_stats}")

    inst_conn = sqlite3.connect(str(INST_DB))
    inst_conn.execute("PRAGMA journal_mode=WAL")
    inst_conn.execute("PRAGMA synchronous=NORMAL")

    orbis_rows = ingest_orbis_xlsx(inst_conn, USB_DIR / "cityu_orbis_bank_liquidity_risk.xlsx")
    print(f"[ORBIS BANK FOCUS] orbis_bank_liquidity_risk: {orbis_rows} rows")

    csv_tables = {
        "lseg_cityu_cftc_cot_positioning.csv": "lseg_cftc_cot_positioning",
        "lseg_cityu_derived_rates.csv": "lseg_derived_rates",
        "lseg_cityu_substitute_rates.csv": "lseg_substitute_rates",
        "lseg_cityu_etf_nav_daily.csv": "lseg_etf_nav_daily",
        "lseg_cityu_short_interest_daily.csv": "lseg_short_interest_daily",
        "lseg_cityu_news_headlines.csv": "lseg_news_headlines",
        "ecocal_indicator_catalog.csv": "lseg_ecocal_catalog",
        "cityu_wrds_05_optionmetrics_opvold.csv": "wrds_optionmetrics_opvold",
        "cityu_wrds_06_optionmetrics_zerocd.csv": "wrds_optionmetrics_zerocd",
        "cityu_wrds_08_trace_convertible_bonds.csv": "wrds_trace_convertible_bonds",
        "cityu_wrds_10_compustat_shortint.csv": "wrds_compustat_shortint",
        "cityu_wrds_11_13f_holdings.csv": "wrds_13f_holdings",
        "cityu_wrds_13_insider_trades_form4.csv": "wrds_insider_trades_form4",
        "cityu_wrds_14_crsp_dsf.csv": "wrds_crsp_dsf",
        "cityu_wrds_15_cboe_indices.csv": "wrds_cboe_indices",
        "cityu_wrds_04_optionmetrics_stdopd.csv": "wrds_optionmetrics_stdopd",
        "cityu_wrds_03_optionmetrics_vsurfd.csv": "wrds_optionmetrics_vsurfd",
        "cityu_wrds_14b_crsp_treasuries_tfz_dly.csv": "wrds_crsp_treasuries",
    }
    for fname, tname in csv_tables.items():
        n = ingest_csv_to_sqlite(inst_conn, USB_DIR / fname, tname)
        print(f"[INST DB] {tname}: {n} rows")
    inst_conn.close()


if __name__ == "__main__":
    main()
