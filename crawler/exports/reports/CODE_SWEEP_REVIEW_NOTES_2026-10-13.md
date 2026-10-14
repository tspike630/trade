# CODE SWEEP 复盘核实笔记 · 2026-10-13/14 (HK)

对照 `CODE_SWEEP_REPORT_2026-10-13.txt` + 本机双树，再合 [#34](https://github.com/tspike630/trade/pull/34)（`2e86ed5`）。

## 成立并改

| ID | 核实 | 落地 |
|----|------|------|
| **P0-3** | 双树 `int(pr["id"])` + `Optional[int]` | `position_id: Optional[str]`，join 存 `str(id)`；夹具 UUID 冒烟 |
| **P1-09/10** | 门槛散落 factor_loop / jev | `review/gates.py` 收拢；40% 重试线对齐 50% degrade |
| **P2-24** | dry-run 仍可写正式路径/插库 | dry-run → `exports/reports/dry-run/`，不 insert |
| **P2-27** | TZ 字符串比 | `--start/--end` 归一 UTC `Z` |
| **P2-28** | 模板冻句 | 去掉 paper=0 / 0.51d / 冻 n |
| **附加** | PnL 口径 / validation_errors | 已实现 MTM 与 closed 分开；JSON 数组按首码分桶 |

## 边界不抢

P0-1 采集 · P0-2 指标 · P0-4/tick→exits 执行 · P1-15 看板 · 决策 Bollinger。看板 `bucket_key` 仍只 `;` 分割，复盘侧已解析 JSON。

## 冒烟（合后本机）

- UUID fixture + gates/pnl assert OK
- `python -m review.run --period 7d --report --db-root /tmp/review_smoke` exit 0
- `python -m review.factor_loop --week 2026-W41 --dry-run`：正式报告 sentinel 未变；候选行 0；`auto_activate=false`；最高 `accepted`

闸门 / paper / 实盘 / 自动激活：未动。
