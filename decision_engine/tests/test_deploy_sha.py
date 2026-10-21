"""Startup deploy identity: DEPLOY_SHA (sha + optional expected tree) and tree_sha256."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from decision_engine.run import log_deploy_identity, read_deploy_info, read_deploy_sha, tree_sha256

SHA = "9487cbba22a245c31c4237ebb40e4508abfed09c"


def _pkg(d: Path) -> Path:
    pkg = d / "pkg"
    (pkg / "core").mkdir(parents=True)
    (pkg / "tests").mkdir()
    (pkg / "__pycache__").mkdir()
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "core" / "a.py").write_text("a = 2\n")
    (pkg / "tests" / "t.py").write_text("t = 3\n")
    (pkg / "__pycache__" / "c.py").write_text("c\n")
    (pkg / "notes.txt").write_text("ignored\n")
    return pkg


class DeployShaTests(unittest.TestCase):
    def test_reads_sha(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "DEPLOY_SHA"
            p.write_text(SHA + "\n")
            self.assertEqual(read_deploy_sha(p), SHA)
            self.assertEqual(read_deploy_info(p), (SHA, None))

    def test_missing_or_empty_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "DEPLOY_SHA"
            self.assertEqual(read_deploy_sha(p), "unknown")
            with self.assertLogs("dexscreener.decision_engine.run", "INFO") as cm:
                self.assertFalse(log_deploy_identity(p, _pkg(Path(d))))
            self.assertTrue(any("deploy_sha=unknown tree_sha256=" in m for m in cm.output))
            p.write_text("  \n")
            self.assertEqual(read_deploy_sha(p), "unknown")

    def test_fingerprint_deterministic_and_scoped(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = _pkg(Path(d))
            a = tree_sha256(pkg)
            self.assertEqual(a, tree_sha256(pkg))
            self.assertEqual(len(a), 64)
            (pkg / "tests" / "t.py").write_text("changed\n")
            (pkg / "__pycache__" / "c.py").write_text("changed\n")
            (pkg / "notes.txt").write_text("changed\n")
            self.assertEqual(a, tree_sha256(pkg))
            (pkg / "core" / "a.py").write_text("a = 3\n")
            self.assertNotEqual(a, tree_sha256(pkg))

    def test_match_and_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = _pkg(Path(d))
            p = Path(d) / "DEPLOY_SHA"
            p.write_text(f"{SHA}\n{tree_sha256(pkg)}\n")
            self.assertFalse(log_deploy_identity(p, pkg))
            p.write_text(f"{SHA}\n{'0' * 64}\n")
            with self.assertLogs("dexscreener.decision_engine.run", "WARNING") as cm:
                self.assertTrue(log_deploy_identity(p, pkg))
            self.assertTrue(any("deploy_sha_mismatch" in m for m in cm.output))


if __name__ == "__main__":
    unittest.main()
