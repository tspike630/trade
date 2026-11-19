"""Cross-margin buys share dexscreener.db without being spot-arm rows."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_LIVE_BNB_CROSS_MARGIN,
    PURPOSE_SHADOW_010,
    SignalBus,
)
from decision_engine.run import main as decision_main
from execution.executors.binance_spot_gate import REPLAY_FLOOR_DECISION_ID
from execution.run import main as execution_main


class CrossMarginPurposeIsolationTests(unittest.TestCase):
    def test_emitter_stamps_the_dedicated_purpose_on_the_shared_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            indicator = root / "indicators" / "dexscreener.db"
            decision = root / "shared" / "dexscreener.db"
            indicator.parent.mkdir()
            decision.parent.mkdir()
            route = root / "route.json"
            route.write_text(
                json.dumps(
                    {
                        "account_domain": "binance_cross_margin",
                        "spot_book_full": False,
                        "execution_mode": "offline_fixture",
                    }
                ),
                encoding="utf-8",
            )
            refused = decision_main(
                [
                    "--once",
                    "--db",
                    str(indicator),
                    "--output-db",
                    str(root / "not-shared.db"),
                    "--purpose",
                    PURPOSE_LIVE_BNB_CROSS_MARGIN,
                    "--account-route-json",
                    str(route),
                    "--strategy",
                    "jev_strength_v1",
                ]
            )
            self.assertEqual(refused, 2)
            shadow = decision_main(
                [
                    "--once",
                    "--db",
                    str(indicator),
                    "--output-db",
                    str(root / "shadow-out.db"),
                    "--purpose",
                    PURPOSE_LIVE_BNB_BOLL_FADE,
                    "--account-route-json",
                    str(route),
                    "--strategy",
                    "jev_strength_v1",
                ]
            )
            self.assertEqual(shadow, 2)
            kept = decision_main(
                [
                    "--once",
                    "--db",
                    str(indicator),
                    "--output-db",
                    str(decision),
                    "--purpose",
                    PURPOSE_LIVE_BNB_CROSS_MARGIN,
                    "--account-route-json",
                    str(route),
                    "--strategy",
                    "jev_strength_v1",
                ]
            )
            self.assertEqual(kept, 0)
            bus = SignalBus(decision, dry_run=False, purpose=PURPOSE_LIVE_BNB_CROSS_MARGIN)
            try:
                published = bus.publish(
                    {
                        "strategy_id": "probe_buy",
                        "action": "buy",
                        "chain_id": "binance",
                        "token_address": "BTCUSDT",
                        "pair_address": "BTCUSDT",
                        "size_pct": 0.1,
                        "risk_passed": True,
                        "reason": "probe",
                        **json.loads(route.read_text(encoding="utf-8")),
                    }
                )
            finally:
                bus.close()
            self.assertIsNotNone(published)
            self.assertEqual(published["purpose"], PURPOSE_LIVE_BNB_CROSS_MARGIN)
            conn = sqlite3.connect(decision)
            row = conn.execute(
                "SELECT purpose, raw_json FROM decision_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], PURPOSE_LIVE_BNB_CROSS_MARGIN)
            extras = json.loads(row[1])["extras"]
            self.assertEqual(extras["account_domain"], "binance_cross_margin")
            self.assertNotEqual(row[0], PURPOSE_SHADOW_010)

    def test_spot_arms_do_not_claim_cross_margin_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decision = root / "dexscreener.db"
            fade_ledger = root / "execution_ledger_live_bnb_boll_fade.db"
            classic_ledger = root / "execution_ledger_live_bnb_boll_classic.db"
            margin_ledger = root / "cross-margin-exec.db"
            self._insert(
                decision,
                REPLAY_FLOOR_DECISION_ID + 1,
                PURPOSE_LIVE_BNB_CROSS_MARGIN,
                {"account_domain": "not-a-venue", "execution_mode": "live"},
            )
            self._insert(
                decision,
                REPLAY_FLOOR_DECISION_ID + 2,
                PURPOSE_LIVE_BNB_BOLL_FADE,
                {},
            )
            fade = self._once(decision, fade_ledger, PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(fade["processed"], 1)
            self.assertEqual(fade["sample"][0]["decision_log_id"], REPLAY_FLOOR_DECISION_ID + 2)
            self.assertNotIn(
                REPLAY_FLOOR_DECISION_ID + 1,
                self._claimed(fade_ledger),
            )
            classic = self._once(decision, classic_ledger, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertNotIn(REPLAY_FLOOR_DECISION_ID + 1, self._claimed(classic_ledger))
            self.assertNotEqual(
                classic["sample"][0]["decision_log_id"] if classic["sample"] else None,
                REPLAY_FLOOR_DECISION_ID + 1,
            )
            margin = self._once(decision, margin_ledger, PURPOSE_LIVE_BNB_CROSS_MARGIN)
            self.assertEqual(margin["processed"], 1)
            sample = margin["sample"][0]
            self.assertEqual(sample["decision_log_id"], REPLAY_FLOOR_DECISION_ID + 1)
            self.assertEqual(sample["exec_result"]["reason"], "account_domain_invalid")
            self.assertFalse(sample["exec_result"]["spot_called"])
            self.assertFalse(sample["exec_result"]["margin_called"])
            self.assertNotEqual(sample["executor"], "binance_spot")

    def _insert(self, db: Path, row_id: int, purpose: str, extras: dict[str, Any]) -> None:
        raw = {"pair_address": "BTCUSDT", "purpose": purpose, "extras": extras}
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                risk_passed INTEGER, mode TEXT, purpose TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO decision_log VALUES (
                ?, '2026-11-19T00:00:00Z', 'probe', 'buy', 'binance', 'BTCUSDT',
                0.1, 'probe', 1, 'live_stub', ?, ?
            )
            """,
            (row_id, purpose, json.dumps(raw)),
        )
        conn.commit()
        conn.close()

    def _once(self, decision: Path, ledger: Path, purpose: str) -> dict[str, Any]:
        import contextlib
        import io

        from execution.run import main as execution_main

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = execution_main(
                [
                    "--once",
                    "--json",
                    "--db",
                    str(decision),
                    "--ledger",
                    str(ledger),
                    "--accept-purpose",
                    purpose,
                ]
            )
        self.assertEqual(code, 0, buf.getvalue())
        lines = [line for line in buf.getvalue().splitlines() if line.startswith("{")]
        return json.loads(lines[-1])

    def _claimed(self, ledger: Path) -> set[int]:
        if not ledger.exists():
            return set()
        conn = sqlite3.connect(ledger)
        try:
            rows = conn.execute("SELECT decision_log_id FROM execution_log").fetchall()
        except sqlite3.Error:
            return set()
        finally:
            conn.close()
        return {int(row[0]) for row in rows}


if __name__ == "__main__":
    unittest.main()
