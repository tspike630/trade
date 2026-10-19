"""Section 10: evolution suggestions (read-only observations; never mutates params)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from review.data import SignalChainRow
from review.gates import (
    CONSECUTIVE_WEEKS,
    MIN_RAW_N as JEV_N_GATE,
    MIN_UNIQUE_TOKEN as JEV_UNIQUE_TOKEN_GATE,
    UP_1H_DEGRADE as JEV_UP1H_WEAK,
    UP_1H_PROMOTE as JEV_UP1H_PASS,
)
from review.optimizer import (
    MIN_HISTORY_DAYS,
    MIN_SAMPLES,
    OptimizeResult,
    check_optimize_gates,
    estimate_history_days,
)

# status vocabulary (钉死)
STATUS_WATCH = "观察中"
STATUS_SUGGEST = "建议落地"
STATUS_DECIDE = "裁决"

LAYERS = ("采集", "指标", "决策", "执行", "复盘")

def _pct(rate: Optional[float], digits: int = 1) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.{digits}f}%"


def _suggestion(
    *,
    layer: str,
    item: str,
    status: str,
    evidence: str,
    action_hint: str,
) -> dict[str, str]:
    return {
        "layer": layer,
        "item": item,
        "status": status,
        "evidence": evidence,
        "action_hint": action_hint,
    }


def _count_news_events(db_root: Path) -> tuple[Optional[int], str]:
    """Return (count, note). count=None if db missing / unreadable."""
    path = db_root / "news_events.db"
    if not path.is_file():
        return None, "news_events.db 不存在（旁路未建或未拉取）"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
            return int(n), f"news_events.db 可读，累计 {n} 条"
        finally:
            conn.close()
    except Exception as e:
        return None, f"news_events.db 打开失败: {e}"


def _read_indicator_weights() -> dict[str, float]:
    """Best-effort import of INDICATORS_WEIGHT_* (default 0)."""
    try:
        import config  # type: ignore

        return {
            "TECHNICAL": float(getattr(config, "INDICATORS_WEIGHT_TECHNICAL", 0) or 0),
            "FUNDING": float(getattr(config, "INDICATORS_WEIGHT_FUNDING", 0) or 0),
            "SENTIMENT": float(getattr(config, "INDICATORS_WEIGHT_SENTIMENT", 0) or 0),
        }
    except Exception:
        return {"TECHNICAL": 0.0, "FUNDING": 0.0, "SENTIMENT": 0.0}


def _token_age_unknown_ratio(attribution: Optional[dict[str, Any]]) -> tuple[int, int]:
    """Return (n_unknown, n_total) from attribution token_age buckets."""
    if not attribution:
        return 0, 0
    dims = attribution.get("dimensions") or {}
    age = dims.get("token_age") or {}
    buckets = age.get("buckets") or {}
    n_unknown = int((buckets.get("unknown") or {}).get("n", 0) or 0)
    n_total = int(attribution.get("n", 0) or 0)
    if n_total <= 0:
        n_total = sum(int((b or {}).get("n", 0) or 0) for b in buckets.values())
    return n_unknown, n_total


def _liq_differentiation_note(attribution: Optional[dict[str, Any]]) -> str:
    """Summarize liq bucket up_1h spread as candidate focus (observation only)."""
    if not attribution:
        return "归因未计算，liq 分层区分度待各层交来"
    buckets = ((attribution.get("dimensions") or {}).get("liq") or {}).get("buckets") or {}
    parts: list[str] = []
    for lab in ("<50k", "50-500k", "≥500k", "unknown"):
        m = buckets.get(lab) or {}
        n = m.get("n", 0)
        rate = m.get("up_1h_rate")
        parts.append(f"{lab}: n={n} up_1h={_pct(rate)}")
    rates = [
        (lab, (buckets.get(lab) or {}).get("up_1h_rate"))
        for lab in ("<50k", "50-500k", "≥500k")
        if (buckets.get(lab) or {}).get("up_1h_rate") is not None
        and (buckets.get(lab) or {}).get("n", 0)
    ]
    spread = ""
    if len(rates) >= 2:
        vals = [r for _, r in rates if r is not None]
        spread = f"；跨桶 up_1h 极差={_pct(max(vals) - min(vals))}"
    return "；".join(parts) + spread


def _jev_gate_distance(jev_slice_metrics: Optional[dict[str, Any]]) -> str:
    if not jev_slice_metrics:
        return "无 JEV 过滤切片样本，门槛距离待决策层交来"
    ut = int(jev_slice_metrics.get("unique_token", 0) or 0)
    n = int(jev_slice_metrics.get("n", 0) or 0)
    up1 = jev_slice_metrics.get("up_1h_rate")
    d_ut = JEV_UNIQUE_TOKEN_GATE - ut
    d_n = JEV_N_GATE - n
    bits = [
        f"过滤后 n={n}（距≥{JEV_N_GATE}：{'已达' if d_n <= 0 else f'差 {d_n}'}）",
        f"unique_token={ut}（距≥{JEV_UNIQUE_TOKEN_GATE}：{'已达' if d_ut <= 0 else f'差 {d_ut}'}）",
        f"up_1h={_pct(up1)}（通过线≥{_pct(JEV_UP1H_PASS)} / 弱线<{_pct(JEV_UP1H_WEAK)}）",
    ]
    return "；".join(bits)


def _r3_skip_evidence(
    *,
    opt_result: Optional[OptimizeResult],
    chain: Sequence[SignalChainRow],
    period_start: Optional[str],
    period_end: Optional[str],
) -> str:
    if opt_result is not None:
        return (
            f"status=`{opt_result.status}`"
            + (f"：{opt_result.reason}" if opt_result.reason else "")
            + f"；history_days={opt_result.history_days}；grid={opt_result.param_grid_size}"
        )
    # synthesize gate check without running full optimize
    history_days = estimate_history_days(
        list(chain), period_start=period_start, period_end=period_end
    )
    ok, reason = check_optimize_gates(
        history_days=history_days,
        n_samples=len(chain),
        min_history_days=MIN_HISTORY_DAYS,
        min_samples=MIN_SAMPLES,
        weekly_check_passed=False,
    )
    if ok:
        return (
            f"闸门可过（history≈{history_days:.2f}d, n={len(chain)}），"
            "但本跑未传 --optimize；正式寻优仍需周检跨门槛且不自动写配置"
        )
    return f"未跑 --optimize；按闸门预检将 skip：{reason}"



# --- PRIOR / self_survival / baseline_history (read-only §10 observers) ---
HK = timezone(timedelta(hours=8))
STABLE_WINDOW_DAYS = 14
STABLE_SURVIVE_4H_DELTA = 0.05


def _parse_recorded_at(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=HK)
        return dt
    except Exception:
        return None


def _load_self_db_snapshot(db_root: Path) -> dict[str, Any]:
    """Read baseline_params.json#self_db + latest self_survival_*.md (observation only)."""
    bp = db_root / "data" / "prior" / "exports" / "baseline_params.json"
    out: dict[str, Any] = {
        "ok": False,
        "note": "baseline_params.json 缺失",
        "report": None,
    }
    if not bp.is_file():
        return out
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["note"] = f"baseline_params.json 读失败: {e}"
        return out
    self_db = data.get("self_db") or {}
    if not self_db:
        out["note"] = "baseline_params.json 无 self_db（先跑 scripts/self_survival.py）"
        return out
    exports = db_root / "data" / "prior" / "exports"
    report_rel = None
    if exports.is_dir():
        cands = sorted(exports.glob("self_survival_*.md"), reverse=True)
        if cands:
            report_rel = f"data/prior/exports/{cands[0].name}"
    span = self_db.get("span_min") or {}
    survive = self_db.get("survive") or {}
    out.update(
        {
            "ok": True,
            "note": self_db.get("note") or "self_db 观测跨度存活",
            "n": self_db.get("n"),
            "survive": dict(survive),
            "survive_n": dict(self_db.get("survive_n") or {}),
            "crawler_window_h": self_db.get("crawler_window_h"),
            "span_min": dict(span),
            "sparse": bool(self_db.get("sparse")),
            "warnings": list(self_db.get("warnings") or []),
            "report": report_rel,
            "source": "baseline_params.json#self_db",
        }
    )
    return out


