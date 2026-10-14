# CODE SWEEP 指标层结案（2026-10-14）

## 状态
- PR [#32](https://github.com/tspike630/trade/pull/32) 已合 `main`（`0ddbfd7`，2026-10-14 00:24 HKT）
- 本机已同步并重启：`indicators --loop` **PID 642834**
- 库已迁 **`tech_updated_at`**（有样本写入）
- 权重仍 **0 / 0 / 0**；闸门未动

## 核实 → 落地

| ID | 核实 | #32 |
| --- | --- | --- |
| P0-2 | 共用 `updated_at`；无 `tech_updated_at` | 加列；仅 technical 真重算 bump；`only_due` 只认它 |
| P1-08 | 不足 bars 保旧值仍刷新时间戳 | 保旧不 bump、不写 history |
| P1-07 | 多写手 history NULL RSI | 补全 + 读侧非空优先 + replay coalesce |
| P2-13 | MACD 非原子 | 全有 / 全 keep / 全 NULL |
| P1-03/P2-05 | IndicatorStore busy 5s | → 30000 |
| P2-16 | MAX 历史流动性 | → 最新 snapshot 流动性 |
| P2-18 | 无 history purge | 14d，仅 `--loop` |
| P1-13 | prior_ingest 删库 | 增量；`--rebuild` 先备份 |
| P1-02 | once/loop 同路径（报告过度） | 不另造锁；与执行 P1-04/05 对拍收口 |

## 读侧交接
- @交易看板 跟读 `tech_updated_at`（[#36](https://github.com/tspike630/trade/pull/36)）
- @决策引擎层 [#31](https://github.com/tspike630/trade/pull/31) 等杨验收

## 测试
- `cd crawler && python3 -m unittest indicators.test_code_sweep`（云端 18 过）

## 链接
- Agent: https://cursor.com/agents/bc-7425b430-489c-5b69-b562-4edac7969c67
- 核实笔记: `CODE_SWEEP_INDICATORS_NOTES_2026-10-13.md`
