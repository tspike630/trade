"""Read-only package scan used by the dashboard smoke test.

The rules are the production rules. Test modules are not exempt: a fixture
that contains write SQL has to live outside this package. Sensitive literals
are assembled so this file can sit inside the scanned tree.
"""

from __future__ import annotations

import re
from pathlib import Path

_WRITE_SQL = re.compile(
    r"(?i)\b(insert\s+into|update\s+[a-z0-9_]+\s+set|delete\s+from|replace\s+into|"
    r"drop\s+table|alter\s+table|create\s+table)\b"
)
_CONNECT = "sqlite3" + ".connect"
_ENV_ASSIGN = re.compile(r"os\.environ\[[^\]]+\]\s*=")
_STORAGE_ASSIGN = 'os.environ["NICEGUI_STORAGE_PATH"]' + " ="
_BANNED = (
    "ui." + "switch",
    "ui." + "toggle",
    "ui." + "checkbox",
    "ui." + "html(",
    "place_" + "order",
)


def package_write_offenders(root: Path) -> list[str]:
    """Return scan hits for production Python files under ``root``.

    ``smoke_test.py`` is the scanner's own harness and is skipped by name.
    ``sessions.py`` may create the chat state database. ``data.py`` may open
    SQLite, and the smoke test checks that those opens stay ``mode=ro``.
    Every other file, including a ``test_*.py`` left inside the package, is
    scanned.
    """
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "smoke_test.py":
            continue
        text = path.read_text(encoding="utf-8")
        if _WRITE_SQL.search(text) and path.name != "sessions.py":
            offenders.append(f"write sql in {path.name}")
        if _CONNECT in text and path.name not in ("data.py", "sessions.py"):
            offenders.append(f"sqlite connect in {path.name}")
        assignments = _ENV_ASSIGN.findall(text)
        allowed_storage = path.name == "__init__.py" and assignments == [_STORAGE_ASSIGN]
        if assignments and not allowed_storage:
            offenders.append(f"env assignment in {path.name}: {assignments}")
        for banned in _BANNED:
            if banned in text:
                offenders.append(f"{banned} in {path.name}")
    return offenders
