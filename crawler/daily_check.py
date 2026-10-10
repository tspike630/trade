#!/usr/bin/env python3
"""
Daily QA checklist for dexscreener-crawler (stabilize checklist).

  python daily_check.py

Exit 0 = no FAIL; 1 = any FAIL.
Also writes logs/daily_check-YYYYMMDD.json (Asia/Hong_Kong calendar day).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    ALERT_MIN_SCORE,
    BACKUP_DIR,
    DB_PATH,
    ENABLED_CHAINS,
    HEARTBEAT_STALE_SEC,
    HISTORY_HOT_INTERVAL_SEC,
    LOG_DIR,
    LOG_FILE,
)
from heartbeat import check_health  # noqa: E402
from store import Store  # noqa: E402

# HK calendar day for report naming (UTC+8)
_HK = timezone(timedelta(hours=8))

HOT_TARGET = HISTORY_HOT_INTERVAL_SEC  # default 300
HOT_TOLERANCE = 30  # ±30s
ERROR_LOG_TARGET = 3  # ≤3 ERRORs / 24h


def _now_hk() -> datetime:
    return datetime.now(_HK)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def _status_line(level: str, title: str, detail: str) -> str:
    return f"[{level}] {title}: {detail}"


class CheckReport:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.lines: list[str] = []

    def add(self, name: str, level: str, detail: str, metrics: dict[str, Any] | None = None) -> None:
        self.checks.append(
            {"name": name, "level": level, "detail": detail, "metrics": metrics or {}}
        )
        self.lines.append(_status_line(level, name, detail))

    @property
    def has_fail(self) -> bool:
        return any(c["level"] == "FAIL" for c in self.checks)

    @property
    def summary_counts(self) -> dict[str, int]:
        c = Counter(x["level"] for x in self.checks)
        return {"PASS": c["PASS"], "WARN": c["WARN"], "FAIL": c["FAIL"]}


def check_history(store: Store, report: CheckReport, db_path: Path) -> None:
    """1) Hot pair_history adjacent deltas ~5min ±30s; row count / db size."""
    total = store.count_pair_history()
    db_size = db_path.stat().st_size if db_path.is_file() else 0

    # Prefer hot-tier pairs with ≥2 rows
    rows = store._conn.execute(
        """
        SELECT chain_id, pair_address, COUNT(*) AS n,
               MIN(recorded_at) AS first_at, MAX(recorded_at) AS last_at
        FROM pair_history
        GROUP BY chain_id, pair_address
        HAVING n >= 2
        ORDER BY n DESC
        LIMIT 20
        """
    ).fetchall()

    deltas: list[float] = []
    hot_deltas: list[float] = []
    sample_pair = None

    # Also pull hot-labeled consecutive points
    for r in rows:
        hist = store.list_pair_history(r["chain_id"], r["pair_address"], limit=50)
        if len(hist) < 2:
            continue
        if sample_pair is None:
            sample_pair = (r["chain_id"], r["pair_address"], len(hist))
        for a, b in zip(hist, hist[1:]):
            ta, tb = _parse_iso(a["recorded_at"]), _parse_iso(b["recorded_at"])
            if not ta or not tb:
                continue
            dsec = (tb - ta).total_seconds()
            if dsec <= 0:
                continue
            deltas.append(dsec)
            tier = (b.get("tier") or a.get("tier") or "").lower()
            if tier == "hot" or dsec <= HOT_TARGET + 120:
                hot_deltas.append(dsec)

    # Single-row pairs: growth baseline only
    pairs_with_hist = store._conn.execute(
        "SELECT COUNT(DISTINCT chain_id || '|' || pair_address) FROM pair_history"
    ).fetchone()[0]

    metrics = {
        "pair_history_rows": total,
        "pairs_with_history": int(pairs_with_hist),
        "pairs_with_2plus": len(rows),
        "db_size_bytes": db_size,
        "db_size_human": _fmt_bytes(db_size),
        "hot_target_sec": HOT_TARGET,
        "tolerance_sec": HOT_TOLERANCE,
        "sample_deltas_sec": [round(x, 1) for x in (hot_deltas or deltas)[:10]],
        "sample_pair": sample_pair,
    }

    if total == 0:
        report.add(
            "History 历史序列",
            "FAIL",
            f"pair_history 为空 / empty — 尚无快照; db={_fmt_bytes(db_size)}",
            metrics,
        )
        return

    if len(hot_deltas) < 1 and len(deltas) < 1:
        report.add(
            "History 历史序列",
            "WARN",
            f"有 {total} 行但尚无相邻间隔样本 (多数 pair 仅 1 点) / "
            f"{total} rows but no adjacent deltas yet; "
            f"pairs={pairs_with_hist} db={_fmt_bytes(db_size)}. "
            f"跑 --loop 热层后再验 ~{HOT_TARGET}s±{HOT_TOLERANCE}s",
            metrics,
        )
        return

    use = hot_deltas if hot_deltas else deltas
    in_band = sum(1 for d in use if abs(d - HOT_TARGET) <= HOT_TOLERANCE)
    # Also accept multiples of warm if mixed — for hot check use median
    use_sorted = sorted(use)
    median = use_sorted[len(use_sorted) // 2]
    ratio_ok = in_band / len(use) if use else 0

    # If median is clearly warm (~1800), WARN that hot cadence not observed
    if abs(median - HOT_TARGET) <= HOT_TOLERANCE and ratio_ok >= 0.5:
        level = "PASS"
        detail = (
            f"hot 间隔中位 {median:.0f}s (目标 {HOT_TARGET}±{HOT_TOLERANCE}); "
            f"in_band={in_band}/{len(use)}; rows={total} db={_fmt_bytes(db_size)}"
        )
    elif abs(median - HOT_TARGET) <= HOT_TOLERANCE * 3:
        level = "WARN"
        detail = (
            f"hot 间隔中位 {median:.0f}s 接近目标 {HOT_TARGET}s 但偏差偏大 "
            f"(±{HOT_TOLERANCE}s 内 {in_band}/{len(use)}); rows={total} db={_fmt_bytes(db_size)}"
        )
    elif len(use) >= 1 and median > HOT_TARGET * 2:
        level = "WARN"
        detail = (
            f"相邻间隔中位 {median:.0f}s (可能 warm/cold 或单次 enrich); "
            f"hot 目标 {HOT_TARGET}s±{HOT_TOLERANCE}s 样本不足; "
            f"rows={total} (+growth) db={_fmt_bytes(db_size)}"
        )
    else:
        level = "WARN"
        detail = (
            f"间隔中位 {median:.0f}s vs 目标 {HOT_TARGET}s; "
            f"samples={len(use)} rows={total} db={_fmt_bytes(db_size)}"
        )

    metrics["median_delta_sec"] = round(median, 1)
    metrics["in_band"] = in_band
    metrics["delta_samples"] = len(use)
    report.add("History 历史序列", level, detail, metrics)


def check_scoring(store: Store, report: CheckReport, log_path: Path) -> None:
    """2) Top-10 scores; suppressed-by-min-score today; distribution."""
    rows = store._conn.execute(
        """
        SELECT chain_id, token_address, quality_score, quality_tags
        FROM tokens
        WHERE quality_score IS NOT NULL
        ORDER BY quality_score DESC
        LIMIT 10
        """
    ).fetchall()
    all_scores = [
        float(r[0])
        for r in store._conn.execute(
            "SELECT quality_score FROM tokens WHERE quality_score IS NOT NULL"
        ).fetchall()
    ]
    scored_n = len(all_scores)
    low_n = sum(1 for s in all_scores if s < ALERT_MIN_SCORE)

    # Distribution buckets
    buckets = {"0-24": 0, "25-49": 0, "50-74": 0, "75-100": 0}
    for s in all_scores:
        if s < 25:
            buckets["0-24"] += 1
        elif s < 50:
            buckets["25-49"] += 1
        elif s < 75:
            buckets["50-74"] += 1
        else:
            buckets["75-100"] += 1

    top10 = [
        {
            "chain": r["chain_id"],
            "token": r["token_address"][:12],
            "score": r["quality_score"],
            "tags": r["quality_tags"],
        }
        for r in rows
    ]

    # Suppressed pushes today from log (trackable)
    today_hk = _now_hk().strftime("%Y-%m-%d")
    suppressed = 0
    if log_path.is_file():
        # log lines like: alert push skipped low_score=...
        pat = re.compile(r"alert push skipped low_score=", re.I)
        # Also match crawler timestamps; count lines from last 24h below in stability —
        # for "today" use HK date string if present, else count all matching in 24h window
        cutoff = _utc_now() - timedelta(hours=24)
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not pat.search(line):
                continue
            # try parse leading ISO-like timestamp
            m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", line)
            if m:
                ts = _parse_iso(m.group(1) + "Z") if "Z" not in m.group(1) else _parse_iso(m.group(1))
                # logging_setup may use local offset
                m2 = re.match(
                    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{4})?",
                    line,
                )
                ts = None
                if m2:
                    raw = m2.group(1)
                    off = m2.group(2)
                    try:
                        if off:
                            # +0000 style → +00:00
                            off_fmt = off[:3] + ":" + off[3:]
                            ts = datetime.fromisoformat(raw + off_fmt)
                        else:
                            ts = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                    except ValueError:
                        ts = None
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts and ts < cutoff:
                    continue
            suppressed += 1

    metrics = {
        "scored_tokens": scored_n,
        "below_min_score": low_n,
        "alert_min_score": ALERT_MIN_SCORE,
        "distribution": buckets,
        "top10": top10,
        "suppressed_push_24h": suppressed,
        "today_hk": today_hk,
    }

    if scored_n == 0:
        report.add(
            "Scoring 质量分",
            "FAIL",
            "无 quality_score / no scored tokens — 先跑 enrich",
            metrics,
        )
        return

    avg = sum(all_scores) / scored_n
    top_s = ", ".join(f"{t['score']}" for t in top10[:5])
    level = "PASS"
    if low_n == scored_n:
        level = "WARN"
    detail = (
        f"已评分 {scored_n} avg={avg:.1f}; top5=[{top_s}]; "
        f"<{ALERT_MIN_SCORE}: {low_n}; 分布 {buckets}; "
        f"日志跳过推送(24h) suppressed_push={suppressed}"
    )
    report.add("Scoring 质量分", level, detail, metrics)


def check_multichain(store: Store, report: CheckReport) -> None:
    """3) Token/pair counts by chain."""
    tok = store._conn.execute(
        "SELECT chain_id, COUNT(*) AS n FROM tokens GROUP BY chain_id ORDER BY n DESC"
    ).fetchall()
    pair = store._conn.execute(
        "SELECT chain_id, COUNT(*) AS n FROM pairs GROUP BY chain_id ORDER BY n DESC"
    ).fetchall()
    by_tok = {r["chain_id"]: r["n"] for r in tok}
    by_pair = {r["chain_id"]: r["n"] for r in pair}
    chains = sorted(set(by_tok) | set(by_pair), key=lambda c: -(by_tok.get(c, 0)))
    enabled = {c.lower() for c in ENABLED_CHAINS}
    unexpected = [c for c in chains if c not in enabled]

    parts = [f"{c}: tokens={by_tok.get(c, 0)} pairs={by_pair.get(c, 0)}" for c in chains]
    metrics = {
        "by_tokens": by_tok,
        "by_pairs": by_pair,
        "enabled_chains": list(ENABLED_CHAINS),
        "unexpected_chains": unexpected,
    }
    if not chains:
        report.add("Multi-chain 多链", "FAIL", "库中无 token/pair", metrics)
        return
    level = "PASS"
    detail = "; ".join(parts)
    if unexpected:
        level = "WARN"
        detail += f" | 非 ENABLED_CHAINS 残留: {unexpected} (pending/历史数据可能)"
    detail += f" | ENABLED={ENABLED_CHAINS}"
    report.add("Multi-chain 多链", level, detail, metrics)


def check_stability(
    report: CheckReport,
    *,
    log_path: Path,
    backup_dir: Path,
    stale_sec: int,
) -> None:
    """4) Heartbeat, backup age, ERROR lines last 24h."""
    ok, msg, hb = check_health(stale_sec=stale_sec)
    hb_metrics: dict[str, Any] = {
        "health_ok": ok,
        "message": msg,
        "heartbeat": hb,
        "stale_sec": stale_sec,
    }
    if ok:
        report.add(
            "Heartbeat 心跳",
            "PASS",
            f"fresh — {msg}; status={hb.get('status') if hb else '-'} "
            f"pid={hb.get('pid') if hb else '-'}",
            hb_metrics,
        )
    elif hb is None:
        report.add("Heartbeat 心跳", "FAIL", f"missing — {msg}", hb_metrics)
    else:
        # stale while crawler not running is WARN for daily QA if file exists
        age_note = msg
        report.add(
            "Heartbeat 心跳",
            "WARN" if hb.get("status") == "ok" else "FAIL",
            f"stale/unhealthy — {age_note} (若未 --loop 属预期 WARN)",
            hb_metrics,
        )

    # Backups
    newest = None
    newest_age_h = None
    if backup_dir.is_dir():
        files = sorted(
            backup_dir.glob("dexscreener-*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if files:
            newest = files[0]
            newest_age_h = (_utc_now().timestamp() - newest.stat().st_mtime) / 3600.0
    bmetrics = {
        "backup_dir": str(backup_dir),
        "newest": newest.name if newest else None,
        "newest_age_hours": round(newest_age_h, 2) if newest_age_h is not None else None,
    }
    if newest is None:
        report.add(
            "Backup 备份",
            "WARN",
            f"无备份文件 / no backups in {backup_dir} — 运行 python backup.py",
            bmetrics,
        )
    elif newest_age_h is not None and newest_age_h <= 36:
        report.add(
            "Backup 备份",
            "PASS",
            f"最新 {newest.name} age={newest_age_h:.1f}h",
            bmetrics,
        )
    elif newest_age_h is not None and newest_age_h <= 72:
        report.add(
            "Backup 备份",
            "WARN",
            f"最新 {newest.name} age={newest_age_h:.1f}h (>36h)",
            bmetrics,
        )
    else:
        report.add(
            "Backup 备份",
            "FAIL",
            f"最新备份过旧 age={newest_age_h:.1f}h — {newest.name if newest else '-'}",
            bmetrics,
        )

    # ERROR lines last 24h
    cutoff = _utc_now() - timedelta(hours=24)
    err_count = 0
    err_samples: list[str] = []
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if " ERROR " not in line and "\tERROR\t" not in line and " ERROR [" not in line:
                # logging format: ... ERROR [logger]
                if " ERROR " not in f" {line} " and not re.search(r"\bERROR\b", line):
                    continue
            if not re.search(r"\bERROR\b", line):
                continue
            m2 = re.match(
                r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{4})?",
                line,
            )
            if m2:
                raw, off = m2.group(1), m2.group(2)
                try:
                    if off:
                        ts = datetime.fromisoformat(raw + off[:3] + ":" + off[3:])
                    else:
                        ts = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts and ts < cutoff:
                    continue
            err_count += 1
            if len(err_samples) < 5:
                err_samples.append(line[:180])
    emetrics = {
        "error_lines_24h": err_count,
        "target_max": ERROR_LOG_TARGET,
        "samples": err_samples,
        "log_path": str(log_path),
    }
    if not log_path.is_file():
        report.add("Logs ERROR", "WARN", f"日志不存在 {log_path}", emetrics)
    elif err_count <= ERROR_LOG_TARGET:
        report.add(
            "Logs ERROR",
            "PASS",
            f"近24h ERROR 行数={err_count} (目标≤{ERROR_LOG_TARGET})",
            emetrics,
        )
    elif err_count <= ERROR_LOG_TARGET * 3:
        report.add(
            "Logs ERROR",
            "WARN",
            f"近24h ERROR 行数={err_count} > 目标{ERROR_LOG_TARGET}",
            emetrics,
        )
    else:
        report.add(
            "Logs ERROR",
            "FAIL",
            f"近24h ERROR 行数={err_count} 远超目标≤{ERROR_LOG_TARGET}",
            emetrics,
        )


def check_resources(report: CheckReport, db_path: Path) -> None:
    """5) DB size + optional crawler RSS."""
    size = db_path.stat().st_size if db_path.is_file() else 0
    rss_mb = None
    pid = None
    # best-effort: heartbeat pid if process still alive
    try:
        from heartbeat import read_heartbeat

        hb = read_heartbeat()
        if hb and hb.get("pid"):
            pid = int(hb["pid"])
            proc = Path(f"/proc/{pid}/status")
            if proc.is_file():
                for line in proc.read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        # kB
                        kb = int(line.split()[1])
                        rss_mb = round(kb / 1024.0, 1)
                        break
    except Exception:  # noqa: BLE001
        pass

    metrics = {
        "db_path": str(db_path),
        "db_size_bytes": size,
        "db_size_human": _fmt_bytes(size),
        "crawler_pid": pid,
        "rss_mb": rss_mb,
    }
    detail = f"DB {_fmt_bytes(size)} ({db_path})"
    if rss_mb is not None:
        detail += f"; crawler pid={pid} RSS={rss_mb} MB"
    elif pid is not None:
        detail += f"; heartbeat pid={pid} 进程已不在 (RSS n/a)"
    else:
        detail += "; RSS n/a (无存活 crawler PID)"
    level = "PASS"
    if size > 2 * 1024 * 1024 * 1024:  # >2GB
        level = "WARN"
        detail += " — DB 较大，注意 retention"
    if not db_path.is_file():
        level = "FAIL"
        detail = f"DB 文件缺失 {db_path}"
    report.add("Resources 资源", level, detail, metrics)



def check_indicators_p0(report: CheckReport, db_path: Path) -> None:
    """P1 light: process presence hints + jev_signals + token_indicators freshness."""
    import sqlite3

    # --- process presence (hints only; do not kill anything) ---
    patterns = {
        "main.py": "main.py",
        "indicators.run": "indicators.run",
        "sources.jev": "sources.jev",
    }
    found: dict[str, list[int]] = {k: [] for k in patterns}
    try:
        # Prefer pgrep if available
        import subprocess

        for label, pat in patterns.items():
            try:
                out = subprocess.check_output(
                    ["pgrep", "-f", pat], text=True, stderr=subprocess.DEVNULL
                )
                pids = [int(x) for x in out.split() if x.strip().isdigit()]
                found[label] = pids
            except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
                found[label] = []
    except Exception:  # noqa: BLE001
        pass

    present = {k: v for k, v in found.items() if v}
    missing = [k for k, v in found.items() if not v]
    proc_metrics = {
        "pids": {k: v for k, v in found.items()},
        "present_count": len(present),
        "expected": list(patterns),
    }
    if len(present) >= 3:
        report.add(
            "Indicators procs 三进程",
            "PASS",
            "main + indicators.run + sources.jev 均可见: "
            + ", ".join(f"{k}={v}" for k, v in found.items()),
            proc_metrics,
        )
    elif len(present) >= 1:
        report.add(
            "Indicators procs 三进程",
            "WARN",
            f"仅见 {list(present)}；缺失 {missing}（未强制 FAIL）",
            proc_metrics,
        )
    else:
        report.add(
            "Indicators procs 三进程",
            "WARN",
            "未检测到 main/indicators/jev 进程（可能未 --loop 或无 pgrep）",
            proc_metrics,
        )

    # --- DB: jev_signals + token_indicators + indicator_history freshness ---
    jev_n = 0
    ind_n = 0
    hist_n = 0
    sent_n = 0
    max_updated = None
    age_sec = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 3000")
        try:
            jev_n = int(conn.execute("SELECT COUNT(*) FROM jev_signals").fetchone()[0])
        except sqlite3.OperationalError:
            jev_n = 0
        try:
            ind_n = int(
                conn.execute("SELECT COUNT(*) FROM token_indicators").fetchone()[0]
            )
            row = conn.execute(
                "SELECT MAX(updated_at) AS m FROM token_indicators"
            ).fetchone()
            max_updated = row["m"] if row else None
            try:
                sent_n = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM token_indicators "
                        "WHERE sentiment_score IS NOT NULL "
                        "OR sentiment_heat_score IS NOT NULL"
                    ).fetchone()[0]
                )
            except sqlite3.OperationalError:
                sent_n = 0
        except sqlite3.OperationalError:
            ind_n = 0
            max_updated = None
        try:
            hist_n = int(
                conn.execute("SELECT COUNT(*) FROM indicator_history").fetchone()[0]
            )
        except sqlite3.OperationalError:
            hist_n = 0
        conn.close()
    except Exception as exc:  # noqa: BLE001
        report.add(
            "Indicators tables 指标表",
            "WARN",
            f"只读查询失败: {exc}",
            {"error": str(exc)},
        )
        return

    if max_updated:
        dt = _parse_iso(max_updated)
        if dt:
            age_sec = (_utc_now() - dt).total_seconds()

    db_metrics = {
        "jev_signals": jev_n,
        "token_indicators": ind_n,
        "indicator_history": hist_n,
        "sentiment_filled": sent_n,
        "max_updated_at": max_updated,
        "age_sec": round(age_sec, 1) if age_sec is not None else None,
    }

    # JEV coverage: WARN ok for low coverage (not FAIL)
    if jev_n == 0:
        report.add(
            "JEV coverage 资金面覆盖",
            "WARN",
            "jev_signals=0 — 尚未拉过 JEV 或表为空（P0 不 FAIL）",
            db_metrics,
        )
    elif jev_n < 10:
        report.add(
            "JEV coverage 资金面覆盖",
            "WARN",
            f"jev_signals={jev_n} 覆盖偏低（预期随热档增长）",
            db_metrics,
        )
    else:
        report.add(
            "JEV coverage 资金面覆盖",
            "PASS",
            f"jev_signals={jev_n}",
            db_metrics,
        )

    # Freshness of token_indicators
    if ind_n == 0:
        report.add(
            "Indicators freshness 指标新鲜度",
            "WARN",
            "token_indicators 为空 — 先跑 python -m indicators.run --once/--full",
            db_metrics,
        )
    elif age_sec is None:
        report.add(
            "Indicators freshness 指标新鲜度",
            "WARN",
            f"rows={ind_n} 但无法解析 max(updated_at)={max_updated}",
            db_metrics,
        )
    elif age_sec <= 15 * 60:
        report.add(
            "Indicators freshness 指标新鲜度",
            "PASS",
            f"rows={ind_n} max(updated_at)={max_updated} age={age_sec:.0f}s",
            db_metrics,
        )
    elif age_sec <= 60 * 60:
        report.add(
            "Indicators freshness 指标新鲜度",
            "WARN",
            f"rows={ind_n} max(updated_at)={max_updated} age={age_sec/60:.1f}min (>15min)",
            db_metrics,
        )
    else:
        report.add(
            "Indicators freshness 指标新鲜度",
            "WARN",
            f"rows={ind_n} max(updated_at)={max_updated} age={age_sec/3600:.1f}h 偏旧（未 FAIL）",
            db_metrics,
        )

    # Hot archival presence (indicator_history)
    if hist_n == 0:
        report.add(
            "Indicators history 热档归档",
            "WARN",
            "indicator_history=0 — 确认 INDICATORS_WRITE_HISTORY_HOT=1 且有 hot tier",
            db_metrics,
        )
    else:
        report.add(
            "Indicators history 热档归档",
            "PASS",
            f"indicator_history={hist_n}",
            db_metrics,
        )

    # Sentiment fill (informational; 0 is OK when x_watch empty)
    if sent_n == 0:
        report.add(
            "Sentiment coverage 舆情覆盖",
            "WARN",
            "sentiment_* 尚未填充 — 需 x_watch + indicators sentiment pass",
            db_metrics,
        )
    else:
        report.add(
            "Sentiment coverage 舆情覆盖",
            "PASS",
            f"sentiment_filled={sent_n}/{ind_n}",
            db_metrics,
        )



def check_execution(report: CheckReport) -> None:
    """Execution gateway: --loop process alive + today's execution_log (ledger only)."""
    import sqlite3
    import subprocess

    # --- a) process presence (hints only; do not kill anything) ---
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"python -m execution\.run --loop"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        pids = [int(x) for x in out.split() if x.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        # Fallback: broader pattern (still require --loop in /proc cmdline)
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", "execution.run"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            for raw in out.split():
                if not raw.strip().isdigit():
                    continue
                pid = int(raw)
                try:
                    cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                        "utf-8", errors="replace"
                    )
                except OSError:
                    continue
                if "execution.run" in cmd and "--loop" in cmd:
                    pids.append(pid)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pids = []

    # de-dupe preserve order
    seen: set[int] = set()
    uniq: list[int] = []
    for p in pids:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    pids = uniq

    proc_metrics: dict[str, Any] = {
        "pids": pids,
        "pidfile": str(Path(LOG_DIR) / "execution_loop.pid"),
        "pattern": "python -m execution.run --loop",
    }
    if len(pids) == 1:
        report.add(
            "Execution proc 执行进程",
            "PASS",
            f"execution.run --loop 存活 pid={pids[0]}",
            proc_metrics,
        )
    elif len(pids) > 1:
        report.add(
            "Execution proc 执行进程",
            "WARN",
            f"检测到多个 --loop 实例 pids={pids}（应只保留一个）",
            proc_metrics,
        )
    else:
        report.add(
            "Execution proc 执行进程",
            "WARN",
            "未检测到 execution.run --loop（可能未常驻；见 logs/execution_loop.pid）",
            proc_metrics,
        )

    # --- b) today's execution_log from independent ledger (never write main DB) ---
    try:
        from execution.ledger import resolve_ledger_path

        ledger_path = resolve_ledger_path()
    except Exception:  # noqa: BLE001
        ledger_path = (ROOT / "execution_ledger.db").resolve()

    today_hk = _now_hk().strftime("%Y-%m-%d")
    start_hk = _now_hk().replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_hk.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ledger_metrics: dict[str, Any] = {
        "ledger_path": str(ledger_path),
        "today_hk": today_hk,
        "since_utc": start_utc,
        "signals": 0,
        "validation_ok": 0,
        "validation_fail": 0,
        "pass_rate": None,
        "by_strategy_id": {},
    }

    if not ledger_path.is_file():
        report.add(
            "Execution ledger 今日台账",
            "WARN",
            f"ledger 不存在: {ledger_path} — 先跑 python -m execution.run --once/--loop",
            ledger_metrics,
        )
        return

    try:
        conn = sqlite3.connect(f"file:{ledger_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 3000")
        # ts stored as UTC ISO (...Z); HK calendar day → UTC lower bound
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE ts >= ?",
                (start_utc,),
            ).fetchone()[0]
        )
        ok_n = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE ts >= ? AND validation_ok = 1",
                (start_utc,),
            ).fetchone()[0]
        )
        fail_n = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE ts >= ? AND validation_ok = 0",
                (start_utc,),
            ).fetchone()[0]
        )
        by_rows = conn.execute(
            """
            SELECT strategy_id, COUNT(*) AS n,
                   SUM(CASE WHEN validation_ok = 1 THEN 1 ELSE 0 END) AS ok_n
            FROM execution_log
            WHERE ts >= ?
            GROUP BY strategy_id
            ORDER BY n DESC
            """,
            (start_utc,),
        ).fetchall()
        by_strategy = {
            str(r["strategy_id"]): {"n": int(r["n"]), "ok": int(r["ok_n"] or 0)}
            for r in by_rows
        }
        conn.close()
    except Exception as exc:  # noqa: BLE001
        report.add(
            "Execution ledger 今日台账",
            "WARN",
            f"只读查询失败: {exc}",
            {**ledger_metrics, "error": str(exc)},
        )
        return

    pass_rate = (ok_n / total) if total else None
    ledger_metrics.update(
        {
            "signals": total,
            "validation_ok": ok_n,
            "validation_fail": fail_n,
            "pass_rate": round(pass_rate, 4) if pass_rate is not None else None,
            "by_strategy_id": by_strategy,
        }
    )

    dist = (
        ", ".join(
            f"{sid}: n={v['n']} ok={v['ok']}" for sid, v in by_strategy.items()
        )
        or "(none)"
    )
    if total == 0:
        report.add(
            "Execution ledger 今日台账",
            "WARN",
            f"今日(HK) execution_log=0 since={start_utc} ledger={ledger_path.name}",
            ledger_metrics,
        )
    else:
        rate_pct = 100.0 * pass_rate if pass_rate is not None else 0.0
        level = "PASS" if fail_n == 0 else "WARN"
        report.add(
            "Execution ledger 今日台账",
            level,
            (
                f"今日信号={total} 校验通过={ok_n}/{total} ({rate_pct:.1f}%) "
                f"fail={fail_n}; by_strategy=[{dist}]"
            ),
            ledger_metrics,
        )



