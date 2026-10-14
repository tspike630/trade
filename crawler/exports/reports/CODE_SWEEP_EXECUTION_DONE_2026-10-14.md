# CODE SWEEP 执行层结案（2026-10-14）

## 状态
- PR [#35](https://github.com/tspike630/trade/pull/35) 已合 `main`（merge commit **`b164aa2`**，2026-10-14 00:43 HKT；标题：Make paper execution idempotent before the paper gate opens）
- 本机 `/workspace/dexscreener-crawler/`（独立树，非 git clone）已从 `tspike630/trade@b164aa2` 同步 execution 相关路径
- `execution.run --loop` 已重启：**PID `660910`**（旧 PID `286377` 已停）
- 闸门仍关：`EXECUTION_PAPER_ENABLED` / `PM_ENABLED` / `EXECUTION_RISK_ENABLED` 默认 **False**（`.env` 未改、未写闸门键；进程 env 亦未设置）
- **本次无需再推 trade**（#35 已合）

## 本机同步摘要

本机布局扁平：`paper_trade.py` / `store.py` 在仓库根（对应上游 `crawler/`）；无 `crawler/execution` 软链，只维护根 `execution/`。

| 上游路径 (b164aa2) | 本机路径 | 校验 |
| --- | --- | --- |
| `execution/` 整树（含新增 `pricing.py`、`tests/`） | `execution/` | sha1 与上游一致 |
| `crawler/paper_trade.py` | `paper_trade.py` | sha1 一致 |
| `crawler/store.py` | `store.py` | sha1 一致 |

同步文件清单：
- `execution/README.md` `config.py` `__init__.py` `exits.py` `gateway.py` `ledger.py` `order_events.py` `position_manager.py` **`pricing.py`（新）** `run.py`
- `execution/executors/paper_executor.py` `__init__.py`
- `execution/risk/limits.py` `__init__.py`
- `execution/tests/__init__.py` **`test_code_sweep.py`（新）**
- `paper_trade.py` `store.py`

**未碰**：`.env`、密钥、业务库内容（`*.db` 未覆盖/未改 schema 以外业务数据）；未打开任何闸门。

### HEAD / 文件含 P0-4 / P1-04
- GitHub `main` tip = **`b164aa2`**（#35）
- **P0-4**：`store.fetch_latest_pair_price` / `Store.latest_pair_price` → `ORDER BY recorded_at DESC LIMIT 1`；`paper_trade.latest_price` 与 `pairs.priceUsd` 取较新，超 `PAPER_PRICE_MAX_AGE_SEC` → `None`
- **P1-04**：`ledger.claim_decision` + `ux_exec_log_decision` UNIQUE；回执/游标/事件 `BEGIN IMMEDIATE` 同事务；游标只 `MAX`；loop 坏行记 `rejected` 后前进

同 PR 已落地：P1-05（PaperLedger 原子现金）、P1-06（strategy 撮合 / 拒部分卖）、P2-08/09/10、以及风险/PM 解析加固（默认仍关）。

## 测过结果

```
.venv/bin/python -m unittest execution.tests.test_code_sweep -v
→ Ran 19 tests in 0.184s  OK
```

覆盖：DESC 最新价、过期价、claim 幂等/崩溃恢复、finalize 回滚、`--since-id` 不回拨、WAL 二次平仓、strategy/partial sell、Z 时间戳 max_hold、tick→scan_exits+卖滑点、百分点解析、PM 无账本 fail-closed、SELL 跳过 max_open、闸门默认关。

```
.venv/bin/python -m execution.run --once
```

回执（闸门关 dry-run）：

```json
{
  "processed": 0,
  "validation_ok": 0,
  "validation_fail": 0,
  "pm_rejected": 0,
  "executed_ok": 0,
  "executed_fail": 0,
  "cursor": 134,
  "paper_enabled": false,
  "pm_enabled": false,
  "sample_receipts": []
}
```

## Loop / 闸门

| 项 | 值 |
| --- | --- |
| PID | **`660910`**（`logs/execution_loop.pid` 已写；日志 `logs/execution_loop.log`） |
| 启动 | 2026-10-14 00:46:23 HKT：`paper_enabled=False paper_db=None paper_tick=False pm_enabled=False` |
| 持续 tick | `paper=False pm=False cursor=134` |
| `EXECUTION_RISK_ENABLED` | 代码默认 `False`（`execution.config`） |
| `.env` | mtime 未变（2026-10-14 00:35:02 HKT，414 B）；无闸门键 |

## 硬阻塞 / 未修项（开闸仍需杨点头）

| ID | 状态 | 说明 |
| --- | --- | --- |
| **P2-38** | **未修 · 待决策** | 网关拒 sample；待决策侧加 `purpose` 后联动，本轮 execution 不单方面改 |
| **P2-06 / P2-07** | **实盘默认安全未改开闸** | PR 内已做 SELL 跳过 max_open、百分点解析、无账本 fail-closed；**`EXECUTION_RISK_ENABLED` / `PM_ENABLED` 仍默认关**，未当「可开风控/PM」验收 |
| **P2-04** | **live 未加固** | 实盘路径 / live 适配器本轮明确不碰 |
| 开 paper / PM / risk | **需杨点头** | 前置代码（P0-4 + P1-04/05/06 等）已合且本机已同步；**禁止擅自开闸** |

## 链接
- PR: https://github.com/tspike630/trade/pull/35
- 核实笔记: `exports/reports/CODE_SWEEP_EXECUTION_NOTES_2026-10-13.md`
- 结案时刻: 2026-10-14 00:47 HKT
