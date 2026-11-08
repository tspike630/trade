"""Operator dotenv follows whichever ``config.py`` ``paper_trade`` imports.

The runtime process imports ``/workspace/.../paper_trade.py``, whose parent
is the repository root, so ``from config import`` loads the root ``config.py``
and that file's ``.env``. ``crawler/config.py`` is also on disk and is not the
module that runs. This fixture builds that flat layout in a temporary tree,
with a synthetic ``.env`` only there. The child drops the live repository
root and ``crawler/`` from ``sys.path`` before the import. It does not read,
delete, or rewrite a live ``.env``. Production load behavior is unchanged.
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
    """Import ``paper_trade`` from ``root``, never from the live checkout."""
    live = {str(ROOT.resolve()), str(CRAWLER.resolve())}
    return (
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        f"live = {live!r}\n"
        f"temp_root = Path({str(root)!r})\n"
        "kept = []\n"
        "for entry in sys.path:\n"
        "    if not entry:\n"
        "        continue\n"
        "    try:\n"
        "        resolved = str(Path(entry).resolve())\n"
        "    except OSError:\n"
        "        continue\n"
        "    if resolved in live:\n"
        "        continue\n"
        "    kept.append(entry)\n"
        "sys.path[:] = [str(temp_root), str(temp_root / 'crawler'), *kept]\n"
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


def _flat_runtime(tree: Path) -> None:
    """Root ``paper_trade.py`` and ``config.py`` win over ``crawler/`` copies."""
    shutil.copy2(CRAWLER / "paper_trade.py", tree / "paper_trade.py")
    shutil.copy2(CRAWLER / "config.py", tree / "config.py")


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
        self,
        proc: subprocess.CompletedProcess[str],
        tree: Path,
        paper: str,
        loss: str,
    ) -> None:
        shown = f"\n{proc.stdout}"
        self.assertEqual(proc.returncode, 0, _public(proc))
        self.assertIn(f"\npaper={paper}\n", shown, _public(proc))
        self.assertIn(f"\nloss={loss}", shown, _public(proc))
        config_path = str((tree / "config.py").resolve())
        paper_path = str((tree / "paper_trade.py").resolve())
        self.assertIn(f"diag.module.config={config_path}", proc.stdout, _public(proc))
        self.assertIn(f"diag.module.paper_trade={paper_path}", proc.stdout, _public(proc))
        self.assertNotIn(
            f"diag.module.config={ROOT.resolve() / 'config.py'}", proc.stdout
        )
        self.assertNotIn(
            f"diag.load.path={(ROOT / '.env').resolve()}", proc.stdout
        )
        live = {str(ROOT.resolve()), str(CRAWLER.resolve())}
        for line in proc.stdout.splitlines():
            if not line.startswith("diag.path["):
                continue
            entry = line.split("=", 1)[1]
            self.assertTrue(entry, line)
            self.assertNotIn(str(Path(entry).resolve()), live, line)

    def test_crawler_dotenv_reloads_after_a_clean_child_unless_guarded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dotenv-fixture-") as tmp:
            tree = Path(tmp).resolve() / "tree"
            shutil.copytree(ROOT, tree, symlinks=True, ignore=_ignore)
            _flat_runtime(tree)
            self.assertTrue((tree / "config.py").is_file())
            self.assertTrue((tree / "crawler" / "config.py").is_file())
            self.assertFalse((tree / ".env").exists())
            self.assertFalse((tree / "crawler" / ".env").exists())
            bare = baseline_environ()
            bare["PYTHONPATH"] = os.pathsep.join(
                [str(_NEUTRAL), str(tree), str(tree / "crawler")]
            )
            # cwd is the temp root, matching path[0] on the runtime. The live
            # checkout is not on sys.path and has no synthetic file.
            clean = self._run(tree, bare, cwd=tree)
            self._expect(clean, tree, "False", "None")
            self.assertIn(f"diag.load.path={tree / '.env'}", clean.stdout)
            self.assertIn("diag.load.exists=False", clean.stdout)

            (tree / ".env").write_text(_FILE, encoding="utf-8")
            leaked = self._run(tree, bare, cwd=tree)
            self._expect(leaked, tree, "True", "200")
            self.assertIn(f"diag.load.path={tree / '.env'}", leaked.stdout)
            self.assertIn("diag.load.exists=True", leaked.stdout)

            guarded = isolated_environ(
                {"PYTHONPATH": os.pathsep.join([str(tree), str(tree / "crawler")])}
            )
            blocked = self._run(tree, guarded, cwd=tree)
            self._expect(blocked, tree, "False", "None")

            explicit = isolated_environ(
                {
                    "PYTHONPATH": os.pathsep.join([str(tree), str(tree / "crawler")]),
                    "EXECUTION_DAILY_LOSS_USD": "250",
                    "EXECUTION_RISK_ENABLED": "1",
                }
            )
            injected = self._run(tree, explicit, cwd=tree)
            self._expect(injected, tree, "False", "250")
