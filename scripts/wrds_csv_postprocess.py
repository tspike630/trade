#!/usr/bin/env python3
"""WRDS 网页导出 CSV 的后处理与质检。

用法:
  python wrds_csv_postprocess.py check  <csv> [--days 10,30,60,91,182]
  python wrds_csv_postprocess.py filter <csv> <out.csv> --days 10,30,60,91,182
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd


def check(path: Path, days: list[int] | None = None) -> int:
    df = pd.read_csv(path, low_memory=False)
    print(f"file      : {path.name}")
    print(f"size      : {path.stat().st_size / 1_048_576:.1f} MiB")
    print(f"rows      : {len(df):,}")
    print(f"cols({len(df.columns):2d}) : {', '.join(map(str, df.columns))}")

    date_cols = [c for c in df.columns if str(c).lower() in {"date", "datadate", "trd_exctn_dt", "fdate", "timestamp_utc"}]
    for c in date_cols:
        s = pd.to_datetime(df[c], errors="coerce")
        if s.notna().any():
            print(f"date range: {c} {s.min()} .. {s.max()}")

    for c in ("days", "days_to_expiration"):
        if c in df.columns and days:
            present = sorted(pd.to_numeric(df[c], errors="coerce").dropna().unique())
            missing = [d for d in days if d not in set(present)]
            print(f"days {c} : present={present[:12]} missing={missing}")

    null_cols = [c for c in df.columns if df[c].isna().all()]
    if null_cols:
        print(f"all-null  : {null_cols}")
    return 0 if len(df) else 1


def filter_days(src: Path, out: Path, days: list[int]) -> int:
    df = pd.read_csv(src, low_memory=False)
    col = next((c for c in ("days", "days_to_expiration") if c in df.columns), None)
    if col is None:
        print(f"[ERR] 找不到 days 列，实际列: {list(df.columns)}", file=sys.stderr)
        return 2
    before = len(df)
    df = df[df[col].isin(days)]
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"filtered : {before:,} -> {len(df):,} rows (days in {days})")
    print(f"written  : {out} ({out.stat().st_size / 1_048_576:.1f} MiB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "filter"])
    ap.add_argument("path", type=Path)
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--days", type=str, default="")
    a = ap.parse_args(argv)
    days = [int(x) for x in a.days.split(",") if x.strip()] if a.days else None

    if a.cmd == "check":
        return check(a.path, days)
    if not a.out:
        ap.error("filter 需要输出路径")
    if not days:
        ap.error("filter 需要 --days")
    return filter_days(a.path, a.out, days)


if __name__ == "__main__":
    raise SystemExit(main())
