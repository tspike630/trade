"""Process environment for execution unit tests.

Operator config (``.env``, a long-running shell, a production uid) must not
decide what a default-off test observes. Call-time reads and child processes
take :func:`isolated_environ`: interpreter basics plus only the keys the test
sets. Import-time flags (``execution.config``) are fixed when the process
starts; :mod:`execution.tests.run_isolated` clears app config before those
imports. A test that wants a production-style value sets that value itself.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

# Child interpreters import sitecustomize from here before application code.
_GUARD_DIR = str(Path(__file__).resolve().parent / "_dotenv_guard")

# Enough for the interpreter, temp files, and ``python -m``. Not app policy.
_KEEP = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SHELL",
        "TERM",
        "SYSTEMROOT",
        "COMSPEC",
        "WINDIR",
        "PYTHONHASHSEED",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
    }
)


def baseline_environ() -> dict[str, str]:
    """Interpreter basics copied from the current process. No app keys."""
    kept = {key: value for key, value in os.environ.items() if key in _KEEP}
    kept.setdefault("PATH", "/usr/bin:/bin")
    return kept


def dotenv_guard_dir() -> str:
    """Directory whose ``sitecustomize`` blocks operator dotenv in a child."""
    return _GUARD_DIR


def isolated_environ(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Baseline plus ``overrides``. A value of ``""`` sets the key empty.

    ``PYTHONPATH`` always starts with the dotenv guard. An override is
    appended so the child can still import the repo.
    """
    env = baseline_environ()
    extra = {str(key): str(value) for key, value in (overrides or {}).items()}
    extra_path = extra.pop("PYTHONPATH", "")
    parts = [_GUARD_DIR]
    if extra_path:
        parts.append(extra_path)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.update(extra)
    return env


@contextmanager
def isolated_env(overrides: Mapping[str, str] | None = None) -> Iterator[dict[str, str]]:
    """Replace ``os.environ`` for the block, then restore every previous key."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(isolated_environ(overrides))
    try:
        yield os.environ
    finally:
        os.environ.clear()
        os.environ.update(saved)


def patch_isolated(overrides: Mapping[str, str] | None = None) -> Any:
    """``patch.dict(..., clear=True)`` so a production book or uid cannot linger."""
    return mock.patch.dict(os.environ, isolated_environ(overrides), clear=True)
