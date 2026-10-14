# CODE SWEEP 采集层结案 · 2026-10-14

## 状态
- **[#33](https://github.com/tspike630/trade/pull/33)** 已合 `main`（`6631254`，2026-10-14 00:26 HKT）
- 本机已同步 crawler 并重启：**PID 650835**（`--helius-listen --sources x --loop`）
- `tests.test_collection_sweep`：**14 过**
- 闸门/权重未动；`.env` / Key / `*.db` 未进仓

## 运维（杨指令 #1）
- 历史 log 脱敏：`stabilize-main.log` 等 `api-key=` **30381+ → 0 明文**（`<REDACTED>`）；pre_redact.bak 已粉碎
- **`HELIUS_API_KEY` 已轮换**写入本机 `.env`（600）；Helius 收流正常
- 旧 key：请杨在仪表盘作废（若尚未）

## PR 覆盖（核实→改）
| ID | 结果 |
|----|------|
| P0-1 | handler `_RedactFilter` + helius 只记 status/类型；session/告警同脱敏 |
| P1-11 | 线索 `clue=True` 不覆盖已有 Dex 画像 |
| P1-12 | TG `ban_until` sidecar sqlite，启动加载 |
| P1-03 | `schema_meta` 一次迁移；`Store.open_ro`；Helius 单写队列 |
| P2-05/17/37/36/23/20/21/22/01/02 | 见 PR 描述 |

## 不抢
- P0-2 写侧 → 指标 #32
- 闸门灯 / paper 前置 → 看板 / 执行

## 笔记
- 核实：`CODE_SWEEP_COLLECTION_NOTES_2026-10-13.md`
