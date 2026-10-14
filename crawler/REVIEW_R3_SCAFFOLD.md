# 复盘优化层 · R3 参数寻优脚手架

> 状态：脚手架落地（2026-10-10）  
> 范围：网格枚举 + 时间切分护栏 + 稳健性评分接口；**不跑正式寻优、不改参、不写配置**  
> 正式寻优门槛：历史 ≥7d 且周检跨门槛。历史跨度以当次回测窗口为准，不要把某次短窗快照写成当前事实。

---

## 1. 模块

| 文件 | 作用 |
|------|------|
| `review/optimizer.py` | R3 核心：`build_param_grid` / `time_split` / `score_robustness` / `optimize` |
| `review/run.py` | CLI `--optimize`：只跑脚手架；不足则 `skipped` 并打印理由 |
| `review/report.py` | 第 9 节占位：「R3 参数寻优 — 待历史≥7天且周检跨门槛」 |
| `review/__init__.py` | 导出公开 API |

### 公开 API

```python
from review.optimizer import (
    build_param_grid,   # strategies A/B/C × threshold × tp × sl × max_hold
    time_split,         # 前 70% 寻优 / 后 30% 样本外（硬护栏）
    score_robustness,   # 相邻参数点突变 → high risk / RED
    optimize,           # OptimizeResult；不足则 status=skipped
    OptimizeResult,
    format_optimize_summary,
)
```

`optimize(..., dry_run=True, run_backtest_fn=None)`：

- 历史 `<7d` 或样本不足或周检未跨门槛 → **`status=skipped` + reason**，不调用重计算
- `dry_run=True`（默认）即使过门槛也不跑回测网格
- `run_backtest_fn` 为可选回调 `(param_dict, rows) -> metrics`；默认不调用（可日后接 `decision_engine.backtest`）
- 输出：`in_sample` / `out_of_sample` 分开的 `list[dict]`；**不自动写配置**
- 样本内最优 ≠ 样本外最优 → `in_sample_ne_out_of_sample=True` + `mismatch_note`

---

## 2. 护栏

1. **时间切分硬护栏**：前 70% 寻优 → 后 30% 样本外；两者都报
2. **稳健性**：相邻网格点核心指标相对跳变 ≥25% 标高风险（过拟合信号）
3. **门槛闸**：历史 ≥7d **且** 周检跨门槛（见 `DECISION_ENGINE_WEEKLY_CHECK.md`：连续 2 周过滤后 1h≥55% 且 unique_token≥10 等）后再正式寻优
4. **只读 / 不改参**：脚手架与 `--optimize` **绝不**改策略参数或闸门；建议表仅结构输出

占位小网格（可配置）：

- strategies：`jev_strength_v1` / `tech_momentum_v1` / `combined_jev_tech_v1`
- threshold：`0.15 / 0.20 / 0.30`
- tp：`8 / 10 / 12`；sl：`4 / 5 / 6`；max_hold：`30 / 60 / 120`（分钟）

---

## 3. 与 R4 的关系

| 阶段 | 做什么 | 不做什么 |
|------|--------|----------|
| **R3** | 枚举 + 切分 + 稳健性评分 + 参数建议表 | 不写配置、不改线上/dry-run 参数 |
| **R4** | 人工确认建议表后，才允许改参/写配置 | 无人工确认不得自动落地 |

CLI：

```bash
python -m review.run --period 7d --report          # 仍 exit 0；第 9 节占位
python -m review.run --period 7d --optimize        # 不足则 skip + 理由原文
```

---

## 4. 验收要点

- `from review.optimizer import build_param_grid, optimize` 可 import
- `--report` exit 0；`--optimize` 在现况下 `status=skipped` 并说明样本/历史不足

脚手架产出仍是原型证据。Alpha Illusion（arXiv:2605.16895）对 R1 / R2 / R3 与 paper 观察期的支撑见 `docs/REVIEW_RESEARCH_MAPPING.md`。M-1 功效见周检「统计功效标注」（[#54](https://github.com/tspike630/trade/pull/54)），不在本文重述。**不算有效性。**
