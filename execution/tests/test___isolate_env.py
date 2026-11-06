"""First module discovery imports.

``python -m unittest discover -s execution/tests`` puts that directory on
``sys.path`` and imports ``test_*.py`` as top-level modules. The package
``__init__`` does not run first, so operator config would already be bound
into ``execution.config`` by the time a later test could clear it.

This file sorts before every other ``test_*.py``. It drops app config, then
rebinds import-time flags if a module already captured them. Cases that need
a production-style value set that value themselves.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_root_s = str(_ROOT)
if _root_s not in sys.path:
    sys.path.insert(0, _root_s)

from execution.tests.isolated_env import baseline_environ  # noqa: E402


def isolate_import_time_config() -> None:
    clean = baseline_environ()
    os.environ.clear()
    os.environ.update(clean)
    if "execution.config" not in sys.modules:
        return
    cfg = importlib.reload(sys.modules["execution.config"])
    gateway = sys.modules.get("execution.gateway")
    if gateway is None:
        return
    for name in (
        "EXECUTION_RISK_ENABLED",
        "EXECUTION_DAILY_LOSS_USD",
        "EXECUTION_MAX_OPEN",
        "EXECUTION_EVENTS_ENABLED",
    ):
        setattr(gateway, name, getattr(cfg, name))


isolate_import_time_config()
