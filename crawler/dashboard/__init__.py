"""Read-only trading monitor.

The repository root ``dashboard/`` is the source of truth. ``crawler/dashboard``
is a lagging copy (it has no ``panels/evolution.py``) and is not kept in sync
by this package.

Meant to be copied into the dexscreener-crawler root.

Session files for the UI (a boolean "logged in" flag only) live under
NICEGUI_STORAGE_PATH. That path is set before NiceGUI is imported so the
session store is not created next to the SQLite databases.
"""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def _default_session_dir() -> None:
    if os.environ.get("NICEGUI_STORAGE_PATH", "").strip():
        return
    base = Path(os.environ.get("TMPDIR", "/tmp"))
    os.environ["NICEGUI_STORAGE_PATH"] = str(base / "dex-dashboard-sessions")


_default_session_dir()
