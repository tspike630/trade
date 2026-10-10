-- Independent indicator tables (Step 1–3).
-- READ from tokens/pairs/pair_history / jev_signals / x_watch / alerts only;
-- WRITE only here.
-- Applied by indicators.base on open (CREATE IF NOT EXISTS + ALTER migrate).

CREATE TABLE IF NOT EXISTS token_indicators (
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    -- technical (Step 1)
    change_5m REAL,
    change_15m REAL,
    change_1h REAL,
    change_4h REAL,
    change_24h REAL,
    volatility_24h REAL,
    volume_strength REAL,
    -- funding / JEV (Step 2)
    jev_score REAL,
    jev_funding_bias REAL,
    jev_updated_at TEXT,
    jev_raw_score REAL,
    jev_signal TEXT,
    jev_confidence REAL,
    jev_strength REAL,
    -- sentiment / X (Step 3)
    sentiment_score REAL,
    sentiment_mentions_1h REAL,
    sentiment_mentions_6h REAL,
    sentiment_mentions_24h REAL,
    sentiment_growth_1h REAL,
    sentiment_growth_6h REAL,
    sentiment_growth_24h REAL,
    sentiment_heat_score REAL,
    sentiment_polarity REAL,
    sentiment_updated_at TEXT,
    -- composite (future)
    composite_score REAL,
    composite_updated_at TEXT,
    -- meta
    tier TEXT,
    primary_pair_address TEXT,
    sample_count INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, token_address)
);

CREATE INDEX IF NOT EXISTS idx_token_indicators_updated
    ON token_indicators (updated_at);

CREATE INDEX IF NOT EXISTS idx_token_indicators_tier
    ON token_indicators (tier);

-- Hot-tier archival / time series of indicator snapshots
CREATE TABLE IF NOT EXISTS indicator_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    token_address TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    change_5m REAL,
    change_15m REAL,
    change_1h REAL,
    change_4h REAL,
    change_24h REAL,
    volatility_24h REAL,
    volume_strength REAL,
    jev_score REAL,
    jev_strength REAL,
    sentiment_score REAL,
    sentiment_mentions_1h REAL,
    sentiment_heat_score REAL,
    sentiment_polarity REAL,
    composite_score REAL,
    tier TEXT,
    primary_pair_address TEXT
);

CREATE INDEX IF NOT EXISTS idx_indicator_history_lookup
    ON indicator_history (chain_id, token_address, recorded_at);