def _self_survival_evidence(snap: dict[str, Any]) -> str:
    if not snap.get("ok"):
        return snap.get("note") or "self_db 不可得"
    survive = snap.get("survive") or {}
    span = snap.get("span_min") or {}
    n = snap.get("n")
    win = snap.get("crawler_window_h")
    sparse = snap.get("sparse")
    report = snap.get("report") or "—"

    def _s(key: str) -> str:
        v = survive.get(key)
        return _pct(float(v), 1) if v is not None else "—"

    bits = [
        f"n={n}",
        f"1h/4h/24h/7d={_s('1h')}/{_s('4h')}/{_s('24h')}/{_s('168h')}",
    ]
    if win is not None:
        bits.append(f"窗长≈{float(win):.1f}h")
    p50 = span.get("p50")
    smax = span.get("max")
    if p50 is not None:
        bits.append(f"跨度 P50≈{float(p50) / 60.0:.2f}h")
    if smax is not None:
        bits.append(f"max≈{float(smax) / 60.0:.2f}h")
    bits.append(f"sparse={'yes' if sparse else 'no'}")
    bits.append("只读 pair_history；裁决前不调权")
    bits.append(f"报告={report}")
    return "；".join(bits)


def _stability_verdict(rows: list[tuple]) -> tuple[str, str]:
    """Align with scripts/prior_baseline_trend.print_stability (observational).

    rows: (id, recorded_at, survive_4h, ...) ascending by id.
    Returns (verdict, detail).
    """
    if not rows:
        return "INSUFFICIENT_DATA", "no rows yet"
    cutoff = datetime.now(HK) - timedelta(days=STABLE_WINDOW_DAYS)
    in_win = []
    for r in rows:
        dt = _parse_recorded_at(r[1])
        if dt is None:
            continue
        if dt.astimezone(HK) >= cutoff:
            in_win.append(r)
    sample = in_win if len(in_win) >= 2 else rows
    s4 = [r[2] for r in sample if r[2] is not None]
    detail_bits = [
        f"window={STABLE_WINDOW_DAYS}d",
        f"points_in_window={len(in_win)}",
        f"used={len(sample)}",
    ]
    if len(s4) < 2:
        return (
            "INSUFFICIENT_DATA",
            " · ".join(detail_bits) + f" · survive_4h points={len(s4)}",
        )
    first, last = s4[0], s4[-1]
    delta = last - first
    span = max(s4) - min(s4)
    ok = abs(delta) <= STABLE_SURVIVE_4H_DELTA and span <= STABLE_SURVIVE_4H_DELTA
    first_dt = _parse_recorded_at(sample[0][1])
    last_dt = _parse_recorded_at(sample[-1][1])
    calendar_days = None
    if first_dt and last_dt:
        calendar_days = (last_dt - first_dt).total_seconds() / 86400.0
        detail_bits.append(f"calendar={calendar_days:.1f}d")
        if calendar_days < STABLE_WINDOW_DAYS * 0.8:
            ok = False
    if len(in_win) < 2:
        ok = False
    detail_bits.append(
        f"survive_4h {_pct(float(first), 2)}→{_pct(float(last), 2)} "
        f"· Δ={delta:+.4f} · span={span:.4f}"
    )
    verdict = "STABLE" if ok else "UNSTABLE_OR_SHORT"
    return verdict, " · ".join(detail_bits)


