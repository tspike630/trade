# CODE SWEEP 决策层认领笔记（2026-10-13，已核实）

杨指示：对照报告核实后再改，尽量改得更好。对照对象：本机 `/workspace/dexscreener-crawler/decision_engine` + GitHub `main` 双树。

## 核实结论

| ID | 报告说法 | 核实 | 更好改法 |
|----|----------|------|----------|
| **P1-01 boll** | 根树仍 boll_up/low，crawler 已 upper/lower | **成立（main tip）**；本机根树已改过尚未全同步 tip | 双树统一 `boll_upper/mid/lower`（adapter/replay/boll_breakout）；启动 log 打印选用列 |
| **P1-01 DB** | 根认 DB_PATH+DECISION；crawler 只认 DECISION | **部分成立**：tip 根树已双认；crawler 仍只 `DECISION_DB_PATH` | crawler config 对齐根树；`run`/`--once` 启动打印 `resolved DB_PATH` 绝对路径 |
| **P0-2 读侧** | list_hot 只按 tier=hot，吃僵尸 | **成立**（本机+tip 均无新鲜度过滤） | 不只信 tier：优先 `tokens.last_seen_at`；若有 `tech_updated_at` 则双条件；缺列 PRAGMA 降级；log 跳过僵尸数。指标修写入（他们主责） |
| **P1-06** | risk 直通、无冷却 | **成立** | `RiskManager` 加 `(strategy,chain,token)` 冷却；`DECISION_COOLDOWN_SEC` **默认 0**（观察期行为不变），文档写 Step B 建议= tick 间隔 |
| **P1-07** | replay 同 bar 留最后一行→残缺；前视价 | **成立**（keep latest per token） | 同 bar 同 token：**合并非空技术列**（或选非空字段最多的行），勿盲信最后一行；入场价用 bar 时刻 nearest ≤tol，文档标已知简化 |
| **P2-05** | signal_bus busy=5000 | **成立** | `PRAGMA busy_timeout=30000` 与 connect timeout 对齐；publish 失败 warn+短重试 1 次 |
| **P2-11** | 回测默认 0.15 ≠ 实时 0.3 | **成立** | CLI 默认改为与 `JEV_MIN_STRENGTH` 同源；周检采样脚本显式传 0.15；正式对比文档写清 |
| **P2-12** | FactorStrategy.sell 不传 pair_address | **成立**（buy 有、sell 无） | `sell(..., pair_address=pos.get("pair_address") or pos.get("primary_pair_address"))` |
| **P2-13 rsi** | min_change_5m=0 且 `<` → 横盘通过 | **成立** | 改为 `c5 is None or c5 <= 0` 拒绝非正；或默认 `min_change_5m` 小正数。MACD 整组归指标 |

## 不认领

P0-1 采集 · P0-3 复盘 · P0-4 执行 · P1-02/03 指标已开 · P1-04/05/卖撮合 执行 · P1-08 指标 · P1-09/10 复盘 · 看板项

## 纪律

- 默认策略仍 jev_strength_v1；不开闸、不调权
- 双树一起改；开 PR；合入再同步本机 dry-run
- 不碰 .env / Key / *.db
