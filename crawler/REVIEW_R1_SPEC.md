# 复盘优化层 · R1 落地说明

> 状态：接口定稿（基于四库真实 schema，2026-10-10 核对）  
> 范围：数据聚合 + 信号层绩效周报（只读，零侵入）  
> 输出：本说明 → 后续填充 `review/` 代码

---

## 0. 目标与边界

**R1 要做什么**

- 串起「信号 → 校验/执行留痕 →（可选）成交」只读链路视图
- 出覆盖 A/B/C 全策略的信号绩效周报（合并而非重复 `jev_validity.py`）
- 把执行层状态（校验通过率、拒绝分布、dry_run 占比）纳入同一份报告

**R1 明确不做**

- 不写任何业务库、不改策略参数
- 不做完整归因（R2）、不做参数寻优（R3）、不自动改参（R4）
- 不假设 paper 已有成交；有成交时 `positions.id` 为 UUID 字符串，不得按 int 解析

**当前数据实况（核对日）**

| 源 | 量 | 含义 |
|---|---|---|
| `decision_log` | 40 | 全 `jev_strength_v1` × buy，`mode=dry_run` |
| `execution_log` | 40 | `decision_log_id` 齐全；`validation_ok=1`，`executed=0`，`executor=step_a_noop` |
| paper `positions` / `fills` | 0 | 开闸前，链路末端空 |
| `indicator_history` | ~86k | 仅 hot，约 5min 粒度 |

---

## 1. 四库路径与只读约定

全部相对仓库根 `/workspace/dexscreener-crawler/`：

| 逻辑名 | 路径 | 负责人 |
|---|---|---|
| 主库 | `dexscreener.db` | 蜘蛛爬虫 / 指标加工层 / 决策写入 |
| 执行账本 | `execution_ledger.db` | 执行管理层 |
| 模拟盘 | `paper_trading.db` | 执行管理层 |
| 回测账本 | `backtest_ledger.db` | 决策引擎层 |

**只读连接（强制）**

```python
def connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
```

可选：先 `cp` 快照到 `/tmp/` 再查，避免与写入抢锁。

---

## 2. 真实表结构摘要（定接口用）

### 2.1 `decision_log`（主库）

| 列 | 类型 | 备注 |
|---|---|---|
| `id` | INTEGER PK | 串联主键 |
| `ts` | TEXT NOT NULL | ISO UTC，如 `2026-10-09T23:50:04Z`（**不是** created_at） |
| `strategy_id` | TEXT | 见下表 |
| `action` | TEXT | buy / … |
| `chain_id` | TEXT | |
| `token_address` | TEXT | |
| `size_pct` | REAL | |
| `reason` | TEXT | |
| `risk_passed` | INTEGER | |
| `mode` | TEXT | 现况全 `dry_run` |
| `raw_json` | TEXT | `pair_address`、`extras.jev_strength` 等在此 |

**策略标识（无 A/B/C 字母列）**

| 简称 | `strategy_id` |
|---|---|
| A | `jev_strength_v1` |
| B | `tech_momentum_v1` |
| C | `combined_jev_tech_v1` |

### 2.2 `execution_log`（execution_ledger.db）

| 列 | 备注 |
|---|---|
| `id` | PK |
| `ts` | |
| `decision_log_id` | → `decision_log.id`（现 40 条无 NULL） |
| `strategy_id`, `action`, `chain_id`, `token_address`, `pair_address`, `size_pct`, `reason`, `mode` | |
| `validation_ok` | INTEGER |
| `validation_errors` | 失败信息在此（**无独立 error 列**） |
| `executed` | INTEGER（现全 0） |
| `executor` | 现 `step_a_noop` |
| `receipt_json` | pm_rejected 等细节 |

### 2.3 paper（paper_trading.db）

