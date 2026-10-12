#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prior_tagger.py — PRIOR_STEP5：JEV×token_age×prior 旁路打标

用法:
  python3 scripts/prior_tagger.py              # 打标全部 decision_log
  python3 scripts/prior_tagger.py --since 2026-10-09T00:00:00Z

输入（只读）:
  - 主库 dexscreener.db（config.DB_PATH，mode=ro）: decision_log + pairs.pair_created_at
  - data/prior/exports/baseline_params.json → self_db.survive['1h'/'4h']

输出（只写）:
  - data/prior/db/prior.db → decision_prior_tags（idempotent upsert by decision_id）

原则: 永不写入业务四库；不改 INDICATORS_WEIGHT_*；不改 trading/alerts；
      默认不进决策路径；周报/复盘可只读 prior.db。

schema 适配（相对原始草案）:
  - decision_id = str(decision_log.id)  # 主库 PK 为 INTEGER
  - signal_ts   = decision_log.ts       # ISO TEXT（须转 ms 再与 pair_created_at 相减）
  - pair_created_at 优先 chain_id+pair_address（raw_json），回退 token_address

survival_ref 按 age_bucket（对齐 step4 joint / PRIOR_STEP5_PATCH）:
  <1h   → survival_ref_1h 仅填；survival_ref_4h=NULL
  1-24h → survival_ref_4h 仅填；survival_ref_1h=NULL
  1-7d / >7d / unknown → 两列 NULL + survival_ref_note='crawler 窗口覆盖上限'
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
try:
    sys.path.insert(0, ROOT)
    from config import DB_PATH as _CFG_DB

    MAIN_DB = _CFG_DB if os.path.isabs(_CFG_DB) else os.path.join(ROOT, _CFG_DB)
except Exception:
    MAIN_DB = os.path.join(ROOT, "dexscreener.db")

PRIOR_BASE = os.path.join(ROOT, "data", "prior")
PRIOR_DB = os.path.join(PRIOR_BASE, "db", "prior.db")
BASELINE_JSON = os.path.join(PRIOR_BASE, "exports", "baseline_params.json")
SCHEMA_SQL = os.path.join(ROOT, "models", "prior_tables.sql")

# Documented defaults if baseline_params.json / self_db missing
# （与 2026-10-10 self_survival 实测一致）
DEFAULT_SURVIVE_1H = 0.9271
DEFAULT_SURVIVE_4H = 0.7529

# TUNABLE: GMGN peak P50≈1.0m ×3 经验窗口（暂定，数据成熟后可调）。
# 含义：信号发出时 token 是否还在创建后 3 分钟内（见顶窗口内），不是「是否已见顶」。
# 可用环境变量 PRIOR_PEAK_WINDOW_MIN 覆盖（分钟）。
PEAK_WINDOW_MIN = float(os.environ.get("PRIOR_PEAK_WINDOW_MIN", "3"))
PEAK_WINDOW_MS = int(PEAK_WINDOW_MIN * 60 * 1000)

HK = timezone(timedelta(hours=8))

# age_bucket → which self_db.survive key to use（对齐 review.joint._BUCKET_PRIOR_HORIZON）
# 长窗因 crawler 覆盖上限 → (None, None) → 两列 NULL + note
AGE_REF: dict[str, tuple[Optional[str], Optional[str]]] = {
    "<1h": ("survive", "1h"),  # 全网观测跨度>1h 存活率
    "1-24h": ("survive", "4h"),  # 本段中位参考用 4h（第四步口径）
    "1-7d": (None, None),  # 覆盖上限，N/A
    ">7d": (None, None),  # 覆盖上限，N/A
    "unknown": (None, None),
}

NOTE_CRAWLER_CAP = "crawler 窗口覆盖上限"


def parse_ts_ms(s: str) -> Optional[int]:
    """ISO signal_ts → UTC ms epoch. None on failure.

    注意单位：signal_ts 是 ISO 文本（decision_log.ts），pair_created_at 是 epoch ms。
    必须先 datetime.fromisoformat(...).timestamp()*1000 再减；禁止字符串直接相减。
    """
    if not s:
        return None
    s = str(s).strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp() * 1000)
    except Exception:
        return None