def load_baseline_history_report(
    db_root: Path, *, limit: int = 8
) -> dict[str, Any]:
    """Read prior.db.baseline_history recent rows + stability (skip on failure)."""
    db_path = db_root / "data" / "prior" / "db" / "prior.db"
    rel = "data/prior/db/prior.db"
    if not db_path.is_file():
        return {"ok": False, "skip": True, "note": f"{rel} 缺失，跳过基线滚动快照"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            total = conn.execute("SELECT COUNT(*) FROM baseline_history").fetchone()[0]
            recent = conn.execute(
                """
                SELECT id, recorded_at, source,
                       survive_1h, survive_4h, survive_24h, survive_7d,
                       crawler_window_hours, n_tokens
                FROM baseline_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            all_rows = conn.execute(
                """
                SELECT id, recorded_at, survive_4h
                FROM baseline_history
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "skip": True,
            "note": f"读 baseline_history 失败，跳过：{e}",
        }
    verdict, detail = _stability_verdict(list(all_rows))
    latest = recent[0] if recent else None
    return {
        "ok": True,
        "skip": False,
        "db": rel,
        "total": int(total),
        "recent": [
            {
                "id": r[0],
                "recorded_at": r[1],
                "source": r[2],
                "survive_1h": r[3],
                "survive_4h": r[4],
                "survive_24h": r[5],
                "survive_7d": r[6],
                "crawler_window_hours": r[7],
                "n_tokens": r[8],
            }
            for r in recent
        ],
        "latest": (
            {
                "id": latest[0],
                "recorded_at": latest[1],
                "source": latest[2],
                "survive_1h": latest[3],
                "survive_4h": latest[4],
                "crawler_window_hours": latest[7],
                "n_tokens": latest[8],
            }
            if latest
            else None
        ),
        "verdict": verdict,
        "verdict_detail": detail,
    }


def _baseline_history_evidence(rep: dict[str, Any]) -> str:
    if not rep.get("ok"):
        return rep.get("note") or "baseline_history 不可得"
    latest = rep.get("latest") or {}
    n_show = len(rep.get("recent") or [])
    total = rep.get("total", 0)

    def _sp(v: Any) -> str:
        return _pct(float(v), 2) if v is not None else "—"

    win = latest.get("crawler_window_hours")
    win_s = f"{float(win):.2f}h" if win is not None else "—"
    return (
        f"最近 {n_show}/{total} 条；最新 survive_1h/4h="
        f"{_sp(latest.get('survive_1h'))}/{_sp(latest.get('survive_4h'))} "
        f"窗长={win_s}；n={latest.get('n_tokens')}；"
        f"id={latest.get('id')} source={latest.get('source')}；"
        f"稳定判定=`{rep.get('verdict')}`（{rep.get('verdict_detail')}）；"
        "详见表下「基线滚动快照」小节"
    )


def format_baseline_history_section(rep: dict[str, Any]) -> str:
    """Markdown subsection for §10 baseline rolling snapshot."""
    lines: list[str] = []
    lines.append("### 基线滚动快照（baseline_history，只观察）")
    lines.append("")
    if not rep.get("ok"):
        lines.append(f"> 跳过：{rep.get('note') or 'prior.db 不可读'}")
        lines.append("")
        return "\n".join(lines)
    lines.append(
        "> PRIOR_STEP6 旁路：`prior.db.baseline_history` 只追加快照；"
        "**不**调权、不进风控。稳定判定对齐 `prior_baseline_trend.py`"
        "（近 14d survive_4h |Δ| 与 span ≤0.05 且日历跨度够约两周 → `STABLE`；"
        "否则 `SHORT` / `UNSTABLE_OR_SHORT` / `INSUFFICIENT_DATA`）。"
    )
    lines.append("")
    recent = rep.get("recent") or []
    lines.append(
        f"最近 **{len(recent)}** 条（共 {rep.get('total', 0)}）；"
        f"库=`{rep.get('db')}`。"
    )
    lines.append("")
    lines.append(
        "| id | recorded_at | source | survive_1h | survive_4h | 窗长(h) | n_tokens |"
    )
    lines.append("|---:|---|---|---:|---:|---:|---:|")
    for r in recent:
        s1 = _pct(float(r["survive_1h"]), 2) if r.get("survive_1h") is not None else "—"
        s4 = _pct(float(r["survive_4h"]), 2) if r.get("survive_4h") is not None else "—"
        win = r.get("crawler_window_hours")
        win_s = f"{float(win):.2f}" if win is not None else "—"
        lines.append(
            f"| {r.get('id')} | {r.get('recorded_at')} | {r.get('source')} | "
            f"{s1} | {s4} | {win_s} | {r.get('n_tokens')} |"
        )
    lines.append("")
    lines.append(
        f"**稳定判定：** `{rep.get('verdict')}` — {rep.get('verdict_detail')}"
        "（observational only）"
    )
    lines.append("")
    lines.append("说明：只观察不调权；RISK_USE_PRIOR 默认关；稳定两周才谈进风控")
    lines.append("")
    return "\n".join(lines)


def _normalize_price_behavior(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten price_behavior.json (or baseline_params#price_behavior) for §10."""
    meta = data.get("meta") or {}
    # Prefer full export schema; fall back to baseline_params summary keys
    win = meta.get("crawler_window_h", data.get("crawler_window_h"))
    short = meta.get("short", data.get("short"))
    n_tok = data.get("n_tokens_analyzed", data.get("n_tokens"))
    returns = data.get("returns") or {}
    ret1 = returns.get("1h") or {}
    # pump: export uses pump['4h']; summary uses pump_4h
    pump_block = data.get("pump_4h")
    if pump_block is None:
        pump4 = ((data.get("pump") or {}).get("4h") or {})
        mult = (pump4.get("multiple") or {}).get("median")
        dur = (pump4.get("duration_min") or {}).get("median")
        # n may sit on multiple or duration
        n_pump = (pump4.get("multiple") or {}).get("n") or (pump4.get("duration_min") or {}).get("n")
        pump_block = {
            "multiple_p50": mult,
            "duration_min_p50": dur,
            "n": n_pump,
        }
    mdd_block = data.get("mdd_4h")
    if mdd_block is None:
        mdd = ((data.get("drawdown") or {}).get("mdd_4h") or {})
        mdd_block = {
            "p50": mdd.get("median", mdd.get("p50")),
            "p90": mdd.get("p90"),
            "n": mdd.get("n"),
        }
    jev = data.get("jev") or {}
    jev_note = data.get("jev_cross_note") or jev.get("note")
    if not jev_note and jev.get("n_with_series") is not None:
        jev_note = (
            f"小样本（n_signals={jev.get('n_signals')}，"
            f"n_with_series={jev.get('n_with_series')}）；仅列表"
        )
    elif not jev_note and data.get("jev_n_with_series") is not None:
        jev_note = f"jev_n_with_series={data.get('jev_n_with_series')}（小样本，仅列表）"
    return {
        "crawler_window_h": win,
        "short": bool(short) if short is not None else False,
        "n_tokens": n_tok,
        "ret1": ret1,
        "pump": pump_block,
        "mdd": mdd_block,
        "jev_note": jev_note,
    }


def _load_price_behavior_snapshot(db_root: Path) -> dict[str, Any]:
    """Optional PRIOR_STEP7 observer; skip if json missing."""
    p = db_root / "data" / "prior" / "exports" / "price_behavior.json"
    if not p.is_file():
        return {"ok": False, "skip": True, "note": "price_behavior.json 缺失，跳过价格行为先验"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skip": True, "note": f"price_behavior.json 读失败：{e}"}
    return {
        "ok": True,
        "skip": False,
        "norm": _normalize_price_behavior(data),
        "path": "data/prior/exports/price_behavior.json",
    }


def _price_behavior_evidence(snap: dict[str, Any]) -> str:
    if not snap.get("ok"):
        return snap.get("note") or "price_behavior 不可得"
    d = snap["norm"]
    win = d.get("crawler_window_h")
    short = d.get("short")
    n_tok = d.get("n_tokens")
    ret1 = d.get("ret1") or {}
    pump = d.get("pump") or {}
    mdd = d.get("mdd") or {}

    def _rpct(v: Any) -> str:
        if v is None:
            return "—"
        return f"{float(v) * 100:.1f}%"

    bits = [
        f"窗长≈{float(win):.1f}h" if win is not None else "窗长=—",
        f"（{'SHORT' if short else 'OK'}）",
        f"n_tokens={n_tok}",
        (
            f"1h median/P10/P90="
            f"{_rpct(ret1.get('median'))}/{_rpct(ret1.get('p10'))}/{_rpct(ret1.get('p90'))}"
            f"（n={ret1.get('n')}）"
        ),
        (
            f"4h拉升 multiple P50={float(pump['multiple_p50']):.2f}× "
            f"duration P50={float(pump['duration_min_p50']):.1f}min"
            f"（n={pump.get('n')}）"
            if pump.get("multiple_p50") is not None
            else "4h拉升=—"
        ),
        (
            f"mdd_4h P50/P90={_rpct(mdd.get('p50'))}/{_rpct(mdd.get('p90'))}"
            f"（n={mdd.get('n')}）"
        ),
        "详见表下「价格行为先验」小节",
    ]
    return "；".join(bits)


def format_price_behavior_section(snap: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("### 价格行为先验（price_behavior，只观察）")
    lines.append("")
    if not snap.get("ok"):
        lines.append(f"> 跳过：{snap.get('note') or 'price_behavior.json 不可读'}")
        lines.append("")
        return "\n".join(lines)
    d = snap["norm"]
    win = d.get("crawler_window_h")
    short = bool(d.get("short"))
    n_tok = d.get("n_tokens")
    ret1 = d.get("ret1") or {}
    pump = d.get("pump") or {}
    mdd = d.get("mdd") or {}
    jev_note = d.get("jev_note")

    def _rpct(v: Any) -> str:
        if v is None:
            return "—"
        return f"{float(v) * 100:.1f}%"

    lines.append(
        "> PRIOR_STEP7 旁路：`data/prior/exports/price_behavior.json` 只读拉升/收益/回撤分布；"
        "**不**调权、不进风控。`RISK_USE_PRIOR` 默认关；SHORT 窗勿外推长期先验。"
    )
    lines.append("")
    win_s = f"{float(win):.1f}h" if win is not None else "—"
    n_s = f"{int(n_tok):,}" if n_tok is not None else "—"
    lines.append(
        f"窗长 ≈ **{win_s}**（**{'SHORT' if short else 'OK'}**）；"
        f"n_tokens ≈ **{n_s}**；源=`{snap.get('path')}`。"
    )
    lines.append("")
    lines.append("| 块 | 指标 | 值 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| A 收益 | 1h median / P10 / P90 / n | "
        f"{_rpct(ret1.get('median'))} / {_rpct(ret1.get('p10'))} / "
        f"{_rpct(ret1.get('p90'))} / {ret1.get('n')} |"
    )
    mult = pump.get("multiple_p50")
    dur = pump.get("duration_min_p50")
    lines.append(
        f"| B 拉升 | 4h multiple P50 / duration_min P50 / n | "
        f"{(f'{float(mult):.2f}×' if mult is not None else '—')} / "
        f"{(f'{float(dur):.1f}min' if dur is not None else '—')} / "
        f"{pump.get('n')} |"
    )
    lines.append(
        f"| C 回撤 | mdd_4h P50 / P90 / n | "
        f"{_rpct(mdd.get('p50'))} / {_rpct(mdd.get('p90'))} / {mdd.get('n')} |"
    )
    lines.append("")
    if jev_note:
        lines.append(f"**JEV 交叉：** {jev_note}")
        lines.append("")
    lines.append(
        "**纪律：** 只观察不调权；`RISK_USE_PRIOR` 默认关；"
        "勿外推长期先验（SHORT）；落地须走 R4。"
    )
    lines.append("")
    lines.append(
        "说明：只观察不调权；RISK_USE_PRIOR 默认关；SHORT 窗勿外推长期先验；落地须走 R4"
    )
    lines.append("")
    return "\n".join(lines)



# --- 指标加工层·周观察（read-only §10；只观察不调权） ---

def _indicator_week_observe_exports_dir(db_root: Path) -> Path:
    return db_root / "data" / "prior" / "exports"


def _pick_latest_indicator_week_observe(exports: Path) -> Optional[Path]:
    """Pick latest indicator_week_observe_YYYYMMDD.json by filename date, else mtime."""
    if not exports.is_dir():
        return None
    cands = list(exports.glob("indicator_week_observe_*.json"))
    if not cands:
        return None

    def _key(p: Path) -> tuple:
        stem = p.stem  # indicator_week_observe_YYYYMMDD
        date_part = stem.rsplit("_", 1)[-1]
        if len(date_part) == 8 and date_part.isdigit():
            return (1, date_part, p.stat().st_mtime)
        return (0, "", p.stat().st_mtime)

    return sorted(cands, key=_key, reverse=True)[0]


def load_indicator_week_observe(db_root: Path) -> dict[str, Any]:
    """Read latest indicator_week_observe_*.json; skip gracefully if missing."""
    exports = _indicator_week_observe_exports_dir(db_root)
    path = _pick_latest_indicator_week_observe(exports)
    if path is None:
        return {
            "ok": False,
            "skip": True,
            "note": (
                "indicator_week_observe_*.json 缺失，跳过指标加工层·周观察"
                f"（期望路径 `{exports.as_posix()}/indicator_week_observe_YYYYMMDD.json`）"
            ),
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "skip": True,
            "note": f"读 {path.name} 失败，跳过：{e}",
            "path": f"data/prior/exports/{path.name}",
        }
    if not isinstance(data, dict):
        return {
            "ok": False,
            "skip": True,
            "note": f"{path.name} 根节点非 object，跳过",
            "path": f"data/prior/exports/{path.name}",
        }
    return {
        "ok": True,
        "skip": False,
        "path": f"data/prior/exports/{path.name}",
        "data": data,
    }


def _status_from_row(raw: Any) -> str:
    s = str(raw or "").strip()
    if s in (STATUS_WATCH, STATUS_SUGGEST, STATUS_DECIDE):
        return s
    if "裁决" in s:
        return STATUS_DECIDE
    if "建议落地" in s:
        return STATUS_SUGGEST
    return STATUS_WATCH


def _indicator_week_observe_suggestions(obs: dict[str, Any]) -> list[dict[str, str]]:
    """Map week-observe JSON into §10 suggestion rows (never mutates weights)."""
    if not obs.get("ok"):
        return []
    data = obs.get("data") or {}
    meta = data.get("sample_meta") or {}
    window = data.get("window") or {}
    rows = data.get("rows") or []
    out: list[dict[str, str]] = []

    # Group rows into a few summary suggestion lines (stable §10 shape).
    up_items = [
        r for r in rows
        if str(r.get("category") or "").startswith("可上调")
        or str(r.get("category") or "") == "旁路可上调观察"
    ]
    down_items = [
        r for r in rows
        if "降权" in str(r.get("category") or "") or "维持0" in str(r.get("category") or "")
    ]
    watch_items = [
        r for r in rows
        if str(r.get("category") or "") == "观察中"
        or (
            _status_from_row(r.get("status")) == STATUS_WATCH
            and r not in up_items
            and r not in down_items
        )
    ]

    win_label = window.get("label") or "—"
    n_snap = meta.get("layer_snapshots")
    n_pair = meta.get("valid_fwd_pairs")
    n_tok = meta.get("tokens")
    up_rate = meta.get("overall_up_rate")
    sample_bit = (
        f"窗={win_label}；分层快照={n_snap}→有效配对={n_pair}/token={n_tok}；"
        f"全体上涨率={_pct(float(up_rate), 1) if up_rate is not None else '—'}"
    )

    if up_items:
        bits = []
        for r in up_items:
            bits.append(f"{r.get('item')}（{r.get('evidence') or '—'}）")
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·可上调观察（仍须 R4）",
                status=STATUS_SUGGEST,
                evidence=(
                    f"来源=指标加工层周观察；{sample_bit}；"
                    + "；".join(bits)
                    + "。「建议落地」≠自动开权；本周维持权重 0"
                ),
                action_hint=(
                    "可上调仅记观察；禁止本周接闸；权重保持 0；落地须走 R4"
                ),
            )
        )

    # Explicit short-window / down-weight lines for success-criteria coverage.
    short_down = [
        r for r in down_items
        if "change_5m" in str(r.get("item") or "") or "15m" in str(r.get("item") or "")
    ]
    other_down = [r for r in down_items if r not in short_down]
    if short_down or any("change_5m" in str(r.get("item") or "") for r in rows):
        ev = "；".join(
            f"{r.get('item')}: {r.get('evidence') or '—'}" for r in (short_down or [
                {"item": "change_5m/15m", "evidence": "短窗混乱"}
            ])
        )
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·change 短窗勿上调（5m/15m）",
                status=STATUS_SUGGEST,
                evidence=f"来源=指标加工层周观察；{ev}",
                action_hint="勿线性上调短窗 change；降权/维持 0；落地须走 R4",
            )
        )

    vol_macd = [
        r for r in other_down
        if any(
            k in str(r.get("item") or "")
            for k in ("volume_strength", "macd_hist", "volatility_24h")
        )
    ]
    if vol_macd:
        ev = "；".join(f"{r.get('item')}: {r.get('evidence') or '—'}" for r in vol_macd)
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·volume_strength / volatility_24h / macd_hist 降权维持0",
                status=STATUS_SUGGEST,
                evidence=(
                    f"来源=指标加工层周观察；{ev}；"
                    "volume 高档警示、volatility 高波差（风险分层旁路）、macd 混乱"
                ),
                action_hint=(
                    "可降权线性信任；volatility 作风险分层旁路；本周维持 0；落地须走 R4"
                ),
            )
        )

    # Remaining down items not covered above
    covered = set(id(x) for x in short_down) | set(id(x) for x in vol_macd)
    rest_down = [r for r in other_down if id(r) not in covered]
    if rest_down:
        ev = "；".join(f"{r.get('item')}: {r.get('evidence') or '—'}" for r in rest_down)
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·其他降权/维持0项",
                status=STATUS_SUGGEST,
                evidence=f"来源=指标加工层周观察；{ev}",
                action_hint="维持权重 0；落地须走 R4",
            )
        )

    watch_names = [str(r.get("item") or "") for r in watch_items]
    if any("sentiment" in n or "spike" in n for n in watch_names) or watch_items:
        ev = "；".join(
            f"{r.get('item')}: {r.get('evidence') or '—'}" for r in watch_items
        ) or "sentiment / spike 样本或信号弱"
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·sentiment / spike 样本不足",
                status=STATUS_WATCH,
                evidence=f"来源=指标加工层周观察；{ev}",
                action_hint="继续攒样本；突热只观察；不调权、不告警、不进决策；落地须走 R4",
            )
        )

    rsi_rows = [
        r for r in rows
        if "rsi_14" in str(r.get("item") or "")
    ]
    if rsi_rows:
        r0 = rsi_rows[0]
        out.append(
            _suggestion(
                layer="指标",
                item="周观察·rsi_14 旁路观察",
                status=_status_from_row(r0.get("status")),
                evidence=(
                    f"来源=指标加工层周观察；{r0.get('evidence') or '—'}；"
                    f"倾向={r0.get('tendency') or '旁路观察不进权重'}"
                ),
                action_hint="登记旁路观察，不进权重；维持 0；落地须走 R4",
            )
        )

    # Always emit verdict row when observe file present.
    verdict = data.get("verdict") or (
        "本周不对任何 INDICATORS_WEIGHT_* 改值；权重保持全 0，待 R4"
    )
    footnote = data.get("self_survival_footnote") or ""
    out.append(
        _suggestion(
            layer="指标",
            item="本周无权重裁决（指标加工层）",
            status=STATUS_DECIDE,
            evidence=(
                f"来源=指标加工层周观察（{obs.get('path')}）；{sample_bit}；"
                f"裁决={verdict}"
                + (f"；脚注={footnote}" if footnote else "")
            ),
            action_hint=(
                "纪律钉死：权重仍全 0；「建议落地」≠自动开权；"
                "不改仓/不改闸；禁止本周接闸；落地须走 R4"
            ),
        )
    )
    return out


