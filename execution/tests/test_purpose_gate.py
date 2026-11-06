"""P2-38: execution gateway rejects non-live_candidate purposes. Temp DBs only.

Run from the repo root:

    python -m unittest execution.tests.test_purpose_gate
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

import execution.gateway as gateway_mod  # noqa: E402
from execution.gateway import (  # noqa: E402
    PURPOSE_LIVE_CANDIDATE,
    PURPOSE_REJECT_INVALID,
    PURPOSE_REJECT_MISSING,
    PURPOSE_REJECT_SAMPLE,
    ExecutionGateway,
    parse_decision_row,
    purpose_reject_reason,
)
from execution.order_events import event_chain  # noqa: E402
from paper_trade import PaperLedger  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Omit:
    """Sentinel: do not write this purpose slot."""


OMIT = _Omit()

_DECISION_DDL = """
CREATE TABLE pair_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    token_address TEXT,
    recorded_at TEXT NOT NULL,
    price_usd REAL,
    volume_h24 REAL,
    volume_h1 REAL,
    liquidity_usd REAL,
    txns_h24 INTEGER,
    tier TEXT
);
CREATE TABLE pairs (
    chain_id TEXT NOT NULL,
    pair_address TEXT NOT NULL,
    token_address TEXT,
    raw_json TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chain_id, pair_address)
);
CREATE TABLE decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    strategy_id TEXT,
    action TEXT,
    chain_id TEXT,
    token_address TEXT,
    size_pct REAL,
    reason TEXT,
    risk_passed INTEGER,
    mode TEXT,
    raw_json TEXT
);
"""


class _DecisionDB:
    def __init__(self, path: Path, *, purpose_column: bool) -> None:
        self.path = path
        self.purpose_column = purpose_column
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_DECISION_DDL)
        if purpose_column:
            self.conn.execute("ALTER TABLE decision_log ADD COLUMN purpose TEXT")
        self.conn.commit()

    def seed_price(self, price: float = 2.0) -> None:
        now = _iso(_now())
        self.conn.execute(
            """
            INSERT INTO pair_history (chain_id, pair_address, recorded_at, price_usd)
            VALUES ('solana', 'pair1', ?, ?)
            """,
            (now, price),
        )
        raw = {"priceUsd": price, "liquidity": {"usd": 1_000_000}}
        self.conn.execute(
            """
            INSERT INTO pairs (chain_id, pair_address, raw_json, source, updated_at)
            VALUES ('solana', 'pair1', ?, 'test', ?)
            """,
            (json.dumps(raw), now),
        )
        self.conn.commit()

    def add(
        self,
        *,
        column_purpose: Any = OMIT,
        raw_purpose: Any = OMIT,
        action: str = "buy",
        size_pct: float = 0.05,
        pair_address: str = "pair1",
        strategy_id: str = "jev_strength_v1",
        extras: dict[str, Any] | None = None,
    ) -> int:
        raw: dict[str, Any] = {"pair_address": pair_address, "extras": extras or {}}
        if raw_purpose is not OMIT:
            raw["purpose"] = raw_purpose
        columns = [
            "ts",
            "strategy_id",
            "action",
            "chain_id",
            "token_address",
            "size_pct",
            "reason",
            "mode",
            "raw_json",
        ]
        values: list[Any] = [
            _iso(_now()),
            strategy_id,
            action,
            "solana",
            "token1",
            size_pct,
            "test",
            "dry_run",
            json.dumps(raw),
        ]
        if self.purpose_column:
            columns.append("purpose")
            values.append(None if column_purpose is OMIT else column_purpose)
        placeholders = ", ".join("?" for _ in columns)
        cur = self.conn.execute(
            f"INSERT INTO decision_log ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()


class _RecordingExecutor:
    """Stand-in PaperExecutor. ``ledger`` is None so PM pass-through does not fill."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ledger = None

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(sig)
        return {"ok": True, "executor": "paper"}


def _receipt(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["receipt_json"] or "{}")


def _errors(row: dict[str, Any]) -> list[str]:
    return json.loads(row["validation_errors"] or "[]")


