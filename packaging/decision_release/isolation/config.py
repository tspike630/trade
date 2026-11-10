"""Validation-only stand-in for crawler/config.py. Not a deploy file.

The real crawler config reads a .env file at import. This module exports only
the logging constants and the default DB path name so logging_setup and
decision_engine.config can import. It does not read a file, the environment,
or the network.
"""

LOG_DIR = "logs"
LOG_FILE = "crawler.log"
LOG_LEVEL = "INFO"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
DB_PATH = "dexscreener.db"
