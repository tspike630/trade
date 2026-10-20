"""Main paper book open-pause flag: buys rejected with receipt, others pass."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import execution.gateway as gateway_mod
from execution.gateway import PAPER_OPEN_PAUSED_REASON, ExecutionGateway, paper_open_paused
from execution.tests.test_purpose_gate import (
    PURPOSE_LIVE_CANDIDATE,
    _DecisionDB,
    _errors,
    _receipt,
    _RecordingExecutor,
)


class PaperOpenPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.flag = self.root / "PAPER_OPEN_PAUSED"
        self.env = mock.patch.dict(
            os.environ,
            {"EXECUTION_PAPER_OPEN_PAUSE_FLAG": str(self.flag), "EXECUTION_PAPER_OPEN_PAUSED": ""},
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_flag_toggle(self):
        self.assertFalse(paper_open_paused())
        self.flag.write_text("1")
        self.assertTrue(paper_open_paused())
        self.flag.unlink()
        with mock.patch.dict(os.environ, {"EXECUTION_PAPER_OPEN_PAUSED": "1"}):
            self.assertTrue(paper_open_paused())

    def _run(self):
        db = _DecisionDB(self.root / "decision.db", purpose_column=True)
        db.seed_price()
        buy = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE)
        sell = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, action="sell")
        db.close()
        paper = _RecordingExecutor()
        prev = gateway_mod.EXECUTION_RISK_ENABLED
        gateway_mod.EXECUTION_RISK_ENABLED = False
        try:
            with ExecutionGateway(decision_db=db.path, ledger_path=self.root / "exec.db",
                                  paper_enabled=True, paper_db=self.root / "paper.db",
                                  paper_executor=paper, pm_enabled=False) as gw:
                gw.process_pending(since_id=0)
                rows = {int(r["decision_log_id"]): r for r in gw.ledger.recent(limit=20)}
                cursor = gw.ledger.get_last_processed_id()
        finally:
            gateway_mod.EXECUTION_RISK_ENABLED = prev
        return paper, rows, buy, sell, cursor

    def test_paused_buy_rejected_with_receipt(self):
        self.flag.write_text("paused")
        paper, rows, buy, sell, cursor = self._run()
        b = rows[buy]
        self.assertEqual(b["status"], "rejected")
        self.assertEqual(b["executed"], 0)
        self.assertEqual(_errors(b), [PAPER_OPEN_PAUSED_REASON])
        self.assertIn("paused", _receipt(b)["note"])
        self.assertEqual([c["decision_log_id"] for c in paper.calls], [sell])
        self.assertEqual(cursor, sell)

    def test_unpaused_buy_executes(self):
        paper, rows, buy, sell, _ = self._run()
        self.assertIn(buy, [c["decision_log_id"] for c in paper.calls])


if __name__ == "__main__":
    unittest.main()
