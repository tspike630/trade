# CODE SWEEP 执行层核实笔记（2026-10-13）

对照 `CODE_SWEEP_REPORT_2026-10-13.txt` + 本机 `/workspace/dexscreener-crawler`。  
**只核实、不改产品代码。** 行号以本机文件为准（与报告 2c6cd94 有轻微漂移）。

## 现网快照（核实时刻 HKT 2026-10-13 ~23:52）

| 项 | 结果 |
|----|------|
| 执行 loop | **存活** PID `286377`：`.venv/bin/python -m execution.run --loop`（`logs/execution_loop.pid` 为陈旧值 87614） |
| paper / pm | 日志持续 `paper=False pm=False`；cursor=134 |
| `execution_ledger.db` | 有 `order_events` 表；`execution_log` 134 行；`order_events` **0 行**（事件功能后加 / 无新决策写事件） |
| `execution_log.decision_log_id` | 仅非 UNIQUE 索引 `idx_exec_log_decision`；**无 UNIQUE**；现网无重复 decision_log_id |
| Python | 本机 3.13.5（`fromisoformat('…Z')` 可用）；报告 P2-09 针对 ≤3.10 |

---

## 核实结论表

| ID | 成立? | 证据 | 加强改法 |
|----|-------|------|----------|
| **P0-4** | **成立** | `store.py:1272` / `paper_executor.py:85`：`ORDER BY recorded_at ASC LIMIT ?`；`paper_trade.py:235-239` `limit=500` 再 `reversed(hist)` 取最早 500 里最后一行 → 历史>500 价永久冻在第 500 点。`purge_old_pair_history` 无调用方。 | 新增 `latest_pair_price`：`ORDER BY recorded_at DESC LIMIT 1`（同索引）；与 `pairs.priceUsd/updated_at` 取较新；超 `max_age_sec` → `None`/`no_price`；retention 挂主循环。**开 paper / 跑 tick 前必改。** |
| **P1-04** | **成立** | `gateway.py:479` paper.execute → `:513-531` append_receipt → `:532` set_last_processed_id → `:533-545` `_emit_order_events`（`order_events.py:62` 每事件新连提交）。`ledger.py:90-91` 非 UNIQUE；`:135-147` 游标直接覆盖非 MAX。`run.py:61-64` `--since-id` 写明仍推进游标；`:228-232` `while True` **无 try/except**；`process_pending:590-591` 单条异常会打爆整批。 | ① `UNIQUE(decision_log_id)` + 先插 `claimed` 抢占；② 回执/游标/事件同 ledger 事务，`record_event` 复用 conn；③ 游标 `MAX(old,new)`；`--since-id` 仅本次、不持久化；④ paper `_buy` 按 `meta.decision_log_id` 拒重；⑤ loop/process_one 包 try+退避，坏消息记 rejected 后前进。 |
| **P1-05** | **成立** | `PaperLedger`：`sqlite3.connect` 默认 timeout→busy 5s；`PRAGMA journal_mode` 实测 `delete`（非 WAL）。`set_cash:115-120` 自 commit；`open_position:145` 先 `set_cash` 再 INSERT；`close_position:188-194` 先加现金再 UPDATE，且 `WHERE id=?` **无** `AND status='open'`。多写方：execution loop / paper_tick_loop / auto_hooks / CLI 可同指 `paper_trading.db`。 | `BEGIN IMMEDIATE` 包开/平；现金 `cash_usd = cash_usd ± ?`；平仓 `UPDATE … WHERE id=? AND status='open'` 且 `rowcount==1`；开 WAL + busy≥30s；**只保留一个 tick 源**。 |
| **P1-06** | **成立** | `_find_open:312-338` 只按 chain+token / chain+pair，**不看 strategy_id**；`list_open` `ORDER BY opened_at` → 最早一笔；`_sell:370-374` 整仓平，忽略 `size_pct`。`position_manager.py:381-382` `PM_ENABLED=0` 全放行。 | `_find_open` 匹配 `meta.strategy_id`；`size_pct<1` 拒或部分平；Step B 建议开 `PM_TOKEN_DEDUP` 或决策冷却（决策层认领冷却）。 |
| **P2-06** | **成立** | `_pretrade_risk:285-296` 无 side 过滤；对 SELL 也跑 `run_all`；`check_max_open` 达上限会挡平仓信号。调用只传 `max_open_per_strategy=`，**从不传** `daily_loss_limit_usd` → `limits.py:80-81` 恒放行。`config.py:12` 只认 `"1"`。 | 风控默认仍关；开启前：SELL 跳过 max_open（或只跑 daily_loss）；gateway 传入日亏限额；开关解析对齐 `_env_bool`。 |
| **P2-07** | **成立** | `_pct_to_fraction:60-64`：`v>1 → /100`，`v==1` 当成 **100%**（本意 1% 则炸）。`check_open:385-390` ledger 缺失 → allow（fail-open）。 | 只接受百分点；启动打印解析值；无账本 **fail-closed**。 |
| **P2-09** | **成立**（路径敏感） | `exits.py:24` `fromisoformat`；`except ValueError → None`。paper `opened_at` 为 `%Y-%m-%dT%H:%M:%SZ`（`paper_trade.py:37`）。≤3.10 时 Z 解析失败 → max_hold 永不触发。**本机 tick 未走 exits**：`paper_trade.tick` 用自有 `_parse`/strptime（能吃 Z）；`paper_executor.tick` 委托该 tick。回测 `scan_exits` 路径受影响。OBS-5：生产若 <3.11 升 P1。 | `_parse_ts` 先 `Z→+00:00`（对齐 replay）；顺带让 `paper_trade.tick` 改调 `scan_exits`（报告称已调，**本机未调**）。 |
| **P2-10** | **成立** | `equity:223` 无 marks 时用 `entry_price`；`_buy:239` `equity()` 无 marks → 仓位/PM 忽略浮盈亏。`tick:274` 平仓价=mid **无滑点**；`_sell:369` `mid*(1-slip)`。TP/SL 基于含滑点入场价。 | 统一滑点模型：tick 平仓与信号卖同口径；equity 估值用 mark；文档写清 TP 相对 mid 还是 fill。 |
| **P2-08** | **成立**（绑 P1-04） | 网关 emit 链齐全（`gateway.py:298-399`：new→validated→submitted/filled/rejected）。缺口：① 事件在游标之后、独立连接（P1-04）；② `cancelled` 从未写；③ docstring `accepted` 不在 `EVENTS`；④ tick 平仓不经网关 → 无 execution_log/事件；⑤ 现网 `order_events` 空（134 回执无补写）。 | 与 P1-04 同事务；平仓腿也走网关写回执+事件；补 cancelled 或删文档。 |

