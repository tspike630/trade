-- CEX 精确上线事件（scripts/ccxt_listing_watch.py 内联建表；本文件仅作文档副本）
-- 库：data/prior/db/prior.db（只写本表；不写 ccxt.db / 业务四库）
-- 口径：CCXT load_markets() 全量 symbol diff；UNIQUE(exchange, symbol) 幂等
-- listing_ts：首次全量见到的 epoch seconds（冷启动会把存量也记成「上线」）
-- 纪律：不调权、不进决策；分支 A 深挖待与链上 join 匹配干净后再启

CREATE TABLE IF NOT EXISTS cex_listing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    listing_ts INTEGER NOT NULL,          -- epoch seconds（首次全量见到）
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'markets_diff',
    UNIQUE (exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cex_listing_events_ts ON cex_listing_events(listing_ts);
