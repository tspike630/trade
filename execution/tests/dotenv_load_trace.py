"""Trace dotenv loads without printing file bodies or the process environment.

Records the path passed to ``load_dotenv``, whether that path exists, and
the caller filename/line. Also notes ``open`` calls whose final path
component is ``.env``. Values other than ``EXECUTION_PAPER_ENABLED`` and
``EXECUTION_DAILY_LOSS_USD`` are never printed.
"""

from __future__ import annotations

import builtins
import importlib.abc
import importlib.machinery
import os
import sys
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

_EVENTS: list[str] = []
_INSTALLED = False
_ORIGINAL_OPEN = builtins.open


def _exists(path: object) -> bool:
    try:
        return Path(os.fspath(path)).exists()  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return False


def _callers() -> str:
    frames: list[str] = []
    for frame in traceback.extract_stack()[:-2]:
        if frame.filename.endswith(("dotenv_load_trace.py", "diagnose_dotenv_load.py")):
            continue
        frames.append(f"{frame.filename}:{frame.lineno}:{frame.name}")
    return " <- ".join(frames[-8:])


def _note(line: str) -> None:
    _EVENTS.append(line)


def _wrap_load(real: Any) -> Any:
    def wrapped(*args: object, **kwargs: object) -> Any:
        path = kwargs.get("dotenv_path", args[0] if args else None)
        shown = "<stream>" if path is None and kwargs.get("stream") is not None else path
        _note(f"diag.load.path={shown!s}")
        _note(f"diag.load.exists={_exists(path) if path is not None else False}")
        _note(f"diag.load.caller={_callers()}")
        result = real(*args, **kwargs)
        _note(f"diag.load.result={result!s}")
        return result

    return wrapped


def _wrap_module(module: ModuleType) -> None:
    current = getattr(module, "load_dotenv", None)
    if current is None or getattr(current, "_dotenv_trace", False):
        return
    wrapped = _wrap_load(current)
    wrapped._dotenv_trace = True  # type: ignore[attr-defined]
    module.load_dotenv = wrapped  # type: ignore[attr-defined]


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: object = None,
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname != "dotenv":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        loader = spec.loader
        original = loader.exec_module

        def exec_module(module: ModuleType) -> None:
            original(module)
            _wrap_module(module)

        loader.exec_module = exec_module  # type: ignore[method-assign]
        return spec


def _traced_open(file: object, *args: object, **kwargs: object) -> object:
    try:
        text = os.fspath(file)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        text = ""
    if text.endswith(".env") or os.path.basename(text) == ".env":
        _note(f"diag.open.path={text}")
        _note(f"diag.open.exists={_exists(text)}")
        _note(f"diag.open.caller={_callers()}")
    return _ORIGINAL_OPEN(file, *args, **kwargs)


def install() -> None:
    """Install the trace once. Does not read or write a configuration file."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    builtins.open = _traced_open  # type: ignore[assignment]
    sys.meta_path.insert(0, _Finder())
    existing = sys.modules.get("dotenv")
    if isinstance(existing, ModuleType):
        _wrap_module(existing)


def report_start() -> None:
    """Print interpreter identity and the two non-secret keys before imports."""
    site = sys.modules.get("sitecustomize")
    print(f"diag.version={sys.version.split()[0]}")
    print(f"diag.executable={sys.executable}")
    print(f"diag.cwd={os.getcwd()}")
    print(f"diag.sitecustomize={getattr(site, '__file__', None)}")
    print("diag.start.paper=" + repr(os.environ.get("EXECUTION_PAPER_ENABLED")))
    print("diag.start.loss=" + repr(os.environ.get("EXECUTION_DAILY_LOSS_USD")))
    for index, entry in enumerate(sys.path):
        print(f"diag.path[{index}]={entry}")


def _module_file(module: ModuleType | None) -> str | None:
    file = getattr(module, "__file__", None)
    if not file:
        return None
    try:
        return str(Path(file).resolve())
    except OSError:
        return str(file)


def report_end(*, paper: bool, loss: object) -> None:
    """Print module files, traced loads, and the two non-secret results."""
    for name in ("config", "paper_trade", "dotenv", "decision_engine.config", "sitecustomize"):
        print(f"diag.module.{name}={_module_file(sys.modules.get(name))}")
    for line in _EVENTS:
        print(line)
    print(f"paper={paper}")
    print(f"loss={loss}")