### 边界（笔记，不认领主责）

| ID | 结论 | 证据 / 备注 |
|----|------|-------------|
| **P0-3** | **不归执行**（复盘主责）；执行侧写入属实 | `paper_trade.py:74` `id TEXT PK`；`:143` `uuid4`；`paper_executor._meta:213-216` 把 `decision_log_id` 写入 `meta_json`。`review/data.py:241` 仍 `int(pr["id"])`。 |
| **P1-15** | **不归执行**（看板已认领） | 闸灯读看板进程 env，非网关真相。 |
| **P0-1 / P0-2** | **不归执行** | 采集 / 指标主责。 |
| **双树 exits** | **部分 / 报告有误** | 回测 `engine.py` 已 `scan_exits` ✓。**本机 `paper_trade.tick` 仍内联 TP/SL，未调 `scan_exits`**（报告「已调」不成立）。遗留 `backtest.py:108+` `backtest_tpsl` 自有退出，被 `--strategy tpsl` / `simulate` 使用。 |

---

## 本轮认领清单（开改优先级）

> 闸门保持默认关；**开 Step B（paper）前必须先合入 P0-4 + P1-04/05/06。**

1. **P0-4** 最新价 DESC LIMIT 1 + 过期拒单（阻塞 paper/tick）
2. **P1-04** UNIQUE 抢占 + 回执/游标/事件单事务 + loop 异常保护 + since-id 不回拨持久游标
3. **P1-05** PaperLedger 原子开平 + WAL + 单 tick 源
4. **P1-06** 卖出按 strategy_id 撮合；拒绝/明确部分卖
5. **P2-08** 随 P1-04；平仓腿事件（可同 PR 或紧随）
6. **P2-10** 滑点/估值口径统一（开 paper 前建议）
7. **P2-09** exits Z 解析（本机 3.13 不急；生产≤3.10 升优）+ 可选 tick→scan_exits
8. **P2-06 / P2-07** 开 `EXECUTION_RISK` / PM 数值规则前必改（默认关，暂不挡 Step B）

---

## 明确不碰

- 闸门默认：`EXECUTION_PAPER_ENABLED` / `PM_ENABLED` / `EXECUTION_RISK_ENABLED` 保持关
- LIVE / 实盘适配器 / kill-switch 路径
- 指标权重、决策策略默认、采集 Helius key
- P0-3 复盘 UUID、`P1-15` 看板灯、P0-1/P0-2 他层主责
- 不改 `.env`、不写业务库、本轮不改产品代码

