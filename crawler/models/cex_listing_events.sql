-- CEX 精确上线事件（QUANT Step 4 ②，load_markets 全量 diff）
-- 脚本 scripts/ccxt_listing_watch.py 内联建表；本文件仅作 review/文档对照。
-- 库：prior.db（data/prior/db/prior.db），不是 ccxt.db，也不是业务四库。
-- 写入：仅本表。不读不写 ccxt.db。行情来自 CCXT load_markets() 全量 symbol，无 topN。
-- 幂等：UNIQUE(exchange, symbol) + INSERT OR IGNORE。
--       冷启动把所方当前全部交易对记为「首次见到」（n 可达几千，预期）。
--       次跑已知集合差集为空，新增 0 行。之后只记真实新增。
-- listing_ts：unix 秒（首次全量见到）。ccxt.db 的 ts / first_seen_ts 是 epoch 毫秒，不要混用。
-- 默认关：不调权、不进决策、不写业务库。
-- 分支 A（per-token 深挖）仍不启：要等本表与链上按 symbol 归一 join 之后。
-- quote-like base（USDT/TRY 等）保留；匹配链上时再滤，对齐 Step 2 的过滤口径，不在本表丢弃。

CREATE TABLE IF NOT EXISTS cex_listing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    listing_ts INTEGER NOT NULL,
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'markets_diff',
    UNIQUE (exchange, symbol)
);
CREATE INDEX IF NOT EXISTS idx_cex_listing_events_ts ON cex_listing_events(listing_ts);
