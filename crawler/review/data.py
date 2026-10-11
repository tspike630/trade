"""Cross-DB read-only aggregation: signal chain, forward returns, market baseline."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

STRENGTH_RE = re.compile(r"jev_strength=([0-9.]+)")


@dataclass
class DbPaths:
    main: str = "dexscreener.db"
    execution: str = "execution_ledger.db"
    paper: str = "paper_trading.db"
    backtest: str = "backtest_ledger.db"  # R1 optional; not in main report


@dataclass
class SignalChainRow:
    # 信号
    decision_id: int
    ts: str
    strategy_id: str
    action: str
    chain_id: str
    token_address: str
    pair_address: Optional[str]
    size_pct: Optional[float]
    reason: Optional[str]
    risk_passed: Optional[int]
    mode: Optional[str]
    raw: dict[str, Any]
    # 信号特征
    jev_strength: Optional[float]
    # 执行
    exec_id: Optional[int]
    exec_ts: Optional[str]
    validation_ok: Optional[int]
    validation_errors: Optional[str]
    executed: Optional[int]
    executor: Optional[str]
    receipt: dict[str, Any]
    # 成交（开闸前全 None）
    position_id: Optional[int]
    fill_side: Optional[str]
    fill_price: Optional[float]
    pnl_usd: Optional[float]
    # 前向（信号层）
    fwd_1h: Optional[float] = None
    fwd_4h: Optional[float] = None
    price_at_signal: Optional[float] = None
    # 信号特征补充（周检过滤口径用；缺则 None）
    liquidity_at_signal: Optional[float] = None
    jev_coverage: Optional[float] = None
    # 内部：同一 decision 多条 execution 时标记
    exec_dup: bool = False


def connect_ro(path: str) -> sqlite3.Connection:
    """强制只读连接。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"db not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_ts(s: str) -> datetime:
    s = (s or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _parse_json(text: Optional[str]) -> dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _extract_jev_strength(raw: dict[str, Any], reason: Optional[str]) -> Optional[float]:
    extras = raw.get("extras")
    if isinstance(extras, dict):
        v = safe_float(extras.get("jev_strength"))
        if v is not None:
            return v
    v = safe_float(raw.get("jev_strength"))
    if v is not None:
        return v
    m = STRENGTH_RE.search(reason or "")
    if m:
        return safe_float(m.group(1))
    return None

def load_signal_chain(
    paths: DbPaths,
    *,
    period_start: Optional[str] = None,  # ISO UTC inclusive
    period_end: Optional[str] = None,  # ISO UTC exclusive
    strategy_id: Optional[str] = None,  # None = 全策略
) -> list[SignalChainRow]:
    """只读：decision LEFT JOIN execution LEFT JOIN paper(meta_json)。

    paper 无匹配时成交字段为 None，不报错。
    同一 decision 多条 execution 时取最新 exec_ts，并标记 exec_dup。
    """
    main = connect_ro(paths.main)
    try:
        sql = """
            SELECT id, ts, strategy_id, action, chain_id, token_address,
                   size_pct, reason, risk_passed, mode, raw_json
            FROM decision_log
            WHERE 1=1
        """
        params: list[Any] = []
        if period_start:
            sql += " AND ts >= ?"
            params.append(period_start)
        if period_end:
            sql += " AND ts < ?"
            params.append(period_end)
        if strategy_id:
            sql += " AND strategy_id = ?"
            params.append(strategy_id)
        sql += " ORDER BY id"
        decisions = main.execute(sql, params).fetchall()
    finally:
        main.close()

    # execution_log：按 decision_log_id 聚合，取最新 ts
    exec_by_dec: dict[int, sqlite3.Row] = {}
    exec_dup_ids: set[int] = set()
    exec_conn = connect_ro(paths.execution)
    try:
        # 若有时间窗，尽量收窄；但用 decision ids 更准
        dec_ids = [int(d["id"]) for d in decisions]
        if not dec_ids:
            exec_rows: list[sqlite3.Row] = []
        else:
            # SQLite 变量上限宽松；40 条一次 IN 足够。大批量时分块。
            exec_rows = []
            chunk = 500
            for i in range(0, len(dec_ids), chunk):
                part = dec_ids[i : i + chunk]
                placeholders = ",".join("?" * len(part))
                exec_rows.extend(
                    exec_conn.execute(
                        f"""
                        SELECT id, ts, decision_log_id, strategy_id, action,
                               chain_id, token_address, pair_address, size_pct,
                               reason, mode, validation_ok, validation_errors,
                               executed, executor, receipt_json
                        FROM execution_log
                        WHERE decision_log_id IN ({placeholders})
                        ORDER BY ts ASC, id ASC
                        """,
                        part,
                    ).fetchall()
                )
        counts: dict[int, int] = defaultdict(int)
        for er in exec_rows:
            did = int(er["decision_log_id"])
            counts[did] += 1
            prev = exec_by_dec.get(did)
            if prev is None:
                exec_by_dec[did] = er
            else:
                # 取最新 exec_ts（同 ts 取更大 id）
                if (er["ts"], er["id"]) >= (prev["ts"], prev["id"]):
                    exec_by_dec[did] = er
        for did, c in counts.items():
            if c > 1:
                exec_dup_ids.add(did)
    finally:
        exec_conn.close()

    # paper positions：meta_json.decision_log_id
    pos_by_dec: dict[int, dict[str, Any]] = {}
    try:
        paper = connect_ro(paths.paper)
        try:
            pos_rows = paper.execute(
                """
                SELECT id, pnl_usd, meta_json, entry_price
                FROM positions
                WHERE json_extract(meta_json, '$.decision_log_id') IS NOT NULL
                """
            ).fetchall()
            for pr in pos_rows:
                meta = _parse_json(pr["meta_json"])
                did = meta.get("decision_log_id")
                try:
                    did_i = int(did)
                except (TypeError, ValueError):
                    continue
                fill = paper.execute(
                    """
                    SELECT side, price FROM fills
                    WHERE position_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (pr["id"],),
                ).fetchone()
                pos_by_dec[did_i] = {
                    "position_id": int(pr["id"]),
                    "pnl_usd": safe_float(pr["pnl_usd"]),
                    "fill_side": fill["side"] if fill else None,
                    "fill_price": (
                        safe_float(fill["price"])
                        if fill
                        else safe_float(pr["entry_price"])
                    ),
                }
        finally:
            paper.close()
    except FileNotFoundError:
        # paper 库缺失时仍允许出报告（成交全 None）
        pass

    rows: list[SignalChainRow] = []
    for d in decisions:
        raw = _parse_json(d["raw_json"])
        reason = d["reason"]
        strength = _extract_jev_strength(raw, reason)
        pair = raw.get("pair_address")
        er = exec_by_dec.get(int(d["id"]))
        if not pair and er is not None:
            pair = er["pair_address"]
        receipt = _parse_json(er["receipt_json"]) if er is not None else {}
        pos = pos_by_dec.get(int(d["id"]), {})
        rows.append(
            SignalChainRow(
                decision_id=int(d["id"]),
                ts=d["ts"],
                strategy_id=d["strategy_id"] or "",
                action=d["action"] or "",
                chain_id=d["chain_id"] or "",
                token_address=d["token_address"] or "",
                pair_address=pair if pair else None,
                size_pct=safe_float(d["size_pct"]),
                reason=reason,
                risk_passed=int(d["risk_passed"]) if d["risk_passed"] is not None else None,
                mode=d["mode"],
                raw=raw,
                jev_strength=strength,
                exec_id=int(er["id"]) if er is not None else None,
                exec_ts=er["ts"] if er is not None else None,
                validation_ok=(
                    int(er["validation_ok"]) if er is not None and er["validation_ok"] is not None else None
                ),
                validation_errors=er["validation_errors"] if er is not None else None,
                executed=(
                    int(er["executed"]) if er is not None and er["executed"] is not None else None
                ),
                executor=er["executor"] if er is not None else None,
                receipt=receipt,
                position_id=pos.get("position_id"),
                fill_side=pos.get("fill_side"),
                fill_price=pos.get("fill_price"),
                pnl_usd=pos.get("pnl_usd"),
                exec_dup=int(d["id"]) in exec_dup_ids,
            )
        )
    return rows


def _nearest_snap(
    conn: sqlite3.Connection,
    chain_id: str,
    pair: str,
    t_iso: str,
    tol_sec: float,
) -> tuple[Optional[float], Optional[float]]:
    """对齐 scripts/jev_validity.nearest_snap：pair_history 就近容差匹配。

    返回 (price_usd, liquidity_usd)；任一缺失为 None。
    """
    row = conn.execute(
        """
        SELECT price_usd, liquidity_usd,
               ABS((julianday(recorded_at) - julianday(?)) * 86400.0) AS dt
        FROM pair_history
        WHERE chain_id = ? AND pair_address = ?
          AND price_usd IS NOT NULL AND price_usd > 0
          AND ABS((julianday(recorded_at) - julianday(?)) * 86400.0) <= ?
        ORDER BY ABS((julianday(recorded_at) - julianday(?)) * 86400.0) ASC
        LIMIT 1
        """,
        (t_iso, chain_id, pair, t_iso, tol_sec, t_iso),
    ).fetchone()
    if not row:
        return None, None
    return float(row["price_usd"]), safe_float(row["liquidity_usd"])


def _liquidity_fallback(r: SignalChainRow) -> Optional[float]:
    """history liq 缺失时回退 raw_json.liquidity.usd（当前快照，与周检一致）。"""
    raw_liq = (r.raw.get("liquidity") or {})
    if isinstance(raw_liq, dict):
        return safe_float(raw_liq.get("usd"))
    return safe_float(raw_liq)


def annotate_forward_returns(
    rows: list[SignalChainRow],
    paths: DbPaths,
    *,
    tol_sec: int = 1800,
) -> list[SignalChainRow]:
    """用 pair_history 标注 fwd_1h / fwd_4h；就近匹配容差默认 30min。"""
    if not rows:
        return rows
    conn = connect_ro(paths.main)
    now = datetime.now(timezone.utc)
    out: list[SignalChainRow] = []
    try:
        for r in rows:
            if not r.pair_address or not r.chain_id:
                out.append(r)
                continue
            try:
                t0 = parse_ts(r.ts)
            except (TypeError, ValueError):
                out.append(r)
                continue
            t0_iso = to_iso_z(t0)
            p0, liq0 = _nearest_snap(conn, r.chain_id, r.pair_address, t0_iso, tol_sec)
            fwd_1h = None
            fwd_4h = None
            if p0 is not None and p0 > 0:
                age = (now - t0).total_seconds()
                for hours, attr in ((1, "1h"), (4, "4h")):
                    if age < hours * 3600 - tol_sec:
                        continue
                    target = to_iso_z(t0 + timedelta(hours=hours))
                    p1, _ = _nearest_snap(
                        conn, r.chain_id, r.pair_address, target, tol_sec
                    )
                    if p1 is None or p1 <= 0:
                        continue
                    ret = (p1 - p0) / p0
                    if attr == "1h":
                        fwd_1h = ret
                    else:
                        fwd_4h = ret
            out.append(
                replace(
                    r,
                    price_at_signal=p0,
                    liquidity_at_signal=(
                        liq0 if liq0 is not None else _liquidity_fallback(r)
                    ),
                    fwd_1h=fwd_1h,
                    fwd_4h=fwd_4h,
                )
            )
    finally:
        conn.close()
    return out


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except sqlite3.Error:
        return []


def load_jev_coverage(
    rows: list[SignalChainRow],
    paths: DbPaths,
    *,
    tol_sec: int = 1800,
) -> list[SignalChainRow]:
    """标注 jev_coverage（0–100），对齐周检 jev_validity 口径。

    优先 `jev_signals.coverage_pct`（token 最新行）；表/列缺失或该 token
    无记录时回退 `token_indicators.jev_confidence * 100`；再缺失 → None。
    表结构用 PRAGMA 探测，兼容列名差异；找不到表不报错（切片自然放宽）。
    """
    if not rows:
        return rows
    out: list[SignalChainRow] = []
    tokens = {r.token_address for r in rows if r.token_address}
    if not tokens:
        return rows

    conn = connect_ro(paths.main)
    try:
        cov_by_token: dict[str, float] = {}

        # 主路径：jev_signals.coverage_pct，按时间列最新
        cols = _table_columns(conn, "jev_signals")
        token_col = "token_address" if "token_address" in cols else (
            "token" if "token" in cols else None)
        cov_col = "coverage_pct" if "coverage_pct" in cols else None
        time_col = next((c for c in ("updated_at", "recorded_at", "ts", "created_at")
                         if c in cols), None)
        if token_col and cov_col:
            for tok in tokens:
                order = f"ORDER BY {time_col} DESC" if time_col else "ORDER BY rowid DESC"
                row = conn.execute(
                    f"SELECT {cov_col} FROM jev_signals "
                    f"WHERE {token_col}=? {order} LIMIT 1",
                    (tok,),
                ).fetchone()
                if row and row[cov_col] is not None:
                    v = safe_float(row[cov_col])
                    if v is not None:
                        cov_by_token[tok] = v

        # 回退：token_indicators.jev_confidence * 100
        for tok in tokens:
            if tok in cov_by_token:
                continue
            cols_t = _table_columns(conn, "token_indicators")
            t_tok = "token_address" if "token_address" in cols_t else None
            conf_col = "jev_confidence" if "jev_confidence" in cols_t else None
            if not t_tok or not conf_col:
                continue
            t_time = next((c for c in ("updated_at", "recorded_at", "ts", "created_at")
                           if c in cols_t), None)
            order = f"ORDER BY {t_time} DESC" if t_time else "ORDER BY rowid DESC"
            row = conn.execute(
                f"SELECT {conf_col} FROM token_indicators "
                f"WHERE {t_tok}=? {order} LIMIT 1",
                (tok,),
            ).fetchone()
            if row and row[conf_col] is not None:
                v = safe_float(row[conf_col])
                if v is not None:
                    cov_by_token[tok] = v * 100.0

        for r in rows:
            cov = cov_by_token.get(r.token_address) if r.token_address else None
            out.append(replace(r, jev_coverage=cov))
    finally:
        conn.close()
    return out


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.median(xs))


def _mean(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.fmean(xs))


def load_market_baseline(
    paths: DbPaths,
    *,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    """同期 hot 截面：avg/median change_1h、change_4h、样本量。"""
    conn = connect_ro(paths.main)
    try:
        rows = conn.execute(
            """
            SELECT change_1h, change_4h
            FROM indicator_history
            WHERE tier = 'hot'
              AND recorded_at >= ?
              AND recorded_at < ?
            """,
            (period_start, period_end),
        ).fetchall()
    finally:
        conn.close()

    c1 = [float(r["change_1h"]) for r in rows if r["change_1h"] is not None]
    c4 = [float(r["change_4h"]) for r in rows if r["change_4h"] is not None]
    # 去极值（截断 |x|>500，与周检 winsor 思路一致；单位为 %）
    c1_w = [x for x in c1 if abs(x) <= 500]
    c4_w = [x for x in c4 if abs(x) <= 500]
    return {
        "n": len(rows),
        "n_1h": len(c1),
        "n_4h": len(c4),
        "avg_change_1h": _mean(c1),
        "avg_change_4h": _mean(c4),
        "median_change_1h": _median(c1),
        "median_change_4h": _median(c4),
        "avg_change_1h_winsor": _mean(c1_w),
        "avg_change_4h_winsor": _mean(c4_w),
        "median_change_1h_winsor": _median(c1_w),
        "median_change_4h_winsor": _median(c4_w),
        "n_1h_winsor": len(c1_w),
        "n_4h_winsor": len(c4_w),
        "period_start": period_start,
        "period_end": period_end,
    }


def summarize_execution(rows: list[SignalChainRow]) -> dict[str, Any]:
    """校验通过率、executed 率、executor 分布、validation_errors 分桶。"""
    n = len(rows)
    with_exec = [r for r in rows if r.exec_id is not None]
    n_exec = len(with_exec)
    ok = sum(1 for r in with_exec if r.validation_ok == 1)
    executed = sum(1 for r in with_exec if r.executed == 1)
    dry = sum(1 for r in rows if (r.mode or "").lower() == "dry_run")
    exec_dup_count = sum(1 for r in rows if r.exec_dup)

    executor_dist: dict[str, int] = defaultdict(int)
    for r in with_exec:
        executor_dist[r.executor or "(null)"] += 1

    err_buckets: dict[str, int] = defaultdict(int)
    for r in with_exec:
        if r.validation_ok == 1 and not r.validation_errors:
            continue
        err = (r.validation_errors or "").strip()
        if not err:
            if r.validation_ok != 1:
                err_buckets["(empty)"] += 1
            continue
        # 粗分桶：取首段 / 首个错误码
        key = err.split(";")[0].split("\n")[0].strip()
        if len(key) > 80:
            key = key[:77] + "..."
        err_buckets[key] += 1

    return {
        "n": n,
        "n_with_exec": n_exec,
        "exec_coverage": (n_exec / n) if n else None,
        "validation_ok": ok,
        "validation_ok_rate": (ok / n_exec) if n_exec else None,
        "executed": executed,
        "executed_rate": (executed / n_exec) if n_exec else None,
        "dry_run": dry,
        "dry_run_rate": (dry / n) if n else None,
        "executor_dist": dict(sorted(executor_dist.items(), key=lambda x: (-x[1], x[0]))),
        "validation_error_buckets": dict(
            sorted(err_buckets.items(), key=lambda x: (-x[1], x[0]))
        ),
        "exec_dup_count": exec_dup_count,
    }


def load_cex_scope(prior_db: str) -> dict[str, Any]:
    """CEX scope watch 最新快照（只读 prior.db.cex_scope_snapshots）。

    缺库、缺表或其它 SQLite 错误返回 available=False，供周报写「暂无数据」，不抛错。
    不写 prior.db，不读业务库，不改权重。
    """
    try:
        conn = sqlite3.connect(f"file:{prior_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM cex_scope_snapshots ORDER BY snap_date DESC LIMIT 7"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"available": False, "reason": str(exc), "rows": []}
    return {"available": True, "rows": [dict(r) for r in rows]}


_LISTING_WINDOW_SEC = 7 * 24 * 3600
_NA_TOKENS = {"", "none", "null", "n/a", "—", "-"}


def _md_tables(text: str) -> list[list[list[str]]]:
    """Split a markdown blob into tables of cell rows (header included)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []

    def flush() -> None:
        nonlocal current
        if current:
            tables.append(current)
        current = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            flush()
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{1,}:?", cell) for cell in cells):
            continue
        current.append(cells)
    flush()
    return tables


def _table_dicts(table: list[list[str]]) -> list[dict[str, str]]:
    if len(table) < 2:
        return []
    header = table[0]
    out: list[dict[str, str]] = []
    for row in table[1:]:
        padded = row + [""] * max(0, len(header) - len(row))
        out.append({header[i]: padded[i] for i in range(len(header))})
    return out


def _clean_na(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if text.lower() in _NA_TOKENS:
        return None
    return text


def load_cex_scope_compare(reports_dir: str) -> dict[str, Any]:
    """最新 CEX 视野对照 md（只读 exports/reports/cex_scope_compare_*.md 首表）。

    没有报告（快照不足 2 行，脚本不落盘）返回 available=False、reason=data accruing。
    文件在但首表读不出来返回 available=False，供周报写「暂无数据」。不抛错、不写库。
    """
    empty: dict[str, Any] = {
        "available": False,
        "reason": "data accruing",
        "path": None,
        "rows": [],
        "window": None,
        "med_delta24h": None,
        "still_rate": None,
        "survive_1h": None,
        "survive_4h": None,
    }
    try:
        directory = Path(reports_dir)
        if not directory.is_dir():
            return empty
        files = sorted(
            p for p in directory.glob("cex_scope_compare_*.md") if p.is_file()
        )
    except OSError as exc:
        empty["reason"] = str(exc)
        return empty
    if not files:
        return empty
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError as exc:
        empty["reason"] = str(exc)
        empty["path"] = str(latest)
        return empty

    tables = _md_tables(text)
    rows = _table_dicts(tables[0]) if tables else []
    if not rows:
        empty["reason"] = "empty table"
        empty["path"] = str(latest)
        return empty

    window_match = re.search(r"快照行数:\s*\d+（([^）]+)）", text)
    window = window_match.group(1).strip() if window_match else None
    if not window and rows:
        start = rows[0].get("日期") or ""
        end = rows[-1].get("日期") or ""
        window = f"{start} → {end}".strip()

    still_rate = None
    survive_1h = None
    survive_4h = None
    if len(tables) >= 2:
        for item in _table_dicts(tables[1]):
            dim = item.get("维度") or ""
            cex_cell = item.get("CEX 视野") or ""
            prior_cell = item.get("链上先验(self_survival)") or ""
            if "留存" in dim or "视野留存" in cex_cell:
                rate_match = re.search(r"视野留存\s*≈\s*(\S+)", cex_cell)
                if rate_match:
                    still_rate = _clean_na(rate_match.group(1))
            if "1h" in dim:
                survive_1h = _clean_na(prior_cell)
            if "4h" in dim:
                survive_4h = _clean_na(prior_cell)

    latest_row = rows[-1]
    return {
        "available": True,
        "reason": "",
        "path": str(latest),
        "rows": rows,
        "window": window,
        "med_delta24h": _clean_na(latest_row.get("新进者 Δ24h 中位")),
        "still_rate": still_rate,
        "survive_1h": survive_1h,
        "survive_4h": survive_4h,
    }


def load_cex_listing_events(
    prior_db: str,
    *,
    now_ts: Optional[int] = None,
) -> dict[str, Any]:
    """近 7 天精确上线事件计数（只读 prior.db.cex_listing_events）。

    缺库、缺表或窗口内 0 行返回 available=False，供周报写「暂无数据」。
    matched 与上线后 1h 表现本步固定 N/A：不重做 Step 2 的脏 join。
    """
    empty: dict[str, Any] = {
        "available": False,
        "reason": "no events",
        "n_7d": 0,
        "exchanges": [],
        "exchange_counts": {},
        "matched": "N/A",
        "med_ret_1h": "N/A",
    }
    now = int(datetime.now(timezone.utc).timestamp()) if now_ts is None else int(now_ts)
    cutoff = now - _LISTING_WINDOW_SEC
    try:
        conn = sqlite3.connect(f"file:{prior_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT exchange, COUNT(*) AS n FROM cex_listing_events "
                "WHERE listing_ts >= ? GROUP BY exchange ORDER BY exchange",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        empty["reason"] = str(exc)
        return empty
    counts = {str(row["exchange"]): int(row["n"]) for row in rows if int(row["n"]) > 0}
    n_7d = sum(counts.values())
    if n_7d <= 0:
        return empty
    return {
        "available": True,
        "reason": "",
        "n_7d": n_7d,
        "exchanges": list(counts.keys()),
        "exchange_counts": counts,
        "matched": "N/A",
        "med_ret_1h": "N/A",
    }
