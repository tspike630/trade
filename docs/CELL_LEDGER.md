# 格数总账 CELL_LEDGER

- `docs/CELL_LEDGER.csv` 只追加（append-only），不改写、不删除已有行。
- 每格一行；区间行用 `cell_id`..`cell_id_end` 表示（如 1–267 历史累计）。
- 累计格数 = 所有行 `max(cell_id, cell_id_end)` 的最大值（当前 271），用于 Bonferroni N。
- 下一次迁移预登记从 `cell_id=272` 开始。
- 逐格明细未落盘的历史部分不凭记忆补编。
