"""Execution unit tests.

Importing this package clears operator config before any test module binds
import-time flags in ``execution.config``. Interpreter basics stay. A case that
needs a production-style value sets it on an isolated env or in a child
process. This does not read or edit a host ``.env`` and does not place orders.
"""

from __future__ import annotations

import os

from execution.tests.isolated_env import baseline_environ

_clean = baseline_environ()
os.environ.clear()
os.environ.update(_clean)
