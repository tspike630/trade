"""RD-Agent 式半自动因子进化循环（JEV 周检联动）。

流程：collect(知识森林) -> propose(提因子) -> backtest(回测) -> gate(门槛) -> store(入库)
半自动：LLM 提因子默认关（JEV_LOOP_LLM_ENABLED=0，规则模板兜底）；
        过门槛只入候选库（status=accepted 待激活），不自动启用策略；不自动调参。

用法：
    python -m review.factor_loop --week 2026-W42 --dry-run
    python -m review.factor_loop --week 2026-W42 --with-backtest
    python -m review.factor_loop --week 2026-W42 --llm   # 需 JEV_LOOP_LLM_ENABLED=1 + GROK key
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review.gates import (
    CONSECUTIVE_WEEKS,
    GATE_HARD_N,
    GATE_UNIQUE,
    GATE_UP_1H,
    GATE_UP_1H_DEGRADE,
    SAMPLE_NOTE,
)

try:
    from config import DB_PATH as _CONFIG_DB_PATH
except ImportError:
    _CONFIG_DB_PATH = None

try:
    from config import REPORTS_DIR as _CONFIG_REPORTS_DIR
except ImportError:
    _CONFIG_REPORTS_DIR = None

try:
    from decision_engine.backtest.run import main as backtest_main
except ImportError:
    backtest_main = None  # type: ignore[assignment]

FACTOR_DB = Path(__file__).resolve().parent / "factor_candidates.db"


def _reports_dir() -> Path:
    raw = _CONFIG_REPORTS_DIR
    if not raw:
        return ROOT / "exports" / "reports"
    path = Path(str(raw))
    if not path.is_absolute():
        path = ROOT / path
    return path


REPORTS = _reports_dir()

# Bandit direction switch. Not the weekly-check consecutive-week gate.
MAX_CONSECUTIVE_FAIL_WEEKS = 3

# jev_validity.py CLI defaults (min_price / tol). Not a new definition.
_MIN_PRICE = 1e-6
_TOL_SEC = 1800.0
_STRATEGY_ID = "jev_strength_v1"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS factor_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id TEXT NOT NULL UNIQUE,
    week TEXT NOT NULL,
    hypothesis TEXT,
    reason TEXT,
    params_json TEXT,
    backtest_json TEXT,
    gate_result TEXT,
    gate_reason TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT,
    confirmed_at TEXT,
    notes TEXT
)
"""

# store() never writes these. activated is human-only.
_STORE_STATUSES = frozenset({"pending", "accepted", "rejected", "retired"})

_METRIC_KEYS = (
    "n_raw",
    "n_filt",
    "n_1h",
    "n_4h",
    "unique_token",
    "up_1h",
    "up_4h",
    "med_1h",
    "med_4h",
    "ic_mean",
    "ir",
    "win_rate",
    "pnl",
    "note",
)


def _load_jev_validity() -> Any:
    """Load this tree's scripts/jev_validity.py (query + HorizonStats口径)."""
    path = ROOT / "scripts" / "jev_validity.py"
    spec = importlib.util.spec_from_file_location("jev_validity_loop", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses look up cls.__module__ in sys.modules during class body.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_JEV = _load_jev_validity()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _blank_metrics(note: str = "") -> dict[str, Any]:
    metrics: dict[str, Any] = {key: None for key in _METRIC_KEYS}
    metrics["n_raw"] = 0
    metrics["n_filt"] = 0
    metrics["n_1h"] = 0
    metrics["n_4h"] = 0
    metrics["unique_token"] = 0
    metrics["note"] = note
    return metrics


def _join_note(prev: Optional[str], extra: str) -> str:
    base = (prev or "").strip().strip(";")
    if not base:
        return extra
    if extra in base.split(";"):
        return base
    return f"{base};{extra}"


def _business_db_path() -> Path:
    """Read path only. Same resolution as scripts/jev_validity.py when it has one."""
    env = os.environ.get("DECISION_DB_PATH", "").strip() or os.environ.get("DB_PATH", "").strip()
    if env:
        return Path(env)
    default_fn = getattr(_JEV, "default_db_path", None)
    if callable(default_fn):
        return Path(default_fn())
    default_db = getattr(_JEV, "DEFAULT_DB", None)
    if default_db:
        return Path(default_db)
    if _CONFIG_DB_PATH:
        path = Path(str(_CONFIG_DB_PATH))
        if not path.is_absolute():
            path = ROOT / path
        return path
    return ROOT / "dexscreener.db"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA_SQL)
    conn.commit()


