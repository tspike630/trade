# Phase 1 Optimization Result

**Time (HK / UTC+8):** 2026-10-10 ~00:51–00:55 HKT  
**DB:** fresh `dexscreener.db` after opt code (then 4 live cycles)

## What changed

| # | Behavior | Implementation |
|---|---|---|
| 1 | Empty-pair skip | `tokens.pair_count` / `tokens.has_pairs`; enrich 后写 0/1；后续 `has_pairs=0` 硬跳过；`NULL`=未 enrich。README 注明 Phase 3 可改为降频 |
| 2 | Lowercase addresses | `norm_address` / `norm_chain`；upsert 与 batch key 一律小写；启动时 `schema_meta.addr_lower_v1` 迁移合并旧大小写行 |
| 3 | Fill batches to 30 | 表 `pending_enrich`；按链凑满 30 才请求；余量跨周期保留；`python main.py --flush-pending` 强制冲刷余量 |
| 4 | Per-batch isolation | 每个 `/tokens/v1` 批次独立 try/except；失败 print + summary，不中断整轮；失败地址留在 pending |

## Live run stats

### Cycle 1 — `python main.py`（无 flush）

- Unique tokens: **68**（inserted 68）
- Enrich: candidates **68**, batches **`[30]`**（仅 1 批满 30）
- Marked: has_pairs=1 **23**, has_pairs=0 **7**
- **pending remaining: 38**（按链不足 30 的攒着）
- pairs: **23**

### Cycle 2 — `python main.py`（无 flush，验证 skip + pending）

- skipped has_pairs=0: **7**
- skipped already (has_pairs=1): **23**
- candidates/queue: **38**（pending）
- batches sent: **0**（各链仍不足 30，继续攒）
- pending remaining: **38**

### Cycle 3 — `python main.py --flush-pending`

- skipped 0/already: **7 / 23**
- batches: **`[3, 1, 11, 23]`**（各链余量强制发送）
- marked has_pairs=1 **+20**, has_pairs=0 **+18**
- pending: **0**
- DB: tokens 68（has_pairs=0: **25**, has_pairs=1: **43**）, pairs **43**

### Cycle 4 — `python main.py`（全量 skip）

- skipped has_pairs=0: **25**
- skipped already: **43**
- candidates: **0**, batches: **0**
- 无多余 enrich 请求

## Lowercase check

- `token_address != lower(token_address)` → **0** rows
- `pair_address` / `chain_id` 同样全部小写
- 样例：`ffs3ezaa4lgtb1jchw66pdtp5rznddkrvfl75cmstnk`（Solana 亦小写存储；API 对小写地址可用）

## Before → after

| 行为 | 优化前 | 优化后 |
|---|---|---|
| 无 pair 的 token | 每轮仍进 enrich | 标记 `has_pairs=0` 后硬跳过 |
| 地址大小写 | 仅 strip，EVM 可能重复 | 强制 lower，PK 稳定 |
| 批次大小 | 按链有多少发多少 | 满 30 才发；余量 pending；`--flush-pending` |
| 单批失败 | 已有 continue | 保持：log + continue，pending 不丢 |

## Errors

四轮均无 API/batch 错误。

## How skip / pending works（简图）

```
列表 upsert tokens
    ↓
has_pairs=0 → skip
has_pairs=1 → skip（Phase1 enrich-once）
has_pairs NULL → 加入 pending_enrich
    ↓
按 chain 合并 pending
    ↓
while len>=30: 发送一批 → 写 pairs + pair_count/has_pairs → 移出 pending
余量: 默认留 pending；--flush-pending 则本轮发送
```