def format_indicator_week_observe_section(obs: dict[str, Any]) -> str:
    """Markdown subsection「指标加工层·周观察（只读）」."""
    lines: list[str] = []
    lines.append("### 指标加工层·周观察（只读）")
    lines.append("")
    if not obs.get("ok"):
        lines.append(f"> 跳过：{obs.get('note') or 'indicator_week_observe 不可读'}")
        lines.append("")
        return "\n".join(lines)

    data = obs.get("data") or {}
    meta = data.get("sample_meta") or {}
    window = data.get("window") or {}
    rows = data.get("rows") or []
    path = obs.get("path") or "data/prior/exports/indicator_week_observe_*.json"

    win_label = window.get("label") or "—"
    fwd = window.get("fwd_horizon") or meta.get("fwd_horizon") or "≈1h"
    n_snap = meta.get("layer_snapshots")
    n_pair = meta.get("valid_fwd_pairs")
    n_tok = meta.get("tokens")
    up_rate = meta.get("overall_up_rate")
    up_s = _pct(float(up_rate), 1) if up_rate is not None else "—"

    lines.append(
        "> 来源=指标加工层周观察；输入=`"
        + path
        + "`；**只观察不调权**。"
        "「建议落地」≠允许自动开权；权重 `INDICATORS_WEIGHT_*=0`。"
    )
    lines.append("")
    lines.append(
        f"窗 **{win_label}**；fwd≈**{fwd}**；分层快照 **{n_snap}** → "
        f"有效配对 **{n_pair}** / token **{n_tok}**；全体上涨率 **{up_s}**。"
    )
    if meta.get("note"):
        lines.append("")
        lines.append(f"样本注记：{meta.get('note')}")
    lines.append("")
    lines.append("| 类别 | 指标 | 观察要点 | 状态 |")
    lines.append("|---|---|---|---|")
    for r in rows:
        cat = r.get("category") or "—"
        item = r.get("item") or "—"
        # backtick multi-names lightly
        item_s = f"`{item}`" if " " not in str(item) or "/" in str(item) else str(item)
        if "/" in str(item) or str(item).replace("_", "").isalnum():
            item_s = f"`{item}`"
        ev = r.get("evidence") or "—"
        tend = r.get("tendency") or ""
        point = ev if not tend else f"{ev}；{tend}"
        st = _status_from_row(r.get("status"))
        lines.append(f"| {cat} | {item_s} | {point} | **{st}** |")
    lines.append("")

    verdict = data.get("verdict") or "权重保持全 0，待 R4"
    lines.append(f"**裁决：** {verdict}")
    lines.append("")
    footnote = data.get("self_survival_footnote")
    if footnote:
        lines.append(f"**脚注（self_survival）：** {footnote}")
        lines.append("")
    lines.append(
        "**纪律：** 权重仍全 0；不改仓/不改闸；只观察；"
        "高档 mean 常被左尾拖累，禁止本周接闸；落地须走 **R4**。"
    )
    lines.append("")
    return "\n".join(lines)


