-- CEX 新币视野快照（QUANT Step 3 分支 B）
-- 脚本 scripts/cex_scope_watch.py 内联建表；本文件仅作 review/文档对照。
-- 库：prior.db（data/prior/db/prior.db），不是 ccxt.db，也不是业务四库。
-- 写入：仅本快照表。ccxt.db 只读（ccxt_listings / ccxt_ticks）。
-- 幂等：UNIQUE(snap_date) + INSERT ON CONFLICT(snap_date) DO UPDATE。
--       --once 同日重跑覆盖当日 1 行，不翻倍。snap_date 为 UTC YYYY-MM-DD。
-- snapshot_ts：unix 秒。ccxt 侧 ts / first_seen_ts 为 epoch 毫秒，24h 窗口在脚本里按毫秒比较。
-- 默认关：不调权、不进决策、不写业务库。分支 A（cex_prior_linkage）不在本表。

CREATE TABLE IF NOT EXISTS cex_scope_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL UNIQUE,
    snapshot_ts INTEGER NOT NULL,
    n_total INTEGER NOT NULL DEFAULT 0,
    n_new_24h INTEGER NOT NULL DEFAULT 0,
    n_still_visible INTEGER NOT NULL DEFAULT 0,
    med_delta24h_new REAL,
    note TEXT
);