def check_position_overview(report: CheckReport) -> None:
    """Step C: paper open-position overview (read-only; does not require PM_ENABLED)."""
    import os

    paper_db = (
        os.environ.get("EXECUTION_PAPER_DB", "").strip()
        or os.environ.get("PAPER_DB_PATH", "").strip()
        or os.environ.get("PM_PAPER_DB", "").strip()
        or str(ROOT / "paper_trading.db")
    )
    path = Path(paper_db)
    metrics: dict[str, Any] = {
        "paper_db": str(path),
        "pm_enabled": os.environ.get("PM_ENABLED", "0"),
        "exists": path.is_file(),
    }
    if not path.is_file():
        report.add(
            "Position overview 持仓概览",
            "WARN",
            f"paper DB 不存在: {path.name}（概览跳过；PM 默认关）",
            metrics,
        )
        return
    try:
        from execution.position_manager import PositionManager, PositionManagerConfig

        cfg = PositionManagerConfig.from_env()
        with PositionManager(paper_db=path, config=cfg) as pm:
            snap = pm.snapshot()
    except Exception as exc:  # noqa: BLE001
        report.add(
            "Position overview 持仓概览",
            "WARN",
            f"读取失败: {exc}",
            {**metrics, "error": str(exc)},
        )
        return

    metrics.update(
        {
            "open_count": snap.get("open_count"),
            "equity_usd": snap.get("equity_usd"),
            "cash_usd": snap.get("cash_usd"),
            "total_size_pct": snap.get("total_size_pct"),
            "by_strategy": snap.get("by_strategy") or {},
            "by_token_n": len(snap.get("by_token") or {}),
            "rules": snap.get("rules") or {},
        }
    )
    by_s = snap.get("by_strategy") or {}
    dist = (
        ", ".join(
            f"{sid}: n={v.get('count')} pct={float(v.get('size_pct') or 0):.2f}"
            for sid, v in by_s.items()
        )
        or "(none)"
    )
    report.add(
        "Position overview 持仓概览",
        "PASS",
        (
            f"open={snap.get('open_count')} equity={snap.get('equity_usd')} "
            f"total_pct={float(snap.get('total_size_pct') or 0):.2f} "
            f"tokens={len(snap.get('by_token') or {})} "
            f"PM_ENABLED={metrics['pm_enabled']}; by_strategy=[{dist}]"
        ),
        metrics,
    )



