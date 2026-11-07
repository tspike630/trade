-- Binance spot side path for tech_boll factors.
-- READ the landed universe (exports/binance_spot_universe.db / the JSON report).
-- WRITE only the tables in this file.
-- Never INSERT/UPDATE/DELETE token_indicators, indicator_history, or pair_history.

CREATE TABLE IF NOT EXISTS binance_klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    quote_volume REAL,
    close_time_ms INTEGER NOT NULL,
    trades INTEGER,
    -- Nullable. /api/v3/klines index 9 (taker buy base). Missing is unknown, not zero.
    taker_buy_volume REAL,
    -- Nullable. /api/v3/klines index 10 (taker buy quote). Missing is unknown, not zero.
    taker_buy_quote_volume REAL,
    PRIMARY KEY (symbol, interval, open_time_ms)
);

CREATE INDEX IF NOT EXISTS idx_binance_klines_symbol
    ON binance_klines (symbol, interval, open_time_ms);

CREATE TABLE IF NOT EXISTS binance_indicators (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT,
    quote_asset TEXT,
    status TEXT,
    interval TEXT NOT NULL,
    price_usd REAL,
    change_5m REAL,
    change_15m REAL,
    change_1h REAL,
    change_4h REAL,
    change_24h REAL,
    volatility_24h REAL,
    volume_strength REAL,
    rsi_14 REAL,
    macd REAL,
    macd_signal REAL,
    macd_hist REAL,
    boll_upper REAL,
    boll_mid REAL,
    boll_lower REAL,
    quote_volume_24h REAL,
    sample_count INTEGER,
    kline_close_time_ms INTEGER,
    tier TEXT,
    tech_updated_at TEXT,
    updated_at TEXT NOT NULL,
    -- Read-only producer fields. NULL when the closed 5m history does not cover
    -- the window. They are not entry gates. See docs/binance_readonly_fields_2026-11-07.md.
    change_7d REAL,
    volume_7d_avg REAL,
    taker_buy_quote_volume_5m REAL,
    buy_ratio_5m REAL,
    buy_ratio_1h REAL,
    avg_trade_usd_5m REAL,
    trade_size_ratio REAL,
    lower_wick_ratio REAL
);

CREATE INDEX IF NOT EXISTS idx_binance_indicators_tech
    ON binance_indicators (tech_updated_at);
