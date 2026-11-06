"""Operator dotenv is reapplied on import unless the test guard is installed.

``crawler/config.py`` loads ``crawler/.env`` (and ``decision_engine.config``
resolves to that same file in this tree). A ``.env`` in the process cwd is
not that file. Production load behavior is unchanged: this test only runs
in children.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.tests.isolated_env import baseline_environ, isolated_environ  # noqa: E402

_FILE = """
EXECUTION_PAPER_ENABLED=1
EXECUTION_DAILY_LOSS_USD=200
EXECUTION_RISK_ENABLED=1
PM_ENABLED=1
""".lstrip()

_CHILD = (
    "import sys\n"
    f"sys.path[:0] = [{str(ROOT)!r}, {str(CRAWLER)!r}]\n"
    "import importlib\n"
    "import paper_trade\n"
    "import execution.config as cfg\n"
    "importlib.reload(cfg)\n"
    "from execution.gateway import _env_bool\n"
    "print('paper=' + str(_env_bool('EXECUTION_PAPER_ENABLED', False)))\n"
    "print('loss=' + str(cfg.EXECUTION_DAILY_LOSS_USD))\n"
)


class OperatorDotenvFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = CRAWLER / ".env"
        self._prior = self.path.read_bytes() if self.path.is_file() else None
        self._parent_paper = os.environ.get("EXECUTION_PAPER_ENABLED")
        self._parent_loss = os.environ.get("EXECUTION_DAILY_LOSS_USD")

    def tearDown(self) -> None:
        if self._prior is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_bytes(self._prior)
        if self._parent_paper is None:
            os.environ.pop("EXECUTION_PAPER_ENABLED", None)
        else:
            os.environ["EXECUTION_PAPER_ENABLED"] = self._parent_paper
        if self._parent_loss is None:
            os.environ.pop("EXECUTION_DAILY_LOSS_USD", None)
        else:
            os.environ["EXECUTION_DAILY_LOSS_USD"] = self._parent_loss

    def _run(self, env: dict[str, str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _CHILD],
            env=env,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_crawler_dotenv_reloads_after_a_clean_child_unless_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd_file = Path(tmp) / ".env"
            cwd_file.write_text(_FILE, encoding="utf-8")
            self.path.unlink(missing_ok=True)
            bare = baseline_environ()
            bare["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(CRAWLER)])
            cwd_only = self._run(bare, cwd=Path(tmp))
            self.assertEqual(cwd_only.returncode, 0, cwd_only.stderr)
            self.assertIn("paper=False", cwd_only.stdout)
            self.assertIn("loss=None", cwd_only.stdout)

        self.path.write_text(_FILE, encoding="utf-8")
        leaked = self._run(bare)
        self.assertEqual(leaked.returncode, 0, leaked.stderr)
        self.assertIn("paper=True", leaked.stdout)
        self.assertIn("loss=200", leaked.stdout)

        guarded = isolated_environ(
            {"PYTHONPATH": os.pathsep.join([str(ROOT), str(CRAWLER)])}
        )
        blocked = self._run(guarded)
        self.assertEqual(blocked.returncode, 0, blocked.stderr)
        self.assertIn("paper=False", blocked.stdout)
        self.assertIn("loss=None", blocked.stdout)

        explicit = isolated_environ(
            {
                "PYTHONPATH": os.pathsep.join([str(ROOT), str(CRAWLER)]),
                "EXECUTION_DAILY_LOSS_USD": "250",
                "EXECUTION_RISK_ENABLED": "1",
            }
        )
        injected = self._run(explicit)
        self.assertEqual(injected.returncode, 0, injected.stderr)
        self.assertIn("paper=False", injected.stdout)
        self.assertIn("loss=250", injected.stdout)
        self.assertEqual(os.environ.get("EXECUTION_PAPER_ENABLED"), self._parent_paper)
        self.assertEqual(os.environ.get("EXECUTION_DAILY_LOSS_USD"), self._parent_loss)
