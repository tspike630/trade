-- prior.db 旁路库表结构（新币先验数据）
-- 独立于业务四库；只读参考用途
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS token_outcomes (
    token_address   TEXT NOT NULL,
    chain           TEXT NOT NULL,
    source          TEXT NOT NULL,
    pool_address    TEXT,                 -- SolRPDs 用；RED-PUMP 无则 NULL
    launch_ts       INTEGER,              -- 上线/首次活动时间（epoch 秒）
    last_ts         INTEGER,              -- 最后活动/结局时间（epoch 秒）
    peak_time_min   INTEGER,              -- 距上线见顶分钟（无则 NULL）
    peak_return     REAL,                 -- 见顶涨幅（无则 NULL）
    outcome         TEXT,                 -- graduated / timeout / active / inactive / rug
    liq_added       REAL,                 -- SolRPDs：累计添加流动性
    liq_removed     REAL,                 -- SolRPDs：累计移除流动性
    add_remove_ratio REAL,                -- 添加/移除比
    num_adds        INTEGER,
    num_removes     INTEGER,
    inactivity_status TEXT,               -- SolRPDs：Active/Inactive...
    extra_json      TEXT,                 -- 原始行完整 JSON（审计用）
    ingested_at     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS provenance (
    source      TEXT PRIMARY KEY,
    fetched_at  TEXT,
    url         TEXT,
    license     TEXT,
    files       TEXT,
    notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_outcomes_source ON token_outcomes(source);
CREATE INDEX IF NOT EXISTS idx_outcomes_token  ON token_outcomes(token_address);

-- ===== 第二步: GMGN OHLCV =====
CREATE TABLE IF NOT EXISTS token_ohlcv (
    token_address   TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'gmgn',
    ticker          TEXT,
    discovery_ts    INTEGER,          -- 发现/上线时间（epoch 秒）
    death_reason    TEXT,             -- GMGN 监控终止原因（viable / non-viable...）
    ohlcv_json      TEXT,             -- 原始 OHLCV（数组）
    peak_ts         INTEGER,          -- 见顶时间（epoch 秒）
    peak_price      REAL,             -- 见顶价格
    peak_return     REAL,             -- 见顶相对首价涨幅（倍）
    first_price     REAL,             -- 首条 OHLCV 价
    last_price      REAL,             -- 末条 OHLCV 价
    n_bars          INTEGER,          -- OHLCV 条数
    ingested_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_token ON token_ohlcv(token_address);

-- ===== 第五步: decision_prior_tags（JEV×token_age×prior 旁路打标）=====
-- decision_id = str(decision_log.id)；signal_ts = decision_log.ts (ISO TEXT)
-- pair_created_at 来自主库 pairs（epoch ms）；年龄差须先把 signal_ts 转 ms 再减
-- 只写 prior.db；主库只读；默认不进决策路径
--
-- 迁移注意（已有 prior.db）：
--   CREATE TABLE IF NOT EXISTS 不会给旧表加列。
--   prior_tagger.ensure_prior_schema 会对已有表执行：
--     ALTER TABLE decision_prior_tags ADD COLUMN survival_ref_note TEXT;  -- after PRAGMA table_info check
--   （经 PRAGMA table_info 探测后再 ADD；勿重复执行裸 ALTER）。勿 DROP 整库以免擦掉其他 prior 表。
--
-- survival_ref 语义（按 age_bucket，非全行同一对）:
--   <1h   → survival_ref_1h 填 self_db.survive['1h']，survival_ref_4h=NULL
--   1-24h → survival_ref_4h 填 self_db.survive['4h']，survival_ref_1h=NULL
--   1-7d / >7d / unknown → 两列 NULL + survival_ref_note='crawler 窗口覆盖上限'
CREATE TABLE IF NOT EXISTS decision_prior_tags (
  decision_id TEXT NOT NULL,           -- str(decision_log.id)；JOIN 主库用 CAST(id AS TEXT)
  chain_id TEXT,
  token_address TEXT,
  signal_ts TEXT,                     -- decision_log.ts (ISO TEXT)；≠ epoch ms
  age_bucket TEXT,                    -- '<1h' | '1-24h' | '1-7d' | '>7d' | 'unknown'
  token_age_hours REAL,               -- (signal_ts_ms - pair_created_at_ms) / 3_600_000
  peak_window INTEGER,                -- 1 if signal within PEAK_WINDOW of pair_created_at
                                      -- TUNABLE: default 3 min = GMGN peak P50≈1.0m ×3（暂定）
  survival_ref_1h REAL,               -- 仅 <1h 桶非空（self_db.survive['1h']）
  survival_ref_4h REAL,               -- 仅 1-24h 桶非空（self_db.survive['4h']）
  survival_ref_note TEXT,             -- 长窗/unknown：'crawler 窗口覆盖上限' 等
  tagged_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (decision_id)
);
CREATE INDEX IF NOT EXISTS idx_dpt_age ON decision_prior_tags(age_bucket);

-- ===== 第六步: baseline_history（基线滚动快照，支持「稳定两周才进风控」证据）=====
-- 只追加、不覆盖；由 scripts/prior_baseline_trend.py 写入
-- source 例: self_survival | gmgn_ohlcv | redpump | manual
CREATE TABLE IF NOT EXISTS baseline_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recorded_at TEXT NOT NULL,
  source TEXT NOT NULL,           -- e.g. self_survival | gmgn_ohlcv | redpump | manual
  graduation_rate REAL,
  same_day_death_rate REAL,
  solrpds_remove_gt_add REAL,
  survive_1h REAL,
  survive_4h REAL,
  survive_24h REAL,
  survive_7d REAL,
  peak_p50_min REAL,
  crawler_window_hours REAL,
  n_tokens INTEGER,
  params_json TEXT,               -- full snapshot blob optional
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_bh_recorded ON baseline_history(recorded_at);