- `account(id, cash_usd, updated_at)` — 现 1 行，`cash_usd=10000`
- `positions.id` 为 **TEXT**（UUID 字符串）。`SignalChainRow.position_id` 为 `Optional[str]`；`meta_json.decision_log_id` 仅在值为整数或整数字符串时解析，坏 meta 跳过。不要 `int(positions.id)`。
- `positions(..., meta_json, pnl_usd, ...)` — 核对日 **0 行**；代码约定开仓时 `meta_json` 含 `strategy_id` + `decision_log_id`
- `fills(...)` — 核对日 **0 行**

### 2.4 市场基准（主库）

- `indicator_history`：`(chain_id, token_address, recorded_at)` + `change_*` / `jev_strength` / `tier` …
- `pair_history`：价格时序 `price_usd` 等
- 关联：token 级 `(chain_id, token_address)`；pair 级 `primary_pair_address` + `chain_id`

### 2.5 回测（backtest_ledger.db）— R1 对照用，非主链路

- **与 `decision_log` 无 id 外键**；仅能靠 `strategy_id` 语义对齐
- 表：`runs` / `positions` / `backtest_log` / `equity_curve` / `account`
- 复用：`decision_engine.backtest.metrics.compute_metrics(closed, equity_curve, *, start_cash) -> dict`

---

## 3. 文件结构

```text
review/
  __init__.py
  data.py         # 跨库只读聚合：信号链路 + 市场基准 + 前向收益
  metrics.py      # 信号层绩效（复用/扩展 compute_metrics 思路）
  report.py       # md 周报 → exports/reports/
  run.py          # CLI：--period / --strategy / --report
  # R2+ 预留（本阶段不实现）
  # attribution.py
  # optimizer.py

exports/reports/
  review_weekly_YYYY-MM-DD.md
  review_signal_chain_YYYY-MM-DD.json   # 可选：链路视图落盘，供 R2 复用
```

独立顶层模块；只读所有输入；报告只写 `exports/reports/`。

---

## 4. 数据流

```text
                    ┌─ dexscreener.db ─────────────────────────┐
                    │  decision_log (id, ts, strategy_id, …)   │
                    │  indicator_history / pair_history        │
                    └──────────────┬───────────────────────────┘
                                   │ LEFT JOIN on id
                    ┌──────────────▼───────────────────────────┐
                    │  execution_ledger.db                     │
                    │  execution_log.decision_log_id           │
                    └──────────────┬───────────────────────────┘
                                   │ LEFT JOIN meta_json.decision_log_id
                    ┌──────────────▼───────────────────────────┐
                    │  paper_trading.db                        │
                    │  positions / fills（现为空 → 字段留空）   │
                    └──────────────┬───────────────────────────┘
                                   ▼
                         SignalChainRow[]  （data.py）
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
        前向收益标注          执行层汇总           按策略分桶指标
   (1h/4h vs pair_history)  (校验/拒绝/dry_run)   (metrics.py)
              └────────────────────┬────────────────────┘
                                   ▼
                         report.py → md (+ 可选 json)
```

**串联键（定死）**

1. `decision_log.id` → `execution_log.decision_log_id`（主链路，已有样例）
2. `execution_log.decision_log_id` → `positions.meta_json.decision_log_id`（开闸后启用；R1 必须容忍缺失）
3. 回测对照：`strategy_id` 字符串对齐（**不用** decision id）

**前向收益（信号层，0 成交也能做）**

对每条 `decision_log`：

1. 从 `raw_json.pair_address`（或 `execution_log.pair_address`）取 pair
2. 在 `pair_history` 找 `ts` 附近价格 `p0`，以及 `ts+1h` / `ts+4h` 的 `p1` / `p4`
3. `fwd_1h = (p1-p0)/p0`，`fwd_4h` 同理；缺价则 NULL（计入 coverage）

**市场基准（同期 hot）**

```sql
SELECT avg(change_1h), avg(change_4h)
FROM indicator_history
WHERE tier='hot' AND recorded_at BETWEEN ? AND ?;
```

