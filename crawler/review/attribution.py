"""R2 signal-layer attribution: dimension buckets + fill placeholders (read-only)."""

from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from review.data import DbPaths, SignalChainRow, connect_ro, parse_ts, safe_float

# --- bucket edges (documented for report) ---
STRENGTH_EDGES = "<0.15 / 0.15–0.17 / 0.17–0.19 / 0.19+ / unknown"
COVERAGE_EDGES = "<10 / 10–20 / 20+ / unknown"
LIQ_EDGES = "<50k / 50–500k / ≥500k / unknown"
TOKEN_AGE_EDGES = "<1d / 1–7d / >7d / unknown"
# market_regime: period-level from baseline median change_1h (winsor preferred), units = %
# bear < -2%; flat in [-2%, +2%]; bull > +2%; unknown if baseline missing
REGIME_RULE = (
    "period baseline median_change_1h_winsor (fallback median_change_1h), "
    "units=%: bear < -2; flat [-2, +2]; bull > +2; else unknown"
)
REGIME_BEAR_LT = -2.0
REGIME_BULL_GT = 2.0

CAVEAT = (
    "归因观察、样本不足（当前约 40 信号 / 回测 ~0.51d）、不作为调参依据；"
    "不触发任何自动动作"
)


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    return float(statistics.median(xs))