def run_checks(*, db: str, stale_sec: int | None) -> CheckReport:
    report = CheckReport()
    db_path = Path(db)
    log_path = Path(LOG_DIR) / LOG_FILE
    backup_dir = Path(BACKUP_DIR)
    stale = HEARTBEAT_STALE_SEC if stale_sec is None else stale_sec

    print("=" * 60)
    print("DexScreener Crawler — Daily QA Check / 每日巡检")
    print(f"time_hk={_now_hk().strftime('%Y-%m-%d %H:%M:%S')} HKT")
    print(f"db={db_path}  ALERT_MIN_SCORE={ALERT_MIN_SCORE}  HOT_INTERVAL={HOT_TARGET}s")
    print("=" * 60)

    if not db_path.is_file():
        report.add("DB", "FAIL", f"数据库不存在: {db_path}", {})
        return report

    with Store(db_path) as store:
        check_history(store, report, db_path)
        check_scoring(store, report, log_path)
        check_multichain(store, report)

    check_stability(report, log_path=log_path, backup_dir=backup_dir, stale_sec=stale)
    check_resources(report, db_path)
    check_indicators_p0(report, db_path)
    check_execution(report)
    check_position_overview(report)

    print()
    for line in report.lines:
        print(line)
    counts = report.summary_counts
    print("-" * 60)
    print(
        f"SUMMARY 汇总: PASS={counts['PASS']} WARN={counts['WARN']} FAIL={counts['FAIL']} "
        f"→ exit={'1' if report.has_fail else '0'}"
    )
    return report


def write_json(report: CheckReport, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    day = _now_hk().strftime("%Y%m%d")
    path = out_dir / f"daily_check-{day}.json"
    payload = {
        "generated_at_hk": _now_hk().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "generated_at_utc": _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": report.summary_counts,
        "exit_code": 1 if report.has_fail else 0,
        "checks": report.checks,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily QA checklist for dexscreener-crawler")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument(
        "--health-stale-sec",
        type=int,
        default=None,
        help=f"Heartbeat stale window (default {HEARTBEAT_STALE_SEC})",
    )
    parser.add_argument(
        "--json-dir",
        default=str(Path(LOG_DIR)),
        help="Directory for daily_check-YYYYMMDD.json",
    )
    args = parser.parse_args(argv)

    # Ensure CWD-friendly relative paths when launched from elsewhere
    os.chdir(ROOT)

    report = run_checks(db=args.db, stale_sec=args.health_stale_sec)
    out = write_json(report, Path(args.json_dir))
    print(f"JSON → {out}")
    return 1 if report.has_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