复盘展示优先中位数 / 去极值（截面波动大）。

---

## 5. 接口签名

### 5.1 `review/data.py`

```python
from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class DbPaths:
    main: str = "dexscreener.db"
    execution: str = "execution_ledger.db"
    paper: str = "paper_trading.db"
    backtest: str = "backtest_ledger.db"  # R1 可选对照

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
    raw: dict[str, Any]          # parsed raw_json
    # 信号特征（从 raw / reason 解析，缺则 None）
    jev_strength: Optional[float]
    # 执行
    exec_id: Optional[int]
    exec_ts: Optional[str]
    validation_ok: Optional[int]
    validation_errors: Optional[str]
    executed: Optional[int]
    executor: Optional[str]
    receipt: dict[str, Any]      # parsed receipt_json
    # 成交（无匹配时全 None；paper id 为 UUID 字符串）
    position_id: Optional[str]
    fill_side: Optional[str]
    fill_price: Optional[float]
    pnl_usd: Optional[float]
    # 前向（信号层）
    fwd_1h: Optional[float]
    fwd_4h: Optional[float]
    price_at_signal: Optional[float]

def load_signal_chain(
    paths: DbPaths,
    *,
    period_start: Optional[str] = None,  # ISO UTC inclusive
    period_end: Optional[str] = None,    # ISO UTC exclusive
    strategy_id: Optional[str] = None,   # None = 全策略
) -> list[SignalChainRow]:
    """只读：decision LEFT JOIN execution LEFT JOIN paper(meta_json)。
    paper 无匹配时成交字段为 None，不报错。
    """

def annotate_forward_returns(
    rows: list[SignalChainRow],
    paths: DbPaths,
    *,
    tol_sec: int = 1800,
) -> list[SignalChainRow]:
    """用 pair_history 标注 fwd_1h / fwd_4h；就近匹配容差默认 30min（对齐 jev_validity）。"""

def load_market_baseline(
    paths: DbPaths,
    *,
    period_start: str,
    period_end: str,
) -> dict[str, Any]:
    """同期 hot 截面：avg/median change_1h、change_4h、样本量。"""

def summarize_execution(rows: list[SignalChainRow]) -> dict[str, Any]:
    """校验通过率、executed 率、executor 分布、validation_errors 分桶。"""
```

### 5.2 `review/metrics.py`

```python
def compute_signal_metrics(rows: list[SignalChainRow]) -> dict[str, Any]:
    """按全样本 + 按 strategy_id 分桶。
    键（R1 最小集）：
      n, unique_token,
      up_1h_rate, up_4h_rate, median_1h, median_4h,
      n_1h, n_4h,          # 有前向价的样本量
      validation_ok_rate, executed_rate,
      dry_run_rate,
      by_strategy: { strategy_id: {…同上…} }
    有成交后再扩展：win_rate / payoff / max_dd（复用 compute_metrics）。
    """

# 与周检对齐的可选切片（合并 jev_validity，而非另起炉灶）
def slice_jev_filtered(
    rows: list[SignalChainRow],
    *,
    min_strength: float = 0.15,
    strategy_id: str = "jev_strength_v1",
) -> list[SignalChainRow]:
    """从链路视图筛出与 scripts/jev_validity.py 同口径的子集。"""
```

### 5.3 `review/report.py`

```python
def render_weekly_md(
    *,
    period_start: str,
    period_end: str,
    chain: list[SignalChainRow],
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    exec_summary: dict[str, Any],
    jev_slice_metrics: Optional[dict[str, Any]] = None,
) -> str:
    """返回 markdown 字符串。章节固定：
    1. 期间与数据覆盖
    2. 全策略信号绩效（1h/4h）
    3. 分策略对比
    4. JEV 过滤切片（对齐周检）
    5. 执行层状态
    6. 市场基准
    7. 缺口与下一步（R2 等待开闸）
    """

def write_report(md: str, out_path: str) -> str:
    """写入 exports/reports/…，返回绝对路径。"""
```