def build_evolution_suggestions(
    *,
    chain: Sequence[SignalChainRow],
    metrics: Optional[dict[str, Any]] = None,
    exec_summary: Optional[dict[str, Any]] = None,
    attribution: Optional[dict[str, Any]] = None,
    fill_attr: Optional[dict[str, Any]] = None,
    jev_slice_metrics: Optional[dict[str, Any]] = None,
    opt_result: Optional[OptimizeResult] = None,
    optimize_section: Optional[str] = None,
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    db_root: Optional[str | Path] = None,
) -> list[dict[str, str]]:
    """Build structured evolution suggestions for weekly report §10.

    All items default to status=观察中 when auto-filled from read-only data.
    Never mutates strategy params / gates; landing goes through R4.
    """
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    metrics = metrics or {}
    exec_summary = exec_summary or {}
    fill_attr = fill_attr or {}
    suggestions: list[dict[str, str]] = []

    # --- 采集 ---
    n_unk, n_tot = _token_age_unknown_ratio(attribution)
    if n_tot > 0:
        age_ev = (
            f"归因 token_age 桶 unknown={n_unk}/{n_tot}"
            "（raw 优先，pairs.pair_created_at 回退）"
        )
    else:
        age_ev = "归因未提供 token_age 分桶"
    suggestions.append(
        _suggestion(
            layer="采集",
            item="token_age 覆盖率观察",
            status=STATUS_WATCH,
            evidence=age_ev,
            action_hint=(
                "补齐后观察 unknown 占比下降；"
                "不改桶边界、不调参"
            ),
        )
    )

    news_n, news_note = _count_news_events(root)
    if news_n is None:
        news_ev = news_note + "；等采集旁路交来后再填条数"
    else:
        news_ev = news_note
    suggestions.append(
        _suggestion(
            layer="采集",
            item="news_events 旁路积累",
            status=STATUS_WATCH,
            evidence=news_ev,
            action_hint=(
                "旁路继续 RSS 入库即可；不接决策/JEV；"
                "链上验证器接入前只记 pending"
            ),
        )
    )

    # --- 指标 ---
    weights = _read_indicator_weights()
    w_sum = sum(weights.values())
    w_ev = (
        f"INDICATORS_WEIGHT_*: TECHNICAL={weights['TECHNICAL']:g} "
        f"FUNDING={weights['FUNDING']:g} SENTIMENT={weights['SENTIMENT']:g} "
        f"（sum={w_sum:g}，全 0 → composite no-op）"
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="合成权重仍全 0",
            status=STATUS_WATCH,
            evidence=w_ev,
            action_hint="保持关闸至明确接线验收；开权走 R4，不由复盘自动改 config",
        )
    )

    suggestions.append(
        _suggestion(
            layer="指标",
            item="liq 分层区分度（候选关注）",
            status=STATUS_WATCH,
            evidence=_liq_differentiation_note(attribution),
            action_hint=(
                "仅作候选关注：样本不足时不据此调参；"
                "若跨桶极差持续且 unique_token 过闸，再交 R3/R4 评估过滤权重"
            ),
        )
    )

    # --- 指标：自库存活 / 基线滚动 / 价格行为（只观察，不调权） ---
    self_snap = _load_self_db_snapshot(root)
    suggestions.append(
        _suggestion(
            layer="指标",
            item="自库存活曲线（self_survival）",
            status=STATUS_WATCH,
            evidence=_self_survival_evidence(self_snap),
            action_hint="与 JEV 周检并排对照；裁决前不调权；只观察不改闸",
        )
    )

    bh_rep = load_baseline_history_report(root)
    suggestions.append(
        _suggestion(
            layer="指标",
            item="基线滚动历史（baseline_history）",
            status=STATUS_WATCH,
            evidence=_baseline_history_evidence(bh_rep),
            action_hint=(
                "只观察不调权；稳定两周（STABLE）后再谈进风控；落地须走 R4"
            ),
        )
    )

    pb_snap = _load_price_behavior_snapshot(root)
    if pb_snap.get("ok") or not pb_snap.get("skip"):
        suggestions.append(
            _suggestion(
                layer="指标",
                item="价格行为（拉升/回撤）",
                status=STATUS_WATCH,
                evidence=_price_behavior_evidence(pb_snap),
                action_hint=(
                    "只观察不调权；RISK_USE_PRIOR 默认关；SHORT 勿外推；落地须走 R4"
                ),
            )
        )

    # --- 指标：加工层周观察（只读；缺文件则跳过，不崩） ---
    iwo = load_indicator_week_observe(root)
    if iwo.get("ok"):
        suggestions.extend(_indicator_week_observe_suggestions(iwo))

    # --- 决策 ---
    suggestions.append(
        _suggestion(
            layer="决策",
            item="JEV unique_token / 门槛距离",
            status=STATUS_WATCH,
            evidence=_jev_gate_distance(jev_slice_metrics),
            action_hint=(
                f"继续周检攒样本；连续 {CONSECUTIVE_WEEKS} 周过滤后 "
                f"1h≥{_pct(JEV_UP1H_PASS, 0)} 且 unique_token≥{JEV_UNIQUE_TOKEN_GATE} "
                f"（且 n≥{JEV_N_GATE}）后再启动 Step2；本层只观察"
            ),
        )
    )

    suggestions.append(
        _suggestion(
            layer="决策",
            item="策略退役规则提醒",
            status=STATUS_WATCH,
            evidence=(
                f"规则（决策表）：连续 {CONSECUTIVE_WEEKS} 周过滤后 "
                f"1h<{_pct(JEV_UP1H_WEAK, 0)} 且 unique_token≥{JEV_UNIQUE_TOKEN_GATE} → "
                "建议降级/停用，勿进 R3 无限调参。"
                f"本期：{_jev_gate_distance(jev_slice_metrics)}。规则只提醒不裁决"
            ),
            action_hint="周检写入决策表时核对此条；退役=止损，调参=修补；落地需 R4",
        )
    )

    # --- 执行 ---
    n_exec = int(exec_summary.get("n_with_exec", 0) or 0)
    executed_rate = exec_summary.get("executed_rate")
    dry_rate = exec_summary.get("dry_run_rate")
    dist = exec_summary.get("executor_dist") or {}
    dist_s = ", ".join(f"{k}={v}" for k, v in dist.items()) or "—"
    fill_note = fill_attr.get("note") or (
        "no_fills" if not fill_attr.get("n_fills") else "has_fills"
    )
    exec_ev = (
        f"executed_rate={_pct(executed_rate)}；dry_run={_pct(dry_rate)}；"
        f"with_exec={n_exec}/{exec_summary.get('n', len(chain))}；"
        f"executor分布=[{dist_s}]；paper fills note={fill_note}"
    )
    suggestions.append(
        _suggestion(
            layer="执行",
            item="paper / executed 状态",
            status=STATUS_WATCH,
            evidence=exec_ev,
            action_hint="开闸与换 executor 属执行层/R4，复盘不自动开闸",
        )
    )

    buckets = exec_summary.get("validation_error_buckets") or {}
    if buckets:
        rej_ev = "；".join(f"{k}={v}" for k, v in buckets.items())
        rej_hint = "分桶仅观察；改校验规则走执行层变更 + R4，不由本节自动改"
    else:
        rej_ev = "本期无 validation_errors 分桶（或全 validation_ok）"
        rej_hint = "保持观察；若后续出现拒绝集中，再交执行层排查"
    suggestions.append(
        _suggestion(
            layer="执行",
            item="拒绝分布",
            status=STATUS_WATCH,
            evidence=rej_ev,
            action_hint=rej_hint,
        )
    )

    # --- 复盘 ---
    r3_ev = _r3_skip_evidence(
        opt_result=opt_result,
        chain=chain,
        period_start=period_start,
        period_end=period_end,
    )
    if optimize_section and opt_result is None:
        r3_ev = r3_ev + f"；report§9={optimize_section[:120]}"
    suggestions.append(
        _suggestion(
            layer="复盘",
            item="R3 skip 原因",
            status=STATUS_WATCH,
            evidence=r3_ev,
            action_hint=(
                "历史≥7d 且周检跨门槛后再正式寻优；"
                "本节只建议，不自动改参/开闸；落地走 R4"
            ),
        )
    )

    return suggestions