def _rate(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return num / den


def _bucket_stats(rows: list[SignalChainRow]) -> dict[str, Any]:
    """Per-bucket: n, up_1h_rate, up_4h_rate, median_1h, median_4h (fwd samples only)."""
    n = len(rows)
    r1 = [r.fwd_1h for r in rows if r.fwd_1h is not None]
    r4 = [r.fwd_4h for r in rows if r.fwd_4h is not None]
    up1 = sum(1 for x in r1 if x > 0)
    up4 = sum(1 for x in r4 if x > 0)
    return {
        "n": n,
        "n_1h": len(r1),
        "n_4h": len(r4),
        "up_1h_rate": _rate(up1, len(r1)),
        "up_4h_rate": _rate(up4, len(r4)),
        "median_1h": _median(r1),
        "median_4h": _median(r4),
    }


def _strength_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 0.15:
        return "<0.15"
    if v < 0.17:
        return "0.15-0.17"
    if v < 0.19:
        return "0.17-0.19"
    return "0.19+"


def _coverage_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 10:
        return "<10"
    if v < 20:
        return "10-20"
    return "20+"


def _liq_bucket(v: Optional[float]) -> str:
    if v is None:
        return "unknown"
    if v < 50_000:
        return "<50k"
    if v < 500_000:
        return "50-500k"
    return "≥500k"


def _extract_created_ms(raw: dict[str, Any]) -> Optional[float]:
    """Pull pair/token creation timestamp (ms or s or ISO) from signal raw_json."""
    candidates: list[Any] = []
    for key in (
        "pairCreatedAt",
        "pair_created_at",
        "createdAt",
        "created_at",
        "tokenCreatedAt",
        "token_created_at",
    ):
        if key in raw:
            candidates.append(raw.get(key))
    extras = raw.get("extras")
    if isinstance(extras, dict):
        for key in (
            "pairCreatedAt",
            "pair_created_at",
            "createdAt",
            "created_at",
            "tokenCreatedAt",
        ):
            if key in extras:
                candidates.append(extras.get(key))
    # nested pair / token objects sometimes carry it
    for nest_key in ("pair", "baseToken", "token"):
        nest = raw.get(nest_key)
        if isinstance(nest, dict):
            for key in ("pairCreatedAt", "createdAt", "created_at"):
                if key in nest:
                    candidates.append(nest.get(key))

    for c in candidates:
        if c is None or c == "":
            continue
        if isinstance(c, (int, float)):
            n = float(c)
            # ms vs s heuristic
            if n > 1e12:
                return n
            if n > 1e9:
                return n * 1000.0
            continue
        if isinstance(c, str):
            s = c.strip()
            # numeric string
            try:
                n = float(s)
                if n > 1e12:
                    return n
                if n > 1e9:
                    return n * 1000.0
            except ValueError:
                pass
            try:
                dt = parse_ts(s)
                return dt.timestamp() * 1000.0
            except (TypeError, ValueError):
                continue
    return None


def _token_age_bucket_computed(r: SignalChainRow) -> str:
    """Coarse bucket from token_age_hours (pairs.pair_created_at) or raw_json fallback."""
    age_hours = r.token_age_hours
    if age_hours is None:
        # fallback: creation ms from signal raw_json (legacy)
        created_ms = _extract_created_ms(r.raw or {})
        if created_ms is None:
            return "unknown"
        try:
            signal_dt = parse_ts(r.ts)
        except (TypeError, ValueError):
            return "unknown"
        age_hours = (
            signal_dt.timestamp() * 1000.0 - float(created_ms)
        ) / 3600000.0
    if age_hours < 0:
        # clock skew / future createdAt → treat as unknown rather than negative age
        return "unknown"
    age_days = age_hours / 24.0
    if age_days < 1:
        return "<1d"
    if age_days <= 7:
        return "1-7d"
    return ">7d"


def _token_age_bucket(
    r: SignalChainRow,
    tags: Optional[dict] = None,
) -> tuple[str, Optional[str]]:
    """Prefer prior.db.decision_prior_tags.age_bucket (mapped fine→coarse).

    Returns (coarse_bucket, mismatch_note|None). Cross-checks computed age.
    """
    computed = _token_age_bucket_computed(r)
    if not tags:
        return computed, None
    tag = tags.get(str(r.decision_id))
    if not tag:
        return computed, None
    fine = tag.get("age_bucket") or "unknown"
    # lazy import to avoid cycle (joint imports _extract_created_ms from here)
    from review.joint import fine_to_coarse_age

    prior_coarse = fine_to_coarse_age(fine)
    note = None
    if prior_coarse != computed:
        note = (
            f"decision_id={r.decision_id}: prior_tagger fine={fine}→{prior_coarse} "
            f"vs computed={computed}"
        )
    return prior_coarse, note


# back-compat alias used by older call sites expecting str
def _token_age_bucket_str(r: SignalChainRow) -> str:
    b, _ = _token_age_bucket(r, tags=None)
    return b


def _regime_from_baseline(baseline: dict | None) -> str:
    if not baseline:
        return "unknown"
    med = baseline.get("median_change_1h_winsor")
    if med is None:
        med = baseline.get("median_change_1h")
    med_f = safe_float(med)
    if med_f is None:
        return "unknown"
    if med_f < REGIME_BEAR_LT:
        return "bear"
    if med_f > REGIME_BULL_GT:
        return "bull"
    return "flat"


def _ordered_bucket_map(
    labels: list[str],
    groups: dict[str, list[SignalChainRow]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for lab in labels:
        out[lab] = _bucket_stats(groups.get(lab, []))
    # include any unexpected keys (should not happen)
    for lab, rs in groups.items():
        if lab not in out:
            out[lab] = _bucket_stats(rs)
    return out


def signal_attribution(
    rows: list[SignalChainRow],
    *,
    baseline: dict | None = None,
    db_root: str | Path | None = None,
) -> dict:
    """按维度分桶，每桶：n, up_1h_rate, up_4h_rate, median_1h, median_4h（有 fwd 的样本）。

    token_age：若 prior.db.decision_prior_tags 有行，优先用 age_bucket（细→粗）
    覆盖/对齐，与现算交叉校验，不一致记 note；分桶源=prior_tagger。
    """
    strength_g: dict[str, list[SignalChainRow]] = defaultdict(list)
    coverage_g: dict[str, list[SignalChainRow]] = defaultdict(list)
    liq_g: dict[str, list[SignalChainRow]] = defaultdict(list)
    age_g: dict[str, list[SignalChainRow]] = defaultdict(list)

    # prefer prior_tagger tags (read-only); lazy import avoids cycle
    from review.joint import load_decision_prior_tags, BUCKET_SOURCE_PRIOR, BUCKET_SOURCE_COMPUTED

    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    tags = load_decision_prior_tags(root)
    use_prior = bool(tags)
    age_mismatch: list[str] = []

    for r in rows:
        strength_g[_strength_bucket(r.jev_strength)].append(r)
        coverage_g[_coverage_bucket(r.jev_coverage)].append(r)
        liq_g[_liq_bucket(r.liquidity_at_signal)].append(r)
        age_b, age_note = _token_age_bucket(r, tags=tags if use_prior else None)
        age_g[age_b].append(r)
        if age_note:
            age_mismatch.append(age_note)

    regime = _regime_from_baseline(baseline)
    # period-level regime: all rows share one bucket (baseline-driven)
    regime_g: dict[str, list[SignalChainRow]] = {
        "bear": [],
        "flat": [],
        "bull": [],
        "unknown": [],
    }
    regime_g[regime] = list(rows)

    med_src = None
    if baseline:
        med_src = baseline.get("median_change_1h_winsor")
        if med_src is None:
            med_src = baseline.get("median_change_1h")

    n_period = len({str(r.decision_id) for r in rows})
    n_tagged = sum(1 for r in rows if str(r.decision_id) in tags) if tags else 0
    bucket_source = BUCKET_SOURCE_PRIOR if use_prior else BUCKET_SOURCE_COMPUTED
    if use_prior:
        age_note = (
            f"分桶源={bucket_source}（prior.db.decision_prior_tags.age_bucket 细→粗；"
            f"本期 {n_tagged}/{n_period}）；"
            "与现算 token_age 交叉校验；"
            "pairs.pair_created_at via chain_id+pair_address (fallback token_address)"
        )
        if age_mismatch:
            age_note += f"；不一致 {len(age_mismatch)} 条"
    else:
        age_note = (
            "分桶源=computed；pairs.pair_created_at via chain_id+pair_address "
            "(fallback token_address); age_hours=(signal_ts_ms-pair_created_at)/3600000; "
            "missing → unknown"
        )

    return {
        "n": len(rows),
        "caveat": CAVEAT,
        "bucket_source": bucket_source,
        "prior_tags_period": f"{n_tagged}/{n_period}",
        "age_mismatch_notes": age_mismatch,
        "dimensions": {
            "strength": {
                "edges": STRENGTH_EDGES,
                "buckets": _ordered_bucket_map(
                    ["<0.15", "0.15-0.17", "0.17-0.19", "0.19+", "unknown"],
                    strength_g,
                ),
            },
            "coverage": {
                "edges": COVERAGE_EDGES,
                "buckets": _ordered_bucket_map(
                    ["<10", "10-20", "20+", "unknown"],
                    coverage_g,
                ),
            },
            "liq": {
                "edges": LIQ_EDGES,
                "buckets": _ordered_bucket_map(
                    ["<50k", "50-500k", "≥500k", "unknown"],
                    liq_g,
                ),
            },
            "token_age": {
                "edges": TOKEN_AGE_EDGES,
                "note": age_note,
                "bucket_source": bucket_source,
                "buckets": _ordered_bucket_map(
                    ["<1d", "1-7d", ">7d", "unknown"],
                    age_g,
                ),
            },
            "market_regime": {
                "edges": "bear / flat / bull / unknown",
                "rule": REGIME_RULE,
                "assigned": regime,
                "baseline_median_change_1h_pct": med_src,
                "buckets": _ordered_bucket_map(
                    ["bear", "flat", "bull", "unknown"],
                    regime_g,
                ),
            },
        },
    }


def fill_attribution(rows: list[SignalChainRow]) -> dict:
    """占位：滑点损耗、pnl 按策略/标的归集。

    当前无成交时返回空结构 + note=no_fills；开闸后不改签名即可填。
    """
    fills = [
        r
        for r in rows
        if r.position_id is not None
        or r.fill_price is not None
        or r.pnl_usd is not None
    ]
    if not fills:
        return {
            "note": "no_fills",
            "n_fills": 0,
            "by_strategy": {},
            "by_token": {},
            "slippage": None,
            "total_pnl_usd": None,
        }

    by_strategy: dict[str, dict[str, Any]] = {}
    by_token: dict[str, dict[str, Any]] = {}
    strat_pnl: dict[str, list[float]] = defaultdict(list)
    tok_pnl: dict[str, list[float]] = defaultdict(list)
    for r in fills:
        sid = r.strategy_id or "(unknown)"
        tok = f"{r.chain_id}:{r.token_address}" if r.token_address else "(unknown)"
        if r.pnl_usd is not None:
            strat_pnl[sid].append(float(r.pnl_usd))
            tok_pnl[tok].append(float(r.pnl_usd))
        by_strategy.setdefault(sid, {"n": 0, "pnl_usd": 0.0})
        by_strategy[sid]["n"] += 1
        by_token.setdefault(tok, {"n": 0, "pnl_usd": 0.0})
        by_token[tok]["n"] += 1

    for sid, pnls in strat_pnl.items():
        by_strategy[sid]["pnl_usd"] = sum(pnls)
        by_strategy[sid]["median_pnl"] = _median(pnls)
    for tok, pnls in tok_pnl.items():
        by_token[tok]["pnl_usd"] = sum(pnls)
        by_token[tok]["median_pnl"] = _median(pnls)

    all_pnl = [float(r.pnl_usd) for r in fills if r.pnl_usd is not None]
    return {
        "note": "fills_present",
        "n_fills": len(fills),
        "by_strategy": by_strategy,
        "by_token": by_token,
        "slippage": None,  # no fill vs signal price ledger yet
        "total_pnl_usd": sum(all_pnl) if all_pnl else None,
    }


def load_backtest_summary(paths: DbPaths) -> dict[str, Any]:
    """只读 backtest_ledger：取最近 done run，按策略汇总 trades/win_rate。

    失败则返回 {"ok": False, "note": ...}，不抛。
    """
    try:
        path = Path(paths.backtest)
        if not path.is_file():
            return {"ok": False, "note": "backtest_ledger missing"}
        conn = connect_ro(str(path))
        try:
            run = conn.execute(
                """
                SELECT run_id, started_at, finished_at, strategy_id, start_cash,
                       end_equity, status, notes
                FROM runs
                WHERE status = 'done'
                ORDER BY finished_at DESC, started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if run is None:
                return {"ok": False, "note": "no done backtest runs"}
            run_id = run["run_id"]
            closed = conn.execute(
                """
                SELECT strategy_id, pnl_usd
                FROM positions
                WHERE run_id = ? AND status = 'closed'
                """,
                (run_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — soft optional
        return {"ok": False, "note": f"backtest read failed: {e}"}

    by_sid: dict[str, list[float]] = defaultdict(list)
    for row in closed:
        sid = row["strategy_id"] or "(unknown)"
        pnl = safe_float(row["pnl_usd"])
        by_sid[sid].append(0.0 if pnl is None else pnl)

    by_strategy: dict[str, dict[str, Any]] = {}
    for sid, pnls in sorted(by_sid.items()):
        n = len(pnls)
        wins = sum(1 for x in pnls if x > 0)
        by_strategy[sid] = {
            "trades": n,
            "win_rate": (wins / n) if n else None,
            "total_pnl_usd": sum(pnls),
        }

    return {
        "ok": True,
        "run_id": run["run_id"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "strategy_id": run["strategy_id"],
        "start_cash": run["start_cash"],
        "end_equity": run["end_equity"],
        "notes": run["notes"],
        "n_closed": len(closed),
        "by_strategy": by_strategy,
    }
