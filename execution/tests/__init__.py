"""Execution unit tests.

Importing this package disables dotenv and then clears operator config,
before any test module binds import-time flags in ``execution.config``.
``crawler/config.py`` would otherwise read ``crawler/.env`` on the next
import and put those keys back. Interpreter basics stay. A case that needs
a production-style value sets it on an isolated env or in a child process.
This does not read or edit a host ``.env`` and does not place orders.
"""

from __future__ import annotations

import os

from execution.tests.dotenv_guard import install
from execution.tests.isolated_env import baseline_environ

install()
_clean = baseline_environ()
os.environ.clear()
os.environ.update(_clean)
