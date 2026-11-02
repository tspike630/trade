"""Peer mode: --accept-purpose / accept_purposes allowlist for isolated PaperLedger.

Run from the repo root:

    python -m unittest execution.tests.test_peer_accept_purpose
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

import execution.gateway as gateway_mod  # noqa: E402
from execution.gateway import (  # noqa: E402
    PURPOSE_LIVE_CANDIDATE,
    PURPOSE_REJECT_INVALID,
    PURPOSE_REJECT_SAMPLE,
    PURPOSE_REJECT_SHADOW,
    ExecutionGateway,
    purpose_reject_reason,
)
from execution.run import (  # noqa: E402
    _parse_accept_purposes,
    _peer_isolation_guard,
)
from execution.tests.test_purpose_gate import (  # noqa: E402
    _DecisionDB,
    _RecordingExecutor,
    _errors,
)


class PurposeRejectPeerTests(unittest.TestCase):
    def test_default_still_live_candidate_only(self) -> None:
        self.assertIsNone(purpose_reject_reason(PURPOSE_LIVE_CANDIDATE))
        self.assertEqual(
            purpose_reject_reason("race_rsi_bounce"), PURPOSE_REJECT_INVALID
        )
        self.assertEqual(
            purpose_reject_reason("shadow_entry_ih_ge1h"), PURPOSE_REJECT_SHADOW
        )

    def test_peer_allowlist_only_listed_purpose(self) -> None:
        accept = frozenset({"race_rsi_bounce"})
        self.assertIsNone(
            purpose_reject_reason("race_rsi_bounce", accept_purposes=accept)
        )
        # live_candidate does NOT auto-pass in peer mode
        self.assertEqual(
            purpose_reject_reason(PURPOSE_LIVE_CANDIDATE, accept_purposes=accept),
            PURPOSE_REJECT_INVALID,
        )
        self.assertEqual(
            purpose_reject_reason("race_macd_cross", accept_purposes=accept),
            PURPOSE_REJECT_INVALID,
        )
        self.assertEqual(
            purpose_reject_reason("sample", accept_purposes=accept),
            PURPOSE_REJECT_SAMPLE,
        )
        self.assertEqual(
            purpose_reject_reason("shadow_entry_ih_ge1h", accept_purposes=accept),
            PURPOSE_REJECT_SHADOW,
        )

    def test_peer_shadow_purpose_allowed_when_listed(self) -> None:
        accept = frozenset({"shadow_entry_ih_ge1h"})
        self.assertIsNone(
            purpose_reject_reason("shadow_entry_ih_ge1h", accept_purposes=accept)
        )
        self.assertEqual(
            purpose_reject_reason("shadow_010", accept_purposes=accept),
            PURPOSE_REJECT_SHADOW,
        )


class PeerIsolationGuardTests(unittest.TestCase):
    def test_parse_accept_purposes_cli_and_env(self) -> None:
        self.assertIsNone(_parse_accept_purposes(None))
        self.assertEqual(
            _parse_accept_purposes(["race_rsi_bounce"]),
            frozenset({"race_rsi_bounce"}),
        )
        with mock.patch.dict(
            os.environ, {"EXECUTION_ACCEPT_PURPOSE": "race_a, race_b"}
        ):
            self.assertEqual(
                _parse_accept_purposes(None), frozenset({"race_a", "race_b"})
            )
        # CLI wins over env
        with mock.patch.dict(
            os.environ, {"EXECUTION_ACCEPT_PURPOSE": "race_env"}
        ):
            self.assertEqual(
                _parse_accept_purposes(["race_cli"]),
                frozenset({"race_cli"}),
            )

    def test_peer_guard_rejects_shared_ledger_and_main_paper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_ledger = root / "execution_ledger.db"
            peer_ledger = root / "execution_ledger_race_rsi_bounce.db"
            main_paper = root / "paper_trading.db"
            peer_paper = root / "paper_eq_rsi_bounce.db"
            accept = frozenset({"race_rsi_bounce"})
            with mock.patch(
                "execution.run._default_main_ledger_path", return_value=main_ledger
            ):
                self.assertIn(
                    "isolated",
                    _peer_isolation_guard(
                        accept_purposes=accept,
                        ledger_path=main_ledger,
                        paper_db=peer_paper,
                        paper_enabled=True,
                    )
                    or "",
                )
                self.assertIn(
                    "paper_trading.db",
                    _peer_isolation_guard(
                        accept_purposes=accept,
                        ledger_path=peer_ledger,
                        paper_db=main_paper,
                        paper_enabled=True,
                    )
                    or "",
                )
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=accept,
                        ledger_path=peer_ledger,
                        paper_db=peer_paper,
                        paper_enabled=True,
                    )
                )
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=None,
                        ledger_path=main_ledger,
                        paper_db=main_paper,
                        paper_enabled=True,
                    )
                )


class PeerGatewayTests(unittest.TestCase):
    def test_gateway_fills_accepted_race_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            db.seed_price()
            race_id = db.add(
                column_purpose="race_rsi_bounce",
                raw_purpose="race_rsi_bounce",
            )
            live_id = db.add(
                column_purpose=PURPOSE_LIVE_CANDIDATE,
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
            )
            other_id = db.add(
                column_purpose="race_macd_cross",
                raw_purpose="race_macd_cross",
            )
            db.close()
            paper = _RecordingExecutor()
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = False
            try:
                with ExecutionGateway(
                    decision_db=db.path,
                    ledger_path=root / "exec_peer.db",
                    paper_enabled=True,
                    paper_db=root / "paper_eq_rsi_bounce.db",
                    paper_executor=paper,
                    pm_enabled=False,
                    accept_purposes=frozenset({"race_rsi_bounce"}),
                ) as gw:
                    stats = gw.process_pending(since_id=0)
                    rows = {
                        int(r["decision_log_id"]): r for r in gw.ledger.recent(limit=20)
                    }
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
            self.assertEqual(stats.processed, 3)
            self.assertEqual(len(paper.calls), 1)
            self.assertEqual(paper.calls[0]["decision_log_id"], race_id)
            self.assertEqual(rows[race_id]["executed"], 1)
            self.assertEqual(rows[live_id]["executed"], 0)
            self.assertEqual(_errors(rows[live_id]), [PURPOSE_REJECT_INVALID])
            self.assertEqual(rows[other_id]["executed"], 0)
            self.assertEqual(_errors(rows[other_id]), [PURPOSE_REJECT_INVALID])

    def test_main_gateway_still_rejects_race(self) -> None:
        """Default gateway (no accept_purposes) unchanged: race_* rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            race_id = db.add(
                column_purpose="race_rsi_bounce",
                raw_purpose="race_rsi_bounce",
            )
            live_id = db.add(
                column_purpose=PURPOSE_LIVE_CANDIDATE,
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
            )
            db.close()
            paper = _RecordingExecutor()
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = False
            try:
                with ExecutionGateway(
                    decision_db=db.path,
                    ledger_path=root / "exec_main.db",
                    paper_enabled=True,
                    paper_executor=paper,
                    pm_enabled=False,
                ) as gw:
                    self.assertIsNone(gw.accept_purposes)
                    stats = gw.process_pending(since_id=0)
                    rows = {
                        int(r["decision_log_id"]): r for r in gw.ledger.recent(limit=20)
                    }
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
            self.assertEqual(stats.processed, 2)
            self.assertEqual(_errors(rows[race_id]), [PURPOSE_REJECT_INVALID])
            self.assertEqual(rows[race_id]["executed"], 0)
            self.assertEqual(len(paper.calls), 1)
            self.assertEqual(paper.calls[0]["decision_log_id"], live_id)


if __name__ == "__main__":
    unittest.main()
