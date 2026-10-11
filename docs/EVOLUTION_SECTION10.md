# 复盘周报 · 第 10 节「进化建议」

> 状态：轻量落地（2026-10-10）  
> 模块：`review/evolution.py` → `build_evolution_suggestions` / `format_evolution_section`  
> 接线：`review/run.py`（`--report`）· 渲染：`review/report.py` §10

## 边界（钉死）

本节**只建议**，从现有只读数据自动填「观察中」项；**不**自动改参、不写 config、不开 paper/live 闸。任何落地须走 **R4 人工确认**。

## 结构

每条建议为字典：`layer`（采集/指标/决策/执行/复盘）、`item`、`status`（观察中|建议落地|裁决）、`evidence`、`action_hint`。

自动观察项（有则填，无则占位）：采集 token_age 覆盖率（接 `pairs.pair_created_at`，观察 unknown 占比下降）与 `news_events.db` 条数；指标权重全 0 + 归因 liq 区分度；决策 JEV unique_token/门槛距离与退役规则提醒；执行 executed=0/paper 未开闸与拒绝分布；复盘 R3 skip 原因。

```bash
.venv/bin/python -m review.run --period 7d --report
# 可选：--optimize 以便 §10 引用真实 R3 skip reason
```