### 5.4 `review/run.py`（CLI）

```text
python -m review.run \
  --period 7d | --start ISO --end ISO \
  [--strategy jev_strength_v1] \
  [--db-root .] \
  [--tol-sec 1800] \
  [--report] \
  [--dump-chain PATH]   # 可选 json
```

默认报告路径：`exports/reports/review_weekly_<end-date>.md`

退出码：0 正常；2 任一库打不开（只读失败也要显式报）。

---

## 6. 与周检 `jev_validity.py` 的关系

| | 周检 | R1 周报 |
|---|---|---|
| 范围 | 仅 `jev_strength_v1` | A/B/C 全策略 + 执行层 |
| 入口 | `scripts/jev_validity.py` | `python -m review.run` |
| 前向口径 | pair 价、tol 1800s | **同口径**（`annotate_forward_returns`） |
| 输出 | `DECISION_ENGINE_JEV_VALIDITY_FILTERED.md` + trend CSV | `exports/reports/review_weekly_*.md` |

R1 **不删除**周检脚本；周报内嵌「JEV 过滤切片」一节，数值应与同参数周检可对账。稳定后可把周检 routine 改为调 `review.run --strategy jev_strength_v1`（另议，非 R1 必做）。

---

## 7. 实现顺序（写代码时）

1. `data.load_signal_chain` — 先 JOIN decision↔execution，断言 40↔40
2. `annotate_forward_returns` — 对账 `jev_validity` 同窗 1h/4h 率
3. `summarize_execution` + `compute_signal_metrics`
4. `render_weekly_md` + CLI
5. （可选）`--dump-chain` json，作为 R2 输入契约冻结

---

## 8. 开放问题（写码前确认 / 写码时兜底）

| # | 问题 | 默认处理 |
|---|---|---|
| 1 | `raw_json` 内 `jev_strength` 路径是 `extras.jev_strength` 还是顶层？ | 两处都试；再 fallback 解析 `reason` 文本 |
| 2 | paper `meta_json` 字段名开闸后是否严格 `decision_log_id`？ | R1 LEFT JOIN 容忍无键；开闸后与执行层再锁死 |
| 3 | 多条 execution 对应同一 decision？ | 取最新 `exec_ts`；报告里记 `exec_dup_count` |
| 4 | backtest 是否进 R1 主报告？ | 默认否；`--with-backtest` 再附一节对照 |

---

## 9. 验收标准（R1 done）

- [ ] 只读打开四路径不报错；写路径仅 `exports/reports/`
- [ ] 对当前库：`load_signal_chain` 得到 40 行，且 40 行都有 `exec_id`
- [ ] `executed` 全 0、`pnl_usd` 全 None 时报告仍完整生成
- [ ] JEV 切片 1h/4h 率与同参 `jev_validity.py` 偏差可解释（容差/过滤对齐）
- [ ] md 含：全策略表、分策略表、执行汇总、市场基准、缺口说明

---

## 10. 警示事例（指针）

平仓三行、**P-2 / P-3** 与 2026-10-19 交付见 `docs/REVIEW_WARNING_CASES.md`（`crawler/` 同文）。周报第 8 节的 liq 桶是信号层前向收益，不是已平仓 PnL。警示行标 R2 规则缺口旁证时**不算有效性**，不据此改 strength 或权重。

M-1 功效不在本文重述，见周检「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)）。Alpha Illusion（arXiv:2605.16895）对 R1 / R2 / R3 与 paper 观察期的支撑，以及 `factor_loop` 只收指标已方向校准且已算子化的候选，见 `docs/REVIEW_RESEARCH_MAPPING.md`（`crawler/` 同文）。**不算有效性。**

---

*核对来源：蜘蛛爬虫 / 执行管理层 / 决策引擎层 / 指标加工层 · 2026-10-10*
