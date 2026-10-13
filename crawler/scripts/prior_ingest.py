#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_ingest.py — 新币先验数据集清洗入库（Step1）

用法: python3 scripts/prior_ingest.py [--rebuild]
输入: data/prior/raw/redpump/unpacked/**/*.{csv,csv.gz}, data/prior/raw/solrpds/dataset/CSV/*.csv
输出: data/prior/db/prior.db (token_outcomes / provenance)
原则: 只读旁路数据, 不碰业务库; 字段不匹配就报错, 不静默。
默认增量: 只删 source in (solrpds, redpump) 的 token_outcomes / provenance 再导入。
--rebuild 才把整个 prior.db 备份后换新文件（会清掉 cex / tags / baseline 等其他表）。

实测 zip 结构 (2026-10-10):
  结局主表 = 04_frozen_inputs/red_pump_2026_v1_outcomes.csv.gz
  列 = graduated_at,mint,symbol,outcome,minutes_to_outcome,...
  另有 05_outputs/cohort_flow.csv (860194 行, 主队列 749816 / GRADUATED 1597)
"""
import csv, glob, gzip, json, os, sqlite3, sys
from datetime import datetime, timezone, timedelta

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "prior")
DB = os.path.join(BASE, "db", "prior.db")
RAW = os.path.join(BASE, "raw")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_epoch(v) -> int | None:
    """兼容 epoch 秒 / 'YYYY-MM-DD HH:MM:SS(.fff)' / ISO(含 Z)"""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    try:
        return int(float(s))
    except ValueError:
        pass
    # ISO 8601 with optional Z / fractional
    if "T" in s:
        try:
            s2 = s.replace("Z", "+00:00")
            return int(datetime.fromisoformat(s2).timestamp())
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s[:26], fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def ingest_solrpds(conn):
    """SolRPDs: 4 个 CSV -> token_outcomes(source='solrpds')"""
    files = sorted(glob.glob(os.path.join(RAW, "solrpds", "dataset", "CSV", "*.csv")))
    if not files:
        sys.exit("[FAIL] SolRPDs CSV 未找到: %s" % os.path.join(RAW, "solrpds", "dataset", "CSV"))
    cols = None
    n = 0
    skipped = 0
    for fp in files:
        with open(fp, newline="", encoding="utf-8", errors="replace") as f:
            rd = csv.DictReader(f)
            if cols is None:
                cols = rd.fieldnames
                print("[solrpds] 列名:", cols)
                req = {"MINT", "LIQUIDITY_POOL_ADDRESS", "INACTIVITY_STATUS"}
                missing = req - set(cols)
                if missing:
                    sys.exit(f"[FAIL] SolRPDs 缺列 {missing}；真实列: {cols}")
            for row in rd:
                mint = (row.get("MINT") or "").strip()
                pool = (row.get("LIQUIDITY_POOL_ADDRESS") or "").strip()
                status = (row.get("INACTIVITY_STATUS") or "").strip()
                if not mint:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO token_outcomes
                       (token_address, chain, source, pool_address, launch_ts, last_ts,
                        outcome, liq_added, liq_removed, add_remove_ratio, num_adds,
                        num_removes, inactivity_status, extra_json, ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        mint, "solana", "solrpds",
                        pool,
                        to_epoch(row.get("FIRST_POOL_ACTIVITY_TIMESTAMP")),
                        to_epoch(row.get("LAST_POOL_ACTIVITY_TIMESTAMP")),
                        status.lower() or "unknown",
                        _f(row.get("TOTAL_ADDED_LIQUIDITY")),
                        _f(row.get("TOTAL_REMOVED_LIQUIDITY")),
                        _f(row.get("ADD_TO_REMOVE_RATIO")),
                        _i(row.get("NUM_LIQUIDITY_ADDS")),
                        _i(row.get("NUM_LIQUIDITY_REMOVES")),
                        status,
                        json.dumps(row, ensure_ascii=False),
                        now_iso(),
                    ),
                )
                n += 1
    conn.commit()
    print(f"[solrpds] 入库 {n} 行（跳过空 MINT {skipped}）")


def _open_text(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return open(path, newline="", encoding="utf-8", errors="replace")


def ingest_redpump(conn):
    """RED-PUMP: 优先 frozen outcomes.csv.gz；否则递归找 csv。
    结构以解压实测为准; 找不到结局列就列出所有文件与列名退出。"""
    base = os.path.join(RAW, "redpump")
    zips = glob.glob(os.path.join(base, "*.zip"))
    unpacked = os.path.join(base, "unpacked")
    if not os.path.isdir(unpacked):
        if not zips:
            sys.exit("[FAIL] 未找到 redpump.zip 或 unpacked/ 目录")
        print("[redpump] 解压中…")
        import zipfile
        with zipfile.ZipFile(zips[0]) as z:
            z.extractall(unpacked)

    # 优先实测主结局表（gzip）
    preferred = sorted(glob.glob(
        os.path.join(unpacked, "**", "red_pump_2026_v1_outcomes.csv.gz"), recursive=True))
    csvs = preferred + sorted(glob.glob(os.path.join(unpacked, "**", "*.csv"), recursive=True))
    # 去重保持顺序
    seen = set(); files = []
    for c in csvs:
        if c not in seen:
            seen.add(c); files.append(c)
    if not files:
        sys.exit("[FAIL] RED-PUMP 解压后无 csv/csv.gz；请人工检查 unpacked/ 结构")
    print("[redpump] 发现候选文件 %d 个:" % len(files))
    for c in files[:40]:
        print("   -", os.path.relpath(c, unpacked))

    target = None
    for c in files:
        with _open_text(c) as f:
            head = f.readline(4000)
        low = head.lower()
        if ("mint" in low) and ("outcome" in low or "graduat" in low or "timeout" in low):
            target = c
            break
    if target is None:
        print("[FAIL] 未在表头中找到 mint+graduat/outcome/timeout 组合。各表头如下：")
        for c in files:
            with _open_text(c) as f:
                print(f"  {os.path.relpath(c, unpacked)}: {f.readline(2000).strip()[:300]}")
        sys.exit(1)

    with _open_text(target) as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames
        print(f"[redpump] 结局表: {os.path.relpath(target, unpacked)}")
        print("[redpump] 列名:", cols)
        def find(keys):
            for k in cols:
                kl = k.strip().lower()
                if any(x in kl for x in keys):
                    return k
            return None
        c_mint = find(["mint", "token_address", "tokenaddress"])
        # 优先精确 outcome / outcome_label，再退到 graduat/status/timeout
        c_out = None
        for k in cols:
            kl = k.strip().lower()
            if kl in ("outcome", "outcome_label"):
                c_out = k; break
        if c_out is None:
            c_out = find(["graduat", "outcome", "status", "timeout"])
        c_launch = find(["created_at", "launch", "first_seen", "created", "first_trade", "mint_time"])
        c_last = find(["graduated_at", "last_seen", "last_trade", "terminal", "exit"])
        c_mins = find(["minutes_to_outcome"])
        missing = [name for name, c in (("mint", c_mint), ("outcome", c_out)) if c is None]
        if missing:
            print(f"[FAIL] RED-PUMP 缺列 {missing}；真实列: {cols}")
            sys.exit(1)
        print(f"[redpump] 映射 mint={c_mint} outcome={c_out} launch={c_launch} last={c_last} mins={c_mins}")
        n = 0
        skipped = 0
        valid_outcomes = {"graduated", "timeout", "active", "inactive", "rug"}
        for row in rd:
            out_raw = str(row.get(c_out) or "").strip()
            out = out_raw.lower()
            # 畸形行（symbol/note 含换行导致列错位）→ 打印计数后跳过，不填静默 NULL
            if out not in valid_outcomes and out_raw not in ("GRADUATED", "TIMEOUT"):
                skipped += 1
                continue
            out = out if out in valid_outcomes else out_raw.lower()
            mint = str(row.get(c_mint) or "").strip()
            if not mint:
                skipped += 1
                continue
            last_ts = to_epoch(row.get(c_last)) if c_last else None
            launch_ts = to_epoch(row.get(c_launch)) if c_launch else None
            mins = _f(row.get(c_mins)) if c_mins else None
            if launch_ts is None and last_ts is not None and mins is not None:
                launch_ts = last_ts - int(mins * 60)
            conn.execute(
                """INSERT INTO token_outcomes
                   (token_address, chain, source, launch_ts, last_ts, outcome, extra_json, ingested_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    mint, "solana", "redpump",
                    launch_ts, last_ts,
                    out, json.dumps(row, ensure_ascii=False), now_iso(),
                ),
            )
            n += 1
            if n % 100000 == 0:
                conn.commit()
                print(f"[redpump] …{n} 行")
    conn.commit()
    print(f"[redpump] 入库 {n} 行（跳过畸形/空 outcome {skipped}）")


INGEST_SOURCES = ("solrpds", "redpump")


def _schema_path() -> str:
    return os.path.join(BASE, "..", "..", "models", "prior_tables.sql")


def backup_and_remove_db(db_path: str) -> str:
    """Copy ``db_path`` to a sibling .bak file, then delete the live file.

    Other prior tables live in this database. Callers use this only for an
    explicit rebuild. If the backup fails, the original file is left in place.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = f"{db_path}.bak-{stamp}"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(bak)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.exists(side):
            os.remove(side)
    return bak


def prepare_prior_connection(db_path: str, schema_path: str, *, rebuild: bool) -> sqlite3.Connection:
    """Open prior.db and apply schema.

    Default (rebuild=False) deletes only solrpds/redpump outcome and provenance
    rows so cex snapshots, decision_prior_tags, and baseline_history stay.
    rebuild=True backs up the file and starts a new database.
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if rebuild and os.path.exists(db_path):
        bak = backup_and_remove_db(db_path)
        print(f"[rebuild] backed up {db_path} -> {bak}")
    elif os.path.exists(db_path):
        print("[incremental] keeping prior.db; replacing solrpds/redpump rows only")
    conn = sqlite3.connect(db_path)
    with open(schema_path, encoding="utf-8") as schema_file:
        conn.executescript(schema_file.read())
    if not rebuild:
        marks = ",".join("?" for _ in INGEST_SOURCES)
        conn.execute(
            f"DELETE FROM token_outcomes WHERE source IN ({marks})",
            INGEST_SOURCES,
        )
        conn.execute(
            f"DELETE FROM provenance WHERE source IN ({marks})",
            INGEST_SOURCES,
        )
        conn.commit()
    return conn


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    rebuild = "--rebuild" in args
    unknown = [a for a in args if a != "--rebuild"]
    if unknown:
        sys.exit(f"[FAIL] unknown args {unknown}; only --rebuild is supported")
    if not os.path.isdir(os.path.join(RAW, "solrpds")) and not os.path.isdir(os.path.join(RAW, "redpump")):
        sys.exit("[FAIL] raw/ 下没有数据目录, 先跑 §2 下载")
    conn = prepare_prior_connection(DB, _schema_path(), rebuild=rebuild)
    ingest_solrpds(conn)
    ingest_redpump(conn)
    # provenance
    conn.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?)",
                 ("solrpds", now_iso(), "https://github.com/DeFiLabX/SolRPDs",
                  "CC-BY-4.0", "dataset/CSV/*.csv (4 个)", "池级属性, INACTIVITY_STATUS 为 rug 信号"))
    conn.execute("INSERT OR REPLACE INTO provenance VALUES (?,?,?,?,?,?)",
                 ("redpump", now_iso(), "https://doi.org/10.5281/zenodo.20633486",
                  "CC-BY-4.0", "RED-PUMP-2026-v1_v1.6_reproducibility.zip → 04_frozen_inputs/red_pump_2026_v1_outcomes.csv.gz",
                  "结局表实测 ~833k 有效 GRADUATED/TIMEOUT；主队列论文口径 749,816 (1,597 GRADUATED)；Zenodo file URL 经 record 20633486→23018519"))
    conn.commit()
    for src in ("solrpds", "redpump"):
        row = conn.execute("SELECT count(*), count(distinct token_address) FROM token_outcomes WHERE source=?", (src,)).fetchone()
        print(f"[汇总] {src}: 行数={row[0]} 唯一token={row[1]}")
    g = conn.execute("SELECT count(*) FROM token_outcomes WHERE source='redpump' AND outcome LIKE '%graduat%'").fetchone()[0]
    print(f"[汇总] redpump GRADUATED={g}")
    conn.close()
    print("DONE")


if __name__ == "__main__":
    main()
