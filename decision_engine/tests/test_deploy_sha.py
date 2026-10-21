"""run.read_deploy_sha reads decision_engine/DEPLOY_SHA (startup log only)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from decision_engine.run import read_deploy_sha


class DeployShaTests(unittest.TestCase):
    def test_reads_sha(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "DEPLOY_SHA"
            p.write_text("9487cbba22a245c31c4237ebb40e4508abfed09c\n")
            self.assertEqual(read_deploy_sha(p), "9487cbba22a245c31c4237ebb40e4508abfed09c")

    def test_missing_or_empty_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "DEPLOY_SHA"
            self.assertEqual(read_deploy_sha(p), "unknown")
            p.write_text("  \n")
            self.assertEqual(read_deploy_sha(p), "unknown")


if __name__ == "__main__":
    unittest.main()
