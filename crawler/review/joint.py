"""JEV × token_age (fine) × prior survival — read-only joint attribution (step 4).

Does not mutate weights / gates / trading. Observation only.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Sequence

from review.attribution import _extract_created_ms
from review.data import SignalChainRow, get_prior_tag_buckets, parse_ts

# Fine token_age edges (hours-based; replaces coarse <1d with <1h / 1-24h)
TOKEN_AGE_FINE_EDGES = "<1h / 1-24h / 1-7d / >7d / unknown"
TOKEN_AGE_FINE_LABELS = ["<1h", "1-24h", "1-7d", ">7d", "unknown"]

# Fine → coarse (attribution §8 token_age dim)
FINE_TO_COARSE = {
    "<1h": "<1d",
    "1-24h": "<1d",
    "1-7d": "1-7d",
    ">7d": ">7d",
    "unknown": "unknown",
}

BUCKET_SOURCE_PRIOR = "prior_tagger"
BUCKET_SOURCE_COMPUTED = "computed"


def load_decision_prior_tags(db_root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Read-only load prior.db.decision_prior_tags keyed by decision_id (str).

    Returns {} if prior.db missing, table missing, or zero rows.
    Never writes. decision_id = str(decision_log.id).
    """
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    db_path = root / "data" / "prior" / "db" / "prior.db"
    if not db_path.is_file():
        return {}
    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # table may not exist yet
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "decision_prior_tags" not in names:
                return {}
            n = conn.execute("SELECT COUNT(*) FROM decision_prior_tags").fetchone()[0]
            if not n:
                return {}
            rows = conn.execute(
                """
                SELECT decision_id, age_bucket, token_age_hours, peak_window,
                       survival_ref_1h, survival_ref_4h, signal_ts,
                       chain_id, token_address
                FROM decision_prior_tags
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        did = str(r["decision_id"])
        out[did] = {
            "decision_id": did,
            "age_bucket": r["age_bucket"],
            "token_age_hours": r["token_age_hours"],
            "peak_window": r["peak_window"],
            "survival_ref_1h": r["survival_ref_1h"],
            "survival_ref_4h": r["survival_ref_4h"],
            "signal_ts": r["signal_ts"],
            "chain_id": r["chain_id"],
            "token_address": r["token_address"],
        }
    return out


def fine_to_coarse_age(fine: str) -> str:
    """Map prior_tagger / joint fine bucket → attribution coarse bucket."""
    return FINE_TO_COARSE.get(fine, "unknown")


def resolve_fine_age_bucket(
    r: SignalChainRow,
    tags: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[str, Optional[str]]:
    """Prefer prior.db age_bucket; cross-check computed token_age.

    Returns (bucket, mismatch_note|None).
    """
    computed = token_age_fine_bucket(r)
    if not tags:
        return computed, None
    tag = tags.get(str(r.decision_id))
    if not tag:
        return computed, None
    prior_b = tag.get("age_bucket") or "unknown"
    if prior_b not in TOKEN_AGE_FINE_LABELS:
        prior_b = "unknown"
    note = None
    if prior_b != computed:
        note = (
            f"decision_id={r.decision_id}: prior_tagger={prior_b} "
            f"vs computed={computed}"
        )
    return prior_b, note


JOINT_CAVEAT = (
    "联合归因观察：样本不足时不作为调参依据；"
    "假说「1–24h 段质量最高」只观察，以本期联合表为准；"
    "不触发任何自动动作 / 不调权"
)

# Map fine age bucket → self_db survive horizon key + minimum crawler window (h)
# prior = fraction of all-network tokens with observation span > horizon
_BUCKET_PRIOR_HORIZON: dict[str, tuple[str, float, str]] = {
    # (survive_key, min_crawler_window_h, label)
    # 旁挂「进入/活过该段」门槛的全网观测跨度存活率
    "<1h": ("1h", 1.0, "全网观测跨度>1h（活过本段）"),
    "1-24h": ("4h", 4.0, "全网观测跨度>4h（本段中位参考）"),
    "1-7d": ("24h", 24.0, "全网观测跨度>24h（进入本段门槛）"),
    ">7d": ("168h", 168.0, "全网观测跨度>7d（进入本段门槛）"),
}


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.median(xs))


def _rate(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return num / den


def token_age_hours(r: SignalChainRow) -> Optional[float]:
    """Resolve age in hours from annotate_token_age or raw_json fallback."""
    if r.token_age_hours is not None:
        return float(r.token_age_hours)
    created_ms = _extract_created_ms(r.raw or {})
    if created_ms is None:
        return None
    try:
        signal_dt = parse_ts(r.ts)
    except (TypeError, ValueError):
        return None
    return (signal_dt.timestamp() * 1000.0 - float(created_ms)) / 3600000.0


def token_age_fine_bucket(r: SignalChainRow) -> str:
    """Fine age: <1h / 1-24h / 1-7d / >7d / unknown (via token_age_hours)."""
    age_hours = token_age_hours(r)
    if age_hours is None or age_hours < 0:
        return "unknown"
    if age_hours < 1.0:
        return "<1h"
    if age_hours < 24.0:
        return "1-24h"
    if age_hours < 168.0:  # 7d
        return "1-7d"
    return ">7d"


def _bucket_compact(rows: Sequence[SignalChainRow]) -> dict[str, Any]:
    """Per-bucket compact stats: n, up_1h, med_1h, up_4h (+ n_1h/n_4h for debug)."""
    n = len(rows)
    r1 = [r.fwd_1h for r in rows if r.fwd_1h is not None]
    r4 = [r.fwd_4h for r in rows if r.fwd_4h is not None]
    up1 = sum(1 for x in r1 if x > 0)
    up4 = sum(1 for x in r4 if x > 0)
    return {
        "n": n,
        "n_1h": len(r1),
        "n_4h": len(r4),
        "up_1h": _rate(up1, len(r1)),
        "med_1h": _median(r1),
        "up_4h": _rate(up4, len(r4)),
        # aliases for report helpers that expect _rate keys
        "up_1h_rate": _rate(up1, len(r1)),
        "up_4h_rate": _rate(up4, len(r4)),
        "median_1h": _median(r1),
    }


def _group_by_fine_age(
    rows: Sequence[SignalChainRow],
    tags: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[dict[str, list[SignalChainRow]], list[str]]:
    """Group by fine age; prefer prior_tagger age_bucket when tags present."""
    g: dict[str, list[SignalChainRow]] = defaultdict(list)
    notes: list[str] = []
    for r in rows:
        bucket, note = resolve_fine_age_bucket(r, tags)
        g[bucket].append(r)
        if note:
            notes.append(note)
    return g, notes


def _load_self_db_prior(db_root: Path) -> dict[str, Any]:
    """Load self_db block from baseline_params.json (read-only). Empty dict if missing."""
    bp = db_root / "data" / "prior" / "exports" / "baseline_params.json"
    if not bp.is_file():
        return {
            "ok": False,
            "note": "baseline_params.json 缺失",
            "survive": {},
            "crawler_window_h": None,
            "source": None,
        }
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "note": f"baseline_params.json 读失败: {e}",
            "survive": {},
            "crawler_window_h": None,
            "source": str(bp),
        }
    self_db = data.get("self_db") or {}
    if not self_db:
        return {
            "ok": False,
            "note": "baseline_params.json 无 self_db（先跑 scripts/self_survival.py）",
            "survive": {},
            "crawler_window_h": None,
            "source": str(bp),
        }
    # Prefer dated self_survival md path for citation
    exports = db_root / "data" / "prior" / "exports"
    report_rel = None
    if exports.is_dir():
        cands = sorted(exports.glob("self_survival_*.md"), reverse=True)
        if cands:
            report_rel = f"data/prior/exports/{cands[0].name}"
    span = self_db.get("span_min") or {}
    span_max_m = span.get("max")
    span_max_h = None
    try:
        if span_max_m is not None:
            span_max_h = float(span_max_m) / 60.0
    except (TypeError, ValueError):
        span_max_h = None
    return {
        "ok": True,
        "note": self_db.get("note") or "self_db 观测跨度存活",
        "survive": dict(self_db.get("survive") or {}),
        "survive_n": dict(self_db.get("survive_n") or {}),
        "crawler_window_h": self_db.get("crawler_window_h"),
        "span_max_h": span_max_h,
        "warnings": list(self_db.get("warnings") or []),
        "sparse": bool(self_db.get("sparse")),
        "n": self_db.get("n"),
        "source": "baseline_params.json#self_db",
        "report": report_rel,
        "survive_basis": self_db.get("survive_basis"),
    }


def _prior_ref_for_bucket(bucket: str, prior: dict[str, Any]) -> dict[str, Any]:
    """Attach network-wide survival reference for one fine age bucket."""
    if bucket == "unknown":
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": "unknown 桶无对应存活窗",
            "horizon": None,
        }
    meta = _BUCKET_PRIOR_HORIZON.get(bucket)
    if meta is None:
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": f"未映射桶 {bucket}",
            "horizon": None,
        }
    horizon_key, min_window_h, label = meta

    if not prior.get("ok"):
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": prior.get("note") or "先验不可得",
            "horizon": horizon_key,
            "prior_label": label,
        }

    window = prior.get("crawler_window_h")
    survive = prior.get("survive") or {}
    rate = survive.get(horizon_key)

    # Window too short for this horizon → N/A (0% would be coverage artefact)
    if window is not None and float(window) < float(min_window_h):
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": (
                f"crawler 窗口≈{float(window):.1f}h < {min_window_h:g}h，"
                f"{horizon_key} 存活受覆盖上限约束（≠链上真值）"
            ),
            "horizon": horizon_key,
            "prior_label": label,
        }

    span_max_h = prior.get("span_max_h")
    if span_max_h is not None and float(span_max_h) < float(min_window_h):
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": (
                f"观测跨度 max≈{float(span_max_h):.1f}h < {min_window_h:g}h，"
                f"{horizon_key} 存活不可得（覆盖上限，≠链上真值）"
            ),
            "horizon": horizon_key,
            "prior_label": label,
        }

    if rate is None:
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": f"self_db.survive[{horizon_key}] 缺失",
            "horizon": horizon_key,
            "prior_label": label,
        }

    # Even with window >= horizon, rate==0 with sparse flag → still show 0 but annotate
    try:
        rate_f = float(rate)
    except (TypeError, ValueError):
        return {
            "prior_survive": None,
            "prior_display": "N/A",
            "prior_reason": f"survive[{horizon_key}] 非数值: {rate!r}",
            "horizon": horizon_key,
            "prior_label": label,
        }

    return {
        "prior_survive": rate_f,
        "prior_display": f"{rate_f * 100:.1f}%",
        "prior_reason": None,
        "horizon": horizon_key,
        "prior_label": label,
        "prior_n": (prior.get("survive_n") or {}).get(horizon_key),
    }


def _table_for_rows(
    rows: Sequence[SignalChainRow],
    prior: dict[str, Any],
    tags: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    groups, mismatch_notes = _group_by_fine_age(rows, tags=tags)
    out: dict[str, dict[str, Any]] = {}
    for lab in TOKEN_AGE_FINE_LABELS:
        stats = _bucket_compact(groups.get(lab, []))
        pref = _prior_ref_for_bucket(lab, prior)
        stats.update(pref)
        out[lab] = stats
    return out, mismatch_notes


def joint_jev_age_prior(
    full_rows: Sequence[SignalChainRow],
    jev_rows: Sequence[SignalChainRow],
    *,
    db_root: str | Path | None = None,
    min_ts: str | None = None,
) -> dict[str, Any]:
    """Build dual tables: JEV-filtered slice + full sample × fine token_age × prior.

    Prefer read-only prior.db.decision_prior_tags.age_bucket when available
    (bucket_source=prior_tagger); else computed token_age_hours.
    Also attach get_prior_tag_buckets() aggregate for §8 auto-bucket reconcile.
    Cross-check mismatches → notes. Observation only.
    """
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    prior = _load_self_db_prior(root)
    tags = load_decision_prior_tags(root)
    use_prior = bool(tags)
    bucket_source = BUCKET_SOURCE_PRIOR if use_prior else BUCKET_SOURCE_COMPUTED

    # §8 auto-bucket table (prefer metrics helper; read-only)
    prior_db = root / "data" / "prior" / "db" / "prior.db"
    main_db = root / "dexscreener.db"
    prior_tag_buckets = get_prior_tag_buckets(
        str(prior_db),
        min_ts=min_ts,
        main_db=str(main_db) if main_db.is_file() else None,
    )

    # coverage among rows in this report period
    full_ids = {str(r.decision_id) for r in full_rows}
    n_tagged_in_period = sum(1 for i in full_ids if i in tags) if tags else 0
    n_period = len(full_ids)

    jev_buckets, jev_notes = _table_for_rows(jev_rows, prior, tags=tags if use_prior else None)
    full_buckets, full_notes = _table_for_rows(
        full_rows, prior, tags=tags if use_prior else None
    )
    # de-dupe mismatch notes (same decision may appear in both slices)
    seen: set[str] = set()
    mismatch_notes: list[str] = []
    for n in jev_notes + full_notes:
        if n not in seen:
            seen.add(n)
            mismatch_notes.append(n)

    if use_prior:
        note = (
            f"分桶源={bucket_source}（prior.db.decision_prior_tags；"
            f"本期覆盖 {n_tagged_in_period}/{n_period}）；"
            "与现算 token_age 交叉校验，不一致记 note；"
            "细桶：<1h / 1-24h / 1-7d / >7d / unknown；"
            "JEV 切片与周检同口径（strength/liq/coverage/price）；"
            "先验旁挂 self_db 观测跨度存活（非链上寿命）"
        )
    else:
        note = (
            "分桶源=computed（prior.db.decision_prior_tags 不可用/空）；"
            "细桶用 token_age_hours：<1h / 1-24h / 1-7d / >7d / unknown；"
            "JEV 切片与周检同口径（strength/liq/coverage/price）；"
            "先验旁挂 self_db 观测跨度存活（非链上寿命）"
        )


    # reconcile full_sample fine-bucket n vs get_prior_tag_buckets
    ptb_by = {str(r.get("age_bucket")): int(r.get("n") or 0) for r in prior_tag_buckets}
    reconcile_notes: list[str] = []
    if use_prior and prior_tag_buckets:
        for lab in TOKEN_AGE_FINE_LABELS:
            jn = int((full_buckets.get(lab) or {}).get("n") or 0)
            pn = int(ptb_by.get(lab, 0))
            if jn != pn:
                reconcile_notes.append(
                    f"全样本联合表 {lab}: n={jn} vs prior_tag_buckets n={pn}"
                )
        if not reconcile_notes:
            note = note + "；先验标签自动分桶与全样本联合表对账一致"
        else:
            note = note + f"；先验标签自动分桶对账不一致 {len(reconcile_notes)} 项"

    return {
        "caveat": JOINT_CAVEAT,
        "edges": TOKEN_AGE_FINE_EDGES,
        "labels": list(TOKEN_AGE_FINE_LABELS),
        "bucket_source": bucket_source,
        "prior_tags_n": len(tags),
        "prior_tags_period": f"{n_tagged_in_period}/{n_period}",
        "mismatch_notes": mismatch_notes,
        "prior_tag_buckets": prior_tag_buckets,
        "reconcile_notes": reconcile_notes,
        "hypothesis": "监控假说「1–24h 段质量最高」（以本期联合表为准，不引用冻结样本）",
        "note": note,
        "prior_meta": {
            "ok": prior.get("ok"),
            "source": prior.get("source"),
            "report": prior.get("report"),
            "crawler_window_h": prior.get("crawler_window_h"),
            "n": prior.get("n"),
            "sparse": prior.get("sparse"),
            "note": prior.get("note"),
            "survive_basis": prior.get("survive_basis"),
        },
        "jev_filtered": {
            "n": len(jev_rows),
            "buckets": jev_buckets,
        },
        "full_sample": {
            "n": len(full_rows),
            "buckets": full_buckets,
        },
    }


def summarize_joint_for_evolution(joint: Optional[dict[str, Any]]) -> str:
    """One-line evidence for §10 observation item."""
    if not joint:
        return "联合表未计算"
    jev = joint.get("jev_filtered") or {}
    full = joint.get("full_sample") or {}
    jb = jev.get("buckets") or {}
    src = joint.get("bucket_source") or BUCKET_SOURCE_COMPUTED
    bits: list[str] = [
        f"分桶源={src}",
        f"JEV过滤 n={jev.get('n', 0)} / 全样本 n={full.get('n', 0)}",
        f"细桶={joint.get('edges')}",
    ]
    if joint.get("prior_tags_period"):
        bits.append(f"prior_tags={joint.get('prior_tags_period')}")
    n_mis = len(joint.get("mismatch_notes") or [])
    if n_mis:
        bits.append(f"交叉校验不一致={n_mis}")
    for lab in ("<1h", "1-24h", "1-7d", ">7d"):
        m = jb.get(lab) or {}
        up = m.get("up_1h")
        if up is None:
            up_s = "—"
        else:
            up_s = f"{up * 100:.1f}%"
        bits.append(f"{lab}: n={m.get('n', 0)} up_1h={up_s}")
    bits.append("联合归因已出，不调权")
    return "；".join(bits)
