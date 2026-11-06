"""Operator dotenv is reapplied on import unless the test guard is installed.

The fixture is a temporary copy of this tree. It does not read, delete, or
rewrite the repository ``.env`` or ``crawler/.env``. ``crawler/config.py`` in
the copy loads that copy's ``crawler/.env``. Guarded children still stub
``dotenv``. Production load behavior is unchanged.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
_LIVE_ENVS = (ROOT / ".env", CRAWLER / ".env")
_TRACE = Path(__file__).resolve().parent / "dotenv_load_trace.py"
_NEUTRAL = Path(__file__).resolve().parent / "_dotenv_neutral"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.tests.isolated_env import baseline_environ, isolated_environ  # noqa: E402

_FILE = (
    "EXECUTION_PAPER_ENABLED=1\n"
    "EXECUTION_DAILY_LOSS_USD=200\n"
    "EXECUTION_RISK_ENABLED=1\n"
    "PM_ENABLED=1\n"
)
_SKIP = {".git", ".venv", "__pycache__", ".env"}


def _ignore(path: str, names: list[str]) -> set[str]:
    del path
    return {name for name in names if name in _SKIP or name.endswith(".pyc")}


def _fingerprint(path: Path) -> tuple[int, int, int, bool] | None:
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    return (info.st_ino, info.st_mtime_ns, info.st_size, stat.S_ISLNK(info.st_mode))


def _child(root: Path) -> str:
    crawler = root / "crawler"
    return (
        "import importlib.util, sys\n"
        f"sys.path[:0] = [{str(root)!r}, {str(crawler)!r}]\n"
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


def _public(proc: subprocess.CompletedProcess[str]) -> str:
    kept: list[str] = []
    for line in f"{proc.stdout}\n{proc.stderr}".splitlines():
        if line.startswith(("diag.", "paper=", "loss=", "Traceback", "File ", "  File")):
            kept.append(line)
    return "\n".join(kept)


class OperatorDotenvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._live = {path: _fingerprint(path) for path in _LIVE_ENVS}

    def tearDown(self) -> None:
        for path, before in self._live.items():
            self.assertEqual(_fingerprint(path), before, path)

    def _run(
        self, root: Path, env: dict[str, str], *, cwd: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _child(root)],
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )

    def _expect(
        self, proc: subprocess.CompletedProcess[str], paper: str, loss: str
    ) -> None:
        shown = f"\n{proc.stdout}"
        self.assertEqual(proc.returncode, 0, _public(proc))
        self.assertIn(f"\npaper={paper}\n", shown, _public(proc))
        self.assertIn(f"\nloss={loss}", shown, _public(proc))

    def test_crawler_dotenv_reloads_after_a_clean_child_unless_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dotenv-fixture-") as tmp:
            tree = Path(tmp) / "tree"
            shutil.copytree(ROOT, tree, symlinks=True, ignore=_ignore)
            self.assertFalse((tree / ".env").exists())
            self.assertFalse((tree / "crawler" / ".env").exists())
            cwd = Path(tmp) / "cwd"
            cwd.mkdir()
            (cwd / ".env").write_text(_FILE, encoding="utf-8")
            bare = baseline_environ()
            bare["PYTHONPATH"] = os.pathsep.join(
                [str(_NEUTRAL), str(tree), str(tree / "crawler")]
            )
            cwd_only = self._run(tree, bare, cwd=cwd)
            self._expect(cwd_only, "False", "None")

            (tree / "crawler" / ".env").write_text(_FILE, encoding="utf-8")
            leaked = self._run(tree, bare, cwd=cwd)
            self._expect(leaked, "True", "200")

            guarded = isolated_environ(
                {"PYTHONPATH": os.pathsep.join([str(tree), str(tree / "crawler")])}
            )
            blocked = self._run(tree, guarded, cwd=cwd)
            self._expect(blocked, "False", "None")

            explicit = isolated_environ(
                {
                    "PYTHONPATH": os.pathsep.join([str(tree), str(tree / "crawler")]),
                    "EXECUTION_DAILY_LOSS_USD": "250",
                    "EXECUTION_RISK_ENABLED": "1",
                }
            )
            injected = self._run(tree, explicit, cwd=cwd)
            self._expect(injected, "False", "250")