def format_evolution_section(
    suggestions: Sequence[dict[str, str]],
    *,
    db_root: Optional[str | Path] = None,
) -> str:
    """Markdown body for §10 (without the ## heading).

    Appends PRIOR_STEP6 基线滚动快照 + PRIOR_STEP7 价格行为先验 + 指标加工层·周观察 when readable.
    """
    lines: list[str] = []
    lines.append(
        "> **钉死：** 本节只建议，**不**自动改参 / 开闸 / 写配置；"
        "任何落地须走 **R4 人工确认**。"
    )
    lines.append("")
    if not suggestions:
        lines.append("_本期无进化建议（各层数据未交来）。_")
        lines.append("")
    else:
        lines.append("| 层 | 项 | 状态 | 证据 | 行动提示 |")
        lines.append("|---|---|---|---|---|")
        for s in suggestions:
            lines.append(
                f"| {s.get('layer', '')} | {s.get('item', '')} | "
                f"**{s.get('status', '')}** | {s.get('evidence', '')} | "
                f"{s.get('action_hint', '')} |"
            )
        lines.append("")
        lines.append(
            f"共 {len(suggestions)} 条；状态词汇：`观察中` / `建议落地` / `裁决`。"
            "自动填充项默认为「观察中」。"
        )
        lines.append("")

    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    bh = load_baseline_history_report(root)
    lines.append(format_baseline_history_section(bh))
    pb = _load_price_behavior_snapshot(root)
    lines.append(format_price_behavior_section(pb))
    iwo = load_indicator_week_observe(root)
    lines.append(format_indicator_week_observe_section(iwo))
    return "\n".join(lines)