def age_bucket(hours: Optional[float]) -> str:
    """Fine age bucket（与 review.joint.token_age_fine_bucket 对齐）."""
    if hours is None or hours < 0:
        return "unknown"
    if hours < 1.0:
        return "<1h"
    if hours < 24.0:
        return "1-24h"
    if hours < 168.0:
        return "1-7d"
    return ">7d"


def load_baseline_params() -> tuple[dict[str, Any], str]:
    """Load baseline_params.json；失败时用文档默认 self_db.survive。"""
    if not os.path.isfile(BASELINE_JSON):
        params = {
            "self_db": {
                "survive": {"1h": DEFAULT_SURVIVE_1H, "4h": DEFAULT_SURVIVE_4H}
            }
        }
        return params, f"baseline_params.json 缺失；用文档默认 {DEFAULT_SURVIVE_1H}/{DEFAULT_SURVIVE_4H}"
    try:
        data = json.loads(open(BASELINE_JSON, encoding="utf-8").read())
    except Exception as e:
        params = {
            "self_db": {
                "survive": {"1h": DEFAULT_SURVIVE_1H, "4h": DEFAULT_SURVIVE_4H}
            }
        }
        return params, f"baseline_params.json 解析失败 ({e})；用文档默认"
    self_db = data.get("self_db") or {}
    survive = dict(self_db.get("survive") or {})
    if survive.get("1h") is None:
        survive["1h"] = DEFAULT_SURVIVE_1H
    if survive.get("4h") is None:
        survive["4h"] = DEFAULT_SURVIVE_4H
    self_db = dict(self_db)
    self_db["survive"] = survive
    data = dict(data)
    data["self_db"] = self_db
    return data, (
        f"self_db.survive 1h={survive.get('1h')} 4h={survive.get('4h')} "
        f"（按 age_bucket 选列；长窗 N/A）"
    )


