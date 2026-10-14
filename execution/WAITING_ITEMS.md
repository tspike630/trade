# 执行层 — 挂起 / 观察登记

**Time (HK / UTC+8):** 2026-10-14  
**用途：** 执行层认领的观察项。只登记，不改代码，不改默认，不写 `.env`，不启用新闸门，不改 loop。

仓库根没有 `WAITING_ITEMS_REGISTRY.md`。本文件是执行层登记，不代替复盘层文件。复盘 **P-2 / P-3** 口径不在这里。E-1 / E-4 / E-5 归指标；E-3 与 N 类无代码。本文件只登执行层认领的 **E-2**。

本文件**不算有效性**。到至少 2026-10-19 周检旁证之前，这些条目只是观察，不是调参依据。

## C4 / E-2 — `target_band_failover`

| 项 | 内容 |
|----|------|
| **ID** | **C4 / E-2** `target_band_failover` |
| **状态** | **WAITING / 只观察。** 先登记，不立即改代码，不实现该逻辑。 |
| **书出处** | 《艾略特波浪理论》**p145**。预定价格目标有用：市场越过该位置时，提醒去看下一个计算出的位置；目标没有达到，或被超过的幅度很大时，重新考虑原先的计数。 |
| **可落地形式（未实现）** | 持仓期间若价格突破目标带，重估下一目标并继续持仓，与现有 `max_hold` 硬时间退出对照。 |
| **对照基线** | 现有 `max_hold`（默认 **60min**）+ TP/SL。信号未带 `tp_pct` / `sl_pct` / `max_hold_min` 时，缺省仍是已有的 10% / 5% / 60min（`execution/README.md`、`execution/exits.py`）。本登记不改这些数。 |
| **升级条件** | **立项须 R3 回测**对照后再谈。没有这次对照，不落 `exits`，不改运行时。 |
| **禁止** | **禁止盘中改** tp / sl / max_hold。不打开 `PM_MAX_PER_TOKEN_PCT`，不加新闸门。 |

### 与 C3 并列

拍板：**新增 C4**，不并入 C3。

- **C3** 仍是复盘表里的「趋势出场」，**只观察**（`docs/REVIEW_WARNING_CASES.md`）。要立项先做 R3 回测。不盘中追 tp / sl / max_hold。P-2 / P-3 口径不动。
- **C4** 与 C3 **并列**。C3 的语义是趋势出场观察；C4 的语义是目标带突破后重估下一目标并继续持仓，对照硬时间退出。两条都停在观察，分开写，避免糊成一条。

指针：

- 参数表现状：[`P1_PM_ROADMAP.md`](P1_PM_ROADMAP.md)「观察项」
- 调研映射：[`RESEARCH_MAPPING.md`](RESEARCH_MAPPING.md)
- 警示侧例：[`WARNING_CASES.md`](WARNING_CASES.md)
- 复盘 C3：`docs/REVIEW_WARNING_CASES.md`（`crawler/REVIEW_WARNING_CASES.md` 同文）

## 不算有效性

- 到至少 **2026-10-19** 周检旁证之前，C4 **不算有效性**，也不是启用依据。
- 纪律同 R1 / R2 / R3：`docs/REVIEW_R1_SPEC.md`、`docs/REVIEW_R3_SCAFFOLD.md`。没有 R3 对照，目标带切换只登记、不落代码。
