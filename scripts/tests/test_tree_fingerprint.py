"""scripts/tree_fingerprint.py: deterministic, scoped, and equal to decision_engine.run.tree_sha256."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tree_fingerprint", ROOT / "scripts" / "tree_fingerprint.py")
tf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tf)


def _pkg(d: Path) -> Path:
    pkg = d / "pkg"
    for sub in ("core", "tests", "__pycache__"):
        (pkg / sub).mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "core" / "a.py").write_text("a = 2\n")
    (pkg / "tests" / "t.py").write_text("t\n")
    (pkg / "__pycache__" / "c.py").write_text("c\n")
    (pkg / "notes.txt").write_text("n\n")
    return pkg


class TreeFingerprintTests(unittest.TestCase):
    def test_known_vector(self):
        with tempfile.TemporaryDirectory() as d:
            pkg = _pkg(Path(d))
            h = hashlib.sha256()
            h.update(b"__init__.py\0x = 1\n\0")
            h.update(b"core/a.py\0a = 2\n\0")
            self.assertEqual(tf.tree_sha256(pkg), h.hexdigest())
            top = hashlib.sha256(b"__init__.py\0x = 1\n\0").hexdigest()
            self.assertEqual(tf.tree_sha256(pkg, top_level_only=True), top)

    def test_matches_decision_engine_run(self):
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "crawler"))
        try:
            from decision_engine import run as de_run
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"decision_engine.run not importable: {exc}")
        if not hasattr(de_run, "tree_sha256"):
            self.skipTest("decision_engine.run.tree_sha256 not on this branch yet (PR #127)")
        pkg = ROOT / "decision_engine"
        self.assertEqual(tf.tree_sha256(pkg), de_run.tree_sha256(pkg))
        with tempfile.TemporaryDirectory() as d:
            p = _pkg(Path(d))
            self.assertEqual(tf.tree_sha256(p), de_run.tree_sha256(p))


if __name__ == "__main__":
    unittest.main()
