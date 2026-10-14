# QUANT_REFACTOR_PLAN_06 — 周检 JEV 复验半自动进化循环（借鉴 RD-Agent）

> 时间：2026-10-12 · 前置：jev_validity.py（周检复用）、factor_ic_history.csv（方案4-2）、review 周报（§1-§11）
> 借鉴：微软 RD-Agent / RD-Agent(Q)（arXiv 2505.15155）——Research 提假设 → Development 回测 → Analysis 评估 → SOTA 集 + 反馈闭环
> 目标：把周检变成「① 提因子 → ② 回测 → ③ 门槛 → ④ 入库」半自动闭环；让决策层因子库每周自进化
> 纪律：LLM 默认关（规则模板兜底）；过门槛只入候选库不自动激活；不自动调参；业务四库只读；全程可审计

---

## 一、文件与数据

| 件 | 说明 |
|----|------|
| `review/factor_loop.py` | 四阶段循环脚本（本次核心，整文件见 §二） |
| `review/factor_candidates.db` | 因子候选库（review 层自己的产物库；**不是业务四库**） |
| `exports/reports/factor_loop_YYYYMMDD.md` | 每周进化报告（周报 § 引用/汇总） |
| 周检定时任务 query | 落地验收后由主 Agent 更新（周一 09:15 加 `factor_loop`） |

---

## 二、源码（整文件可复制）

### 2.1 表（review/factor_candidates.db，脚本内幂等建）

```sql
CREATE TABLE IF NOT EXISTS factor_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    factor_id TEXT NOT NULL UNIQUE,      -- 如 jev_liq100_cov15_strength020_v1
    week TEXT NOT NULL,                   -- 如 2026-W42
    hypothesis TEXT,                      -- 假设（LLM 或规则模板生成）
    reason TEXT,                          -- 理由
    params_json TEXT,                     -- 参数（filters + 阈值）
    backtest_json TEXT,                   -- 回测指标
    gate_result TEXT,                     -- pass / need_retest / fail
    gate_reason TEXT,                     -- 判定原因（三段式：观察/评估/判定）
    status TEXT DEFAULT 'pending',        -- pending / accepted(待激活) / activated / rejected / retired
    created_at TEXT,
    confirmed_at TEXT,
    notes TEXT                            -- 教训/反馈（喂给下周提因子）
);
```

### 2.2 `review/factor_loop.py`

