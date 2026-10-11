-- CCXT 旁路表（core.py 内联建表；本文件仅作 review/文档对照，勿手动重复执行冲突）
-- ccxt_ticks:  每轮 top 行情快照，UNIQUE(exchange, symbol, ts)
-- ccxt_ohlcv:  K线，UNIQUE(exchange, symbol, timeframe, ts) 幂等 upsert
-- ccxt_listings: 新 symbol 首次发现（CEX 上线事件雏形），UNIQUE(exchange, symbol)
-- 库：ccxt.db（独立旁路库，不碰业务四库）

CREATE TABLE IF NOT EXISTS ccxt_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    last_price REAL,
    bid REAL,
    ask REAL,
    high_24h REAL,
    low_24h REAL,
    volume_24h REAL,
    quote_volume_24h REAL,
    change_24h_pct REAL,
    UNIQUE (exchange, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_ccxt_ticks_ex_sym ON ccxt_ticks(exchange, symbol, ts);

CREATE TABLE IF NOT EXISTS ccxt_ohlcv (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, quote_volume REAL,
    UNIQUE (exchange, symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_ccxt_ohlcv_ex_sym_tf ON ccxt_ohlcv(exchange, symbol, timeframe, ts);

CREATE TABLE IF NOT EXISTS ccxt_listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    first_seen_ts INTEGER NOT NULL,
    first_price REAL,
    note TEXT,
    UNIQUE (exchange, symbol)
);