class PurposeParseTests(unittest.TestCase):
    def test_column_wins_and_strip_is_case_sensitive(self) -> None:
        sig = parse_decision_row(
            {
                "id": 1,
                "purpose": " live_candidate ",
                "raw_json": json.dumps(
                    {"pair_address": "pair1", "purpose": "sample", "extras": {}}
                ),
            }
        )
        self.assertEqual(sig["purpose"], PURPOSE_LIVE_CANDIDATE)
        self.assertIsNone(purpose_reject_reason(sig["purpose"]))
        self.assertEqual(sig["raw"]["purpose"], "sample")
        self.assertNotIn("purpose", sig["extras"])

        sample = parse_decision_row(
            {
                "id": 2,
                "raw_json": json.dumps({"pair_address": "pair1", "purpose": " sample "}),
            }
        )
        self.assertEqual(sample["purpose"], "sample")
        self.assertEqual(purpose_reject_reason(sample["purpose"]), PURPOSE_REJECT_SAMPLE)

        invalid = parse_decision_row(
            {
                "id": 3,
                "purpose": "SAMPLE",
                "raw_json": json.dumps({"pair_address": "pair1", "purpose": "sample"}),
            }
        )
        self.assertEqual(invalid["purpose"], "SAMPLE")
        self.assertEqual(purpose_reject_reason(invalid["purpose"]), PURPOSE_REJECT_INVALID)

        missing = parse_decision_row(
            {
                "id": 4,
                "purpose": None,
                "raw_json": json.dumps(
                    {"pair_address": "pair1", "purpose": PURPOSE_LIVE_CANDIDATE}
                ),
            }
        )
        self.assertIsNone(missing["purpose"])
        self.assertEqual(purpose_reject_reason(missing["purpose"]), PURPOSE_REJECT_MISSING)

        blank = parse_decision_row(
            {
                "id": 5,
                "purpose": "  ",
                "raw_json": json.dumps({"pair_address": "pair1"}),
            }
        )
        self.assertIsNone(blank["purpose"])
        self.assertEqual(purpose_reject_reason(""), PURPOSE_REJECT_MISSING)

    def test_extras_purpose_is_not_the_gate(self) -> None:
        sig = parse_decision_row(
            {
                "id": 1,
                "raw_json": json.dumps(
                    {
                        "pair_address": "pair1",
                        "purpose": PURPOSE_LIVE_CANDIDATE,
                        "extras": {"purpose": "sample"},
                    }
                ),
            }
        )
        self.assertEqual(sig["purpose"], PURPOSE_LIVE_CANDIDATE)
        self.assertEqual(sig["extras"]["purpose"], "sample")