def survival_refs_for_bucket(
    bucket: str, params: dict[str, Any]
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """按 age_bucket 填 survival_ref_1h / survival_ref_4h / note。

    <1h   → (rate_1h, None, None)
    1-24h → (None, rate_4h, None)
    其余  → (None, None, NOTE_CRAWLER_CAP)
    无对应值时写 None（NULL），不写 0。
    """
    spec = AGE_REF.get(bucket, (None, None))
    if spec[0] is None or spec[1] is None:
        return None, None, NOTE_CRAWLER_CAP
    try:
        raw = (params.get("self_db") or {}).get(spec[0], {}).get(spec[1])
    except Exception:
        raw = None
    if raw is None:
        # 缺键：短窗仍尽量回落文档默认；长窗不应走到这里
        if spec[1] == "1h":
            raw = DEFAULT_SURVIVE_1H
        elif spec[1] == "4h":
            raw = DEFAULT_SURVIVE_4H
        else:
            return None, None, NOTE_CRAWLER_CAP
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return None, None, NOTE_CRAWLER_CAP
    if bucket == "<1h":
        return rate, None, None
    if bucket == "1-24h":
        return None, rate, None
    return None, None, NOTE_CRAWLER_CAP


def ensure_prior_schema(prior_conn: sqlite3.Connection) -> None:
    """Apply models/prior_tables.sql (IF NOT EXISTS) + ALTER-safe new columns."""
    if os.path.isfile(SCHEMA_SQL):
        prior_conn.executescript(open(SCHEMA_SQL, encoding="utf-8").read())
        prior_conn.commit()
    else:
        prior_conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decision_prior_tags (
              decision_id TEXT NOT NULL,
              chain_id TEXT,
              token_address TEXT,
              signal_ts TEXT,
              age_bucket TEXT,
              token_age_hours REAL,
              peak_window INTEGER,
              survival_ref_1h REAL,
              survival_ref_4h REAL,
              survival_ref_note TEXT,
              tagged_at TEXT DEFAULT (datetime('now')),
              PRIMARY KEY (decision_id)
            );
            CREATE INDEX IF NOT EXISTS idx_dpt_age ON decision_prior_tags(age_bucket);
            """
        )
        prior_conn.commit()

    # CREATE IF NOT EXISTS 不会给旧表加列；用 PRAGMA 探测后再 ADD COLUMN
    cols = {r[1] for r in prior_conn.execute("PRAGMA table_info(decision_prior_tags)")}
    if "survival_ref_note" not in cols:
        prior_conn.execute(
            "ALTER TABLE decision_prior_tags ADD COLUMN survival_ref_note TEXT"
        )
        prior_conn.commit()


def open_main_ro(path: str) -> sqlite3.Connection:
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def fetch_decisions(main: sqlite3.Connection, since: Optional[str]) -> list[sqlite3.Row]:
    if since:
        return main.execute(
            """
            SELECT id, ts, chain_id, token_address, raw_json
            FROM decision_log
            WHERE ts >= ?
            ORDER BY id
            """,
            (since,),
        ).fetchall()
    return main.execute(
        """
        SELECT id, ts, chain_id, token_address, raw_json
        FROM decision_log
        ORDER BY id
        """
    ).fetchall()


def resolve_pair_created(
    main: sqlite3.Connection,
    chain_id: str,
    token_address: Optional[str],
    pair_address: Optional[str],
) -> Optional[int]:
    """Prefer (chain_id, pair_address); fallback earliest (chain_id, token_address)."""
    if chain_id and pair_address:
        row = main.execute(
            """
            SELECT pair_created_at FROM pairs
            WHERE chain_id = ? AND pair_address = ?
              AND pair_created_at IS NOT NULL
            """,
            (chain_id, pair_address),
        ).fetchone()
        if row and row["pair_created_at"] is not None:
            return int(row["pair_created_at"])
    if chain_id and token_address:
        row = main.execute(
            """
            SELECT pair_created_at FROM pairs
            WHERE chain_id = ? AND token_address = ?
              AND pair_created_at IS NOT NULL
            ORDER BY pair_created_at ASC
            LIMIT 1
            """,
            (chain_id, token_address),
        ).fetchone()
        if row and row["pair_created_at"] is not None:
            return int(row["pair_created_at"])
    return None


def tag_one(
    dec: sqlite3.Row,
    main: sqlite3.Connection,
    params: dict[str, Any],
) -> dict[str, Any]:
    did = str(dec["id"])
    signal_ts = dec["ts"]  # ISO TEXT
    chain_id = dec["chain_id"]
    token_address = dec["token_address"]
    pair_address = None
    try:
        raw = json.loads(dec["raw_json"] or "{}")
        pair_address = raw.get("pair_address")
    except Exception:
        raw = {}

    # pair_created_at: epoch ms；signal_ts: ISO → ms（禁止字符串相减）
    created_ms = resolve_pair_created(main, chain_id or "", token_address, pair_address)
    signal_ms = parse_ts_ms(signal_ts)

    token_age_h: Optional[float] = None
    peak = 0
    if created_ms is not None and signal_ms is not None:
        # token_age_hours = (signal_ts_ms - pair_created_at_ms) / 3_600_000
        delta_ms = signal_ms - created_ms
        token_age_h = delta_ms / 3_600_000.0
        if 0 <= delta_ms <= PEAK_WINDOW_MS:
            peak = 1

    bucket = age_bucket(token_age_h)
    if created_ms is None:
        bucket = "unknown"
        token_age_h = None
        peak = 0

    ref_1h, ref_4h, ref_note = survival_refs_for_bucket(bucket, params)

    return {
        "decision_id": did,
        "chain_id": chain_id,
        "token_address": token_address,
        "signal_ts": signal_ts,
        "age_bucket": bucket,
        "token_age_hours": token_age_h,
        "peak_window": peak,
        "survival_ref_1h": ref_1h,
        "survival_ref_4h": ref_4h,
        "survival_ref_note": ref_note,
    }


UPSERT_SQL = """
INSERT INTO decision_prior_tags (
  decision_id, chain_id, token_address, signal_ts,
  age_bucket, token_age_hours, peak_window,
  survival_ref_1h, survival_ref_4h, survival_ref_note, tagged_at
) VALUES (
  :decision_id, :chain_id, :token_address, :signal_ts,
  :age_bucket, :token_age_hours, :peak_window,
  :survival_ref_1h, :survival_ref_4h, :survival_ref_note, datetime('now')
)
ON CONFLICT(decision_id) DO UPDATE SET
  chain_id = excluded.chain_id,
  token_address = excluded.token_address,
  signal_ts = excluded.signal_ts,
  age_bucket = excluded.age_bucket,
  token_age_hours = excluded.token_age_hours,
  peak_window = excluded.peak_window,
  survival_ref_1h = excluded.survival_ref_1h,
  survival_ref_4h = excluded.survival_ref_4h,
  survival_ref_note = excluded.survival_ref_note,
  tagged_at = datetime('now')
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="PRIOR_STEP5 prior_tagger（旁路打标）")
    ap.add_argument(
        "--since",
        default=None,
        help="可选 ISO 下界，过滤 decision_log.ts >= since（默认全部）",
    )
    ap.add_argument(
        "--main-db",
        default=MAIN_DB,
        help=f"主库路径（只读，默认 {MAIN_DB}）",
    )
    ap.add_argument(
        "--prior-db",
        default=PRIOR_DB,
        help=f"prior.db 路径（只写，默认 {PRIOR_DB}）",
    )
    args = ap.parse_args()

    if not os.path.isfile(args.main_db):
        print(f"ERROR: main db missing: {args.main_db}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(args.prior_db), exist_ok=True)

    params, surv_note = load_baseline_params()
    print(f"survival_ref source: {surv_note}")
    print(f"peak_window_min={PEAK_WINDOW_MIN} (TUNABLE；GMGN P50×3 暂定)")
    print(f"main_db(ro)={os.path.abspath(args.main_db)}")
    print(f"prior_db(rw)={os.path.abspath(args.prior_db)}")
    if args.since:
        print(f"since={args.since}")

    main_con = open_main_ro(args.main_db)
    prior = sqlite3.connect(args.prior_db)
    try:
        ensure_prior_schema(prior)
        decisions = fetch_decisions(main_con, args.since)
        print(f"decision_log rows to tag: {len(decisions)}")

        rows: list[dict[str, Any]] = []
        for dec in decisions:
            rows.append(tag_one(dec, main_con, params))

        prior.executemany(UPSERT_SQL, rows)
        prior.commit()

        counts = Counter(r["age_bucket"] for r in rows)
        peak_n = sum(1 for r in rows if r["peak_window"] == 1)
        n_pk = prior.execute("SELECT COUNT(*) FROM decision_prior_tags").fetchone()[0]
        print("--- summary ---")
        print(f"n_tagged={len(rows)}  prior.db PK count={n_pk}")
        print(f"peak_window=1: {peak_n}")
        for label in ["<1h", "1-24h", "1-7d", ">7d", "unknown"]:
            print(f"  {label}: {counts.get(label, 0)}")
        for k, v in sorted(counts.items()):
            if k not in ("<1h", "1-24h", "1-7d", ">7d", "unknown"):
                print(f"  {k}: {v}")

        # sample survival_ref pattern per bucket
        print("--- survival_ref sample by age_bucket ---")
        for lab in ["<1h", "1-24h", "1-7d", ">7d", "unknown"]:
            samp = next((r for r in rows if r["age_bucket"] == lab), None)
            if not samp:
                print(f"  {lab}: (empty)")
                continue
            print(
                f"  {lab}: ref_1h={samp['survival_ref_1h']!r} "
                f"ref_4h={samp['survival_ref_4h']!r} "
                f"note={samp['survival_ref_note']!r} "
                f"id={samp['decision_id']}"
            )

        now_hk = datetime.now(HK).strftime("%Y-%m-%d %H:%M:%S HKT")
        print(f"done @ {now_hk}")
    finally:
        main_con.close()
        prior.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
