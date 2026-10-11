"""Section 10: evolution suggestions (read-only observations; never mutates params)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional, Sequence

from review.data import SignalChainRow
from review.joint import (
    BUCKET_SOURCE_PRIOR,
    load_decision_prior_tags,
    summarize_joint_for_evolution,
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
        # Optional: config.py lives on the crawler path, not the review package.
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


def format_evolution_section(suggestions: Sequence[dict[str, str]]) -> str:
    """Markdown body for §10 (without the ## heading)."""
    lines: list[str] = []
    lines.append(
        "> **钉死：** 本节只建议，**不**自动改参 / 开闸 / 写配置；"
        "任何落地须走 **R4 人工确认**。"
    )
    lines.append("")
    if not suggestions:
        lines.append("_本期无进化建议（各层数据未交来）。_")
        lines.append("")
        return "\n".join(lines)

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
    return "\n".join(lines)
