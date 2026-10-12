-- CEX 视野监控快照（scripts/cex_scope_watch.py 内联建表；本文件仅作文档副本）
-- 库：data/prior/db/prior.db（只写本表；只读 ccxt.db）
-- 口径：每日 UTC 1 行，UNIQUE(snap_date) + ON CONFLICT upsert 幂等
-- 纪律：不调权、不进决策、不写业务四库

CREATE TABLE IF NOT EXISTS cex_scope_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snap_date TEXT NOT NULL UNIQUE,          -- UTC YYYY-MM-DD
    snapshot_ts INTEGER NOT NULL,            -- epoch seconds
    n_total INTEGER NOT NULL DEFAULT 0,      -- 累计 ccxt_listings
    n_new_24h INTEGER NOT NULL DEFAULT 0,    -- 近 24h 新进入视野（first_seen_ts ms）
    n_still_visible INTEGER NOT NULL DEFAULT 0, -- 近 24h 仍有 tick 的 DISTINCT symbol
    med_delta24h_new REAL,                   -- 新进入者 change_24h_pct 中位
    note TEXT
);
