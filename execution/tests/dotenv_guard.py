"""Stop test imports from applying an operator dotenv file.

``crawler/config.py`` and ``decision_engine/config.py`` call
``load_dotenv`` at import, and if ``dotenv`` is missing they parse the same
file. Isolation that only clears ``os.environ`` loses to the next import.
Production still loads that file: this guard is installed by the execution
test package and by child processes whose ``PYTHONPATH`` lists
``execution/tests/_dotenv_guard`` first. It is not on the production path.
"""

from __future__ import annotations

import sys
import types


def _load_dotenv(*_args: object, **_kwargs: object) -> bool:
    return False


def _dotenv_values(*_args: object, **_kwargs: object) -> dict[str, str]:
    return {}


def _find_dotenv(*_args: object, **_kwargs: object) -> str:
    return ""


def install() -> None:
    """Make ``import dotenv`` a no-op for the rest of this process."""
    existing = sys.modules.get("dotenv")
    if existing is not None and getattr(existing, "_execution_test_guard", False):
        return
    module = existing if existing is not None else types.ModuleType("dotenv")
    module.load_dotenv = _load_dotenv  # type: ignore[attr-defined]
    module.dotenv_values = _dotenv_values  # type: ignore[attr-defined]
    module.find_dotenv = _find_dotenv  # type: ignore[attr-defined]
    module._execution_test_guard = True  # type: ignore[attr-defined]
    sys.modules["dotenv"] = module
    for name in ("config", "decision_engine.config", "sources.ccxt.config"):
        loaded = sys.modules.get(name)
        if loaded is not None and hasattr(loaded, "load_dotenv"):
            loaded.load_dotenv = _load_dotenv  # type: ignore[attr-defined]
