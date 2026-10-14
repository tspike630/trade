# CODE SWEEP 复盘结案 · 2026-10-14 (HK)

| 项 | 状态 |
|----|------|
| PR | [#34](https://github.com/tspike630/trade/pull/34) **已合** `main` `2e86ed5` |
| 本机 | `review/` 已同步；fixture + `review.run --period 7d` + `factor_loop --dry-run` 过 |
| 闸门 / paper / 实盘 / 自动激活 | **未动**（过闸最高仍 `accepted`） |

## 已落地

- **P0-3** `position_id→str`（UUID 不再 `int()`）
- **P1-09/10** `review/gates.py` 门槛收拢；40% 线对齐 50% degrade
- **P2-24/27/28** dry-run 隔离 / TZ→UTC Z / 去冻句
- PnL：已实现 vs MTM 分列；`validation_errors` JSON 首码分桶

## 硬阻塞（杨清单）— 复盘不认领改码

| ID | 归谁 | 复盘侧 |
|----|------|--------|
| P2-38 purpose 采样/常驻 | 决策写 + 执行网关拒 | 字段进库后周报/归因只读区分；**不改参、不开闸** |
| P2-12 sell `pair_address` | 决策 #31 | 合入后归因可读，不抢 |
| P2-06/07 风控默认关 | 执行 | 只观测；开闸前周报继续标 OFF |
| P2-04 live_trade 加固 | 执行 | 不碰 |

未修完硬阻塞前：复盘继续只读聚合，**不建议、不触发**开实盘。
