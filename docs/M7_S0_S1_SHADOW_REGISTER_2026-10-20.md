# M7 · S0/S1 影簿注册 · 2026-10-20

> **状态：已注册（五层已过 · 今晚落地）**  
> **只写文档与旁路记账设计；不改 `.env`、不改主簿阈值、不改 `DECISION_MIN_LIQ`。**  
> 母案：`docs/SURVIVAL_20k_METHODS_2026-10-20.md` §M7；合流 M1-A（拒 `liq < 100k` 纸面假说）。

---

## 1. 注册声明

| 簿 | 门槛 | 性质 | 记账含义 |
|---|---|---|---|
| **S0** | 现行 `min_liq = 50k`（与 live/回测地板一致） | **主簿对照** | 凡已按现行闸可开的信号，记入 S0 对照集；**主路径仍只认 S0** |
| **S1** | `min_liq = 100k` | **只读影簿** | 同一信号在 S1 下「会不会开」的反事实；**不驱动成交** |

分流细则：

- `liq_usd ≥ 100_000` → `would_open_S0=1`、`would_open_S1=1`；`bucket=S1`（双过闸）。
- `liq_usd ∈ [50_000, 100_000)` → `would_open_S0=1`、`would_open_S1=0`；`bucket=S0`，并在 `extras` 标 **`S1_skip` 候选**。  
  **不算实拒**，除非另批 soft-skip 接入主路径（见 §6）。
- `liq_usd < 50_000` → 主闸本就不该写成过闸 buy；若日志出现，记 `extras.leak_below_S0=1`，**不并进** `S1_skip` / `liq_missing`。

本注册**不等于**已把主闸改成 100k，也**不等于** M1 硬过滤已上线。

---

## 2. NULL / 缺失流动性：单独档 `liq_missing`

| 条件 | `bucket` | 禁止 |
|---|---|---|
| `liquidity_usd` 为 NULL / 缺字段 / 非数 | **`liq_missing`** | **禁止**并进「低于门槛」「S1_skip」「liq_below_min」任一计数 |

与周检三桶口径对齐精神（缺失 vs 低于 50k vs 过闸），但本影簿枚举为 **`S0 | S1 | liq_missing`**（互斥）：  
`S0` = 仅过 50k 未过 100k；`S1` = 已过 100k；缺失独占一档。

---

## 3. 与 Phase0 双列关系（并存 · 不互相吞并）

| 体系 | 字段 / 旗 | 用途 | 对成交 |
|---|---|---|---|
| **Phase0** | `shadow_liq_lt_150k` + `shadow_R3`（现行旁路实现见 `indicators/shadow_stop_prone.py` · `shadow_skip_stop_prone` / rule R3） | **止损倾向打标**（入口特征旁证） | 旁路，不进决策 |
| **M7** | `bucket` / `would_open_S0` / `would_open_S1` | **门槛分流记账**（50k 对照 vs 100k 影簿） | 只读影簿；主闸不动 |

- Phase0 回答：「这笔像不像易止损？」  
- M7 回答：「若地板是 50k / 100k，会不会开？」  
- **两套并存**：同一 `decision_log_id` 可同时挂 Phase0 旗与 M7 分桶；**禁止**用 Phase0 旗改写 M7 桶，也**禁止**用 M7 `S1_skip` 冒充 Phase0 止损标。

---

## 4. 记账字段建议（CSV 列）

建议落点（只读追加，不改主库 schema 亦可）：  
`exports/m7_s0_s1_shadow_register.csv`（或执行侧只读旁表，同列）。

| 列 | 类型 | 说明 |
|---|---|---|
| `ts` | ISO-8601 | 信号时间（建议原 UTC `Z`；看板转 HKT） |
| `decision_log_id` | int | `decision_log.id` |
| `chain` | text | `chain_id` |
| `token` | text | `token_address` |
| `liq_usd` | float / empty | 来自 `raw_json.extras.liquidity_usd`；缺失则空 |
| `bucket` | enum | `S0` \| `S1` \| `liq_missing` |
| `would_open_S0` | 0/1 | `liq_usd ≥ 50k` 且非 missing |
| `would_open_S1` | 0/1 | `liq_usd ≥ 100k` 且非 missing |
| `extras` | JSON/text | 至少可含：`S1_skip`（当 `bucket=S0`）、`purpose`、`strategy_id`、`leak_below_S0`、Phase0 旗引用（可选） |

