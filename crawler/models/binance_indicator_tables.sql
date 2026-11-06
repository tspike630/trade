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
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_binance_indicators_tech
    ON binance_indicators (tech_updated_at);

-- Action-zone / α-ruler columns are not listed in the CREATE above.
-- ensure_schema adds them with ALTER TABLE binance_indicators ADD COLUMN.
-- Do not rebuild the table. They are not live entry gates.
-- Canonical stop column is stop_price_zone (L - 0.1*S).
-- natural_stop_price is the non-canonical pin_low - 10 bps variant.
