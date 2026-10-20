import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution.gateway import PURPOSE_REJECT_SHADOW, purpose_reject_reason
from execution.run import _shadow_isolation_guard


def _mk(path: Path, purposes):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, purpose TEXT, raw_json TEXT)")
    for p in purposes:
        c.execute("INSERT INTO decision_log (purpose) VALUES (?)", (p,))
    c.commit()
    c.close()


class ShadowIsolationGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"EXECUTION_ALLOW_NONSTANDARD_DECISION_DB": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_main_db_ok(self):
        db = self.d / "dexscreener.db"
        _mk(db, ["live_candidate", "sample"])
        self.assertIsNone(_shadow_isolation_guard(str(db)))

    def test_nonstandard_name_refused(self):
        db = self.d / "decision_shadow_entry_010.db"
        _mk(db, ["live_candidate"])
        self.assertIn("not dexscreener.db", _shadow_isolation_guard(str(db)))

    def test_nonstandard_name_allowed_by_env(self):
        db = self.d / "smoke.db"
        _mk(db, ["live_candidate"])
        with mock.patch.dict(os.environ, {"EXECUTION_ALLOW_NONSTANDARD_DECISION_DB": "1"}):
            self.assertIsNone(_shadow_isolation_guard(str(db)))

    def test_shadow_rows_refused_even_in_main_name(self):
        db = self.d / "dexscreener.db"
        _mk(db, ["live_candidate", "shadow_010"])
        self.assertIn("shadow_", _shadow_isolation_guard(str(db)))

    def test_row_gate_rejects_shadow(self):
        self.assertEqual(purpose_reject_reason("shadow_010"), PURPOSE_REJECT_SHADOW)
        self.assertIsNone(purpose_reject_reason("live_candidate"))


if __name__ == "__main__":
    unittest.main()
