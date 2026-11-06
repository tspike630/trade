"""Startup stub for execution test children.

Python imports ``sitecustomize`` when this directory is the first
``PYTHONPATH`` entry. The stub is in place before ``-c`` or ``-m`` imports
``crawler.config``. Production commands do not put this directory on
``PYTHONPATH``.
"""

from __future__ import annotations

import sys
import types

_module = types.ModuleType("dotenv")


def _load_dotenv(*_args: object, **_kwargs: object) -> bool:
    return False


def _dotenv_values(*_args: object, **_kwargs: object) -> dict[str, str]:
    return {}


def _find_dotenv(*_args: object, **_kwargs: object) -> str:
    return ""


_module.load_dotenv = _load_dotenv  # type: ignore[attr-defined]
_module.dotenv_values = _dotenv_values  # type: ignore[attr-defined]
_module.find_dotenv = _find_dotenv  # type: ignore[attr-defined]
_module._execution_test_guard = True  # type: ignore[attr-defined]
sys.modules["dotenv"] = _module
