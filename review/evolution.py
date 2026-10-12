"""Section 10: evolution suggestions (read-only observations; never mutates params)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

from review.data import SignalChainRow
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

# JEV weekly-check thresholds (align DECISION_ENGINE_WEEKLY_CHECK.md)
JEV_UNIQUE_TOKEN_GATE = 10
JEV_N_GATE = 15
JEV_UP1H_PASS = 0.55
JEV_UP1H_WEAK = 0.50


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

def _pandas_ta_bypass_note(db_root: Path) -> Optional[str]:
    """PRAGMA-probe rsi_14/macd/boll_mid on token_indicators / indicator_history.

    Returns evidence string when any target column exists; None to skip the item.
    Read-only; never mutates weights.
    """
    path = db_root / "dexscreener.db"
    if not path.is_file():
        return None
    targets = ("rsi_14", "macd", "boll_mid")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            bits: list[str] = []
            any_col = False
            for table in ("token_indicators", "indicator_history"):
                try:
                    cols = {
                        str(r[1])
                        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                    }
                except sqlite3.Error:
                    continue
                present = [c for c in targets if c in cols]
                if not present:
                    continue
                any_col = True
                try:
                    tot = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    continue
                parts: list[str] = []
                for c in present:
                    try:
                        n = int(
                            conn.execute(
                                f"SELECT COUNT(*) FROM {table} WHERE {c} IS NOT NULL"
                            ).fetchone()[0]
                        )
                    except sqlite3.Error:
                        continue
                    cov = f"{(n / tot * 100):.1f}%" if tot > 0 else "—"
                    parts.append(f"{c}={n}/{tot}（{cov}）")
                if parts:
                    bits.append(f"{table}: " + "；".join(parts))
            if not any_col:
                return None
            if not bits:
                return "列已存在但无法统计有值行数"
            return "；".join(bits)
        finally:
            conn.close()
    except Exception as e:
        return f"dexscreener.db 打开失败: {e}"



def _prior_survival_bypass_note(db_root: Path) -> Optional[str]:
    """Read-only prior baseline trio for §10 observation (PRIOR_STEP1).

    Prefers data/prior/exports/baseline_params.json weekly_baseline_trio
    (+ survival_*.md path). Never emits deprecated survive_*/lifespan curves
    (RED-PUMP last_ts is 24h TIMEOUT window end, not death time).
    Returns None only when nothing exists.
    """
    prior_root = db_root / "data" / "prior"
    exports = prior_root / "exports"
    db_path = prior_root / "db" / "prior.db"

    report_rel: Optional[str] = None
    trio_bits: list[str] = []
    notes: list[str] = []

    # survival report path (prefer dated 20261010, else newest survival_*.md)
    md_text = ""
    if exports.is_dir():
        preferred = exports / "survival_20261010.md"
        if preferred.is_file():
            report_rel = "data/prior/exports/survival_20261010.md"
            try:
                md_text = preferred.read_text(encoding="utf-8")
            except OSError:
                md_text = ""
        else:
            cands = sorted(
                (
                    p
                    for p in exports.glob("survival_*.md")
                    if not p.name.endswith("_v2.md")
                ),
                reverse=True,
            )
            if cands:
                report_rel = f"data/prior/exports/{cands[0].name}"
                try:
                    md_text = cands[0].read_text(encoding="utf-8")
                except OSError:
                    md_text = ""

    # baseline_params.json — prefer weekly_baseline_trio (三件套)
    bp = exports / "baseline_params.json"
    data: Optional[dict[str, Any]] = None
    if bp.is_file():
        try:
            data = json.loads(bp.read_text(encoding="utf-8"))
        except Exception as e:
            notes.append(f"baseline_params.json 读失败: {e}")

    if isinstance(data, dict):
        trio = data.get("weekly_baseline_trio") or {}
        rp = data.get("redpump") or {}
        cg = data.get("coingecko") or {}
        sol = data.get("solrpds") or {}

        grad = trio.get("graduation_rate")
        if grad is None:
            grad = rp.get("graduation_rate")
        death = trio.get("same_day_death_rate")
        if death is None:
            death = cg.get("same_day_death_rate")
        rug = trio.get("solrpds_remove_gt_add_rate")
        if rug is None:
            rug = sol.get("remove_gt_add_rate")

        # Detect "三件套 ready" vs legacy survive_* only
        has_trio = grad is not None and death is not None and rug is not None
        if has_trio:
            try:
                trio_bits.append(f"毕业率 {float(grad) * 100:.1f}%")
            except (TypeError, ValueError):
                trio_bits.append(f"毕业率 {grad}")
            try:
                trio_bits.append(f"当天死 {float(death) * 100:.2f}%(CoinGecko)")
            except (TypeError, ValueError):
                trio_bits.append(f"当天死 {death}(CoinGecko)")
            try:
                trio_bits.append(f"SolRPDs 池移除>添加 {float(rug) * 100:.1f}%")
            except (TypeError, ValueError):
                trio_bits.append(f"SolRPDs 池移除>添加 {rug}")

            ut = sol.get("unique_tokens")
            if ut is not None:
                notes.append(f"unique token 本库 {ut}（提示≈22195）")
        else:
            # Placeholder when metrics layer has not shipped the trio yet
            trio_bits = [
                "毕业率≈0.2%",
                "当天死≈68.67%(CoinGecko)",
                "SolRPDs 池移除>添加≈60.1%",
            ]
            notes.append("可用基线待三件套（当前 baseline 未齐）")

        inv = (
            (data.get("meta") or {}).get("invalidity_note_zh")
            or (rp.get("deprecated") or {}).get("reason_zh")
        )
        if inv:
            notes.append(
                "旧存活曲线/寿命分位口径不可用（RED-PUMP 24h TIMEOUT 窗口）"
            )
        else:
            notes.append(
                "旧存活曲线/寿命分位口径不可用（RED-PUMP 24h TIMEOUT 窗口）"
            )
    elif report_rel or md_text:
        # Parse trio from markdown §6 if JSON missing
        m_grad = re.search(r"毕业率\s*[|：:]*\s*\*?\*?([0-9.]+)%", md_text)
        m_death = re.search(r"当天死\s*[|：:]*\s*\*?\*?([0-9.]+)%", md_text)
        m_rug = re.search(
            r"(?:池移除>添加|移除>添加)[^\d]*\*?\*?([0-9.]+)%", md_text
        )
        if m_grad and m_death and m_rug:
            trio_bits = [
                f"毕业率 {m_grad.group(1)}%",
                f"当天死 {m_death.group(1)}%(CoinGecko)",
                f"SolRPDs 池移除>添加 {m_rug.group(1)}%",
            ]
        else:
            trio_bits = [
                "毕业率≈0.2%",
                "当天死≈68.67%(CoinGecko)",
                "SolRPDs 池移除>添加≈60.1%",
            ]
        notes.append(
            "旧存活曲线/寿命分位口径不可用（RED-PUMP 24h TIMEOUT 窗口）"
        )
        m_ut = re.search(r"结果：\s*([0-9,]+)", md_text)
        if m_ut:
            notes.append(
                f"unique token 本库 {m_ut.group(1).replace(',', '')}（提示≈22195）"
            )
    elif not db_path.is_file():
        return None
    else:
        trio_bits = [
            "毕业率≈0.2%",
            "当天死≈68.67%(CoinGecko)",
            "SolRPDs 池移除>添加≈60.1%",
        ]
        notes.append(
            "旧存活曲线/寿命分位口径不可用（RED-PUMP 24h TIMEOUT 窗口）"
        )

    bits: list[str] = []
    if trio_bits:
        bits.append(" / ".join(trio_bits))
    bits.extend(notes)
    if report_rel:
        bits.append(f"报告={report_rel}")
    else:
        bits.append("报告路径待旁路导出")
    return "；".join(bits)



def _prior_gmgn_v2_bypass_note(db_root: Path) -> Optional[str]:
    """Read-only GMGN STEP2 side-note from survival_*_v2.md.

    Official kagglehub pack is Tokens=956 (完整数据集 / full release);
    unique>1000 gate cannot be met from this source. Peak/gain may seed a
    prior prototype; 1h/4h/24h survival needs a longer OHLCV source. Does
    not replace the PRIOR_STEP1 三件套 main entry.
    """
    exports = db_root / "data" / "prior" / "exports"
    if not exports.is_dir():
        return None
    preferred = exports / "survival_20261010_v2.md"
    if preferred.is_file():
        v2 = preferred
    else:
        cands = sorted(exports.glob("survival_*_v2.md"), reverse=True)
        if not cands:
            return None
        v2 = cands[0]
    try:
        md = v2.read_text(encoding="utf-8")
    except OSError:
        return None

    report_rel = f"data/prior/exports/{v2.name}"

    # 见顶 P25/P50/P75（§3 table first data row）
    peak_p25 = peak_p50 = peak_p75 = None
    peak_n = None
    in_peak = False
    for line in md.splitlines():
        if line.startswith("## 3."):
            in_peak = True
            continue
        if in_peak and line.startswith("##"):
            break
        if in_peak and line.startswith("|") and "---" not in line and "P25" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4:
                peak_p25, peak_p50, peak_p75, peak_n = cells[0], cells[1], cells[2], cells[3]
            break

    m_span = re.search(r"监控跨度[^:\n]*:\s*([^\n]+)", md)
    span = m_span.group(1).strip() if m_span else "—"

    # death_reason shares
    dr_parts: list[str] = []
    n_total = None
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        name, cnt, pct = cells[0], cells[1], cells[2]
        if name in ("price_stable", "volume_low", "candle_gap", "timeout"):
            dr_parts.append(f"{name} {pct}")
        if name in ("合计", "total") and cnt.isdigit():
            n_total = int(cnt)
    if n_total is None:
        m_n = re.search(r"n[=≈]\s*(\d+)", md)
        if m_n:
            n_total = int(m_n.group(1))

    # §4 见顶涨幅 P50
    gain_p50 = None
    in_gain = False
    for line in md.splitlines():
        if line.startswith("## 4."):
            in_gain = True
            continue
        if in_gain and line.startswith("##"):
            break
        if in_gain and line.startswith("|") and "---" not in line and "P25" not in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                gain_p50 = cells[1]
            break

    peak_s = (
        f"见顶 P25/P50/P75={peak_p25}/{peak_p50}/{peak_p75}"
        if peak_p25 and peak_p50 and peak_p75
        else "见顶分位待解析"
    )
    if peak_n:
        peak_s += f"（见顶样本 {peak_n}）"
    gain_s = f"涨幅 P50={gain_p50}" if gain_p50 else "涨幅分位待解析"
    dr_s = "、".join(dr_parts) if dr_parts else "death_reason 占比待解析"
    n_s = f"n={n_total}" if n_total is not None else "n=—"

    return (
        f"{peak_s} / {gain_s} 可作先验雏形；监控跨度 {span}；"
        f"death_reason（监控终止≠归零）{dr_s}；{n_s}；"
        "kagglehub `moussaouallaf/gmgn-solana-token-charts` 官方 Tokens=956 即完整数据集"
        "（Kaggle v3 全量，无更大 pack），unique>1000 验收线本源无法满足；"
        "1h/4h/24h 存活因监控跨度短全 0，需另找长 OHLCV 数据源才换挂真实存活曲线；"
        f"报告={report_rel}"
    )



def _self_survival_bypass_note(db_root: Path) -> Optional[str]:
    """Read-only self crawler observation-span survival for §10 (旁注).

    Prefers data/prior/exports/self_survival_20261010.md, else newest
    self_survival_*.md. Does not replace PRIOR_STEP1 三件套 or GMGN STEP2.
    """
    exports = db_root / "data" / "prior" / "exports"
    if not exports.is_dir():
        return None
    preferred = exports / "self_survival_20261010.md"
    if preferred.is_file():
        md_path = preferred
    else:
        cands = sorted(exports.glob("self_survival_*.md"), reverse=True)
        if not cands:
            return None
        md_path = cands[0]
    try:
        md = md_path.read_text(encoding="utf-8")
    except OSError:
        return None

    report_rel = f"data/prior/exports/{md_path.name}"

    n_total = None
    m_n = re.search(
        r"unique tokens[^\n]*:\s*\*\*?([0-9,]+)\*\*?", md, re.I
    )
    if m_n:
        n_total = int(m_n.group(1).replace(",", ""))

    rates: dict[str, str] = {}
    for label, key in (
        ("1h", "1h"),
        ("4h", "4h"),
        ("24h", "24h"),
        ("7d", "7d"),
    ):
        m = re.search(
            rf"\|\s*{re.escape(label)}(?:\s*\([^)]*\))?\s*\|\s*([0-9.]+)%",
            md,
        )
        if m:
            rates[key] = m.group(1) + "%"

    span_p50 = span_max = None
    m_span = re.search(
        r"跨度（小时）:\s*P25=[0-9.]+h\s*/\s*P50=([0-9.]+)h\s*/\s*P75=[0-9.]+h\s*/\s*max=([0-9.]+)h",
        md,
    )
    if m_span:
        span_p50, span_max = m_span.group(1), m_span.group(2)

    fake = "PASS" if re.search(r"假象扫描[^\n]*:\s*PASS", md) else None
    if fake is None and re.search(r"假象扫描[^\n]*:\s*FAIL", md):
        fake = "FAIL"

    n_s = f"n={n_total}" if n_total is not None else "n=—"
    rate_s = (
        f"1h/4h/24h/7d="
        f"{rates.get('1h', '—')}/{rates.get('4h', '—')}/"
        f"{rates.get('24h', '—')}/{rates.get('7d', '—')}"
    )
    if span_p50 and span_max:
        span_s = f"跨度 P50≈{span_p50}h；max≈{span_max}h"
    else:
        span_s = "跨度分位待解析"
    fake_s = f"假象扫描 {fake}" if fake else "假象扫描待解析"

    return (
        f"{n_s}；{rate_s}；{span_s} → 24h/7d=0% 是 crawler 短窗覆盖上限；"
        f"只读 pair_history；{fake_s}；报告={report_rel}"
    )



def _parse_bh_recorded(s: str):
    """Parse baseline_history.recorded_at → aware datetime (HK if naive)."""
    from datetime import datetime, timedelta, timezone

    if not s:
        return None
    try:
        t = s.strip()
        if t.endswith("Z"):
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt
    except Exception:
        return None


def load_baseline_history_report(
    db_root: Optional[str | Path] = None,
    *,
    n: int = 10,
    stable_window_days: int = 14,
    stable_delta: float = 0.05,
) -> dict[str, Any]:
    """Read-only prior.db.baseline_history for weekly §10 (PRIOR_STEP6).

    Observation only — never mutates weights / risk. If db or table missing,
    returns status=missing_* so the report can skip with a note.
    """
    from datetime import datetime, timedelta, timezone

    HK = timezone(timedelta(hours=8))
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    db_path = root / "data" / "prior" / "db" / "prior.db"
    out: dict[str, Any] = {
        "status": "ok",
        "n_show": n,
        "total": 0,
        "rows": [],
        "verdict": "INSUFFICIENT_DATA",
        "verdict_detail": "",
        "note": "",
        "db_rel": "data/prior/db/prior.db",
    }
    if not db_path.is_file():
        out["status"] = "missing_db"
        out["note"] = "prior.db 不存在；跳过 baseline_history（旁路未建）"
        out["verdict"] = "INSUFFICIENT_DATA"
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "baseline_history" not in tables:
                out["status"] = "missing_table"
                out["note"] = (
                    "prior.db 无 baseline_history 表；跳过"
                    "（可跑 scripts/prior_baseline_trend.py 建表并追加）"
                )
                out["verdict"] = "INSUFFICIENT_DATA"
                return out
            total = int(
                conn.execute("SELECT COUNT(*) FROM baseline_history").fetchone()[0]
            )
            out["total"] = total
            rows = conn.execute(
                """
                SELECT id, recorded_at, source,
                       survive_1h, survive_4h, survive_24h, survive_7d,
                       crawler_window_hours, n_tokens, note
                FROM baseline_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(n)),),
            ).fetchall()
            # Ascending for stability (all history, not just shown N)
            all_rows = conn.execute(
                """
                SELECT id, recorded_at, survive_4h
                FROM baseline_history
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        out["status"] = "error"
        out["note"] = f"baseline_history 读取失败: {e}"
        out["verdict"] = "INSUFFICIENT_DATA"
        return out

    out["rows"] = [
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
            "note": r[9],
        }
        for r in rows
    ]
    if total == 0:
        out["status"] = "empty"
        out["note"] = "baseline_history 表空；尚无快照"
        out["verdict"] = "INSUFFICIENT_DATA"
        out["verdict_detail"] = "need ≥2 snapshots spanning ~14d"
        return out

    # Stability (align scripts/prior_baseline_trend.py print_stability)
    cutoff = datetime.now(HK) - timedelta(days=stable_window_days)
    in_win = []
    for r in all_rows:
        dt = _parse_bh_recorded(r[1])
        if dt is None:
            continue
        if dt.astimezone(HK) >= cutoff:
            in_win.append(r)
    sample = in_win if len(in_win) >= 2 else all_rows
    s4 = [r[2] for r in sample if r[2] is not None]
    if len(s4) < 2:
        out["verdict"] = "INSUFFICIENT_DATA"
        out["verdict_detail"] = (
            f"survive_4h points={len(s4)}；需 ≥2 点且跨约 {stable_window_days}d"
        )
        return out

    first, last = s4[0], s4[-1]
    delta = last - first
    span = max(s4) - min(s4)
    ok = abs(delta) <= stable_delta and span <= stable_delta
    first_dt = _parse_bh_recorded(sample[0][1])
    last_dt = _parse_bh_recorded(sample[-1][1])
    cal_days = None
    if first_dt and last_dt:
        cal_days = (last_dt - first_dt).total_seconds() / 86400.0
        if cal_days < stable_window_days * 0.8:
            ok = False
    if len(in_win) < 2:
        ok = False
    out["verdict"] = "STABLE" if ok else "UNSTABLE_OR_SHORT"
    # SHORT shorthand when calendar span is the blocker
    if (
        out["verdict"] == "UNSTABLE_OR_SHORT"
        and cal_days is not None
        and cal_days < stable_window_days * 0.8
        and abs(delta) <= stable_delta
        and span <= stable_delta
    ):
        out["verdict"] = "SHORT"  # observational alias under UNSTABLE_OR_SHORT family
    detail_parts = [
        f"window={stable_window_days}d",
        f"points_in_window={len(in_win)}",
        f"used={len(sample)}",
        f"survive_4h {first * 100:.2f}%→{last * 100:.2f}%",
        f"Δ={delta:+.4f}",
        f"span={span:.4f}",
    ]
    if cal_days is not None:
        detail_parts.append(f"calendar={cal_days:.1f}d")
    out["verdict_detail"] = " · ".join(detail_parts)
    out["note"] = "只观察不调权；RISK_USE_PRIOR 默认关；稳定两周才谈进风控"
    return out


def format_baseline_history_section(
    info: Optional[dict[str, Any]] = None,
    *,
    db_root: Optional[str | Path] = None,
    n: int = 10,
) -> str:
    """Markdown subsection under §10: recent baseline_history snapshots."""
    if info is None:
        info = load_baseline_history_report(db_root, n=n)

    lines: list[str] = []
    lines.append("### 基线滚动快照（baseline_history，只观察）")
    lines.append("")
    lines.append(
        "> PRIOR_STEP6 旁路：`prior.db.baseline_history` 只追加快照；"
        "**不**调权、不进风控。稳定判定对齐 `prior_baseline_trend.py`"
        "（近 14d survive_4h |Δ| 与 span ≤0.05 且日历跨度够约两周 → `STABLE`；"
        "否则 `SHORT` / `UNSTABLE_OR_SHORT` / `INSUFFICIENT_DATA`）。"
    )
    lines.append("")

    status = info.get("status") or "ok"
    if status == "missing_db":
        lines.append(f"_跳过：{info.get('note')}_")
        lines.append("")
        return "\n".join(lines)
    if status == "missing_table":
        lines.append(f"_跳过：{info.get('note')}_")
        lines.append("")
        return "\n".join(lines)
    if status == "error":
        lines.append(f"_跳过：{info.get('note')}_")
        lines.append("")
        return "\n".join(lines)
    if status == "empty":
        lines.append(f"_表空：{info.get('note')}_")
        lines.append("")
        lines.append(
            f"稳定判定：`{info.get('verdict', 'INSUFFICIENT_DATA')}`"
            + (f"（{info.get('verdict_detail')}）" if info.get("verdict_detail") else "")
        )
        lines.append("")
        return "\n".join(lines)

    rows = info.get("rows") or []
    total = info.get("total", 0)
    lines.append(
        f"最近 **{len(rows)}** 条（共 {total}）；库=`{info.get('db_rel', 'data/prior/db/prior.db')}`。"
    )
    lines.append("")
    lines.append(
        "| id | recorded_at | source | survive_1h | survive_4h | 窗长(h) | n_tokens |"
    )
    lines.append("|---:|---|---|---:|---:|---:|---:|")

    def _pct_cell(x: Any) -> str:
        if x is None:
            return "—"
        try:
            return f"{float(x) * 100:.2f}%"
        except (TypeError, ValueError):
            return str(x)

    def _num_cell(x: Any, digits: int = 2) -> str:
        if x is None:
            return "—"
        try:
            return f"{float(x):.{digits}f}"
        except (TypeError, ValueError):
            return str(x)

    for r in rows:
        lines.append(
            f"| {r.get('id')} | {r.get('recorded_at') or '—'} | "
            f"{r.get('source') or '—'} | {_pct_cell(r.get('survive_1h'))} | "
            f"{_pct_cell(r.get('survive_4h'))} | "
            f"{_num_cell(r.get('crawler_window_hours'))} | "
            f"{r.get('n_tokens') if r.get('n_tokens') is not None else '—'} |"
        )
    lines.append("")
    verdict = info.get("verdict") or "INSUFFICIENT_DATA"
    detail = info.get("verdict_detail") or ""
    lines.append(
        f"**稳定判定：** `{verdict}`"
        + (f" — {detail}" if detail else "")
        + "（observational only）"
    )
    if info.get("note"):
        lines.append("")
        lines.append(f"说明：{info['note']}")
    lines.append("")
    return "\n".join(lines)



def _resolve_price_behavior_json(db_root: Optional[str | Path] = None) -> Path:
    """Resolve price_behavior.json from crawler root, prior root, or exports path."""
    root = Path(db_root) if db_root else Path(__file__).resolve().parents[1]
    candidates = [
        root,
        root / "price_behavior.json",
        root / "data" / "prior" / "exports" / "price_behavior.json",
        root / "exports" / "price_behavior.json",
        root / "data" / "prior" / "exports",
    ]
    for c in candidates:
        if c.is_file() and c.name == "price_behavior.json":
            return c
        if c.is_dir():
            j = c / "price_behavior.json"
            if j.is_file():
                return j
    # default expected path (may be missing)
    return root / "data" / "prior" / "exports" / "price_behavior.json"


def load_price_behavior_report(
    db_root: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Read-only data/prior/exports/price_behavior.json for weekly §10 (PRIOR_STEP7).

    Observation only — never mutates weights / risk. Missing file → status=missing
    so the report can skip with a note.
    """
    out: dict[str, Any] = {
        "status": "ok",
        "path_rel": "data/prior/exports/price_behavior.json",
        "note": "",
        "meta": {},
        "n_tokens": None,
        "returns_1h": {},
        "pump_4h": {},
        "mdd_4h": {},
        "jev": {},
        "raw": None,
    }
    path = _resolve_price_behavior_json(db_root)
    # keep relative note stable when under crawler root
    crawler = Path(__file__).resolve().parents[1]
    try:
        out["path_rel"] = str(path.relative_to(crawler))
    except ValueError:
        out["path_rel"] = str(path)

    if not path.is_file():
        out["status"] = "missing"
        out["note"] = (
            "price_behavior.json 不存在；跳过价格行为先验"
            "（可跑 scripts/prior_price_behavior.py 产出）"
        )
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["status"] = "error"
        out["note"] = f"price_behavior.json 读取失败: {e}"
        return out

    if not isinstance(data, dict):
        out["status"] = "error"
        out["note"] = "price_behavior.json 格式异常（非 object）"
        return out

    meta = data.get("meta") or {}
    out["meta"] = meta
    out["n_tokens"] = data.get("n_tokens_analyzed")
    out["returns_1h"] = (data.get("returns") or {}).get("1h") or {}
    pump = data.get("pump") or {}
    out["pump_4h"] = {
        "multiple": (pump.get("4h") or {}).get("multiple") or {},
        "duration_min": (pump.get("4h") or {}).get("duration_min") or {},
    }
    out["mdd_4h"] = (data.get("drawdown") or {}).get("mdd_4h") or {}
    out["jev"] = data.get("jev") or {}
    out["raw"] = data
    out["note"] = (
        "只观察不调权；RISK_USE_PRIOR 默认关；"
        "SHORT 窗勿外推长期先验；落地须走 R4"
    )
    return out


def format_price_behavior_section(
    info: Optional[dict[str, Any]] = None,
    *,
    db_root: Optional[str | Path] = None,
) -> str:
    """Markdown subsection under §10: price_behavior priors (observation only)."""
    if info is None:
        info = load_price_behavior_report(db_root)

    lines: list[str] = []
    lines.append("### 价格行为先验（price_behavior，只观察）")
    lines.append("")
    lines.append(
        "> PRIOR_STEP7 旁路：`data/prior/exports/price_behavior.json` "
        "只读拉升/收益/回撤分布；**不**调权、不进风控。"
        "`RISK_USE_PRIOR` 默认关；SHORT 窗勿外推长期先验。"
    )
    lines.append("")

    status = info.get("status") or "ok"
    if status in ("missing", "error"):
        lines.append(f"_跳过：{info.get('note')}_")
        lines.append("")
        return "\n".join(lines)

    meta = info.get("meta") or {}
    win_h = meta.get("crawler_window_h")
    short = bool(meta.get("short"))
    n_tokens = info.get("n_tokens")
    win_s = f"{float(win_h):.1f}h" if isinstance(win_h, (int, float)) else "—"
    short_s = "**SHORT**" if short else "满窗"
    n_s = f"{int(n_tokens):,}" if isinstance(n_tokens, (int, float)) else "—"

    def _pct_from_ret(x: Any, digits: int = 1) -> str:
        if x is None:
            return "—"
        try:
            return f"{float(x) * 100:.{digits}f}%"
        except (TypeError, ValueError):
            return str(x)

    def _num(x: Any, digits: int = 2) -> str:
        if x is None:
            return "—"
        try:
            return f"{float(x):.{digits}f}"
        except (TypeError, ValueError):
            return str(x)

    r1 = info.get("returns_1h") or {}
    p4 = info.get("pump_4h") or {}
    mult = p4.get("multiple") or {}
    dur = p4.get("duration_min") or {}
    mdd = info.get("mdd_4h") or {}
    jev = info.get("jev") or {}

    lines.append(
        f"窗长 ≈ **{win_s}**（{short_s}）；n_tokens ≈ **{n_s}**；"
        f"源=`{info.get('path_rel', 'data/prior/exports/price_behavior.json')}`。"
    )
    lines.append("")
    lines.append("| 块 | 指标 | 值 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| A 收益 | 1h median / P10 / P90 / n | "
        f"{_pct_from_ret(r1.get('median'))} / "
        f"{_pct_from_ret(r1.get('p10'))} / "
        f"{_pct_from_ret(r1.get('p90'))} / "
        f"{r1.get('n') if r1.get('n') is not None else '—'} |"
    )
    lines.append(
        f"| B 拉升 | 4h multiple P50 / duration_min P50 / n | "
        f"{_num(mult.get('median'), 2)}× / "
        f"{_num(dur.get('median'), 1)}min / "
        f"{mult.get('n') if mult.get('n') is not None else '—'} |"
    )
    lines.append(
        f"| C 回撤 | mdd_4h P50 / P90 / n | "
        f"{_pct_from_ret(mdd.get('median'))} / "
        f"{_pct_from_ret(mdd.get('p90'))} / "
        f"{mdd.get('n') if mdd.get('n') is not None else '—'} |"
    )
    lines.append("")

    # JEV cross — one-liner, small-n disclaimer
    jev_note = jev.get("note") or ""
    n_sig = jev.get("n_signals")
    if jev.get("available"):
        lines.append(
            f"**JEV 交叉：** 小样本（n_signals={n_sig}），仅列表；"
            + (f" {jev_note}" if jev_note else "勿强读 vs 全量基线。")
        )
    else:
        lines.append("**JEV 交叉：** 不可用；跳过。")
    lines.append("")
    lines.append(
        "**纪律：** 只观察不调权；`RISK_USE_PRIOR` 默认关；"
        "勿外推长期先验（SHORT）；落地须走 R4。"
    )
    if info.get("note"):
        lines.append("")
        lines.append(f"说明：{info['note']}")
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
    joint: Optional[dict[str, Any]] = None,
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
        unk_pct = 100.0 * n_unk / n_tot
        if n_unk == n_tot:
            age_ev = (
                f"归因 token_age 桶 unknown={n_unk}/{n_tot}"
                f"（创建时间仍缺，全样本 unknown；pairs.pair_created_at 回填后应下降）"
            )
            age_hint = (
                "确认 pairs.pair_created_at 回填与信号 chain_id+pair_address 对齐；"
                "复盘侧仅观察 unknown 占比下降，不改桶边界"
            )
        else:
            age_ev = (
                f"归因 token_age 已接 pairs.pair_created_at；"
                f"unknown={n_unk}/{n_tot}（{unk_pct:.1f}%），"
                f"相对此前全 unknown 已下降；仍观察中"
            )
            age_hint = (
                "继续观察 unknown 占比与分桶稳定性；"
                "不改桶边界，落地须走 R4"
            )
    else:
        age_ev = "归因未提供 token_age 分桶，等采集/信号层交来创建时间覆盖率"
        age_hint = (
            "确认 pairs.pair_created_at 回填与 annotate_token_age 接线；"
            "复盘侧仅观察 unknown 占比下降，不改桶边界"
        )
    suggestions.append(
        _suggestion(
            layer="采集",
            item="token_age 覆盖率观察",
            status=STATUS_WATCH,
            evidence=age_ev,
            action_hint=age_hint,
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

    ta_ev = _pandas_ta_bypass_note(root)
    if ta_ev is not None:
        suggestions.append(
            _suggestion(
                layer="指标",
                item="pandas-ta 旁路 RSI/MACD/BOLL",
                status=STATUS_WATCH,
                evidence=ta_ev,
                action_hint=(
                    "有分层区分度再建议上调权重；裁决前权重保持 0"
                ),
            )
        )

    prior_ev = _prior_survival_bypass_note(root)
    if prior_ev is not None:
        suggestions.append(
            _suggestion(
                layer="指标",
                item="先验存活过滤（PRIOR_STEP1）",
                status=STATUS_WATCH,
                evidence=prior_ev,
                action_hint=(
                    "裁决前不调权、不进策略；与现网 JEV 过滤对照后再谈落地"
                ),
            )
        )

    gmgn_ev = _prior_gmgn_v2_bypass_note(root)
    if gmgn_ev is not None:
        suggestions.append(
            _suggestion(
                layer="指标",
                item="GMGN 见顶分布（STEP2 完整 n=956）",
                status=STATUS_WATCH,
                evidence=gmgn_ev,
                action_hint="另找长 OHLCV 数据源后再换挂真实存活曲线；裁决前不调权",
            )
        )

    self_ev = _self_survival_bypass_note(root)
    if self_ev is not None:
        suggestions.append(
            _suggestion(
                layer="指标",
                item="自库存活曲线（self_survival）",
                status=STATUS_WATCH,
                evidence=self_ev,
                action_hint="与 JEV 周检并排对照；裁决前不调权",
            )
        )

    bh_info = load_baseline_history_report(root, n=10)
    if bh_info.get("status") in ("missing_db", "missing_table", "error"):
        bh_ev = bh_info.get("note") or "baseline_history 不可用；已跳过"
    elif bh_info.get("status") == "empty":
        bh_ev = (
            f"表空；稳定判定=`{bh_info.get('verdict')}`；"
            "满窗重跑 self_survival 后会追加"
        )
    else:
        rows = bh_info.get("rows") or []
        latest = rows[0] if rows else {}
        s1 = latest.get("survive_1h")
        s4 = latest.get("survive_4h")
        win = latest.get("crawler_window_hours")
        s1s = f"{s1 * 100:.2f}%" if isinstance(s1, (int, float)) else "—"
        s4s = f"{s4 * 100:.2f}%" if isinstance(s4, (int, float)) else "—"
        wins = f"{win:.2f}h" if isinstance(win, (int, float)) else "—"
        bh_ev = (
            f"最近 {len(rows)}/{bh_info.get('total', 0)} 条；"
            f"最新 survive_1h/4h={s1s}/{s4s} 窗长={wins}；"
            f"稳定判定=`{bh_info.get('verdict')}`"
            + (
                f"（{bh_info.get('verdict_detail')}）"
                if bh_info.get("verdict_detail")
                else ""
            )
            + "；详见表下「基线滚动快照」小节"
        )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="基线滚动历史（baseline_history）",
            status=STATUS_WATCH,
            evidence=bh_ev,
            action_hint=(
                "只观察不调权；稳定两周（STABLE）后再谈进风控；"
                "落地须走 R4"
            ),
        )
    )

    pb_info = load_price_behavior_report(root)
    if pb_info.get("status") in ("missing", "error"):
        pb_ev = pb_info.get("note") or "price_behavior 不可用；已跳过"
    else:
        meta = pb_info.get("meta") or {}
        win_h = meta.get("crawler_window_h")
        short = bool(meta.get("short"))
        r1 = pb_info.get("returns_1h") or {}
        mult = (pb_info.get("pump_4h") or {}).get("multiple") or {}
        dur = (pb_info.get("pump_4h") or {}).get("duration_min") or {}
        mdd = pb_info.get("mdd_4h") or {}
        win_s = f"{float(win_h):.1f}h" if isinstance(win_h, (int, float)) else "—"
        short_tag = "SHORT" if short else "满窗"
        def _pb_pct(x):
            if x is None:
                return "—"
            try:
                return f"{float(x) * 100:.1f}%"
            except (TypeError, ValueError):
                return str(x)
        def _pb_num(x, d=2):
            if x is None:
                return "—"
            try:
                return f"{float(x):.{d}f}"
            except (TypeError, ValueError):
                return str(x)
        pb_ev = (
            f"窗长≈{win_s}（{short_tag}）n_tokens={pb_info.get('n_tokens')}；"
            f"1h median/P10/P90={_pb_pct(r1.get('median'))}/"
            f"{_pb_pct(r1.get('p10'))}/{_pb_pct(r1.get('p90'))}"
            f"（n={r1.get('n')}）；"
            f"4h拉升 multiple P50={_pb_num(mult.get('median'))}×"
            f" duration P50={_pb_num(dur.get('median'), 1)}min"
            f"（n={mult.get('n')}）；"
            f"mdd_4h P50/P90={_pb_pct(mdd.get('median'))}/"
            f"{_pb_pct(mdd.get('p90'))}（n={mdd.get('n')}）；"
            "详见表下「价格行为先验」小节"
        )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="价格行为（拉升/回撤）",
            status=STATUS_WATCH,
            evidence=pb_ev,
            action_hint=(
                "只观察不调权；RISK_USE_PRIOR 默认关；"
                "SHORT 勿外推；落地须走 R4"
            ),
        )
    )



    # --- 指标加工层·周观察（只读，不调权）---
    # 数据窗 ~2026-10-09 19:12Z～10-11 14:04Z（~43h），未满 7d；
    # 主结论基于非零 fwd 样本（n=27510）；权重仍全 0。
    suggestions.append(
        _suggestion(
            layer="指标",
            item="change_1h/15m/5m 区分度（周观察）",
            status=STATUS_WATCH,
            evidence=(
                "指标加工层周观察：有效窗~43h（未满7d）；"
                "非零 fwd n=27510 上涨率≈47%；"
                "change_1h/15m/5m top20% 相对 bot20% 方向胜率差约 +19/+16/+9pp；"
                "有区分度，建议继续观察，非裁决"
            ),
            action_hint=(
                "来源=指标加工层周观察；只观察不调权；"
                "满7d 后再复测；落地须走 R4"
            ),
        )
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="volatility_24h 高档更差（风险分层）",
            status=STATUS_WATCH,
            evidence=(
                "volatility_24h 高档显著更差："
                "胜率差约 -31.5pp、med差约 -6.4pp；"
                "适合风险分层而非做多信号；"
                "rsi_14、change_4h/24h 有正差但覆盖或历史短；"
                "jev_strength 仅约 +4.6pp — 均标观察中"
            ),
            action_hint=(
                "作风险分层候选继续观察；不作做多信号；"
                "不调权；落地须走 R4"
            ),
        )
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="volume_strength / macd_hist 可降权倾向",
            status=STATUS_WATCH,
            evidence=(
                "volume_strength、macd_hist 高档未优于低档（略反向）；"
                "可降权倾向，非本周裁决"
            ),
            action_hint=(
                "倾向记下，本周无下调裁决；满7d 复测后再谈；"
                "权重保持 0；落地须走 R4"
            ),
        )
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="sentiment / spike 样本不足",
            status=STATUS_WATCH,
            evidence=(
                "sentiment_heat_score 有效配对 <50；"
                "spike/spike_ratio 落地但尚无 +1h 配对（爬虫窗口末）；"
                "CCXT/侧信道不作区分度依据"
            ),
            action_hint=(
                "等 spike 可配对 + 样本够后再复测；"
                "不调权；落地须走 R4"
            ),
        )
    )
    suggestions.append(
        _suggestion(
            layer="指标",
            item="本周无权重裁决（指标加工层）",
            status=STATUS_WATCH,
            evidence=(
                "INDICATORS_WEIGHT_*=0；未改代码/未开闸/未写库；"
                "本周无权重上调/下调裁决；"
                "数据：indicator_history×pair_history；"
                "方法：token×小时去重，主 pair 价算后续~1h 收益（±15min 近邻）"
            ),
            action_hint=(
                "纪律钉死：权重仍全 0；不改仓；只观察；"
                "满7d 或 spike 可配对后再复测；落地须走 R4"
            ),
        )
    )

    # --- 决策 ---
    suggestions.append(
        _suggestion(
            layer="决策",
            item="JEV unique_token / 门槛距离",
            status=STATUS_WATCH,
            evidence=_jev_gate_distance(jev_slice_metrics),
            action_hint=(
                "继续周检攒样本；连续 2 周过滤后 1h≥55% 且 unique_token≥10 "
                "（且 n≥15）后再启动 Step2；本层只观察"
            ),
        )
    )

    suggestions.append(
        _suggestion(
            layer="决策",
            item="策略退役规则提醒",
            status=STATUS_WATCH,
            evidence=(
                "规则（决策表）：连续 N=2 周过滤后 1h<50% 且 unique_token≥10 → "
                "建议降级/停用，勿进 R3 无限调参；现样本未跨门槛，规则只提醒不裁决"
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
            item="executed=0 / paper 未开闸",
            status=STATUS_WATCH,
            evidence=exec_ev,
            action_hint="开闸前属预期；开闸与换 executor 属执行层/R4，复盘不自动开闸",
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
    # joint attribution observation (step 4)
    try:
        from review.joint import summarize_joint_for_evolution
        joint_ev = summarize_joint_for_evolution(joint)
    except Exception as e:  # noqa: BLE001
        joint_ev = f"联合表摘要失败: {e}"
    suggestions.append(
        _suggestion(
            layer="复盘",
            item="JEV×币龄×先验联合归因",
            status=STATUS_WATCH,
            evidence=joint_ev,
            action_hint=(
                "联合归因已出，不调权；n 仍小；监控假说「1–24h 段质量最高」（过滤后 JEV n=7 up_1h=85.7%；过滤增益最大；>7d n=4 且 4h 差）；"
                "落地须走 R4"
            ),
        )
    )

    # prior_tagger bypass feed for §8 joint / attribution
    try:
        from review.joint import load_decision_prior_tags, BUCKET_SOURCE_PRIOR
        tags = load_decision_prior_tags(root)
        n_tags = len(tags)
        n_chain = len({str(r.decision_id) for r in chain}) if chain else 0
        n_hit = sum(1 for r in chain if str(r.decision_id) in tags) if tags and chain else 0
        src = (joint or {}).get("bucket_source") or (
            BUCKET_SOURCE_PRIOR if n_tags else "computed"
        )
        if n_tags > 0:
            pt_ev = (
                f"prior_tagger 已回填 {n_tags}/{n_tags}（decision_prior_tags）；"
                f"本期信号覆盖 {n_hit}/{n_chain}；§8 分桶源={src}，可自动吃标签"
            )
        else:
            pt_ev = (
                "prior.db.decision_prior_tags 空/缺失；§8 仍用现算 token_age；"
                "复盘前可跑 scripts/prior_tagger.py"
            )
    except Exception as e:  # noqa: BLE001
        pt_ev = f"prior_tagger 状态读取失败: {e}"
    suggestions.append(
        _suggestion(
            layer="复盘",
            item="prior_tagger 旁路标签",
            status=STATUS_WATCH,
            evidence=pt_ev,
            action_hint=(
                "只读 prior.db；§8/联合表优先吃 age_bucket；"
                "不进决策/不调权；落地须走 R4"
            ),
        )
    )

    suggestions.append(
        _suggestion(
            layer="复盘",
            item="候选分层过滤 token_age∈[1h,24h]",
            status=STATUS_WATCH,
            evidence=(
                "过滤后 JEV 1–24h 段 n=7 up_1h=85.7%，过滤增益最大；"
                ">7d n=4 且 4h 差；监控假说「1–24h 段质量最高」"
            ),
            action_hint=(
                "status=观察中；等 unique≥10 再攒几周才谈建议落地；"
                "裁决前不加币龄闸；落地须走 R4"
            ),
        )
    )


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



def format_indicator_weekly_obs_section() -> str:
    """Markdown subsection under §10: 指标加工层·周观察（只读，不调权）。

    Static Monday observation from the indicator layer — observation only;
    never mutates INDICATORS_WEIGHT_* / gates / DB. Landing requires R4.
    """
    lines: list[str] = []
    lines.append("### 指标加工层·周观察（只读）")
    lines.append("")
    lines.append(
        "> 来源=指标加工层周观察；数据=`dexscreener.db` 的"
        " `indicator_history`×`pair_history`；"
        "有效窗约 **2026-10-09 19:12Z～10-11 14:04Z（~43h）**，未满 7 天。"
        "方法：按 `token×小时` 去重，用主 pair 价算后续 ~1h 收益（±15min 近邻）；"
        "主结论基于**非零 fwd** 样本（n=27510，上涨率≈47%；全样本含约 30% 零收益）。"
        "权重：`INDICATORS_WEIGHT_*=0`，未改代码/未开闸/未写库。"
    )
    lines.append("")
    lines.append("| 类别 | 指标 | 观察要点 | 状态 |")
    lines.append("|---|---|---|---|")
    lines.append(
        "| 有区分度 | `change_1h` / `15m` / `5m` | "
        "top20% 相对 bot20% 方向胜率差约 **+19 / +16 / +9pp**；"
        "建议继续观察，非裁决 | **观察中** |"
    )
    lines.append(
        "| 有区分度（风险） | `volatility_24h` | "
        "高档显著更差（胜率差约 **-31.5pp**、med差约 **-6.4pp**）；"
        "适合风险分层而非做多信号 | **观察中** |"
    )
    lines.append(
        "| 观察中 | `rsi_14`、`change_4h`/`24h` | "
        "有正差但覆盖或历史短 | **观察中** |"
    )
    lines.append(
        "| 观察中 | `jev_strength` | 仅约 **+4.6pp** | **观察中** |"
    )
    lines.append(
        "| 可降权倾向 | `volume_strength`、`macd_hist` | "
        "高档未优于低档（略反向） | **观察中** |"
    )
    lines.append(
        "| 样本不足 | `sentiment_heat_score` | 有效配对 **<50** | **观察中** |"
    )
    lines.append(
        "| 样本不足 | `spike` / `spike_ratio` | "
        "落地但尚无 +1h 配对（爬虫窗口末） | **观察中** |"
    )
    lines.append("")
    lines.append(
        "**动作：** 本周无权重上调/下调裁决；满 7d 或 spike 可配对后再复测。"
        " CCXT/侧信道不作区分度依据。"
    )
    lines.append("")
    lines.append(
        "**纪律：** 权重仍全 0；不改仓；只观察；落地须走 **R4**。"
    )
    lines.append("")
    return "\n".join(lines)


def format_evolution_section(
    suggestions: Sequence[dict[str, str]],
    *,
    db_root: Optional[str | Path] = None,
    baseline_history: Optional[dict[str, Any]] = None,
    baseline_n: int = 10,
    price_behavior: Optional[dict[str, Any]] = None,
) -> str:
    """Markdown body for §10 (without the ## heading).

    Appends baseline_history (PRIOR_STEP6), price_behavior (PRIOR_STEP7),
    and 指标加工层·周观察 subsections (all observation-only).
    Missing sources → skip with an explicit note.
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

    # §10 subsection: baseline_history rolling snapshots
    lines.append(
        format_baseline_history_section(
            baseline_history, db_root=db_root, n=baseline_n
        )
    )
    # §10 subsection: price_behavior priors (PRIOR_STEP7)
    lines.append(
        format_price_behavior_section(
            price_behavior, db_root=db_root
        )
    )
    # §10 subsection: 指标加工层·周观察（只读，不调权）
    lines.append(format_indicator_weekly_obs_section())
    return chr(10).join(lines)