主键建议：`decision_log_id`（一行一信号）。

---

## 5. 执掌

| 层 | 职责 |
|---|---|
| **决策** | 出信号时**旁路标记**分桶（或供执行消费的同构字段）；**不**改主簿 `DECISION_MIN_LIQ` / `.env` |
| **执行** | **可选**只读落表 / 追加 CSV；纸面成交仍跟 S0 主闸；S1 永不下单 |
| **复盘 / 看板** | 挂 S0 vs S1 对照（笔数、Σpnl、止损占负）；与 Phase0 双列分栏展示 |
| **采集** | 保证 `extras.liquidity_usd` 写入覆盖；缺口进 `liq_missing`，不猜数回填进门槛桶 |

**主簿钉死**：`.env` 内 `DECISION_MIN_LIQ` / `DECISION_MIN_LIQ_USD` **不动**（现行默认 **50000**）。本文件注册影簿 ≠ 改闸。

---

## 6. 否决（写死）

1. **把影簿当成已改闸**——任何「已经按 100k 跑主簿」的表述或对 `.env` / `RiskManager` 地板的静默改写。  
2. **S1 无批就 soft-skip 主路径**——`S1_skip` 仅候选记账；未另批 soft 前，**禁止**因 `liq ∈ [50k,100k)` 拦截主路径开仓。  
3. 把 `liq_missing` 并进低于门槛或 `S1_skip`。  
4. 用 M7 计数推翻 / 顶替 Phase0 止损打标，或反之。  
5. `purpose=sample` 流入可成交路径（母案 S3；本注册不授权）。

---

## 附录 A · 只读示意样本（`decision_log` buy）

**抽法**（只读，未改库 / 未改 `.env`）：

```text
dexscreener.db.decision_log
  WHERE action='buy'
  ORDER BY id DESC
  LIMIT 20
```

流动性取自 `raw_json.extras.liquidity_usd`；分桶规则同 §1–§2。

### A.1 最近 20 条 buy 分桶计数

| bucket | n | would_open_S0 | would_open_S1 | 注 |
|---|---:|---:|---:|---|
| **S1** | **20** | 20 | 20 | 全部 `liq ≥ 100k` |
| **S0** | **0** | — | — | 本窗无 `[50k,100k)` |
| **liq_missing** | **0** | — | — | 本窗 extras 均有 liq |

- 时间窗（信号 `ts`）：**2026-10-20 08:44:22 – 09:02:23 HKT (Asia/Hong_Kong)**（源库 UTC `2026-10-20T00:44:22Z` … `T01:02:23Z`）。  
- `purpose`：全部 `live_candidate`；`strategy_id=jev_strength_v1`。  
- 代币高度重复（主要 `9bb6…pump` / `ai66…`），**示意 ≠ 独立样本**。  
- 抽数时刻约 **2026-10-20 13:09 HKT (Asia/Hong_Kong)**。

### A.2 对照：全库 `purpose=live_candidate` buy（非仅 20）

| bucket | n |
|---|---:|
| S1 (`≥100k`) | 538 |
| S0 (`[50k,100k)` · S1_skip 候选) | 8 |
| liq_missing | 68 |
| **合计** | **614** |

说明：全库 `action=buy` 共 751，其中非 `live_candidate` / 早期行 extras 缺 liq 更多；**缺失必须单列**，与本注册一致。S0=8 仅为历史过闸 buy 中落在半开区间者，**尚未**构成 soft-skip 批准。

### A.3 数据缺口

| 缺口 | 状态 |
|---|---|
| 专用 `exports/m7_s0_s1_shadow_register.csv` | **尚未落表**（本文件只注册列与规则） |
| 最近 20 条 buy 的 S0 / missing 示意 | **本窗为 0**；要看 S1_skip 请用 A.2 或更长窗 |
| Phase0 `shadow_liq_lt_150k` 列名与 M7 CSV 自动 join | 未在本注册实现；并存约定见 §3 |
| paper `positions` 按 M7 桶回贴 Σpnl | 未跑；复盘后续挂对照即可 |

---

## 签字栏（流程）

| 项 | 值 |
|---|---|
| 注册日 | 2026-10-20 |
| 范围 | M7 S0/S1 影簿记账 |
| 改闸 | **否** |
| 下一步 | 决策旁路标记 → 执行可选落 CSV → 复盘/看板挂对照 |
