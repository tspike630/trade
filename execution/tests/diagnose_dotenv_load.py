#!/usr/bin/env python3
"""Read-only trace of who sets paper and daily-loss.

Does not create, delete, or rewrite any ``.env``. Does not print environment
dumps, secrets, or file bodies. Run from the repository root:

    python3 execution/tests/diagnose_dotenv_load.py

The process environment is not passed through, except interpreter basics, so
a value printed below was applied by startup code or by an import.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
_TRACE = Path(__file__).resolve().parent / "dotenv_load_trace.py"
_KEEP = (
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
)


def _child_code() -> str:
    return (
        "import importlib.util, sys\n"
        f"sys.path[:0] = [{str(ROOT)!r}, {str(CRAWLER)!r}]\n"
        f"spec = importlib.util.spec_from_file_location('_dotenv_load_trace', {str(_TRACE)!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.report_start()\n"
        "mod.install()\n"
        "import importlib\n"
        "import paper_trade\n"
        "import execution.config as cfg\n"
        "importlib.reload(cfg)\n"
        "from execution.gateway import _env_bool\n"
        "mod.report_end(paper=_env_bool('EXECUTION_PAPER_ENABLED', False), "
        "loss=cfg.EXECUTION_DAILY_LOSS_USD)\n"
    )


def _baseline() -> dict[str, str]:
    env = {key: os.environ[key] for key in _KEEP if key in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin")
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(CRAWLER)])
    return env


def _run(label: str, *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print(f"diag.case={label}")
    print(f"diag.case.cwd={cwd}")
    for name, path in (
        ("repo", ROOT / ".env"),
        ("crawler", CRAWLER / ".env"),
        ("cwd", cwd / ".env"),
    ):
        print(f"diag.file.{name}.path={path}")
        print(f"diag.file.{name}.exists={path.exists()}")
    proc = subprocess.run(
        [sys.executable, "-c", _child_code()],
        env=env if env is not None else _baseline(),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith(("diag.", "paper=", "loss=")):
            print(line)
    if proc.returncode != 0:
        print(f"diag.case.returncode={proc.returncode}")
        for line in (proc.stderr or "").splitlines()[-20:]:
            if ".env" in line and "=" in line and not line.startswith("diag."):
                continue
            print(f"diag.stderr={line}")


def main() -> None:
    print("diag.mode=readonly")
    print(f"diag.host.version={sys.version.split()[0]}")
    print(f"diag.host.executable={sys.executable}")
    print("diag.parent.paper=" + repr(os.environ.get("EXECUTION_PAPER_ENABLED")))
    print("diag.parent.loss=" + repr(os.environ.get("EXECUTION_DAILY_LOSS_USD")))
    with tempfile.TemporaryDirectory(prefix="dotenv-diag-") as tmp:
        cwd = Path(tmp)
        (cwd / ".env").write_text(
            "EXECUTION_PAPER_ENABLED=1\nEXECUTION_DAILY_LOSS_USD=200\n",
            encoding="utf-8",
        )
        neutral = Path(__file__).resolve().parent / "_dotenv_neutral"
        traced = _baseline()
        traced["PYTHONPATH"] = os.pathsep.join([str(neutral), str(ROOT), str(CRAWLER)])
        _run("temp-cwd-neutral-sitecustomize", cwd=cwd, env=traced)
        _run("temp-cwd-with-synthetic-dotenv", cwd=cwd)
    _run("repository-cwd-no-file-changes", cwd=ROOT)


if __name__ == "__main__":
    main()