class PurposeGateTests(unittest.TestCase):
    def _rows_by_decision(self, gw: ExecutionGateway) -> dict[int, dict[str, Any]]:
        return {int(r["decision_log_id"]): r for r in gw.ledger.recent(limit=20)}

    def test_live_candidate_reaches_validation_and_dry_run_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            ok_id = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, raw_purpose="sample")
            bad_id = db.add(
                column_purpose=" live_candidate ",
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
                pair_address="",
            )
            db.close()
            with ExecutionGateway(
                decision_db=db.path,
                ledger_path=root / "exec.db",
                paper_enabled=False,
                pm_enabled=False,
            ) as gw:
                stats = gw.process_pending(since_id=0)
                rows = self._rows_by_decision(gw)
                cursor = gw.ledger.get_last_processed_id()
                ok_events = event_chain(gw.ledger.path, int(rows[ok_id]["id"]))
            self.assertEqual(stats.processed, 2)
            self.assertEqual(stats.validation_ok, 1)
            self.assertEqual(stats.validation_fail, 1)
            self.assertEqual(stats.executed_ok, 0)
            self.assertEqual(cursor, bad_id)
            ok = rows[ok_id]
            self.assertEqual(ok["status"], "validated")
            self.assertEqual(ok["executed"], 0)
            self.assertEqual(ok["validation_ok"], 1)
            self.assertEqual(_receipt(ok)["status"], "dry_run_acked")
            self.assertEqual([e["event"] for e in ok_events], ["new", "validated"])
            bad = rows[bad_id]
            self.assertEqual(bad["status"], "rejected")
            self.assertEqual(bad["executed"], 0)
            self.assertIn("missing_required:pair_address", _errors(bad))
            self.assertNotIn(PURPOSE_REJECT_MISSING, _errors(bad))
            self.assertNotIn(PURPOSE_REJECT_SAMPLE, _errors(bad))
            self.assertEqual(_receipt(bad)["status"], "validation_failed")

    def test_sample_rejected_without_fill_even_if_paper_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            db.seed_price()
            sample_id = db.add(
                column_purpose=" sample ",
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
            )
            live_id = db.add(
                column_purpose=PURPOSE_LIVE_CANDIDATE,
                raw_purpose="sample",
            )
            db.close()
            paper = _RecordingExecutor()
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = False
            try:
                with ExecutionGateway(
                    decision_db=db.path,
                    ledger_path=root / "exec.db",
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    paper_executor=paper,
                    pm_enabled=False,
                ) as gw:
                    stats = gw.process_pending(since_id=0)
                    rows = self._rows_by_decision(gw)
                    sample_events = event_chain(gw.ledger.path, int(rows[sample_id]["id"]))
                    cursor = gw.ledger.get_last_processed_id()
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
            self.assertEqual(stats.processed, 2)
            self.assertEqual(cursor, live_id)
            sample = rows[sample_id]
            self.assertEqual(sample["status"], "rejected")
            self.assertEqual(sample["executed"], 0)
            self.assertEqual(sample["executor"], "step_a_noop")
            self.assertEqual(sample["validation_ok"], 0)
            self.assertEqual(_errors(sample), [PURPOSE_REJECT_SAMPLE])
            receipt = _receipt(sample)
            self.assertEqual(receipt["status"], "rejected")
            self.assertEqual(receipt["reason"], PURPOSE_REJECT_SAMPLE)
            self.assertEqual(receipt["error"], PURPOSE_REJECT_SAMPLE)
            self.assertEqual([e["event"] for e in sample_events], ["new", "rejected"])
            self.assertEqual(sample_events[1]["detail"]["stage"], "purpose")
            self.assertEqual(sample_events[1]["detail"]["error"], PURPOSE_REJECT_SAMPLE)
            self.assertEqual(len(paper.calls), 1)
            self.assertEqual(paper.calls[0]["decision_log_id"], live_id)
            self.assertEqual(rows[live_id]["executed"], 1)
            self.assertEqual(rows[live_id]["executor"], "paper")

    def test_sample_dry_run_still_writes_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            did = db.add(column_purpose="sample", raw_purpose="sample")
            db.close()
            with ExecutionGateway(
                decision_db=db.path,
                ledger_path=root / "exec.db",
                paper_enabled=False,
                pm_enabled=False,
            ) as gw:
                stats = gw.process_pending(since_id=0)
                row = gw.ledger.get_by_decision_log_id(did)
                cursor = gw.ledger.get_last_processed_id()
            self.assertEqual(stats.processed, 1)
            self.assertEqual(stats.validation_fail, 1)
            self.assertEqual(stats.executed_ok, 0)
            self.assertEqual(cursor, did)
            assert row is not None
            self.assertEqual(row["status"], "rejected")
            self.assertEqual(row["executed"], 0)
            self.assertEqual(_receipt(row)["reason"], PURPOSE_REJECT_SAMPLE)
            self.assertEqual(_receipt(row)["paper_enabled"], False)

    def test_missing_null_and_invalid_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            null_id = db.add(
                column_purpose=None,
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
            )
            blank_id = db.add(column_purpose="  ", raw_purpose=PURPOSE_LIVE_CANDIDATE)
            invalid_id = db.add(column_purpose="Live_Candidate", raw_purpose="sample")
            absent_raw_id = db.add(column_purpose=None, raw_purpose=OMIT)
            db.close()
            paper = _RecordingExecutor()
            with ExecutionGateway(
                decision_db=db.path,
                ledger_path=root / "exec.db",
                paper_enabled=True,
                paper_executor=paper,
                pm_enabled=False,
            ) as gw:
                stats = gw.process_pending(since_id=0)
                rows = self._rows_by_decision(gw)
                cursor = gw.ledger.get_last_processed_id()
            self.assertEqual(stats.processed, 4)
            self.assertEqual(stats.executed_ok, 0)
            self.assertEqual(paper.calls, [])
            self.assertEqual(cursor, absent_raw_id)
            self.assertEqual(_errors(rows[null_id]), [PURPOSE_REJECT_MISSING])
            self.assertEqual(_errors(rows[blank_id]), [PURPOSE_REJECT_MISSING])
            self.assertEqual(_errors(rows[invalid_id]), [PURPOSE_REJECT_INVALID])
            self.assertEqual(_errors(rows[absent_raw_id]), [PURPOSE_REJECT_MISSING])
            for did in (null_id, blank_id, invalid_id, absent_raw_id):
                self.assertEqual(rows[did]["status"], "rejected")
                self.assertEqual(rows[did]["executed"], 0)
                self.assertEqual(rows[did]["executor"], "step_a_noop")

    def test_purpose_only_in_raw_json_when_column_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=False)
            live_id = db.add(raw_purpose=PURPOSE_LIVE_CANDIDATE)
            sample_id = db.add(raw_purpose="sample")
            missing_id = db.add(raw_purpose=OMIT)
            db.close()
            with ExecutionGateway(
                decision_db=db.path,
                ledger_path=root / "exec.db",
                paper_enabled=False,
                pm_enabled=False,
            ) as gw:
                pending = gw.fetch_pending(0)
                self.assertEqual(len(pending), 3)
                self.assertNotIn("purpose", pending[0].keys())
                parsed = parse_decision_row(pending[0])
                self.assertEqual(parsed["purpose"], PURPOSE_LIVE_CANDIDATE)
                stats = gw.process_pending(since_id=0)
                rows = self._rows_by_decision(gw)
            self.assertEqual(stats.validation_ok, 1)
            self.assertEqual(stats.validation_fail, 2)
            self.assertEqual(rows[live_id]["status"], "validated")
            self.assertEqual(rows[live_id]["executed"], 0)
            self.assertEqual(_receipt(rows[live_id])["status"], "dry_run_acked")
            self.assertEqual(_errors(rows[sample_id]), [PURPOSE_REJECT_SAMPLE])
            self.assertEqual(_errors(rows[missing_id]), [PURPOSE_REJECT_MISSING])
            self.assertEqual(rows[sample_id]["executed"], 0)
            self.assertEqual(rows[missing_id]["executed"], 0)

    def test_real_paper_fills_live_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            db.seed_price()
            sample_id = db.add(column_purpose="sample", raw_purpose="sample")
            live_id = db.add(
                column_purpose=PURPOSE_LIVE_CANDIDATE,
                raw_purpose=PURPOSE_LIVE_CANDIDATE,
            )
            db.close()
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = False
            try:
                with ExecutionGateway(
                    decision_db=db.path,
                    ledger_path=root / "exec.db",
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    pm_enabled=False,
                ) as gw:
                    gw.process_pending(since_id=0)
                    rows = self._rows_by_decision(gw)
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
            self.assertEqual(rows[sample_id]["executed"], 0)
            self.assertEqual(rows[sample_id]["status"], "rejected")
            self.assertEqual(rows[live_id]["executed"], 1)
            self.assertEqual(rows[live_id]["executor"], "paper")
            paper = PaperLedger(root / "paper.db")
            try:
                opens = paper.list_open()
            finally:
                paper.close()
            self.assertEqual(len(opens), 1)
            meta = json.loads(opens[0]["meta_json"])
            self.assertEqual(meta["decision_log_id"], live_id)

    def test_stale_claim_of_sample_does_not_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            did = db.add(column_purpose="sample", raw_purpose="sample")
            db.close()
            paper = _RecordingExecutor()
            with ExecutionGateway(
                decision_db=db.path,
                ledger_path=root / "exec.db",
                paper_enabled=True,
                paper_executor=paper,
                pm_enabled=False,
            ) as gw:
                claim = gw.ledger.claim_decision(
                    decision_log_id=did,
                    strategy_id="jev_strength_v1",
                    action="buy",
                    chain_id="solana",
                    token_address="token1",
                    pair_address="pair1",
                    size_pct=0.05,
                    reason="test",
                    mode="dry_run",
                )
                stale = _iso(_now() - timedelta(seconds=120))
                gw.ledger.conn.execute(
                    "UPDATE execution_log SET status_updated_at = ? WHERE id = ?",
                    (stale, claim["execution_log_id"]),
                )
                stats = gw.process_pending(since_id=0)
                row = gw.ledger.get_by_decision_log_id(did)
            self.assertEqual(stats.processed, 1)
            self.assertEqual(paper.calls, [])
            assert row is not None
            self.assertEqual(row["status"], "rejected")
            self.assertEqual(row["executed"], 0)
            self.assertEqual(_errors(row), [PURPOSE_REJECT_SAMPLE])

    def test_gates_stay_off_by_default(self) -> None:
        env = os.environ.copy()
        # Default-off is about an unset process, not the operator .env.
        # EXECUTION_DAILY_LOSS_USD=200 in production must not leak in here.
        for key in (
            "EXECUTION_PAPER_ENABLED",
            "PM_ENABLED",
            "EXECUTION_RISK_ENABLED",
            "EXECUTION_PAPER_TICK",
            "EXECUTION_DAILY_LOSS_USD",
        ):
            env.pop(key, None)
        code = (
            "import json, sqlite3, sys, tempfile\n"
            "from pathlib import Path\n"
            f"sys.path[:0] = [{str(ROOT)!r}, {str(CRAWLER)!r}]\n"
            "import importlib\n"
            "import execution.config as cfg\n"
            "importlib.reload(cfg)\n"
            "from execution.gateway import ExecutionGateway, _env_bool\n"
            "assert cfg.EXECUTION_RISK_ENABLED is False, cfg.EXECUTION_RISK_ENABLED\n"
            "assert cfg.EXECUTION_DAILY_LOSS_USD is None\n"
            "assert _env_bool('EXECUTION_PAPER_ENABLED', False) is False\n"
            "assert _env_bool('PM_ENABLED', False) is False\n"
            "assert _env_bool('EXECUTION_RISK_ENABLED', False) is False\n"
            "root = Path(tempfile.mkdtemp())\n"
            "db = root / 'd.db'\n"
            "conn = sqlite3.connect(db)\n"
            "conn.execute(\n"
            "    'CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT, '\n"
            "    'strategy_id TEXT, action TEXT, chain_id TEXT, token_address TEXT, '\n"
            "    'size_pct REAL, reason TEXT, risk_passed INTEGER, mode TEXT, '\n"
            "    'purpose TEXT, raw_json TEXT)'\n"
            ")\n"
            "raw_live = json.dumps({'pair_address': 'pair1', 'purpose': 'live_candidate', 'extras': {}})\n"
            "raw_sample = json.dumps({'pair_address': 'pair1', 'purpose': 'sample', 'extras': {}})\n"
            "conn.execute(\n"
            "    'INSERT INTO decision_log (id, strategy_id, action, chain_id, token_address, '\n"
            "    'size_pct, reason, mode, purpose, raw_json) VALUES '\n"
            "    '(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)',\n"
            "    ('jev_strength_v1', 'buy', 'solana', 'token1', 0.05, 't', 'dry_run', 'sample', raw_sample),\n"
            ")\n"
            "conn.execute(\n"
            "    'INSERT INTO decision_log (id, strategy_id, action, chain_id, token_address, '\n"
            "    'size_pct, reason, mode, purpose, raw_json) VALUES '\n"
            "    '(2, ?, ?, ?, ?, ?, ?, ?, ?, ?)',\n"
            "    ('jev_strength_v1', 'buy', 'solana', 'token1', 0.05, 't', 'dry_run', 'live_candidate', raw_live),\n"
            ")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "gw = ExecutionGateway(decision_db=db, ledger_path=root / 'exec.db')\n"
            "try:\n"
            "    assert gw.paper_enabled is False\n"
            "    assert gw.pm_enabled is False\n"
            "    stats = gw.process_pending(since_id=0)\n"
            "    rows = {r['decision_log_id']: r for r in gw.ledger.recent(limit=5)}\n"
            "finally:\n"
            "    gw.close()\n"
            "assert stats.executed_ok == 0\n"
            "assert rows[1]['status'] == 'rejected'\n"
            "assert rows[1]['executed'] == 0\n"
            "assert 'purpose_sample' in (rows[1]['validation_errors'] or '')\n"
            "assert rows[2]['status'] == 'validated'\n"
            "assert rows[2]['executed'] == 0\n"
            "print('defaults_off')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("defaults_off", proc.stdout)


if __name__ == "__main__":
    unittest.main()
