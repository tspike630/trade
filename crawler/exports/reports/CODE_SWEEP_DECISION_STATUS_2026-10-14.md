# CODE SWEEP 决策层状态（2026-10-14）

## 状态

| 项 | 状态 |
|----|------|
| 核实笔记 | `CODE_SWEEP_DECISION_NOTES_2026-10-13.md` |
| 改码 PR #31 | ✅ **已合** `365097a`（squash）；本机已同步 |
| P2-38 `purpose` | ⏳ draft [#37](https://github.com/tspike630/trade/pull/37)；等杨验合 |
| 本机 dry-run | PID **666728**；`list_hot` 拒僵尸 → `hot_loaded=0`（等指标灌 `tech_updated_at`） |
| 闸门 / 默认策略 | 未动 · `jev_strength_v1` · `DECISION_COOLDOWN_SEC=0` · 权重 0/0/0 |

## PR #31 已含（双树）

- P1-01 boll_upper/lower + crawler `DB_PATH`/`DECISION_DB_PATH`
- P0-2 读侧：`last_seen_at` + `tech_updated_at`（缺列降级）
- P1-06 冷却默认 0
- P1-07 replay 同 bar 合并非空技术列
- P2-05/11/12/13 busy 30s、回测频率跟 live、sell pair、rsi 拒横盘
- `unittest decision_engine.tests.test_sweep_fixes` 双树 19 过

## PR #37（draft）

- `decision_log.purpose` 列 + `raw_json.purpose`
- 常驻/`--once` → `live_candidate`；周采样 / `--min-jev-strength` 低于 live 默认 → `sample`
- 历史行 NULL 不回填；`test_purpose` 双树 18 过
- 网关拒 sample 归执行联动（执行已开）

## 依赖 / 开闸

- 指标 #32 已合；tech 仍在灌 → 灌满后 `list_hot` 才有候选
- 开 paper 不归决策；硬阻塞齐 + 杨点头
