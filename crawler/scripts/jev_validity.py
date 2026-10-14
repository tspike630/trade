#!/usr/bin/env python3
"""Filtered JEV strength validity re-check on decision_log buys.

Forward 1h/4h returns for jev_strength_v1 buy signals, with liquidity /
coverage / price filters and strength strata. Read-only on business tables;
writes the optional markdown report, appends one trend CSV row under
exports/reports/jev_validity_trend.csv, and prints a summary to stdout.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from review.gates import (
    CONSECUTIVE_WEEKS,
    MIN_RAW_N,
    MIN_UNIQUE_TOKEN,
    UP_1H_DEGRADE,
    UP_1H_PROMOTE,
)


def default_db_path() -> Path:
    """Crawl DB: DECISION_DB_PATH or DB_PATH, else repo root, else crawler/."""
    env = os.environ.get("DECISION_DB_PATH", "").strip() or os.environ.get("DB_PATH", "").strip()
    if env:
        return Path(env)
    root_db = _REPO / "dexscreener.db"
    if root_db.is_file() or not (_REPO / "crawler" / "config.py").is_file():
        return root_db
    return _REPO / "crawler" / "dexscreener.db"


DEFAULT_DB = default_db_path()
DEFAULT_TREND_CSV = _REPO / "exports" / "reports" / "jev_validity_trend.csv"
STRATEGY_ID = "jev_strength_v1"
STRENGTH_RE = re.compile(r"jev_strength=([0-9.]+)")
HK_TZ = timezone(timedelta(hours=8))


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


def now_hk_label() -> str:
    return datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M HKT")


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


def median(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.median(xs))


def mean(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.fmean(xs))


@dataclass
class Snap:
    price: float
    liquidity: Optional[float]
    recorded_at: str
    dt_sec: float


@dataclass
class Sample:
    id: int
    ts: str
    chain_id: str
    token_address: str
    pair_address: Optional[str]
    strength: Optional[float]
    coverage: Optional[float]
    coverage_src: str
    liq_usd: Optional[float]
    liq_src: str
    price0: Optional[float]
    p0_dt: Optional[float]
    p0_at: Optional[str]
    ret_1h: Optional[float]
    ret_4h: Optional[float]
    status_1h: str = "pending"
    status_4h: str = "pending"
    p1h_dt: Optional[float] = None
    p4h_dt: Optional[float] = None
    filter_fail: list[str] = field(default_factory=list)

    @property
    def token_short(self) -> str:
        t = self.token_address or ""
        return t[:12] + "…" if len(t) > 12 else t


def open_db(path: Path) -> sqlite3.Connection:
    # URI read-only for the main DB file; analysis never writes business tables.
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_buys(conn: sqlite3.Connection, min_strength: float) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, ts, strategy_id, action, chain_id, token_address,
               reason, mode, raw_json
        FROM decision_log
        WHERE action = 'buy' AND strategy_id = ?
        ORDER BY id
        """,
        (STRATEGY_ID,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        raw = {}
        if d.get("raw_json"):
            try:
                raw = json.loads(d["raw_json"])
            except json.JSONDecodeError:
                raw = {}
        extras = raw.get("extras") or {}
        strength = safe_float(extras.get("jev_strength"))
        if strength is None:
            m = STRENGTH_RE.search(d.get("reason") or "")
            strength = float(m.group(1)) if m else None
        if strength is None or strength < min_strength:
            continue
        d["raw"] = raw
        d["extras"] = extras
        d["strength"] = strength
        d["pair_address"] = raw.get("pair_address")
        out.append(d)
    return out


def resolve_pair(
    conn: sqlite3.Connection, chain_id: str, token: str, pair: Optional[str]
) -> Optional[str]:
    if pair:
        return pair
    ti = conn.execute(
        """
        SELECT primary_pair_address FROM token_indicators
        WHERE chain_id = ? AND token_address = ?
        """,
        (chain_id, token),
    ).fetchone()
    if ti and ti["primary_pair_address"]:
        return ti["primary_pair_address"]
    row = conn.execute(
        """
        SELECT pair_address FROM pairs
        WHERE chain_id = ? AND token_address = ?
        ORDER BY updated_at DESC LIMIT 1
        """,
        (chain_id, token),
    ).fetchone()
    return row["pair_address"] if row else None


def resolve_coverage(
    conn: sqlite3.Connection, chain_id: str, token: str, extras: dict
) -> tuple[Optional[float], str]:
    """Prefer jev_signals.coverage_pct; else jev_confidence*100; else extras."""
    js = conn.execute(
        """
        SELECT coverage_pct FROM jev_signals
        WHERE chain_id = ? AND token_address = ?
        """,
        (chain_id, token),
    ).fetchone()
    if js is not None and js["coverage_pct"] is not None:
        return float(js["coverage_pct"]), "jev_signals.coverage_pct"

    ti = conn.execute(
        """
        SELECT jev_confidence FROM token_indicators
        WHERE chain_id = ? AND token_address = ?
        """,
        (chain_id, token),
    ).fetchone()
    if ti is not None and ti["jev_confidence"] is not None:
        return float(ti["jev_confidence"]) * 100.0, "token_indicators.jev_confidence*100"

    for key in ("coverage", "coverage_pct", "jev_coverage"):
        v = safe_float(extras.get(key))
        if v is not None:
            # extras may store 0-1 or 0-100; treat <=1.5 as fraction
            if v <= 1.5:
                return v * 100.0, f"extras.{key}*100"
            return v, f"extras.{key}"
    conf = safe_float(extras.get("jev_confidence"))
    if conf is not None:
        return conf * 100.0 if conf <= 1.5 else conf, "extras.jev_confidence"
    return None, "missing"


def nearest_snap(
    conn: sqlite3.Connection,
    chain_id: str,
    pair: str,
    t_iso: str,
    tol_sec: float,
) -> Optional[Snap]:
    row = conn.execute(
        """
        SELECT price_usd, liquidity_usd, recorded_at,
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
        return None
    return Snap(
        price=float(row["price_usd"]),
        liquidity=safe_float(row["liquidity_usd"]),
        recorded_at=row["recorded_at"],
        dt_sec=float(row["dt"]),
    )


def liquidity_fallback_pairs(
    conn: sqlite3.Connection, chain_id: str, pair: str
) -> Optional[float]:
    row = conn.execute(
        """
        SELECT raw_json FROM pairs
        WHERE chain_id = ? AND pair_address = ?
        """,
        (chain_id, pair),
    ).fetchone()
    if not row or not row["raw_json"]:
        return None
    try:
        raw = json.loads(row["raw_json"])
    except json.JSONDecodeError:
        return None
    liq = raw.get("liquidity")
    if isinstance(liq, dict):
        return safe_float(liq.get("usd"))
    return safe_float(liq)


def liquidity_near_t0(
    conn: sqlite3.Connection,
    chain_id: str,
    pair: str,
    t_iso: str,
    tol_sec: float,
    snap0: Optional[Snap],
) -> tuple[Optional[float], str]:
    if snap0 and snap0.liquidity is not None:
        return snap0.liquidity, "pair_history.liquidity_usd@t0"
    # Prefer any history row in window with non-null liquidity (closest)
    row = conn.execute(
        """
        SELECT liquidity_usd, recorded_at,
               ABS((julianday(recorded_at) - julianday(?)) * 86400.0) AS dt
        FROM pair_history
        WHERE chain_id = ? AND pair_address = ?
          AND liquidity_usd IS NOT NULL
          AND ABS((julianday(recorded_at) - julianday(?)) * 86400.0) <= ?
        ORDER BY ABS((julianday(recorded_at) - julianday(?)) * 86400.0) ASC
        LIMIT 1
        """,
        (t_iso, chain_id, pair, t_iso, tol_sec, t_iso),
    ).fetchone()
    if row:
        return float(row["liquidity_usd"]), "pair_history.liquidity_usd@near_t0"
    fb = liquidity_fallback_pairs(conn, chain_id, pair)
    if fb is not None:
        return fb, "pairs.raw_json.liquidity.usd (current snapshot)"
    return None, "missing"


def compute_ret(p0: float, p1: float) -> float:
    return (p1 - p0) / p0


def build_sample(
    conn: sqlite3.Connection,
    row: dict,
    tol_sec: float,
    min_liq: float,
    min_coverage: float,
    min_price: float,
) -> Sample:
    chain = row["chain_id"]
    token = row["token_address"]
    pair = resolve_pair(conn, chain, token, row.get("pair_address"))
    cov, cov_src = resolve_coverage(conn, chain, token, row.get("extras") or {})
    t0 = parse_ts(row["ts"])
    t0_iso = to_iso_z(t0)
    snap0 = nearest_snap(conn, chain, pair, t0_iso, tol_sec) if pair else None
    liq, liq_src = (
        liquidity_near_t0(conn, chain, pair, t0_iso, tol_sec, snap0)
        if pair
        else (None, "missing")
    )
    price0 = snap0.price if snap0 else None

    fails: list[str] = []
    if pair is None:
        fails.append("no_pair")
    if price0 is None:
        fails.append("no_price")
    elif price0 < min_price:
        fails.append(f"price<{min_price}")
    if liq is None:
        fails.append("no_liq")
    elif liq < min_liq:
        fails.append(f"liq<{min_liq:g}")
    if cov is None:
        fails.append("no_coverage")
    elif cov < min_coverage:
        fails.append(f"coverage<{min_coverage:g}")

    sample = Sample(
        id=int(row["id"]),
        ts=row["ts"],
        chain_id=chain,
        token_address=token,
        pair_address=pair,
        strength=row.get("strength"),
        coverage=cov,
        coverage_src=cov_src,
        liq_usd=liq,
        liq_src=liq_src,
        price0=price0,
        p0_dt=snap0.dt_sec if snap0 else None,
        p0_at=snap0.recorded_at if snap0 else None,
        ret_1h=None,
        ret_4h=None,
        filter_fail=fails,
    )

    if not pair or price0 is None or price0 <= 0:
        sample.status_1h = "missing"
        sample.status_4h = "missing"
        return sample

    now = datetime.now(timezone.utc)
    for horizon_h, attr_ret, attr_status, attr_dt in (
        (1, "ret_1h", "status_1h", "p1h_dt"),
        (4, "ret_4h", "status_4h", "p4h_dt"),
    ):
        target = t0 + timedelta(hours=horizon_h)
        age = (now - t0).total_seconds()
        if age < horizon_h * 3600 - tol_sec:
            setattr(sample, attr_status, "pending_horizon")
            continue
        snap = nearest_snap(conn, chain, pair, to_iso_z(target), tol_sec)
        if snap is None:
            setattr(sample, attr_status, "missing")
            continue
        setattr(sample, attr_ret, compute_ret(price0, snap.price))
        setattr(sample, attr_status, "ok")
        setattr(sample, attr_dt, snap.dt_sec)

    return sample


@dataclass
class HorizonStats:
    n: int = 0
    up: int = 0
    down: int = 0
    flat: int = 0
    rets: list[float] = field(default_factory=list)

    @property
    def up_rate(self) -> Optional[float]:
        return (self.up / self.n) if self.n else None

    @property
    def non_down_rate(self) -> Optional[float]:
        return ((self.up + self.flat) / self.n) if self.n else None

    @property
    def med(self) -> Optional[float]:
        return median(self.rets)

    @property
    def avg(self) -> Optional[float]:
        return mean(self.rets)

    @property
    def mx(self) -> Optional[float]:
        return max(self.rets) if self.rets else None

    @property
    def mn(self) -> Optional[float]:
        return min(self.rets) if self.rets else None


def collect_stats(samples: Iterable[Sample], horizon: str) -> HorizonStats:
    st = HorizonStats()
    for s in samples:
        if horizon == "1h":
            status, ret = s.status_1h, s.ret_1h
        else:
            status, ret = s.status_4h, s.ret_4h
        if status != "ok" or ret is None:
            continue
        st.n += 1
        st.rets.append(ret)
        if ret > 0:
            st.up += 1
        elif ret < 0:
            st.down += 1
        else:
            st.flat += 1
    return st


def winsorize_stats(st: HorizonStats, cap: float = 5.0) -> HorizonStats:
    """Appendix: drop |ret| > cap (default 500%)."""
    kept = [r for r in st.rets if abs(r) <= cap]
    out = HorizonStats()
    for r in kept:
        out.n += 1
        out.rets.append(r)
        if r > 0:
            out.up += 1
        elif r < 0:
            out.down += 1
        else:
            out.flat += 1
    return out


def pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{100.0 * x:.{digits}f}%"


def pct_ret(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{100.0 * x:+.{digits}f}%"


def strata_bucket(strength: Optional[float]) -> Optional[str]:
    if strength is None:
        return None
    if 0.15 <= strength < 0.17:
        return "0.15–0.17"
    if 0.17 <= strength < 0.19:
        return "0.17–0.19"
    if strength >= 0.19:
        return "0.19+"
    return None


STRATA_ORDER = ["0.15–0.17", "0.17–0.19", "0.19+"]


def unique_token_count(samples: Sequence[Sample]) -> int:
    return len({(s.chain_id, s.token_address) for s in samples})


def decision_line(
    n: int,
    up_rate: Optional[float],
    unique_token: int,
    *,
    two_week_ok: Optional[bool] = None,
) -> str:
    """Effective sample: unique_token≥10 is harder than raw n≥15.

    If raw n≥15 but unique_token<10 → still 样本不足.
    """
    sample_ok = n >= MIN_RAW_N and unique_token >= MIN_UNIQUE_TOKEN
    tw = ""
    if two_week_ok is True:
        tw = (
            f" 连续{CONSECUTIVE_WEEKS}周门槛：满足"
            f"（1h≥{UP_1H_PROMOTE:.0%} 且 unique_token≥{MIN_UNIQUE_TOKEN}）。"
        )
    elif two_week_ok is False:
        tw = (
            f" 连续{CONSECUTIVE_WEEKS}周门槛：未满足（需连续 {CONSECUTIVE_WEEKS} 周 "
            f"1h≥{UP_1H_PROMOTE:.0%} 且 unique_token≥{MIN_UNIQUE_TOKEN}）。"
        )

    if not sample_ok:
        if n >= MIN_RAW_N and unique_token < MIN_UNIQUE_TOKEN:
            return (
                f"n={n}≥{MIN_RAW_N} 但 unique_token={unique_token}<{MIN_UNIQUE_TOKEN} → "
                "样本不足（去重后不足），当前数据下无法确认过滤后有效性；"
                "保留覆盖率爬坡期 caveat，暂缓据此开 Step2。"
                + tw
            )
        return (
            f"n={n}<{MIN_RAW_N}（unique_token={unique_token}）→ 样本不足，"
            "当前数据下无法确认过滤后有效性；"
            "保留覆盖率爬坡期 caveat，暂缓据此开 Step2。"
            + tw
        )
    assert up_rate is not None
    if up_rate >= UP_1H_PROMOTE:
        return (
            f"n={n}≥{MIN_RAW_N} 且 unique_token={unique_token}≥{MIN_UNIQUE_TOKEN} "
            f"且 1h 上涨率 {pct(up_rate)}≥{UP_1H_PROMOTE:.0%} → "
            "有效需过滤，可进 Step2（仍建议 paper 先行）。"
            + tw
        )
    if up_rate < UP_1H_DEGRADE:
        return (
            f"n={n}≥{MIN_RAW_N} 且 unique_token={unique_token}≥{MIN_UNIQUE_TOKEN} "
            f"且 1h 上涨率 {pct(up_rate)}<{UP_1H_DEGRADE:.0%} → "
            "当前数据下单因子确认偏弱，降级建议；Step2 可转向技术面。"
            + tw
        )
    return (
        f"n={n}≥{MIN_RAW_N} 且 unique_token={unique_token}≥{MIN_UNIQUE_TOKEN} "
        f"且 1h 上涨率 {pct(up_rate)} 介于 {UP_1H_DEGRADE:.0%}–{UP_1H_PROMOTE:.0%} → "
        "边缘区间，继续积累样本后再判；暂不据此开 Step2。"
        + tw
    )


def fmt_stats_row(label: str, st: HorizonStats) -> str:
    return (
        f"| **{label}** | {st.n} | {st.up} | {st.down} | {st.flat} | "
        f"**{pct(st.up_rate)}** | {pct(st.non_down_rate)} | "
        f"{pct_ret(st.med)} | {pct_ret(st.avg)} | {pct_ret(st.mx)} | {pct_ret(st.mn)} |"
    )


def alignment_quality(samples: Sequence[Sample]) -> dict[str, tuple[Optional[float], Optional[float]]]:
    def _summ(vals: list[float]) -> tuple[Optional[float], Optional[float]]:
        if not vals:
            return None, None
        return median(vals), max(vals)

    p0 = [s.p0_dt for s in samples if s.p0_dt is not None]
    p1 = [s.p1h_dt for s in samples if s.p1h_dt is not None]
    p4 = [s.p4h_dt for s in samples if s.p4h_dt is not None]
    return {"p0": _summ(p0), "p1h": _summ(p1), "p4h": _summ(p4)}


def run_analysis(
    conn: sqlite3.Connection,
    *,
    min_strength: float,
    min_liq: float,
    min_coverage: float,
    min_price: float,
    tol_sec: float,
) -> dict[str, Any]:
    buys = load_buys(conn, min_strength=min_strength)
    all_samples = [
        build_sample(
            conn,
            b,
            tol_sec=tol_sec,
            min_liq=min_liq,
            min_coverage=min_coverage,
            min_price=min_price,
        )
        for b in buys
    ]
    filtered = [s for s in all_samples if not s.filter_fail]
    cov_sources = sorted({s.coverage_src for s in all_samples})
    liq_sources = sorted({s.liq_src for s in filtered + all_samples})

    st1 = collect_stats(filtered, "1h")
    st4 = collect_stats(filtered, "4h")
    st1_raw = collect_stats(all_samples, "1h")
    st4_raw = collect_stats(all_samples, "4h")

    strata: dict[str, dict[str, HorizonStats]] = {}
    for name in STRATA_ORDER:
        bucket = [s for s in filtered if strata_bucket(s.strength) == name]
        strata[name] = {
            "n_filt": len(bucket),
            "1h": collect_stats(bucket, "1h"),
            "4h": collect_stats(bucket, "4h"),
        }

    unique_token = unique_token_count(filtered)
    return {
        "n_raw": len(all_samples),
        "n_filt": len(filtered),
        "unique_token": unique_token,
        "all_samples": all_samples,
        "filtered": filtered,
        "st1": st1,
        "st4": st4,
        "st1_raw": st1_raw,
        "st4_raw": st4_raw,
        "st1_w": winsorize_stats(st1),
        "st4_w": winsorize_stats(st4),
        "strata": strata,
        "cov_sources": cov_sources,
        "liq_sources": liq_sources,
        "align": alignment_quality(filtered),
        "params": {
            "min_strength": min_strength,
            "min_liq": min_liq,
            "min_coverage": min_coverage,
            "min_price": min_price,
            "tol_sec": tol_sec,
        },
    }


TREND_FIELDS = [
    "date",
    "raw_n",
    "filtered_n",
    "unique_token",
    "up_1h_rate",
    "up_4h_rate",
    "median_1h",
    "median_4h",
    "n_019plus",
    "up_1h_019plus",
    "n_1h",
    "n_4h",
]


def _rate_or_empty(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"{x:.6f}"


def _ret_or_empty(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"{x:.6f}"


def build_trend_row(result: dict[str, Any], when: Optional[datetime] = None) -> dict[str, str]:
    when = when or datetime.now(HK_TZ)
    st1, st4 = result["st1"], result["st4"]
    s019 = result["strata"]["0.19+"]["1h"]
    return {
        "date": when.strftime("%Y-%m-%d"),
        "raw_n": str(result["n_raw"]),
        "filtered_n": str(result["n_filt"]),
        "unique_token": str(result["unique_token"]),
        "up_1h_rate": _rate_or_empty(st1.up_rate),
        "up_4h_rate": _rate_or_empty(st4.up_rate),
        "median_1h": _ret_or_empty(st1.med),
        "median_4h": _ret_or_empty(st4.med),
        "n_019plus": str(result["strata"]["0.19+"]["n_filt"]),
        "up_1h_019plus": _rate_or_empty(s019.up_rate),
        "n_1h": str(st1.n),
        "n_4h": str(st4.n),
    }


def read_trend_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def append_trend_row(path: Path, row: dict[str, str]) -> list[dict[str, str]]:
    """Append one trend row; return prior rows (before this append)."""
    prior = read_trend_rows(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TREND_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)
    return prior


def _parse_rate(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def week_gate_ok(row: dict[str, str]) -> bool:
    """One week satisfies the promote 1h line and unique_token floor."""
    up = _parse_rate(row.get("up_1h_rate", ""))
    ut = _parse_int(row.get("unique_token", ""))
    if up is None or ut is None:
        return False
    return up >= UP_1H_PROMOTE and ut >= MIN_UNIQUE_TOKEN


def two_week_consecutive_ok(prior: list[dict[str, str]], current: dict[str, str]) -> bool:
    """Last CONSECUTIVE_WEEKS rows (including current) all pass week_gate_ok."""
    need_prior = max(CONSECUTIVE_WEEKS - 1, 0)
    if len(prior) < need_prior:
        return False
    window = list(prior[-need_prior:]) + [current] if need_prior else [current]
    return all(week_gate_ok(row) for row in window)


def compare_week_section(
    prior: list[dict[str, str]], current: dict[str, str]
) -> list[str]:
    lines: list[str] = []
    a = lines.append
    a("## 对比上周")
    a("")
    if not prior:
        a("**首次记录**：尚无历史趋势行；本跑已写入 `exports/reports/jev_validity_trend.csv`。")
        a("")
        return lines
    last = prior[-1]
    def _delta_int(key: str) -> str:
        cur = _parse_int(current.get(key, ""))
        prev = _parse_int(last.get(key, ""))
        if cur is None or prev is None:
            return "—"
        d = cur - prev
        sign = "+" if d >= 0 else ""
        return f"{sign}{d}"

    def _delta_rate(key: str) -> str:
        cur = _parse_rate(current.get(key, ""))
        prev = _parse_rate(last.get(key, ""))
        if cur is None or prev is None:
            return "—"
        d = cur - prev
        return f"{d*100:+.1f}pp"

    a(f"相对上次（`{last.get('date', '?')}`）→ 本次（`{current.get('date', '?')}`）：")
    a("")
    a("| 指标 | 上次 | 本次 | Δ |")
    a("|------|------|------|---|")
    a(
        f"| filtered_n (n) | {last.get('filtered_n', '—')} | "
        f"{current.get('filtered_n', '—')} | {_delta_int('filtered_n')} |"
    )
    a(
        f"| unique_token | {last.get('unique_token', '—')} | "
        f"{current.get('unique_token', '—')} | {_delta_int('unique_token')} |"
    )
    prev_up = _parse_rate(last.get("up_1h_rate", ""))
    cur_up = _parse_rate(current.get("up_1h_rate", ""))
    a(
        f"| 1h 上涨率 | {pct(prev_up)} | {pct(cur_up)} | {_delta_rate('up_1h_rate')} |"
    )
    a(
        f"| 0.19+ 层 n | {last.get('n_019plus', '—')} | "
        f"{current.get('n_019plus', '—')} | {_delta_int('n_019plus')} |"
    )
    a("")
    return lines


def render_md(
    primary: dict[str, Any],
    contrast: Optional[dict[str, Any]],
    db_path: Path,
    *,
    trend_prior: Optional[list[dict[str, str]]] = None,
    trend_row: Optional[dict[str, str]] = None,
    two_week_ok: Optional[bool] = None,
) -> str:
    p = primary["params"]
    lines: list[str] = []
    a = lines.append

    a("# Decision Engine — JEV 有效性复验（过滤版）")
    a("")
    a(f"**Time (HK / UTC+8):** {now_hk_label()}")
    a(f"**Repo:** `/workspace/dexscreener-crawler`")
    a(f"**DB:** `{db_path.name}`（只读业务表；本报告由 `scripts/jev_validity.py` 生成）")
    a(f"**策略:** `{STRATEGY_ID}` / `action=buy`")
    a("")
    a("## 原则纠正")
    a("")
    a(
        "上次快验（`DECISION_ENGINE_JEV_VALIDITY.md`）只能说明"
        "「**阈值 0.15 + 零过滤**」弱信号集缺乏前向上涨预测力，"
        "**不能**直接判 JEV 维度无用。"
    )
    a("")
    a("已知偏差：")
    a("")
    a("- 低置信强度（全部 `>=0.15`，max≈0.205，远低于默认 0.3）")
    a("- 混入归零币 / 极低流动性")
    a("- 大量 flat / 冷门 pair（稀疏快照）")
    a("- **覆盖率仍在爬坡期**（库内 max `jev_confidence`≈0.25 / coverage≈25%）")
    a("")
    a("本报告在**同一批** `decision_log` buy 上加重过滤与分层，结论一律带「**当前数据下**」caveat。")
    a("")
    a("## 过滤口径")
    a("")
    a("| 过滤 | 默认 | 来源 |")
    a("|------|------|------|")
    a(
        f"| `liquidity.usd >= min_liq` | **{p['min_liq']:g}** "
        f"（对照可试 100000） | 优先 `pair_history.liquidity_usd` 在 t0±tol；"
        "缺失时回退 `pairs.raw_json.liquidity.usd`（**当前**快照，非历史） |"
    )
    a(
        f"| JEV coverage >= {p['min_coverage']:g} | **{p['min_coverage']:g}** | "
        "优先 **`jev_signals.coverage_pct`**（0–100）；"
        "否则 `token_indicators.jev_confidence * 100`；"
        f"本批实际用到：`{', '.join(primary['cov_sources'])}` |"
    )
    a(
        f"| 排除信号时价格 < min_price 或无对价 | **{p['min_price']}** | "
        "`pair_history.price_usd` @ t0±tol |"
    )
    a(
        f"| strength 下限（与信号生成一致） | **{p['min_strength']}** | "
        "`raw_json.extras.jev_strength` / reason 解析 |"
    )
    a(
        f"| 价格对齐容差 | **±{int(p['tol_sec'])}s (±30min)** | "
        "与上次快验一致 |"
    )
    a("")
    a("分层（过滤后）：strength `0.15–0.17` / `0.17–0.19` / `0.19+`。")
    a("")
    a("主指标：**中位数**；均值仅参考。flat（ret==0）单列；同时报上涨率(ret>0)与未跌率(ret≥0)。")
    a("")
    a("## 样本规模")
    a("")
    a("| 集合 | n |")
    a("|------|---|")
    a(f"| 过滤前（min_strength≥{p['min_strength']} buy） | **{primary['n_raw']}** |")
    a(
        f"| 过滤后（liq≥{p['min_liq']:g}, cov≥{p['min_coverage']:g}, "
        f"price≥{p['min_price']}） | **{primary['n_filt']}** |"
    )
    a(
        f"| **unique_token**（过滤后按 chain_id+token_address 去重） | "
        f"**{primary['unique_token']}** |"
    )
    if contrast is not None:
        a(
            f"| 对照过滤（liq≥{contrast['params']['min_liq']:g}, 其余同） | "
            f"**{contrast['n_filt']}** "
            f"（unique_token={contrast['unique_token']}） |"
        )
    a("")
    a(
        f"有效样本门槛：优先看 **unique_token≥{MIN_UNIQUE_TOKEN}**"
        f"（比 raw/过滤后 n≥{MIN_RAW_N} 更硬）；"
        f"若 n≥{MIN_RAW_N} 但 unique_token<{MIN_UNIQUE_TOKEN} → 仍判「样本不足」。"
    )
    a("")

    # drop reasons
    from collections import Counter

    fails = Counter()
    for s in primary["all_samples"]:
        if s.filter_fail:
            for f in s.filter_fail:
                fails[f] += 1
    if fails:
        a("过滤失败原因计数（可重叠）：")
        a("")
        for k, v in fails.most_common():
            a(f"- `{k}`: {v}")
        a("")

    a("### 价格对齐质量（过滤后）")
    a("")
    a("| 锚点 | median abs(Δt) | max abs(Δt) |")
    a("|------|-----------------|--------------|")
    for key, label in (("p0", "p0"), ("p1h", "p1h"), ("p4h", "p4h")):
        med_dt, max_dt = primary["align"][key]
        med_s = f"{med_dt:.0f}s" if med_dt is not None else "—"
        max_s = f"{max_dt:.0f}s" if max_dt is not None else "—"
        a(f"| {label} | {med_s} | {max_s} |")
    a("")

    a("## 过滤前（对照，零过滤）")
    a("")
    a(
        "| 窗口 | n | 上涨 | 下跌 | flat | **上涨率** | 未跌率 | "
        "中位收益 | 均值(参考) | 最大 | 最小 |"
    )
    a("|------|---|------|------|------|------------|--------|----------|------------|------|------|")
    a(fmt_stats_row("1h", primary["st1_raw"]))
    a(fmt_stats_row("4h", primary["st4_raw"]))
    a("")

    a(f"## 过滤后（默认 liq≥{p['min_liq']:g}）")
    a("")
    a(
        "| 窗口 | n | 上涨 | 下跌 | flat | **上涨率** | 未跌率 | "
        "中位收益 | 均值(参考) | 最大 | 最小 |"
    )
    a("|------|---|------|------|------|------------|--------|----------|------------|------|------|")
    a(fmt_stats_row("1h", primary["st1"]))
    a(fmt_stats_row("4h", primary["st4"]))
    a("")
    a(
        f"决策摘要（按 1h 上涨率 + unique_token）："
        f"**{decision_line(primary['st1'].n, primary['st1'].up_rate, primary['unique_token'], two_week_ok=two_week_ok)}**"
    )
    a("")

    if contrast is not None:
        cp = contrast["params"]
        a(f"## 对照过滤（liq≥{cp['min_liq']:g}）")
        a("")
        a(
            "| 窗口 | n | 上涨 | 下跌 | flat | **上涨率** | 未跌率 | "
            "中位收益 | 均值(参考) | 最大 | 最小 |"
        )
        a("|------|---|------|------|------|------------|--------|----------|------------|------|------|")
        a(fmt_stats_row("1h", contrast["st1"]))
        a(fmt_stats_row("4h", contrast["st4"]))
        a("")

    # 对比上周（趋势 CSV）
    if trend_row is not None:
        for line in compare_week_section(trend_prior or [], trend_row):
            a(line)

    a("## 分层表（默认过滤后，按 strength）")
    a("")
    a(
        "| 分层 | n_filt | 1h n | 1h 上涨率 | 1h 中位 | 4h n | 4h 上涨率 | 4h 中位 |"
    )
    a("|------|--------|------|-----------|---------|------|-----------|---------|")
    for name in STRATA_ORDER:
        bucket = primary["strata"][name]
        s1, s4 = bucket["1h"], bucket["4h"]
        a(
            f"| {name} | {bucket['n_filt']} | {s1.n} | {pct(s1.up_rate)} | "
            f"{pct_ret(s1.med)} | {s4.n} | {pct(s4.up_rate)} | {pct_ret(s4.med)} |"
        )
    a("")

    a("## 解读决策表")
    a("")
    a("| 条件 | 解读 |")
    a("|------|------|")
    a(f"| n<{MIN_RAW_N}，或 n≥{MIN_RAW_N} 但 unique_token<{MIN_UNIQUE_TOKEN} | 样本不足 |")
    a(
        f"| n≥{MIN_RAW_N} 且 unique_token≥{MIN_UNIQUE_TOKEN} 且上涨率≥{UP_1H_PROMOTE:.0%} | "
        "有效需过滤，可进 Step2 |"
    )
    a(
        f"| n≥{MIN_RAW_N} 且 unique_token≥{MIN_UNIQUE_TOKEN} 且上涨率仍<{UP_1H_DEGRADE:.0%} | "
        "当前数据下单因子确认偏弱，降级建议 + Step2 可转向技术面 |"
    )
    a(f"| 其他（{UP_1H_DEGRADE:.0%}–{UP_1H_PROMOTE:.0%}） | 边缘，继续积累 |")
    a(
        f"| 连续 {CONSECUTIVE_WEEKS} 周 1h≥{UP_1H_PROMOTE:.0%} "
        f"且 unique_token≥{MIN_UNIQUE_TOKEN} | Step2 启动的周检硬门槛（见趋势 CSV） |"
    )
    a("")
    a(
        f"**本跑判定：** "
        f"{decision_line(primary['st1'].n, primary['st1'].up_rate, primary['unique_token'], two_week_ok=two_week_ok)}"
    )
    a("")
    if two_week_ok is True:
        a(
            f"**连续 {CONSECUTIVE_WEEKS} 周门槛：** 满足"
            f"（最近 {CONSECUTIVE_WEEKS} 周均 1h≥{UP_1H_PROMOTE:.0%} "
            f"且 unique_token≥{MIN_UNIQUE_TOKEN}）。"
        )
    elif two_week_ok is False:
        a(
            f"**连续 {CONSECUTIVE_WEEKS} 周门槛：** 未满足（需连续 {CONSECUTIVE_WEEKS} 周 "
            f"1h≥{UP_1H_PROMOTE:.0%} 且 unique_token≥{MIN_UNIQUE_TOKEN}）。"
        )
    else:
        a(f"**连续 {CONSECUTIVE_WEEKS} 周门槛：** 无历史可比（首次）→ 未满足。")
    a("")

    a("## 附录：排除 |ret|>500%")
    a("")
    a(
        "| 窗口 | n | 上涨率 | 中位 | 均值(参考) |"
    )
    a("|------|---|--------|------|------------|")
    for label, st in (("1h", primary["st1_w"]), ("4h", primary["st4_w"])):
        a(
            f"| {label} | {st.n} | {pct(st.up_rate)} | "
            f"{pct_ret(st.med)} | {pct_ret(st.avg)} |"
        )
    a("")

    a("## 过滤后逐条摘要")
    a("")
    a(
        "| id | ts (UTC) | token | strength | cov | liq | "
        "ret_1h | ret_4h | 1h/4h |"
    )
    a("|----|----------|-------|----------|-----|-----|--------|--------|-------|")
    for s in primary["filtered"]:
        cov_s = f"{s.coverage:.0f}" if s.coverage is not None else "—"
        liq_s = f"{s.liq_usd:,.0f}" if s.liq_usd is not None else "—"
        str_s = f"{s.strength:.4f}" if s.strength is not None else "—"
        a(
            f"| {s.id} | {s.ts} | {s.token_short} | {str_s} | {cov_s} | {liq_s} | "
            f"{pct_ret(s.ret_1h)} | {pct_ret(s.ret_4h)} | "
            f"{s.status_1h}/{s.status_4h} |"
        )
    a("")

    a("## Caveats")
    a("")
    a("- **覆盖率爬坡期**：当前库 max coverage/confidence 仍低；过滤不能消除数据幼态。")
    a("- 信号全部来自降低阈值 0.15 的验证跑，不是默认 0.3。")
    a("- `jev_signals` / `token_indicators` 为 token 级最新行，非严格信号时刻快照。")
    a("- `pairs.raw_json` 流动性回退是**当前**值，可能与 t0 不一致（本批多数用 history）。")
    a("- 无手续费 / 滑点 / 买卖价差；未做卖出规则。")
    a(
        "- 收益统计仍按信号条数（可跨批次重复 token）；"
        f"**unique_token** 单独计数并参与门槛（≥{MIN_UNIQUE_TOKEN}）。"
    )
    a("- 未做统计显著性检验。")
    a("")
    a("## 复现")
    a("")
    a("```bash")
    a("cd /workspace/dexscreener-crawler")
    a(".venv/bin/python scripts/jev_validity.py \\")
    a(f"  --db {db_path} \\")
    a(f"  --min-strength {p['min_strength']} --min-liq {p['min_liq']:g} \\")
    a(f"  --min-coverage {p['min_coverage']:g} --min-price {p['min_price']} \\")
    a(f"  --tol-sec {int(p['tol_sec'])} \\")
    a("  --out DECISION_ENGINE_JEV_VALIDITY_FILTERED.md")
    a("")
    a("# 流动性对照")
    a(".venv/bin/python scripts/jev_validity.py --min-liq 100000 --out /tmp/jev_liq100k.md")
    a("```")
    a("")
    return "\n".join(lines) + "\n"


def print_summary(
    label: str,
    result: dict[str, Any],
    *,
    two_week_ok: Optional[bool] = None,
) -> None:
    p = result["params"]
    st1, st4 = result["st1"], result["st4"]
    print(f"=== {label} ===")
    print(
        f"filters: strength>={p['min_strength']} liq>={p['min_liq']:g} "
        f"cov>={p['min_coverage']:g} price>={p['min_price']} tol=±{int(p['tol_sec'])}s"
    )
    print(
        f"n_raw={result['n_raw']}  n_filt={result['n_filt']}  "
        f"unique_token={result['unique_token']}"
    )
    print(
        f"1h: n={st1.n} up={pct(st1.up_rate)} non_down={pct(st1.non_down_rate)} "
        f"med={pct_ret(st1.med)} mean={pct_ret(st1.avg)} flat={st1.flat}"
    )
    print(
        f"4h: n={st4.n} up={pct(st4.up_rate)} non_down={pct(st4.non_down_rate)} "
        f"med={pct_ret(st4.med)} mean={pct_ret(st4.avg)} flat={st4.flat}"
    )
    print("strata:", end=" ")
    bits = []
    for name in STRATA_ORDER:
        b = result["strata"][name]
        bits.append(f"{name}:n={b['n_filt']}/1h_up={pct(b['1h'].up_rate)}")
    print(" | ".join(bits))
    print(
        "decision:",
        decision_line(st1.n, st1.up_rate, result["unique_token"], two_week_ok=two_week_ok),
    )
    print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Filtered JEV validity re-check")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--min-strength", type=float, default=0.15)
    ap.add_argument("--min-liq", type=float, default=50000.0)
    ap.add_argument("--min-coverage", type=float, default=10.0)
    ap.add_argument("--min-price", type=float, default=1e-6)
    ap.add_argument("--tol-sec", type=float, default=1800.0)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Markdown report path (default: repo DECISION_ENGINE_JEV_VALIDITY_FILTERED.md)",
    )
    ap.add_argument(
        "--contrast-liq",
        type=float,
        default=100000.0,
        help="Also run a liquidity contrast (set 0 to skip)",
    )
    ap.add_argument("--no-contrast", action="store_true")
    ap.add_argument(
        "--trend-csv",
        type=Path,
        default=None,
        help="Trend CSV to append (default: exports/reports/jev_validity_trend.csv)",
    )
    ap.add_argument(
        "--no-trend",
        action="store_true",
        help="Skip appending/reading the trend CSV",
    )
    args = ap.parse_args(argv)

    db_path = args.db.resolve()
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    out_path = args.out
    if out_path is None:
        out_path = db_path.parent / "DECISION_ENGINE_JEV_VALIDITY_FILTERED.md"

    conn = open_db(db_path)
    try:
        primary = run_analysis(
            conn,
            min_strength=args.min_strength,
            min_liq=args.min_liq,
            min_coverage=args.min_coverage,
            min_price=args.min_price,
            tol_sec=args.tol_sec,
        )
        contrast = None
        if not args.no_contrast and args.contrast_liq and args.contrast_liq != args.min_liq:
            contrast = run_analysis(
                conn,
                min_strength=args.min_strength,
                min_liq=args.contrast_liq,
                min_coverage=args.min_coverage,
                min_price=args.min_price,
                tol_sec=args.tol_sec,
            )
    finally:
        conn.close()

    trend_prior: list[dict[str, str]] = []
    trend_row: Optional[dict[str, str]] = None
    two_week_ok: Optional[bool] = None
    trend_path: Optional[Path] = None
    if not args.no_trend:
        trend_path = (args.trend_csv or DEFAULT_TREND_CSV).resolve()
        trend_row = build_trend_row(primary)
        trend_prior = append_trend_row(trend_path, trend_row)
        if not trend_prior:
            two_week_ok = None  # 首次：无上周可比
        else:
            two_week_ok = two_week_consecutive_ok(trend_prior, trend_row)
        print(f"trend: appended → {trend_path} (prior_rows={len(trend_prior)})")

    print_summary(
        f"primary liq>={args.min_liq:g}", primary, two_week_ok=two_week_ok
    )
    if contrast is not None:
        print_summary(f"contrast liq>={args.contrast_liq:g}", contrast)

    md = render_md(
        primary,
        contrast,
        db_path,
        trend_prior=trend_prior if not args.no_trend else None,
        trend_row=trend_row,
        two_week_ok=two_week_ok if not args.no_trend else None,
    )
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
