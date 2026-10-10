#!/usr/bin/env bash
# Stabilize loop launcher — does NOT export secrets (HELIUS/X/Telegram come from
# process environment and optional .env loaded by config.py).
set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

# Default single-chain for stabilize noise reduction (override allowed)
export ENABLED_CHAINS="${ENABLED_CHAINS:-solana}"

# Never put API keys in this file. Expected in environment or .env:
#   HELIUS_API_KEY, X_BEARER_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID*

exec python main.py --helius-listen --sources x --loop --interval 60
