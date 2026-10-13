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
    # 成交（无匹配时全 None）。paper positions.id 是 TEXT UUID，不要 int()。
    position_id: Optional[str]
    fill_side: Optional[str]
    fill_price: Optional[float]
    pnl_usd: Optional[float]
    position_status: Optional[str] = None  # open | closed | None
    # 前向（信号层）
    fwd_1h: Optional[float] = None
    fwd_4h: Optional[float] = None
    price_at_signal: Optional[float] = None
    # 信号特征补充（周检过滤口径用；缺则 None）
    liquidity_at_signal: Optional[float] = None
    jev_coverage: Optional[float] = None
    # token_age：pairs.pair_created_at（ms）→ 相对信号时刻的小时数
    pair_created_at: Optional[int] = None  # ms epoch from pairs
    token_age_hours: Optional[float] = None
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


_INT_ID_RE = re.compile(r"-?\d+")


def parse_decision_log_id(value: Any) -> Optional[int]:
    """Integer decision_log id. Skip missing, bool, and non-integral meta."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return None
    if isinstance(value, str) and _INT_ID_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def position_id_text(value: Any) -> Optional[str]:
    """Paper position ids are UUID strings. Keep numeric ids as text too."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validation_error_bucket_key(err: Optional[str]) -> Optional[str]:
    """First validation error code.

    ``execution_log.validation_errors`` is a JSON array
    (``json.dumps(list[str])``). Older rows may be ``;`` or newline separated.
    Empty array / blank → None. First code is clipped to 80 characters.
    """
    text = (err or "").strip()
    if not text:
        return None
    first: Optional[str] = None
    if text[0] == "[":
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                piece = str(item).strip()
                if piece:
                    first = piece
                    break
            if first is None:
                return None
    if first is None:
        first = text.split(";")[0].split("\n")[0].strip()
    if len(first) >= 2 and first[0] == first[-1] == '"':
        try:
            loaded = json.loads(first)
        except json.JSONDecodeError:
            loaded = first[1:-1]
        if isinstance(loaded, str):
            first = loaded.strip()
    if not first:
        return None
    if len(first) > 80:
        return first[:77] + "..."
    return first


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
            colnames = {
                str(row[1])
                for row in paper.execute("PRAGMA table_info(positions)").fetchall()
            }
            status_col = "status" in colnames
            select_cols = "id, pnl_usd, meta_json, entry_price"
            if status_col:
                select_cols += ", status"
            pos_rows = paper.execute(
                f"""
                SELECT {select_cols}
                FROM positions
                WHERE json_extract(meta_json, '$.decision_log_id') IS NOT NULL
                """
            ).fetchall()
            for pr in pos_rows:
                meta = _parse_json(pr["meta_json"])
                did_i = parse_decision_log_id(meta.get("decision_log_id"))
                if did_i is None:
                    continue
                fill = paper.execute(
                    """
                    SELECT side, price FROM fills
                    WHERE position_id = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (pr["id"],),
                ).fetchone()
                status = None
                if status_col and pr["status"] is not None:
                    status = str(pr["status"])
                pos_by_dec[did_i] = {
                    "position_id": position_id_text(pr["id"]),
                    "position_status": status,
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
                position_status=pos.get("position_status"),
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


def load_ccxt_market_baseline(
    db_root: str | Path,
    *,
    winsor_abs: float = 50.0,
    top_n: int = 5,
) -> dict[str, Any]:
    """Read-only CCXT sidechannel baseline from ``ccxt.db`` (second market ref).

    Missing db / tables → ``ok=False`` with ``note`` (caller renders skip).
    Uses latest tick round per exchange for change_24h stats; full-DB counts
    for ticks / listings / ohlcv. Observation only — never weights or alerts.
    """
    root = Path(db_root)
    db_path = root / "ccxt.db"
    out: dict[str, Any] = {
        "ok": False,
        "note": "",
        "path": str(db_path),
        "exchanges": [],
        "by_exchange": {},
        "ticks_total": 0,
        "listings_total": 0,
        "ohlcv_total": 0,
        "latest_round_n": 0,
        "median_change_24h": None,
        "avg_change_24h": None,
        "median_change_24h_winsor": None,
        "avg_change_24h_winsor": None,
        "n_change_24h": 0,
        "n_change_24h_winsor": 0,
        "winsor_abs": winsor_abs,
        "top_by_quote": [],
        "scope": "latest_round_per_exchange",
        "status_tag": "SHORT",
    }
    if not db_path.is_file():
        out["note"] = "ccxt.db 不存在（旁路未建或未拉取）；跳过 CEX 第二参照"
        return out

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        out["note"] = f"ccxt.db 打开失败: {e}"
        return out

    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        needed = {"ccxt_ticks", "ccxt_listings", "ccxt_ohlcv"}
        missing = sorted(needed - tables)
        if missing:
            out["note"] = (
                f"ccxt.db 缺表 {missing}；跳过 CEX 第二参照"
            )
            return out

        # Full-DB counts by exchange
        by_ex: dict[str, dict[str, int]] = {}

        def _ensure(ex: str) -> dict[str, int]:
            if ex not in by_ex:
                by_ex[ex] = {"ticks": 0, "listings": 0, "ohlcv": 0}
            return by_ex[ex]

        for row in conn.execute(
            "SELECT exchange, COUNT(*) AS n FROM ccxt_ticks GROUP BY exchange"
        ):
            _ensure(str(row["exchange"]))["ticks"] = int(row["n"])
        for row in conn.execute(
            "SELECT exchange, COUNT(*) AS n FROM ccxt_listings GROUP BY exchange"
        ):
            _ensure(str(row["exchange"]))["listings"] = int(row["n"])
        for row in conn.execute(
            "SELECT exchange, COUNT(*) AS n FROM ccxt_ohlcv GROUP BY exchange"
        ):
            _ensure(str(row["exchange"]))["ohlcv"] = int(row["n"])

        exchanges = sorted(by_ex.keys())
        ticks_total = sum(v["ticks"] for v in by_ex.values())
        listings_total = sum(v["listings"] for v in by_ex.values())
        ohlcv_total = sum(v["ohlcv"] for v in by_ex.values())

        # Latest round per exchange (max ts)
        latest_rows: list[sqlite3.Row] = []
        for ex in exchanges:
            mx = conn.execute(
                "SELECT MAX(ts) FROM ccxt_ticks WHERE exchange = ?",
                (ex,),
            ).fetchone()[0]
            if mx is None:
                continue
            part = conn.execute(
                """
                SELECT exchange, symbol, ts, last_price, quote_volume_24h,
                       change_24h_pct
                FROM ccxt_ticks
                WHERE exchange = ? AND ts = ?
                """,
                (ex, mx),
            ).fetchall()
            latest_rows.extend(part)

        changes = [
            float(r["change_24h_pct"])
            for r in latest_rows
            if r["change_24h_pct"] is not None
        ]
        changes_w = [x for x in changes if abs(x) <= winsor_abs]

        # Top by quote_volume within latest round
        ranked = sorted(
            latest_rows,
            key=lambda r: float(r["quote_volume_24h"] or 0),
            reverse=True,
        )
        top: list[dict[str, Any]] = []
        for r in ranked[: max(0, top_n)]:
            top.append(
                {
                    "exchange": str(r["exchange"]),
                    "symbol": str(r["symbol"]),
                    "quote_volume_24h": (
                        float(r["quote_volume_24h"])
                        if r["quote_volume_24h"] is not None
                        else None
                    ),
                    "change_24h_pct": (
                        float(r["change_24h_pct"])
                        if r["change_24h_pct"] is not None
                        else None
                    ),
                    "last_price": (
                        float(r["last_price"])
                        if r["last_price"] is not None
                        else None
                    ),
                }
            )

        # SHORT if few rounds overall
        distinct_rounds = conn.execute(
            "SELECT COUNT(DISTINCT ts) FROM ccxt_ticks"
        ).fetchone()[0]
        status_tag = "SHORT" if int(distinct_rounds or 0) < 10 else "OK"

        out.update(
            {
                "ok": True,
                "note": (
                    f"ccxt.db 可读 · 全库 ticks={ticks_total} "
                    f"listings={listings_total} ohlcv={ohlcv_total} · "
                    f"最近一轮/所 n={len(latest_rows)}"
                ),
                "exchanges": exchanges,
                "by_exchange": by_ex,
                "ticks_total": ticks_total,
                "listings_total": listings_total,
                "ohlcv_total": ohlcv_total,
                "latest_round_n": len(latest_rows),
                "median_change_24h": _median(changes),
                "avg_change_24h": _mean(changes),
                "median_change_24h_winsor": _median(changes_w),
                "avg_change_24h_winsor": _mean(changes_w),
                "n_change_24h": len(changes),
                "n_change_24h_winsor": len(changes_w),
                "top_by_quote": top,
                "distinct_ts": int(distinct_rounds or 0),
                "status_tag": status_tag,
            }
        )
        return out
    except Exception as e:
        out["note"] = f"ccxt.db 读取失败: {e}"
        return out
    finally:
        conn.close()


def annotate_token_age(
    rows: list[SignalChainRow],
    paths: DbPaths,
) -> list[SignalChainRow]:
    """Join pairs.pair_created_at and set token_age_hours.

    Prefer (chain_id, pair_address); fallback (chain_id, token_address).
    age_hours = (signal_ts_ms - pair_created_at) / 3600000; missing → None.
    """
    if not rows:
        return rows

    pair_keys: set[tuple[str, str]] = set()
    tok_keys: set[tuple[str, str]] = set()
    for r in rows:
        chain = (r.chain_id or "").strip()
        if not chain:
            continue
        if r.pair_address:
            pair_keys.add((chain, r.pair_address.strip()))
        if r.token_address:
            tok_keys.add((chain, r.token_address.strip()))

    created_by_pair: dict[tuple[str, str], int] = {}
    created_by_tok: dict[tuple[str, str], int] = {}

    conn = connect_ro(paths.main)
    try:
        cols = _table_columns(conn, "pairs")
        if "pair_created_at" not in cols:
            return rows

        # batch by pair_address
        pair_list = sorted(pair_keys)
        chunk = 400
        for i in range(0, len(pair_list), chunk):
            part = pair_list[i : i + chunk]
            if not part:
                continue
            # OR of (chain_id=? AND pair_address=?) — safer than IN of tuples
            clauses = " OR ".join(
                "(chain_id = ? AND pair_address = ?)" for _ in part
            )
            params: list[Any] = []
            for c, p in part:
                params.extend([c, p])
            for row in conn.execute(
                f"""
                SELECT chain_id, pair_address, pair_created_at
                FROM pairs
                WHERE pair_created_at IS NOT NULL
                  AND ({clauses})
                """,
                params,
            ).fetchall():
                key = (str(row["chain_id"]), str(row["pair_address"]))
                ms = row["pair_created_at"]
                if ms is None:
                    continue
                try:
                    created_by_pair[key] = int(ms)
                except (TypeError, ValueError):
                    continue

        # token_address fallback (used only when pair join misses)
        tok_list = sorted(tok_keys)
        for i in range(0, len(tok_list), chunk):
            part = tok_list[i : i + chunk]
            if not part:
                continue
            clauses = " OR ".join(
                "(chain_id = ? AND token_address = ?)" for _ in part
            )
            params = []
            for c, t in part:
                params.extend([c, t])
            for row in conn.execute(
                f"""
                SELECT chain_id, token_address, pair_created_at
                FROM pairs
                WHERE pair_created_at IS NOT NULL
                  AND ({clauses})
                """,
                params,
            ).fetchall():
                key = (str(row["chain_id"]), str(row["token_address"]))
                if key in created_by_tok:
                    continue  # keep first
                ms = row["pair_created_at"]
                if ms is None:
                    continue
                try:
                    created_by_tok[key] = int(ms)
                except (TypeError, ValueError):
                    continue
    finally:
        conn.close()

    out: list[SignalChainRow] = []
    for r in rows:
        chain = (r.chain_id or "").strip()
        created: Optional[int] = None
        if chain and r.pair_address:
            created = created_by_pair.get((chain, r.pair_address.strip()))
        if created is None and chain and r.token_address:
            created = created_by_tok.get((chain, r.token_address.strip()))

        age_hours: Optional[float] = None
        if created is not None:
            try:
                signal_dt = parse_ts(r.ts)
                signal_ms = signal_dt.timestamp() * 1000.0
                age_hours = (signal_ms - float(created)) / 3600000.0
            except (TypeError, ValueError):
                age_hours = None

        out.append(
            replace(
                r,
                pair_created_at=created,
                token_age_hours=age_hours,
            )
        )
    return out


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
        # JSON array (ledger) or legacy ";"/newline text. First code only.
        key = validation_error_bucket_key(err)
        if not key:
            if r.validation_ok != 1:
                err_buckets["(empty)"] += 1
            continue
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


def get_prior_tag_buckets(
    prior_db: str,
    min_ts: str | None = None,
    *,
    main_db: str | None = None,
) -> list[dict]:
    """读 decision_prior_tags，按 age_bucket 分桶（供周报 §8 联合表）。只读。

    decision_id = str(decision_log.id)。若提供 main_db，ATTACH 后用
    CAST(d.id AS TEXT) = t.decision_id 关联（勿直接 id=decision_id 依赖隐式亲和）。
    未提供 main_db 时仅查 prior.db：unique_token 用 tags.token_address，
    min_ts 过滤 t.signal_ts（= decision_log.ts 拷贝）。
    复盘 owns 周报接线；本函数只保证可读表。
    """
    p = Path(prior_db)
    if not p.is_file():
        return []
    # 主连接挂 prior（只读）；需要 decision_log 时 ATTACH 主库
    conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "decision_prior_tags" not in names:
            return []

        params: list[Any] = []
        if main_db and Path(main_db).is_file():
            # ATTACH 主库只读；JOIN 用 CAST 对齐 TEXT decision_id
            conn.execute(
                "ATTACH DATABASE ? AS main_ro",
                (f"file:{Path(main_db).resolve()}?mode=ro",),
            )
            where = ""
            if min_ts:
                where = "WHERE d.ts >= ?"
                params.append(min_ts)
            sql = f"""
                SELECT t.age_bucket,
                       COUNT(*) AS n,
                       COUNT(DISTINCT COALESCE(d.token_address, t.token_address))
                           AS unique_token,
                       SUM(CASE WHEN t.peak_window = 1 THEN 1 ELSE 0 END) AS in_peak,
                       SUM(CASE WHEN t.survival_ref_1h IS NOT NULL THEN 1 ELSE 0 END)
                           AS has_ref_1h,
                       SUM(CASE WHEN t.survival_ref_4h IS NOT NULL THEN 1 ELSE 0 END)
                           AS has_ref_4h
                FROM decision_prior_tags t
                LEFT JOIN main_ro.decision_log d
                  ON CAST(d.id AS TEXT) = t.decision_id
                {where}
                GROUP BY t.age_bucket
                ORDER BY CASE t.age_bucket
                    WHEN '<1h' THEN 1 WHEN '1-24h' THEN 2
                    WHEN '1-7d' THEN 3 WHEN '>7d' THEN 4 ELSE 5 END
            """
        else:
            where = ""
            if min_ts:
                where = "WHERE t.signal_ts >= ?"
                params.append(min_ts)
            sql = f"""
                SELECT t.age_bucket,
                       COUNT(*) AS n,
                       COUNT(DISTINCT t.token_address) AS unique_token,
                       SUM(CASE WHEN t.peak_window = 1 THEN 1 ELSE 0 END) AS in_peak,
                       SUM(CASE WHEN t.survival_ref_1h IS NOT NULL THEN 1 ELSE 0 END)
                           AS has_ref_1h,
                       SUM(CASE WHEN t.survival_ref_4h IS NOT NULL THEN 1 ELSE 0 END)
                           AS has_ref_4h
                FROM decision_prior_tags t
                {where}
                GROUP BY t.age_bucket
                ORDER BY CASE t.age_bucket
                    WHEN '<1h' THEN 1 WHEN '1-24h' THEN 2
                    WHEN '1-7d' THEN 3 WHEN '>7d' THEN 4 ELSE 5 END
            """
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def load_sentiment_spike_linkage(
    db_root: str | Path,
    *,
    period_start: str,
    period_end: str,
    main_db_name: str = "dexscreener.db",
) -> dict[str, Any]:
    """Read-only: social spike × on-chain linkage for weekly review §B.

    Probes ``token_indicators`` (preferred) or tables named like
    ``sentiment`` / ``token_sentiment`` / ``social_*`` for a ``spike`` column.
    Missing db / table / column / rows → ``ok=False`` + ``note`` (never raises).
    Observation only — no weights, alerts, or writes.
    """
    root = Path(db_root)
    db_path = root / main_db_name
    empty: dict[str, Any] = {
        "ok": False,
        "empty": True,
        "note": "",
        "path": str(db_path),
        "table": None,
        "n_spike": 0,
        "n_spike_with_jev": 0,
        "n_1h": 0,
        "n_4h": 0,
        "up_1h_rate": None,
        "up_4h_rate": None,
        "up_1h_n": 0,
        "up_4h_n": 0,
        "change_source": None,
        "period_start": period_start,
        "period_end": period_end,
        "hit_gate_55": False,
    }

    def _fail(note: str) -> dict[str, Any]:
        out = dict(empty)
        out["note"] = note
        return out

    if not db_path.is_file():
        return _fail(f"{main_db_name} 不存在；暂无数据（等 sentiment.spike 入库）")

    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        return _fail(f"打开 {main_db_name} 失败: {e}")

    try:
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        def _cols(table: str) -> set[str]:
            return {
                str(r[1])
                for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }

        # Prefer token_indicators when spike is present (SENTIMENT_SPIKE.md).
        candidates: list[str] = []
        if "token_indicators" in tables:
            candidates.append("token_indicators")
        for t in sorted(tables):
            tl = t.lower()
            if t in candidates:
                continue
            if (
                "sentiment" in tl
                or tl.startswith("social_")
                or tl in ("token_sentiment", "sentiment")
            ):
                candidates.append(t)

        chosen: Optional[str] = None
        cols: set[str] = set()
        for t in candidates:
            try:
                c = _cols(t)
            except Exception:
                continue
            if "spike" in c:
                chosen, cols = t, c
                break

        if chosen is None:
            if not candidates:
                return _fail("暂无数据（等 sentiment.spike 入库）：未找到 sentiment 相关表")
            return _fail(
                "暂无数据（等 sentiment.spike 入库）："
                f"已见表 {candidates} 但无 spike 列"
            )

        # Time column for period filter
        ts_col = None
        for cand in (
            "sentiment_updated_at",
            "spike_at",
            "recorded_at",
            "updated_at",
            "ts",
        ):
            if cand in cols:
                ts_col = cand
                break

        where = "spike = 1"
        params: list[Any] = []
        if ts_col:
            where += f" AND {ts_col} >= ? AND {ts_col} < ?"
            params.extend([period_start, period_end])

        # Identity columns
        has_chain = "chain_id" in cols
        has_token = "token_address" in cols
        if has_chain and has_token:
            id_sql = "chain_id, token_address"
        elif has_token:
            id_sql = "token_address AS token_address, '' AS chain_id"
        else:
            return _fail(
                f"表 `{chosen}` 有 spike 但缺 token_address；无法汇总联动"
            )

        change_1h_expr = "change_1h" if "change_1h" in cols else "NULL AS change_1h"
        change_4h_expr = "change_4h" if "change_4h" in cols else "NULL AS change_4h"
        jev_col = None
        for jc in ("jev_signal", "jev_score", "jev_strength"):
            if jc in cols:
                jev_col = jc
                break
        jev_sel = f"{jev_col}" if jev_col else "NULL AS jev_signal"

        try:
            rows = conn.execute(
                f"""
                SELECT {id_sql}, {change_1h_expr}, {change_4h_expr}, {jev_sel}
                FROM {chosen}
                WHERE {where}
                """,
                params,
            ).fetchall()
        except Exception as e:
            return _fail(f"查询 `{chosen}.spike` 失败: {e}")

        if not rows:
            note = (
                f"`{chosen}.spike` 列存在，但期间 "
                f"[{period_start}, {period_end}) 无 spike=1 行"
            )
            if not ts_col:
                note += "（无时间列，已扫全表）"
            return _fail(note)

        # Unique spike tokens
        keys: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            k = (str(r["chain_id"] or ""), str(r["token_address"] or ""))
            if k[1] and k not in seen:
                seen.add(k)
                keys.append(k)
        n_spike = len(keys)

        # JEV overlap: jev_signals table and/or non-null jev_* on row
        has_jev_table = "jev_signals" in tables
        jev_keys: set[tuple[str, str]] = set()
        if has_jev_table and keys:
            try:
                # Chunk IN queries to stay safe
                chunk = 200
                for i in range(0, len(keys), chunk):
                    part = keys[i : i + chunk]
                    placeholders = ",".join(["(?,?)"] * len(part))
                    flat: list[Any] = []
                    for ch, tok in part:
                        flat.extend([ch, tok])
                    hit = conn.execute(
                        f"""
                        SELECT DISTINCT chain_id, token_address
                        FROM jev_signals
                        WHERE (chain_id, token_address) IN ({placeholders})
                        """,
                        flat,
                    ).fetchall()
                    for h in hit:
                        jev_keys.add(
                            (str(h["chain_id"] or ""), str(h["token_address"] or ""))
                        )
            except Exception:
                jev_keys = set()

        if jev_col:
            for r in rows:
                k = (str(r["chain_id"] or ""), str(r["token_address"] or ""))
                if not k[1]:
                    continue
                v = r[jev_col] if jev_col in r.keys() else None
                if v is None or v == "":
                    continue
                # treat numeric 0 as no-signal only for score/strength; keep string labels
                if jev_col in ("jev_score", "jev_strength"):
                    fv = safe_float(v)
                    if fv is None or fv == 0:
                        continue
                jev_keys.add(k)

        n_spike_with_jev = len(jev_keys)

        # Up rates from snapshot change_1h / change_4h (percent units)
        c1 = [safe_float(r["change_1h"]) for r in rows]
        c4 = [safe_float(r["change_4h"]) for r in rows]
        c1v = [x for x in c1 if x is not None]
        c4v = [x for x in c4 if x is not None]
        up1 = sum(1 for x in c1v if x > 0)
        up4 = sum(1 for x in c4v if x > 0)
        n_1h = len(c1v)
        n_4h = len(c4v)
        up_1h_rate = (up1 / n_1h) if n_1h else None
        up_4h_rate = (up4 / n_4h) if n_4h else None

        change_source = None
        if "change_1h" in cols or "change_4h" in cols:
            change_source = (
                f"`{chosen}`.change_1h/4h（突热快照并存涨跌幅，非 spike 后严格前向）"
            )
        else:
            change_source = "缺 change_1h/4h 列；上涨率暂缺"

        hit = bool(
            (up_1h_rate is not None and up_1h_rate > 0.55)
            or (up_4h_rate is not None and up_4h_rate > 0.55)
        )

        return {
            "ok": True,
            "empty": False,
            "note": "",
            "path": str(db_path),
            "table": chosen,
            "ts_col": ts_col,
            "n_spike": n_spike,
            "n_spike_rows": len(rows),
            "n_spike_with_jev": n_spike_with_jev,
            "n_1h": n_1h,
            "n_4h": n_4h,
            "up_1h_rate": up_1h_rate,
            "up_4h_rate": up_4h_rate,
            "up_1h_n": up1,
            "up_4h_n": up4,
            "change_source": change_source,
            "period_start": period_start,
            "period_end": period_end,
            "hit_gate_55": hit,
        }
    except Exception as e:
        return _fail(f"sentiment spike 联动汇总异常: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_cex_scope(prior_db: str | Path) -> dict[str, Any]:
    """CEX scope watch 最新快照（只读 prior.db.cex_scope_snapshots）。

    缺库/缺表/异常 → available=False + reason；调用方渲染「暂无数据」不失败。
    Observation only — no weights, alerts, or writes.
    """
    p = Path(prior_db)
    out: dict[str, Any] = {"available": False, "reason": "", "rows": [], "path": str(p)}
    if not p.is_file():
        out["reason"] = f"prior.db 不存在: {p}"
        return out
    try:
        conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "cex_scope_snapshots" not in names:
                out["reason"] = "缺表 cex_scope_snapshots（等 cex_scope_watch 首跑）"
                return out
            rows = conn.execute(
                "SELECT * FROM cex_scope_snapshots ORDER BY snap_date DESC LIMIT 7"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["reason"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = str(exc)
        return out
    return {"available": True, "reason": "", "rows": [dict(r) for r in rows], "path": str(p)}


def load_cex_scope_compare(
    db_root: str | Path,
    *,
    reports_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load latest cex_scope_compare_*.md summary for weekly §6e.

    Reads exports/reports/cex_scope_compare_YYYYMMDD.md (newest by name).
    Also checks prior.db snapshot count so caller can show「数据积累中」when <2.
    available=False when no report / parse fail — never raises.
    Observation only — no weights or writes.
    """
    root = Path(db_root)
    out: dict[str, Any] = {
        "available": False,
        "reason": "",
        "accruing": False,
        "snap_rows": 0,
        "window": "",
        "med_delta24h_new": None,
        "still_rate": None,
        "survive_1h": None,
        "survive_4h": None,
        "path": "",
    }
    prior_db = root / "data" / "prior" / "db" / "prior.db"
    try:
        if prior_db.is_file():
            conn = sqlite3.connect(f"file:{prior_db.resolve()}?mode=ro", uri=True)
            try:
                names = {
                    str(r[0])
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if "cex_scope_snapshots" in names:
                    n = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM cex_scope_snapshots"
                        ).fetchone()[0]
                    )
                    out["snap_rows"] = n
                    if n < 2:
                        out["accruing"] = True
                        out["reason"] = f"快照行数={n} < 2（数据积累中）"
                        return out
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"读快照计数失败: {exc}"

    rdir = Path(reports_dir) if reports_dir else (root / "exports" / "reports")
    if not rdir.is_dir():
        out["reason"] = out["reason"] or f"reports 目录不存在: {rdir}"
        if out["snap_rows"] < 2:
            out["accruing"] = True
            out["reason"] = out["reason"] or "数据积累中"
        return out

    files = sorted(rdir.glob("cex_scope_compare_*.md"))
    if not files:
        if out["snap_rows"] < 2:
            out["accruing"] = True
            out["reason"] = out["reason"] or "数据积累中（尚无 compare 产物）"
        else:
            out["reason"] = "尚无 cex_scope_compare_*.md（请跑 scripts/cex_scope_compare.py）"
        return out

    latest = files[-1]
    out["path"] = str(latest)
    try:
        text = latest.read_text(encoding="utf-8")
    except OSError as exc:
        out["reason"] = str(exc)
        return out

    # Parse header: 快照行数: N（DATE → DATE）
    m = re.search(
        r"快照行数:\s*(\d+)（([^→]+)→\s*([^）]+)）",
        text,
    )
    if m:
        out["window"] = f"{m.group(2).strip()} → {m.group(3).strip()}"
        try:
            out["snap_rows"] = int(m.group(1))
        except ValueError:
            pass

    # First data table rows (date | n_new | still | med)
    table_rows: list[dict[str, Any]] = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("| 日期 |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            if re.match(r"^\|\s*-+", line):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) >= 4:
                table_rows.append(
                    {
                        "date": parts[0],
                        "n_new_24h": parts[1],
                        "still_visible": parts[2],
                        "med_delta24h": parts[3],
                    }
                )

    if table_rows:
        last = table_rows[-1]
        med_s = last.get("med_delta24h")
        if med_s and med_s not in ("N/A", "—", "-"):
            try:
                out["med_delta24h_new"] = float(str(med_s).rstrip("%"))
            except ValueError:
                out["med_delta24h_new"] = med_s

    # 对照 section: 视野留存 / 链上存活
    m_rate = re.search(r"视野留存\s*≈\s*([^\s|]+)", text)
    if m_rate and m_rate.group(1) not in ("N/A", "—"):
        try:
            out["still_rate"] = float(m_rate.group(1))
        except ValueError:
            out["still_rate"] = m_rate.group(1)

    m_1h = re.search(r"链上 1h 存活\s*\|\s*—\s*\|\s*([^\s|]+)", text)
    if m_1h and m_1h.group(1) not in ("N/A", "—"):
        out["survive_1h"] = m_1h.group(1).strip()
    m_4h = re.search(r"链上 4h 存活\s*\|\s*—\s*\|\s*([^\s|]+)", text)
    if m_4h and m_4h.group(1) not in ("N/A", "—"):
        out["survive_4h"] = m_4h.group(1).strip()

    if not out["window"] and table_rows:
        out["window"] = f"{table_rows[0]['date']} → {table_rows[-1]['date']}"

    out["available"] = True
    out["reason"] = ""
    out["rows"] = table_rows
    return out


def load_cex_listing_events(
    prior_db: str | Path,
    *,
    days: int = 7,
) -> dict[str, Any]:
    """CEX 精确上线事件（只读 prior.db.cex_listing_events）for weekly §6f.

    缺库/缺表/无行 → available=False + reason；matched / 1h 表现本步留 N/A
    （分支 A 深挖未启）。Observation only.
    """
    p = Path(prior_db)
    out: dict[str, Any] = {
        "available": False,
        "reason": "",
        "n_7d": 0,
        "n_total": 0,
        "exchanges": [],
        "matched": "N/A",
        "med_ret_1h": "N/A",
        "path": str(p),
        "rows": [],
    }
    if not p.is_file():
        out["reason"] = f"prior.db 不存在: {p}"
        return out
    try:
        conn = sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            names = {
                str(r[0])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "cex_listing_events" not in names:
                out["reason"] = "缺表 cex_listing_events（等 ccxt_listing_watch 首跑）"
                return out
            n_total = int(
                conn.execute("SELECT COUNT(*) FROM cex_listing_events").fetchone()[0]
            )
            out["n_total"] = n_total
            if n_total == 0:
                out["reason"] = "cex_listing_events 空表"
                return out
            cutoff = int(datetime.now(timezone.utc).timestamp()) - int(days) * 86400
            n_7d = int(
                conn.execute(
                    "SELECT COUNT(*) FROM cex_listing_events WHERE listing_ts >= ?",
                    (cutoff,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT exchange, symbol, listing_ts, base, quote, source"
                " FROM cex_listing_events WHERE listing_ts >= ?"
                " ORDER BY listing_ts DESC LIMIT 200",
                (cutoff,),
            ).fetchall()
            by_ex = conn.execute(
                "SELECT exchange, COUNT(*) AS n FROM cex_listing_events"
                " WHERE listing_ts >= ? GROUP BY exchange ORDER BY n DESC",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out["reason"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["reason"] = str(exc)
        return out

    out["available"] = True
    out["reason"] = ""
    out["n_7d"] = n_7d
    out["exchanges"] = [f"{r['exchange']}({r['n']})" for r in by_ex]
    out["rows"] = [dict(r) for r in rows]
    # 分支 A 深挖未启：matched / 上线后 1h 表现保持 N/A
    out["matched"] = "N/A"
    out["med_ret_1h"] = "N/A"
    return out
