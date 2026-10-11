"""DexScreener crawler configuration (Phase 1–5 + P0 hardening)."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    _env = Path(__file__).resolve().parent / ".env"
    if _env.is_file():
        for _line in _env.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

BASE_URL = "https://api.dexscreener.com"

POLL_INTERVAL_SECONDS = 60
PROFILE_POLL_INTERVAL_SEC = 60

BATCH_SIZE = 30
REQUEST_TIMEOUT_SECONDS = 30.0

MAX_RETRIES = 4
RETRY_BACKOFF_BASE_SECONDS = 1.0
RETRY_BACKOFF_MAX_SECONDS = 60.0
RETRY_JITTER_RATIO = 0.25
RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

HOT_SEEN_WINDOW_SEC = 3600
HOT_ENRICH_MIN_INTERVAL_SEC = 60
WARM_ENRICH_EVERY_CYCLES = 5
WARM_ENRICH_MIN_INTERVAL_SEC = 300
RECHECK_ZERO_PAIR_SECONDS = 86400
RECHECK_ZERO_PAIR_EVERY_CYCLES = 100
ASYNC_MAX_CONCURRENCY = 3

ALERT_PRICE_CHANGE_PCT = 20.0
ALERT_VOLUME_H24_CHANGE_PCT = 50.0
ALERT_LIQUIDITY_DROP_PCT = 30.0
ALERT_COOLDOWN_SEC = int(os.environ.get("ALERT_COOLDOWN_SEC", "600"))  # raised 300→600 (TG push throttle)
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip() or None
ALERT_WEBHOOK_TIMEOUT_SEC = 10.0

HEARTBEAT_PATH = "logs/heartbeat.json"
HEARTBEAT_STALE_SEC = 120

# Proxy pool (DEFAULT OFF)
USE_PROXY = os.environ.get("USE_PROXY", "0").strip().lower() in ("1", "true", "yes", "on")
PROXY_LIST = os.environ.get("PROXY_LIST", "").strip()
PROXY_FILE = os.environ.get("PROXY_FILE", "proxies.txt")
PROXY_BAD_COOLDOWN_SEC = int(os.environ.get("PROXY_BAD_COOLDOWN_SEC", "300"))
# P0: health probe
PROXY_HEALTH_INTERVAL_SEC = float(os.environ.get("PROXY_HEALTH_INTERVAL_SEC", "60"))
PROXY_HEALTH_PROBE_URL = os.environ.get(
    "PROXY_HEALTH_PROBE_URL", "https://api.dexscreener.com/token-profiles/latest/v1"
)
PROXY_PROBE_FAIL_EVICT = int(os.environ.get("PROXY_PROBE_FAIL_EVICT", "3"))
PROXY_AVAILABILITY_ALERT_RATIO = float(
    os.environ.get("PROXY_AVAILABILITY_ALERT_RATIO", "0.3")
)  # alert if healthy/total below this

ENABLED_SOURCES = os.environ.get("ENABLED_SOURCES", "dexscreener").strip()
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "").strip() or None
# P1.3 X API v2 social (optional — unset bearer = no-op)
X_API_BASE = os.environ.get("X_API_BASE", "https://api.x.com/2").rstrip("/")
X_DAILY_BUDGET_REQUESTS = int(os.environ.get("X_DAILY_BUDGET_REQUESTS", "100"))
X_HOURLY_BUDGET_REQUESTS = int(os.environ.get("X_HOURLY_BUDGET_REQUESTS", "20"))
X_RATE_CAPACITY = float(os.environ.get("X_RATE_CAPACITY", "5"))  # token bucket size
X_RATE_REFILL_PER_SEC = float(os.environ.get("X_RATE_REFILL_PER_SEC", "0.1"))  # ~6/min soft
X_SEARCH_MAX_RESULTS = int(os.environ.get("X_SEARCH_MAX_RESULTS", "10"))
X_EARLY_WATCH_SEC = int(os.environ.get("X_EARLY_WATCH_SEC", "3600"))
X_HOT_WATCH_LIMIT = int(os.environ.get("X_HOT_WATCH_LIMIT", "15"))
X_EARLY_WATCH_LIMIT = int(os.environ.get("X_EARLY_WATCH_LIMIT", "10"))
X_MENTION_SPIKE_RATIO = float(os.environ.get("X_MENTION_SPIKE_RATIO", "3.0"))
X_MENTION_SPIKE_MIN = int(os.environ.get("X_MENTION_SPIKE_MIN", "5"))
X_DUAL_SIGNAL_LOOKBACK_SEC = int(os.environ.get("X_DUAL_SIGNAL_LOOKBACK_SEC", "1800"))
SOLANA_LOGS_FIXTURE = os.environ.get(
    "SOLANA_LOGS_FIXTURE", "fixtures/solana_logs_sample.json"
)

# API server + P0 auth
API_HOST = os.environ.get("API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("API_PORT", "8080"))
API_SECRET = os.environ.get("API_SECRET", "").strip() or None
ALLOW_INSECURE_API = os.environ.get("ALLOW_INSECURE_API", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

# P0 backup
DB_PATH = "dexscreener.db"
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")
BACKUP_RETAIN = int(os.environ.get("BACKUP_RETAIN", "7"))

LOG_DIR = "logs"
LOG_FILE = "crawler.log"
LOG_LEVEL = "INFO"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

USER_AGENT = "dexscreener-crawler/p5.1 (+https://api.dexscreener.com)"

# P1.1 Telegram alerting (optional — unset token = log/webhook only)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None  # fallback all
TELEGRAM_CHAT_ID_MAIN = os.environ.get("TELEGRAM_CHAT_ID_MAIN", "").strip() or None
TELEGRAM_CHAT_ID_ALERT = os.environ.get("TELEGRAM_CHAT_ID_ALERT", "").strip() or None
TELEGRAM_CHAT_ID_ADMIN = os.environ.get("TELEGRAM_CHAT_ID_ADMIN", "").strip() or None
TELEGRAM_TIMEOUT_SEC = float(os.environ.get("TELEGRAM_TIMEOUT_SEC", "15"))

# Telegram rate-limit / 429 storm protection (do not disable Telegram)
TG_MAX_RETRIES = int(os.environ.get("TG_MAX_RETRIES", "3"))  # attempts per message
TG_429_SLEEP_CAP_SEC = float(os.environ.get("TG_429_SLEEP_CAP_SEC", "30"))  # never block crawl for hours
TG_RATE_MIN_INTERVAL_SEC = float(os.environ.get("TG_RATE_MIN_INTERVAL_SEC", "1.0"))  # ~1 msg/s per chat
TG_RATE_PER_MIN = int(os.environ.get("TG_RATE_PER_MIN", "20"))  # ~20/min per chat (groups)
TG_QUEUE_MAX = int(os.environ.get("TG_QUEUE_MAX", "80"))
TG_QUEUE_DROP_LOW = os.environ.get("TG_QUEUE_DROP_LOW", "1").strip().lower() in (
    "1", "true", "yes", "on",
)

# Telegram push throttle (delivery only — detect/store keep ALERT_* / should_push_alert)
# Defaults are conservative to avoid long Telegram flood-wait bans.
_TELEGRAM_PUSH_EXCLUDE_RAW = os.environ.get(
    "TELEGRAM_PUSH_EXCLUDE_TYPES", "new_listing"
).strip()
TELEGRAM_PUSH_EXCLUDE_TYPES = frozenset(
    t.strip().lower() for t in _TELEGRAM_PUSH_EXCLUDE_RAW.split(",") if t.strip()
)
TELEGRAM_PUSH_MIN_SEVERITY = (
    os.environ.get("TELEGRAM_PUSH_MIN_SEVERITY", "high").strip().lower() or "high"
)
TELEGRAM_PUSH_MIN_SCORE = float(os.environ.get("TELEGRAM_PUSH_MIN_SCORE", "35"))
# Push-only cooldown (same key fields as store cooldown; does not change has_recent_alert)
TELEGRAM_PUSH_COOLDOWN_SEC = int(
    os.environ.get("TELEGRAM_PUSH_COOLDOWN_SEC", "600")
)


# P1.2 Helius Solana logsSubscribe (optional)
HELIUS_API_KEY = os.environ.get("HELIUS_API_KEY", "").strip() or None
HELIUS_WS_URL = os.environ.get("HELIUS_WS_URL", "").strip() or None  # override full WSS URL
HELIUS_HTTP_URL = os.environ.get("HELIUS_HTTP_URL", "").strip() or None
# Comma-separated program IDs for logsSubscribe mentions (see sources/solana_helius.py defaults)
HELIUS_PROGRAM_IDS = os.environ.get("HELIUS_PROGRAM_IDS", "").strip() or None
HELIUS_MENTIONS = os.environ.get("HELIUS_MENTIONS", "").strip() or None  # alias of PROGRAM_IDS
HELIUS_PING_INTERVAL_SEC = float(os.environ.get("HELIUS_PING_INTERVAL_SEC", "120"))
HELIUS_RECONNECT_MAX_SEC = float(os.environ.get("HELIUS_RECONNECT_MAX_SEC", "60"))
HELIUS_DISCONNECT_ALERT_AFTER = int(os.environ.get("HELIUS_DISCONNECT_ALERT_AFTER", "3"))

# P2: multi-chain filter (comma-separated). Empty = all chains allowed.
# Documented: solana, ethereum, bsc, base (plus any DexScreener chainId).
# Alias: CHAINS
_CHAINS_RAW = (
    os.environ.get("ENABLED_CHAINS")
    or os.environ.get("CHAINS")
    or "solana,ethereum,bsc,base"
).strip()
ENABLED_CHAINS = [c.strip().lower() for c in _CHAINS_RAW.split(",") if c.strip()]

# P2: pair_history snapshot intervals by tier
HISTORY_HOT_INTERVAL_SEC = int(os.environ.get("HISTORY_HOT_INTERVAL_SEC", "300"))  # ~5 min
HISTORY_WARM_INTERVAL_SEC = int(os.environ.get("HISTORY_WARM_INTERVAL_SEC", "1800"))  # ~30 min
HISTORY_COLD_INTERVAL_SEC = int(os.environ.get("HISTORY_COLD_INTERVAL_SEC", "7200"))
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "14"))

# P2: quality score — alerts below this are stored but Telegram/webhook not pushed
ALERT_MIN_SCORE = float(os.environ.get("ALERT_MIN_SCORE", "25"))

# P3: on-chain risk (optional external scanner; unset URL = heuristic only)
RISK_ENABLED = os.environ.get("RISK_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
RISK_API_URL = os.environ.get("RISK_API_URL", "").strip() or None  # e.g. GoPlus/RugCheck-compatible
RISK_API_KEY = os.environ.get("RISK_API_KEY", "").strip() or None  # never log
RISK_TIMEOUT_SEC = float(os.environ.get("RISK_TIMEOUT_SEC", "8"))
RISK_MAX_DEDUCTION = float(os.environ.get("RISK_MAX_DEDUCTION", "40"))
# goplus | generic — goplus builds token_security URLs from RISK_API_URL base
RISK_PROVIDER = os.environ.get("RISK_PROVIDER", "goplus").strip().lower()

# P3: worker queue
WORKER_BACKEND = os.environ.get("WORKER_BACKEND", "sqlite").strip().lower()  # sqlite | redis
WORKER_DB_PATH = os.environ.get("WORKER_DB_PATH", "worker_queue.db")
REDIS_URL = os.environ.get("REDIS_URL", "").strip() or None
WORKER_POLL_SEC = float(os.environ.get("WORKER_POLL_SEC", "2"))
WORKER_CLAIM_TTL_SEC = int(os.environ.get("WORKER_CLAIM_TTL_SEC", "120"))

# P4: Grok report automation
GROK_REPORT_DIR = os.environ.get("GROK_REPORT_DIR", "exports/reports")
GROK_REPORT_STATE = os.environ.get("GROK_REPORT_STATE", "exports/grok_report_state.json")
GROK_REPORT_TELEGRAM = os.environ.get("GROK_REPORT_TELEGRAM", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
GROK_SYSTEM_PROMPT_PATH = os.environ.get(
    "GROK_SYSTEM_PROMPT_PATH", "GROK_SYSTEM_PROMPT.md"
)

# P4: paper trading ledger
PAPER_DB_PATH = os.environ.get("PAPER_DB_PATH", "paper_trading.db")
PAPER_START_CASH = float(os.environ.get("PAPER_START_CASH", "10000"))  # use 1000 for small stabilize books
PAPER_AUTO_OPEN = os.environ.get("PAPER_AUTO_OPEN", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
PAPER_AUTO_MIN_SCORE = float(os.environ.get("PAPER_AUTO_MIN_SCORE", "70"))
PAPER_AUTO_SIZE_USD = float(os.environ.get("PAPER_AUTO_SIZE_USD", "50"))

# Optional auto deep-DD after high score (default OFF — avoid spam during stabilize)
DD_AUTO_ENABLED = os.environ.get("DD_AUTO_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
DD_AUTO_MIN_SCORE = float(os.environ.get("DD_AUTO_MIN_SCORE", "75"))
DD_AUTO_ON_NEW_LISTING = os.environ.get("DD_AUTO_ON_NEW_LISTING", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# P5: live trading — HARD DEFAULT OFF. Never commit exchange secrets.
LIVE_TRADING_ENABLED = os.environ.get("LIVE_TRADING_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# Must equal exact phrase to allow live place_order when enabled
LIVE_TRADING_CONFIRM = os.environ.get("LIVE_TRADING_CONFIRM", "").strip()
LIVE_TRADING_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISKS"
LIVE_KILL_SWITCH_PATH = os.environ.get("LIVE_KILL_SWITCH_PATH", "LIVE_TRADING_DISABLED")
LIVE_MAX_ORDER_USD = float(os.environ.get("LIVE_MAX_ORDER_USD", "25"))
LIVE_MAX_DAILY_NOTIONAL_USD = float(os.environ.get("LIVE_MAX_DAILY_NOTIONAL_USD", "100"))
LIVE_EXCHANGE_API_URL = os.environ.get("LIVE_EXCHANGE_API_URL", "").strip() or None
LIVE_EXCHANGE_API_KEY = os.environ.get("LIVE_EXCHANGE_API_KEY", "").strip() or None  # never log
LIVE_EXCHANGE_API_SECRET = os.environ.get("LIVE_EXCHANGE_API_SECRET", "").strip() or None  # never log

# P5 arb
ARB_MIN_SPREAD_PCT = float(os.environ.get("ARB_MIN_SPREAD_PCT", "3.0"))
ARB_MIN_LIQ_USD = float(os.environ.get("ARB_MIN_LIQ_USD", "5000"))

# ---------------------------------------------------------------------------
# Indicators 旁路 (Step 1) — independent process; writes only token_indicators*
# ---------------------------------------------------------------------------
INDICATORS_ENABLED = os.environ.get("INDICATORS_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
INDICATORS_HOT_INTERVAL_SEC = int(os.environ.get("INDICATORS_HOT_INTERVAL_SEC", "300"))  # 5 min
INDICATORS_WARM_INTERVAL_SEC = int(os.environ.get("INDICATORS_WARM_INTERVAL_SEC", "1800"))  # 30 min
INDICATORS_COLD_INTERVAL_SEC = int(os.environ.get("INDICATORS_COLD_INTERVAL_SEC", "14400"))  # 4 h
INDICATORS_LOOP_INTERVAL_SEC = int(os.environ.get("INDICATORS_LOOP_INTERVAL_SEC", "300"))
# Volatility: sample stdev of simple returns over this lookback (default 24h)
INDICATORS_VOLATILITY_WINDOW_SEC = int(
    os.environ.get("INDICATORS_VOLATILITY_WINDOW_SEC", str(24 * 3600))
)
# Archive hot-tier snapshots into indicator_history
INDICATORS_WRITE_HISTORY_HOT = os.environ.get(
    "INDICATORS_WRITE_HISTORY_HOT", "1"
).strip().lower() in ("1", "true", "yes", "on")
# Composite weights — ALL default 0 so composite/scoring/alerts stay off.
# Skeleton in indicators/composite.py only writes when sum(weights) > 0.
INDICATORS_WEIGHT_TECHNICAL = float(os.environ.get("INDICATORS_WEIGHT_TECHNICAL", "0"))
INDICATORS_WEIGHT_FUNDING = float(os.environ.get("INDICATORS_WEIGHT_FUNDING", "0"))
INDICATORS_WEIGHT_SENTIMENT = float(os.environ.get("INDICATORS_WEIGHT_SENTIMENT", "0"))
# Optional post-indicator hook (auto_hooks.maybe_on_indicator); default OFF
INDICATORS_ON_UPDATE_HOOK = os.environ.get(
    "INDICATORS_ON_UPDATE_HOOK", "0"
).strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# JEV Token Intelligence enricher (Step 2) — independent; rate-limited
# ---------------------------------------------------------------------------
JEV_ENABLED = os.environ.get("JEV_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
JEV_BASE_URL = os.environ.get("JEV_BASE_URL", "https://jevs.fun").rstrip("/")
# Optional API key (never log). Empty = public unauthenticated scans (unchanged).
JEV_API_KEY = os.environ.get("JEV_API_KEY", "").strip()
# How to send the key: header (X-API-Key) | bearer (Authorization: Bearer) | query (api_key=)
_JEV_AUTH_RAW = os.environ.get("JEV_AUTH_MODE", "header").strip().lower()
JEV_AUTH_MODE = _JEV_AUTH_RAW if _JEV_AUTH_RAW in ("header", "bearer", "query") else "header"
JEV_HOT_INTERVAL_SEC = int(os.environ.get("JEV_HOT_INTERVAL_SEC", "900"))  # 15 min
JEV_MAX_PER_MIN = int(os.environ.get("JEV_MAX_PER_MIN", "7"))  # stay under 8/min IP
JEV_HOT_LIMIT = int(os.environ.get("JEV_HOT_LIMIT", "20"))  # max hot tokens per cycle
JEV_ON_NEW_LISTING = os.environ.get("JEV_ON_NEW_LISTING", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
JEV_REQUEST_TIMEOUT_SEC = float(os.environ.get("JEV_REQUEST_TIMEOUT_SEC", "30"))
JEV_RAW_MAX_CHARS = int(os.environ.get("JEV_RAW_MAX_CHARS", "8000"))  # truncate raw JSON
JEV_PENDING_PATH = os.environ.get("JEV_PENDING_PATH", "exports/jev_pending.jsonl")


# ---------------------------------------------------------------------------
# Sentiment / X indicators (Step 3) — READ x_watch + social alerts only
# ---------------------------------------------------------------------------
SENTIMENT_ENABLED = os.environ.get("SENTIMENT_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
SENTIMENT_WRITE_HISTORY = os.environ.get(
    "SENTIMENT_WRITE_HISTORY", "1"
).strip().lower() in ("1", "true", "yes", "on")
# Optional lookback for social_spike / sentiment_anomaly flags (seconds)
SENTIMENT_ALERT_LOOKBACK_SEC = int(os.environ.get("SENTIMENT_ALERT_LOOKBACK_SEC", str(6 * 3600)))
