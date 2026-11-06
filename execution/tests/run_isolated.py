"""Run the execution unit suite with operator config removed first.

Import-time switches in ``execution.config`` are bound when a test module
imports the gateway. Scrubbing app keys inside a test is too late for those.
This entry point clears them before unittest imports any test.

    python -m execution.tests.run_isolated

The process environment may already contain operator config. This command
does not read or edit ``crawler/.env`` (the file ``crawler/config.py`` loads
on import) and does not place orders. A cwd ``.env`` is not that file.
A case that needs a production-style value sets it on an
:func:`execution.tests.isolated_env.isolated_environ` dict.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


def _scrub() -> None:
    # Local import after this function is defined, but the scrub itself must
    # run before unittest loads test modules. isolated_env does not import them.
    from execution.tests.isolated_env import baseline_environ

    clean = baseline_environ()
    os.environ.clear()
    os.environ.update(clean)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    crawler = root / "crawler"
    for entry in (str(crawler), str(root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    _scrub()
    suite = unittest.defaultTestLoader.discover(
        str(root / "execution" / "tests"),
        pattern="test_*.py",
        top_level_dir=str(root),
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