```python
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
import csv
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import DB_PATH, REPORTS_DIR  # noqa: E402  (REPORTS_DIR 若不存在用 exports/reports)

FACTOR_DB = Path(__file__).resolve().parent / "factor_candidates.db"
REPORTS = REPORTS_DIR if "REPORTS_DIR" in globals() else ROOT / "exports" / "reports"

# ---- 门槛（沿用已定决策表，勿调） ----
GATE_HARD_N = 15          # raw n 硬门槛
GATE_UNIQUE = 10          # unique_token 硬门槛
GATE_UP_1H = 0.55         # 过滤后 1h 上涨率有效门槛（连续两周达标才激活）
GATE_UP_1H_RETRY = 0.40   # 40%~55% -> need_retest

# ---- 防冗余：同方向连续失败 N 周 -> 换方向（简化 bandit） ----
MAX_CONSECUTIVE_FAIL_WEEKS = 3


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ #
# 阶段 0：collect —— 知识森林（本周上下文）
# ------------------------------------------------------------------ #
def collect_week_context(week: str) -> dict[str, Any]:
    """读本周验证结果 + IC 历史 + 历史候选（含教训），组上下文包。"""
    ctx: dict[str, Any] = {"week": week, "trials": [], "ic_history": [], "prior_candidates": []}

    # 1) 本周 jev_validity 输出（执行 AI 对齐现有 jev_validity.py 的真实输出文件/格式）
    for pat in ("jev_validity*.json", "jev_validity*.csv", "jev_validity*.md"):
        for f in sorted(REPORTS.glob(pat)):
            if f.suffix == ".json":
                try:
                    ctx["trials"].append(json.loads(f.read_text()))
                except Exception:  # noqa: BLE001
                    pass
            elif f.suffix == ".csv":
                with f.open() as fh:
                    ctx["trials"].extend(list(csv.DictReader(fh)))
            break  # 取最新一个即可

    # 2) IC 历史（方案4-2 产物）
    icf = REPORTS / "factor_ic_history.csv"
    if icf.exists():
        with icf.open() as fh:
            ctx["ic_history"] = list(csv.DictReader(fh))

    # 3) 历史候选（SOTA 集 + 教训）
    conn = sqlite3.connect(str(FACTOR_DB))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS factor_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor_id TEXT NOT NULL UNIQUE, week TEXT NOT NULL,
                hypothesis TEXT, reason TEXT, params_json TEXT,
                backtest_json TEXT, gate_result TEXT, gate_reason TEXT,
                status TEXT DEFAULT 'pending', created_at TEXT,
                confirmed_at TEXT, notes TEXT
            )
            """
        )
        for r in conn.execute(
            "SELECT factor_id, week, gate_result, status, notes FROM factor_candidates "
            "ORDER BY id DESC LIMIT 50"
        ).fetchall():
            ctx["prior_candidates"].append(
                {"factor_id": r[0], "week": r[1], "gate_result": r[2], "status": r[3], "notes": r[4]}
            )
    finally:
        conn.close()
    return ctx


# ------------------------------------------------------------------ #
# 阶段 1：propose —— 提因子（1-5 个；简单→复杂；防冗余）
# ------------------------------------------------------------------ #
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
    for p in grid:
        fid = f"jev_liq{p['liq']//1000}k_cov{p['cov']}_s{p['strength']*100:.0f}".replace(".", "")
        cands.append(
            {
                "factor_id": f"{fid}_v1",
                "hypothesis": f"JEV 强度≥{p['strength']} 且流动性≥${p['liq']} 且覆盖≥{p['cov']} 的新币，1h 上涨率显著>50%",
                "reason": f"过滤维度组合（强度×流动性×覆盖），简单优先；上周最优切片反推候选。",
                "params": p,
                "source": "rule_grid",
            }
        )

    # B) LLM 增强（JEV_LOOP_LLM_ENABLED=1 且有 key 时）：
    #    RD-Agent 风格 prompt：给 Trial 列表（观察/评估/判定）+ 历史 SOTA + 教训，
    #    要求 JSON { "hypothesis": "...", "reason": "...", "params": {...} }。
    #    解析失败 / 无 key / 超时 -> 静默回落规则模板（exit 0，notes 标注）。
    if llm:
        try:
            _llm_propose(ctx, cands)   # 见 §三 说明；执行 AI 按已有 GROK 链路接
        except Exception:  # noqa: BLE001
            pass

    # C) 防冗余 + 教训去重：已在历史库存在（含 rejected）的同参数候选 -> 标注重试
    prior_ids = {c["factor_id"] for c in ctx.get("prior_candidates", [])}
    out = []
    for c in cands:
        c["retry"] = c["factor_id"] in prior_ids
        out.append(c)
    return out[:5]


# ------------------------------------------------------------------ #
# 阶段 2：backtest —— 回测（切片统计；可选叠加回测框架）
# ------------------------------------------------------------------ #
def backtest_candidate(cand: dict[str, Any], *, with_backtest: bool = False) -> dict[str, Any]:
    """对候选参数跑切片统计（n/unique/1h/4h/med）。

    实现口径与 jev_validity.py 对齐：decision_log(阈过滤后) join token_indicators，
    统计过滤后样本的 1h/4h 上涨率与中位收益。执行 AI 复用现有查询函数。
    """
    p = cand["params"]
    metrics = {
        "n_raw": 0, "unique_token": 0,
        "up_1h": None, "up_4h": None, "med_1h": None,
        "ic_mean": None, "ir": None, "win_rate": None, "pnl": None,
        "note": "",
    }
    # TODO(exec AI): 用与 jev_validity.py 一致的 SQL/统计函数填充上述指标；
    #                --with-backtest 时叠加 decision_engine/backtest.run 出 win_rate/pnl。
    return metrics


# ------------------------------------------------------------------ #
# 阶段 3：gate —— 门槛判定（防火墙，三段式结论）
# ------------------------------------------------------------------ #
def gate_candidate(metrics: dict[str, Any], *, week: str) -> dict[str, Any]:
    """硬指标判定：pass / need_retest / fail + 观察/评估/判定三段式。"""
    n = metrics.get("n_raw") or 0
    uni = metrics.get("unique_token") or 0
    up1h = metrics.get("up_1h")
    obs = f"n={n}, unique={uni}, up_1h={up1h}"
    eva: str
    if n < GATE_HARD_N or uni < GATE_UNIQUE:
        eva = f"样本不足（n<{GATE_HARD_N} 或 unique<{GATE_UNIQUE}）"
        return {"gate_result": "fail", "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:样本不足不入库", "status": "rejected"}
    if up1h is None:
        eva = "无 1h 数据"
        return {"gate_result": "fail", "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:数据缺失", "status": "rejected"}
    if up1h >= GATE_UP_1H:
        eva = f"过滤后 1h 上涨率 {up1h:.1%}≥{GATE_UP_1H:.0%}（首次达标；连续两周达标才激活）"
        return {"gate_result": "pass", "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:候选有效，待连续两周确认", "status": "accepted"}
    if up1h >= GATE_UP_1H_RETRY:
        eva = f"过滤后 1h 上涨率 {up1h:.1%} 在 {GATE_UP_1H_RETRY:.0%}~{GATE_UP_1H:.0%} 之间"
        return {"gate_result": "need_retest", "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:下周复验", "status": "pending"}
    eva = f"过滤后 1h 上涨率 {up1h:.1%}<{GATE_UP_1H_RETRY:.0%}"
    return {"gate_result": "fail", "gate_reason": f"观察:{obs}; 评估:{eva}; 判定:无预测力，记录教训", "status": "rejected"}


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
    conn = sqlite3.connect(str(FACTOR_DB))
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO factor_candidates
                (factor_id, week, hypothesis, reason, params_json, backtest_json,
                 gate_result, gate_reason, status, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cand["factor_id"], week, cand["hypothesis"], cand["reason"],
                json.dumps(cand["params"], default=str),
                json.dumps(metrics, default=str),
                decision["gate_result"], decision["gate_reason"], decision["status"],
                _utc_now_iso(), cand.get("source", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------------ #
# CLI 主流程
# ------------------------------------------------------------------ #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="JEV 因子进化循环（RD-Agent 式）")
    ap.add_argument("--week", required=True, help="ISO 周，如 2026-W42")
    ap.add_argument("--dry-run", action="store_true", help="只出候选与判定，不写库")
    ap.add_argument("--with-backtest", action="store_true", help="叠加回测框架出 win_rate/pnl")
    ap.add_argument("--llm", action="store_true", help="启用 LLM 提因子（需 JEV_LOOP_LLM_ENABLED=1）")
    args = ap.parse_args(argv)

    ctx = collect_week_context(args.week)
    cands = propose_candidates(ctx, llm=args.llm)
    if not cands:
        print(json.dumps({"week": args.week, "candidates": 0, "note": "无候选"}), default=str)
        return 0

    rows = []
    for c in cands:
        m = backtest_candidate(c, with_backtest=args.with_backtest)
        d = gate_candidate(m, week=args.week)
        rows.append({"factor_id": c["factor_id"], "hypothesis": c["hypothesis"],
                     "metrics": m, "gate": d})
        if not args.dry_run:
            store_candidate(c, m, d, week=args.week)

    out = {"week": args.week, "candidates": len(rows), "rows": rows,
           "dry_run": args.dry_run}
    print(json.dumps(out, default=str, indent=2))

    # 进化报告（dry-run 也写 exports/，供周报引用）
    rep = REPORTS / f"factor_loop_{args.week.replace('-', '')}.md"
    with rep.open("w") as fh:
        fh.write(f"# 因子进化周报 {args.week}\n\n")
        for r in rows:
            fh.write(f"## {r['factor_id']} — {r['gate']['gate_result']}\n\n")
            fh.write(f"- 假设: {r['hypothesis']}\n- 回测: {json.dumps(r['metrics'], ensure_ascii=False)}\n")
            fh.write(f"- 判定: {r['gate']['gate_reason']}\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 三、LLM 增强说明（可选，默认关）

`JEV_LOOP_LLM_ENABLED=1` 时，`_llm_propose(ctx, cands)` 走现有 Grok 链路（grok_report.py 同款 HTTP），prompt 为 RD-Agent 风格：

```
以下为本期验证试验（观察/评估/判定）与历史候选（含失败教训）：
[ctx.trials + ctx.prior_candidates 序列化]
请分析成功/失败原因，提出 1-3 个改进或新方向的候选因子，输出 JSON：
{"hypothesis": "...", "reason": "...", "params": {"strength": 0.x, "liq": int, "cov": int}}
约束：简单优先；不得与历史候选（SOTA 集）参数重复；失败 3 周方向需更换。
```

解析失败/无 key/超时 → 静默回落规则模板（`cand["source"]="rule_fallback"`），exit 0，不阻塞周检。

---

## 四、验收线

| # | 检查 | 期望 |
|---|------|------|
| V1 | `PRAGMA table_info(factor_candidates)` | 12 列 + UNIQUE(factor_id) |
| V2 | 无 LLM 全流程（fixture 或真库） | ≥3 候选、backtest 指标齐全；构造 pass（up_1h=0.60, n=20, unique=12）→`pass/accepted`、样本不足（n=10）→`fail/rejected`，判定正确 |
| V3 | 幂等 | 同 week 重跑 → 同 factor_id 更新不新增行；factor_loop 报告可重复生成 |
| V4 | LLM 可选 | 无 key 时 `--llm` 静默回落规则（exit 0，source=rule_fallback）；有 key 时 JSON 解析成功 |
| V5 | 隔离 | 只写 review/factor_candidates.db + exports/reports/；业务四库 mtime 不变；**不激活任何策略**（status 最高=accepted） |
| V6 | 周报联动 | factor_loop 报告生成成功；主 Agent 验收后更新周检定时任务 query（周一 09:15 加 `python -m review.factor_loop --week ...`） |

---

## 五、不做（纪律）

- **不自动激活/注册策略**：过门槛仅 status=accepted（待激活）；激活需人工确认或 `JEV_LOOP_AUTO_ACTIVATE=1` 显式开关
- **不自动调参**：候选参数即入库固化，不迭代调优
- 不做双臂老虎机完整实现（简化=连续 3 周同方向失败自动换方向）
- 不改 jev_validity.py / factor_ic_history / 现有周报 §1-§11（只新增 § 因子进化块引用）
- 不 git push / 不打印密钥

---
*转执行 AI：§二 2.1 建表（脚本内幂等）+ 2.2 整文件创建 → 填充 backtest_candidate 的 TODO（复用 jev_validity.py 查询口径）→ 可选接 _llm_propose → §四 V1-V6 打勾，回传数字（V2 判定样例、V3 幂等前后行数、V5 mtime 对比）。*