def _connect_factor_db() -> sqlite3.Connection:
    FACTOR_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(FACTOR_DB))
    _ensure_schema(conn)
    return conn


def _clamp_status(status: str) -> str:
    """Candidate store only. Never persist activated from this loop."""
    if status in _STORE_STATUSES:
        return status
    return "pending"


def _factor_id(params: dict[str, Any]) -> str:
    strength = float(params["strength"])
    liq = int(params["liq"])
    cov = int(params["cov"])
    fid = f"jev_liq{liq // 1000}k_cov{cov}_s{strength * 100:.0f}".replace(".", "")
    return f"{fid}_v1"


def _llm_enabled() -> bool:
    return os.environ.get("JEV_LOOP_LLM_ENABLED", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _grok_api_key() -> Optional[str]:
    for name in ("GROK_API_KEY", "XAI_API_KEY"):
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return None


# ------------------------------------------------------------------ #
# 阶段 0：collect —— 知识森林（本周上下文）
# ------------------------------------------------------------------ #
def collect_week_context(week: str) -> dict[str, Any]:
    """读本周验证结果 + IC 历史 + 历史候选（含教训），组上下文包。"""
    ctx: dict[str, Any] = {"week": week, "trials": [], "ic_history": [], "prior_candidates": []}

    # 1) 本周 jev_validity 输出（执行 AI 对齐现有 jev_validity.py 的真实输出文件/格式）
    if REPORTS.is_dir():
        for pat in ("jev_validity*.json", "jev_validity*.csv", "jev_validity*.md"):
            files = sorted(REPORTS.glob(pat))
            if not files:
                continue
            latest = files[-1]
            if latest.suffix == ".json":
                try:
                    ctx["trials"].append(json.loads(latest.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError, UnicodeError):
                    pass
            elif latest.suffix == ".csv":
                try:
                    with latest.open(encoding="utf-8", newline="") as fh:
                        ctx["trials"].extend(list(csv.DictReader(fh)))
                except OSError:
                    pass

    # 2) IC 历史（方案4-2 产物）
    icf = REPORTS / "factor_ic_history.csv"
    if icf.is_file():
        try:
            with icf.open(encoding="utf-8", newline="") as fh:
                ctx["ic_history"] = list(csv.DictReader(fh))
        except OSError:
            ctx["ic_history"] = []

    # 3) 历史候选（SOTA 集 + 教训）
    conn = _connect_factor_db()
    try:
        for row in conn.execute(
            "SELECT factor_id, week, gate_result, status, notes, gate_reason "
            "FROM factor_candidates ORDER BY id DESC LIMIT 50"
        ).fetchall():
            ctx["prior_candidates"].append(
                {
                    "factor_id": row[0],
                    "week": row[1],
                    "gate_result": row[2],
                    "status": row[3],
                    "notes": row[4],
                    "gate_reason": row[5],
                }
            )
    finally:
        conn.close()
    return ctx


def _latest_ic(ctx: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    """Copy ic_mean / ir only when factor_ic_history.csv already has those columns."""
    rows = ctx.get("ic_history") or []
    if not rows:
        return None, None
    last = rows[-1]

    def _pick(*keys: str) -> Optional[float]:
        for key in keys:
            raw = last.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
        return None

    return _pick("ic_mean", "ic", "mean_ic"), _pick("ir", "ic_ir")


# ------------------------------------------------------------------ #
# 阶段 1：propose —— 提因子（1-5 个；简单→复杂；防冗余）
# ------------------------------------------------------------------ #
def _llm_propose(ctx: dict[str, Any], cands: list[dict[str, Any]]) -> int:
    """RD-Agent 风格 Grok chat/completions。无 key / 关 / 解析失败则抛出让上层兜底。

    不把密钥写入返回值、notes 或异常文本。
    """
    if not _llm_enabled():
        raise RuntimeError("llm_disabled")
    key = _grok_api_key()
    if not key:
        raise RuntimeError("llm_no_key")

    prior = []
    for item in (ctx.get("prior_candidates") or [])[:20]:
        prior.append(
            {
                "factor_id": item.get("factor_id"),
                "week": item.get("week"),
                "gate_result": item.get("gate_result"),
                "status": item.get("status"),
                "notes": item.get("notes"),
                "gate_reason": item.get("gate_reason"),
            }
        )
    trials = ctx.get("trials") or []
    blob = json.dumps(
        {"week": ctx.get("week"), "trials": trials[:5], "prior_candidates": prior},
        ensure_ascii=False,
        default=str,
    )
    if len(blob) > 12000:
        blob = blob[:12000]
    prompt = (
        "以下为本期验证试验（观察/评估/判定）与历史候选（含失败教训）：\n"
        f"{blob}\n"
        "请分析成功/失败原因，提出 1-3 个改进或新方向的候选因子，输出 JSON：\n"
        '{"hypothesis": "...", "reason": "...", "params": {"strength": 0.2, "liq": 50000, "cov": 10}}\n'
        "可以输出一个对象或对象数组。\n"
        "约束：简单优先；不得与历史候选（SOTA 集）参数重复；"
        f"失败 {MAX_CONSECUTIVE_FAIL_WEEKS} 周方向需更换。"
    )
    base = os.environ.get("GROK_API_BASE", "https://api.x.ai/v1").strip().rstrip("/") or "https://api.x.ai/v1"
    model = os.environ.get("GROK_MODEL", "grok-3-mini").strip() or "grok-3-mini"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你只输出 JSON，不输出其它文字。不要建议自动激活策略或自动调参。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("llm_http_failed") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("llm_bad_response") from exc
    if not isinstance(content, str):
        raise RuntimeError("llm_bad_response")
    parsed = _parse_llm_candidates(content)
    if not parsed:
        raise RuntimeError("llm_parse_failed")

    existing = {_factor_id(c["params"]) for c in cands if isinstance(c.get("params"), dict)}
    existing.update(str(c.get("factor_id")) for c in cands)
    added = 0
    for item in parsed:
        params = item["params"]
        fid = _factor_id(params)
        if fid in existing:
            continue
        existing.add(fid)
        cands.append(
            {
                "factor_id": fid,
                "hypothesis": item["hypothesis"],
                "reason": item["reason"],
                "params": params,
                "source": "llm",
            }
        )
        added += 1
        if added >= 3:
            break
    if added == 0:
        raise RuntimeError("llm_duplicate_only")
    return added


def _parse_llm_candidates(content: str) -> list[dict[str, Any]]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            start = text.find("[")
            end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            obj = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(obj, dict) and isinstance(obj.get("candidates"), list):
        obj = obj["candidates"]
    if isinstance(obj, dict):
        obj = [obj]
    if not isinstance(obj, list):
        return []
    out: list[dict[str, Any]] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        params = item.get("params")
        if not isinstance(params, dict):
            continue
        try:
            norm = {
                "strength": float(params["strength"]),
                "liq": int(params["liq"]),
                "cov": int(params["cov"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        hypothesis = str(item.get("hypothesis") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not hypothesis:
            continue
        out.append({"hypothesis": hypothesis, "reason": reason, "params": norm})
    return out


def propose_candidates(ctx: dict[str, Any], *, llm: bool = False) -> list[dict[str, Any]]:
    """生成候选因子。规则模板兜底；LLM 增强可选（失败自动兜底）。"""
    cands: list[dict[str, Any]] = []

    # A) 规则模板：参数网格（简单优先，≤5 个）
    grid = [
        {"strength": 0.20, "liq": 50_000, "cov": 10},
        {"strength": 0.20, "liq": 100_000, "cov": 10},
        {"strength": 0.25, "liq": 50_000, "cov": 15},
        {"strength": 0.15, "liq": 100_000, "cov": 15},
    ]
    for params in grid:
        cands.append(
            {
                "factor_id": _factor_id(params),
                "hypothesis": (
                    f"JEV 强度≥{params['strength']} 且流动性≥${params['liq']} "
                    f"且覆盖≥{params['cov']} 的新币，1h 上涨率显著>50%"
                ),
                "reason": "过滤维度组合（强度×流动性×覆盖），简单优先；上周最优切片反推候选。",
                "params": params,
                "source": "rule_grid",
            }
        )

    # B) LLM 增强（JEV_LOOP_LLM_ENABLED=1 且有 key 时）。
    #    解析失败 / 无 key / 超时 / 默认关 -> 静默回落规则模板（exit 0）。
    if llm:
        try:
            _llm_propose(ctx, cands)
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            for cand in cands:
                if cand.get("source") != "llm":
                    cand["source"] = "rule_fallback"

    # C) 防冗余 + 教训去重：已在历史库存在（含 rejected）的同参数候选 -> 标注重试
    prior_ids = {c["factor_id"] for c in ctx.get("prior_candidates", [])}
    out = []
    for cand in cands:
        cand["retry"] = cand["factor_id"] in prior_ids
        out.append(cand)
    return out[:5]


# ------------------------------------------------------------------ #
# 阶段 2：backtest —— 回测（切片统计；可选叠加回测框架）
# ------------------------------------------------------------------ #
def backtest_candidate(
    cand: dict[str, Any],
    *,
    with_backtest: bool = False,
    ctx: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """切片统计，口径直接调用 scripts/jev_validity.run_analysis。

    n_raw / n_filt / unique_token 用 run_analysis 的同名字段。
    up_1h / up_4h / med_1h 用过滤后 HorizonStats（st1/st4），与
    decision_line、趋势 CSV 的 up_1h_rate / median_1h 同一套样本。
    n_1h 是 decision_line 对比 MIN_RAW_N 的样本数（st1.n），单独记录，
    门槛函数仍按计划读 n_raw。
    n_raw 是本候选参数扫描计数，不是周检 decision_log 信号样本；
    激活仍走周检口径（见 review.gates.SAMPLE_NOTE）。
    """
    params = cand.get("params") or {}
    try:
        strength = float(params["strength"])
        liq = float(params["liq"])
        cov = float(params["cov"])
    except (KeyError, TypeError, ValueError):
        return _blank_metrics("bad_params")

    metrics = _blank_metrics("")
    ic_mean, ir = _latest_ic(ctx or {})
    metrics["ic_mean"] = ic_mean
    metrics["ir"] = ir

    db_path = _business_db_path()
    if not db_path.is_file():
        metrics["note"] = "db_missing"
        return metrics

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _JEV.open_db(db_path)
        result = _JEV.run_analysis(
            conn,
            min_strength=strength,
            min_liq=liq,
            min_coverage=cov,
            min_price=_MIN_PRICE,
            tol_sec=_TOL_SEC,
        )
    except (sqlite3.Error, OSError, FileNotFoundError):
        metrics["note"] = "jev_query_failed"
        return metrics
    finally:
        if conn is not None:
            conn.close()

    st1 = result["st1"]
    st4 = result["st4"]
    metrics.update(
        {
            "n_raw": int(result["n_raw"]),
            "n_filt": int(result["n_filt"]),
            "n_1h": int(st1.n),
            "n_4h": int(st4.n),
            "unique_token": int(result["unique_token"]),
            "up_1h": st1.up_rate,
            "up_4h": st4.up_rate,
            "med_1h": st1.med,
            "med_4h": st4.med,
            "note": "jev_validity.run_analysis",
        }
    )
    if with_backtest:
        _overlay_framework_backtest(metrics, params, db_path)
    return metrics


def _overlay_framework_backtest(
    metrics: dict[str, Any],
    params: dict[str, Any],
    db_path: Path,
) -> None:
    """决策引擎回测框架的 win_rate / pnl。账本写临时目录，不碰业务库。"""
    if backtest_main is None:
        metrics["note"] = _join_note(metrics.get("note"), "backtest_import_unavailable")
        return
    tmp = Path(tempfile.mkdtemp(prefix="jev_loop_bt_"))
    ledger = tmp / "loop_backtest.db"
    json_out = tmp / "result.json"
    argv = [
        "--strategy",
        _STRATEGY_ID,
        "--db",
        str(db_path),
        "--ledger",
        str(ledger),
        "--min-liq",
        str(params["liq"]),
        "--min-coverage",
        str(params["cov"]),
        "--min-price",
        str(_MIN_PRICE),
        "--min-jev-strength",
        str(params["strength"]),
        "--days",
        "7",
        "--json-out",
        str(json_out),
    ]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            backtest_main(argv)
        if not json_out.is_file():
            metrics["note"] = _join_note(metrics.get("note"), "backtest_no_json")
            return
        payload = json.loads(json_out.read_text(encoding="utf-8"))
        by = payload.get("by_strategy") or {}
        row = by.get(_STRATEGY_ID) or {}
        if row:
            metrics["win_rate"] = row.get("win_rate")
            metrics["pnl"] = row.get("total_pnl_usd")
            metrics["note"] = _join_note(metrics.get("note"), "decision_engine.backtest")
        elif not payload.get("ok", True):
            metrics["note"] = _join_note(metrics.get("note"), "backtest_insufficient_data")
    except (OSError, json.JSONDecodeError, RuntimeError, ValueError, sqlite3.Error):
        metrics["note"] = _join_note(metrics.get("note"), "backtest_failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ #
# 阶段 3：gate —— 门槛判定（防火墙，三段式结论）
# ------------------------------------------------------------------ #
def gate_candidate(metrics: dict[str, Any], *, week: str) -> dict[str, Any]:
    """硬指标判定：pass / need_retest / fail + 观察/评估/判定三段式。

    M-2 方向校准不在这里做。``review.gates.calibrate_direction`` 会在 IC
    有限时记下正反两个符号，IC 缺失则 fail closed；周跑仍只看 n / unique /
    up_1h。接入会在 ic 为空时改掉 accepted，所以先留
    ``DIRECTION_CALIBRATION_TODO``。权重保持 0。
    """
    del week  # 周次由 store 写入；判定本身不改门槛
    n = metrics.get("n_raw") or 0
    uni = metrics.get("unique_token") or 0
    up1h = metrics.get("up_1h")
    obs = f"n={n}, unique={uni}, up_1h={up1h}"
    eva: str
    if n < GATE_HARD_N or uni < GATE_UNIQUE:
        eva = f"样本不足（n<{GATE_HARD_N} 或 unique<{GATE_UNIQUE}）"
        return {
            "gate_result": "fail",
            "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:样本不足不入库",
            "status": "rejected",
        }
    if up1h is None:
        eva = "无 1h 数据"
        return {
            "gate_result": "fail",
            "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:数据缺失",
            "status": "rejected",
        }
    if up1h >= GATE_UP_1H:
        eva = (
            f"过滤后 1h 上涨率 {up1h:.1%}≥{GATE_UP_1H:.0%}"
            f"（首次达标；连续 {CONSECUTIVE_WEEKS} 周达标才考虑激活，"
            "本循环 status 最高 accepted）"
        )
        return {
            "gate_result": "pass",
            "gate_reason": (
                f"观察:{obs}; 评估:{eva}; "
                f"判定:候选有效，待连续 {CONSECUTIVE_WEEKS} 周确认，不自动激活"
            ),
            "status": "accepted",
        }
    if up1h >= GATE_UP_1H_DEGRADE:
        eva = (
            f"过滤后 1h 上涨率 {up1h:.1%} 在 "
            f"{GATE_UP_1H_DEGRADE:.0%}~{GATE_UP_1H:.0%} 之间"
        )
        return {
            "gate_result": "need_retest",
            "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:下周复验",
            "status": "pending",
        }
    eva = f"过滤后 1h 上涨率 {up1h:.1%}<{GATE_UP_1H_DEGRADE:.0%}"
    return {
        "gate_result": "fail",
        "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:无预测力，记录教训",
        "status": "rejected",
    }


# ------------------------------------------------------------------ #
# 阶段 4：store —— 入库（SOTA 集 + 反馈）
# ------------------------------------------------------------------ #
def store_candidate(
    cand: dict[str, Any],
    metrics: dict[str, Any],
    decision: dict[str, Any],
    *,
    week: str,
) -> None:
    """写 factor_candidates（factor_id 唯一；重跑=更新不重复）。"""
    source = str(cand.get("source") or "")
    lesson = str(decision.get("gate_reason") or "")
    notes = source if not lesson else f"{source} | {lesson}"
    status = _clamp_status(str(decision.get("status") or "pending"))
    conn = _connect_factor_db()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_candidates
                (factor_id, week, hypothesis, reason, params_json, backtest_json,
                 gate_result, gate_reason, status, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cand["factor_id"],
                week,
                cand["hypothesis"],
                cand["reason"],
                json.dumps(cand["params"], default=str),
                json.dumps(metrics, default=str),
                decision["gate_result"],
                decision["gate_reason"],
                status,
                _utc_now_iso(),
                notes,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_report(
    week: str,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> Path:
    """Write the factor-loop markdown. dry-run never touches the production path."""
    base = REPORTS / "dry-run" if dry_run else REPORTS
    base.mkdir(parents=True, exist_ok=True)
    rep = base / f"factor_loop_{week.replace('-', '')}.md"
    lines = [f"# 因子进化周报 {week}", ""]
    if dry_run:
        lines.append(
            "> dry-run：不插入 factor_candidates，不覆盖 "
            "`exports/reports/factor_loop_*.md`。"
        )
        lines.append("")
    lines.append(SAMPLE_NOTE)
    lines.append("")
    lines.append("过门槛仅 status=accepted（待激活）。本循环不自动启用策略、不自动调参。")
    lines.append("")
    for row in rows:
        gate = row["gate"]
        lines.append(f"## {row['factor_id']} — {gate['gate_result']}")
        lines.append("")
        lines.append(f"- 假设: {row['hypothesis']}")
        lines.append(f"- 来源: {row.get('source', '')}")
        lines.append(f"- 回测: {json.dumps(row['metrics'], ensure_ascii=False)}")
        lines.append(f"- 判定: {gate['gate_reason']}")
        lines.append(f"- 状态: {gate['status']}")
        lines.append("")
    rep.write_text("\n".join(lines), encoding="utf-8")
    return rep


# ------------------------------------------------------------------ #
# CLI 主流程
# ------------------------------------------------------------------ #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JEV 因子进化循环（RD-Agent 式）")
    ap.add_argument("--week", required=True, help="ISO 周，如 2026-W42")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="候选与判定只写 dry-run 报告；不插入 factor_candidates，不覆盖正式周报",
    )
    ap.add_argument("--with-backtest", action="store_true", help="叠加回测框架出 win_rate/pnl")
    ap.add_argument("--llm", action="store_true", help="启用 LLM 提因子（需 JEV_LOOP_LLM_ENABLED=1）")
    args = ap.parse_args(argv)

    ctx = collect_week_context(args.week)
    cands = propose_candidates(ctx, llm=args.llm)
    if not cands:
        print(json.dumps({"week": args.week, "candidates": 0, "note": "无候选"}, default=str))
        return 0

    rows = []
    for cand in cands:
        metrics = backtest_candidate(cand, with_backtest=args.with_backtest, ctx=ctx)
        decision = gate_candidate(metrics, week=args.week)
        rows.append(
            {
                "factor_id": cand["factor_id"],
                "hypothesis": cand["hypothesis"],
                "source": cand.get("source", ""),
                "retry": bool(cand.get("retry")),
                "metrics": metrics,
                "gate": decision,
            }
        )
        if not args.dry_run:
            store_candidate(cand, metrics, decision, week=args.week)

    sources = sorted({str(r.get("source") or "") for r in rows})
    out = {
        "week": args.week,
        "candidates": len(rows),
        "sources": sources,
        "rows": rows,
        "dry_run": args.dry_run,
        "auto_activate": False,
    }
    report_path = _write_report(args.week, rows, dry_run=bool(args.dry_run))
    out["report"] = str(report_path)
    print(json.dumps(out, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
