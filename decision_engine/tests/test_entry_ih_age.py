"""Entry sidebook B1: emit selfbuilt A buys only when entry_ih_age_h >= 1.

entry_ih_age_h is now minus the latest indicator_history.recorded_at for the
token (pair only when that table is not token-keyed). Missing history is
ih_hist_missing and is never stored as 0. The resident live_candidate path
does not apply this gate, including when DECISION_ENTRY_IH_SIDEBOOK=1.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_CANDIDATE, SignalBus
from decision_engine.entry_ih_age import (
    ENTRY_IH_MIN_HOURS,
    REJECT_IH_AGE_BELOW,
    REJECT_IH_HIST_MISSING,
    filter_buys_by_entry_ih_age,
)
from decision_engine.run import main
from decision_engine.strategies import get_strategy
from decision_engine.strategies.selfbuilt_momentum_a import STRATEGY_ID
from execution.gateway import PURPOSE_REJECT_SHADOW, purpose_reject_reason

NOW = datetime(2026, 10, 30, 9, 0, 0, tzinfo=timezone.utc)
CREATED_MS = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
SIDEBOOK_PURPOSE = "shadow_entry_ih_ge1h"
_ROOT = Path(__file__).resolve().parents[2]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _buy(token: str, pair: str | None = None) -> dict:
    return {
        "strategy_id": STRATEGY_ID,
        "action": "buy",
        "chain_id": "solana",
        "token_address": token,
        "pair_address": pair or f"pair-{token}",
        "size_pct": 0.05,
        "reason": "selfbuilt",
    }


def _rows(db: Path) -> list[sqlite3.Row]:
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "decision_log" not in names:
            return []
        return list(conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall())
    finally:
        conn.close()


def _seed_selfbuilt(path: Path, *, now: datetime) -> None:
    """Three strategy-admitted tokens: hist age 2h, 20min, and none."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE token_indicators (
            chain_id TEXT,
            token_address TEXT,
            tier TEXT,
            primary_pair_address TEXT,
            updated_at TEXT,
            jev_strength REAL,
            volatility_24h REAL,
            change_1h REAL,
            rsi_14 REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE pairs (
            chain_id TEXT,
            pair_address TEXT,
            token_address TEXT,
            raw_json TEXT,
            updated_at TEXT,
            pair_created_at INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE indicator_history (
            chain_id TEXT,
            token_address TEXT,
            recorded_at TEXT,
            primary_pair_address TEXT
        )
        """
    )
    raw = json.dumps({"liquidity": {"usd": 80_000}})
    tokens = [
        ("OldIh", "pair-old", _iso(now - timedelta(hours=2)), "2026-10-30T08:00:00Z"),
        ("YoungIh", "pair-young", _iso(now - timedelta(minutes=20)), "2026-10-30T08:01:00Z"),
        ("MissingIh", "pair-missing", None, "2026-10-30T08:02:00Z"),
    ]
    for token, pair, recorded, updated in tokens:
        conn.execute(
            """
            INSERT INTO token_indicators VALUES
                ('solana', ?, 'hot', ?, ?, 0.2, 1.0, 0.5, 50.0)
            """,
            (token, pair, updated),
        )
        conn.execute(
            "INSERT INTO pairs VALUES ('solana', ?, ?, ?, ?, ?)",
            (pair, token, raw, updated, CREATED_MS),
        )
        if recorded is not None:
            conn.execute(
                """
                INSERT INTO indicator_history
                    (chain_id, token_address, recorded_at, primary_pair_address)
                VALUES ('solana', ?, ?, ?)
                """,
                (token, recorded, pair),
            )
            # Older row must lose to the latest recorded_at.
            conn.execute(
                """
                INSERT INTO indicator_history
                    (chain_id, token_address, recorded_at, primary_pair_address)
                VALUES ('solana', ?, ?, ?)
                """,
                (token, _iso(now - timedelta(days=3)), pair),
            )
    conn.commit()
    conn.close()


class FilterTests(unittest.TestCase):
    def test_admits_at_least_one_hour_and_stamps_age(self) -> None:
        recorded = _iso(NOW - timedelta(hours=1))
        kept, rejected = filter_buys_by_entry_ih_age(
            [_buy("Old")],
            lambda chain, token, pair=None: recorded,
            min_hours=ENTRY_IH_MIN_HOURS,
            now=NOW,
        )
        self.assertEqual(rejected, {})
        self.assertEqual(len(kept), 1)
        self.assertGreaterEqual(kept[0]["entry_ih_age_h"], 1.0)
        self.assertAlmostEqual(kept[0]["entry_ih_age_h"], 1.0, places=4)

    def test_rejects_under_one_hour(self) -> None:
        recorded = _iso(NOW - timedelta(minutes=59))
        kept, rejected = filter_buys_by_entry_ih_age(
            [_buy("Young")],
            lambda chain, token, pair=None: recorded,
            min_hours=1.0,
            now=NOW,
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected, {REJECT_IH_AGE_BELOW: 1})
        self.assertNotEqual(REJECT_IH_AGE_BELOW, REJECT_IH_HIST_MISSING)

    def test_missing_hist_is_not_fake_zero(self) -> None:
        calls: list[tuple] = []

        def lookup(chain, token, pair=None):
            calls.append((chain, token, pair))
            return None

        kept, rejected = filter_buys_by_entry_ih_age(
            [_buy("Gone", "pair-gone")],
            lookup,
            min_hours=1.0,
            now=NOW,
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected, {REJECT_IH_HIST_MISSING: 1})
        self.assertEqual(calls, [("solana", "Gone", "pair-gone")])
        blank, blank_rejected = filter_buys_by_entry_ih_age(
            [_buy("Blank")],
            lambda chain, token, pair=None: "   ",
            now=NOW,
        )
        self.assertEqual(blank, [])
        self.assertEqual(blank_rejected, {REJECT_IH_HIST_MISSING: 1})
        garbage, garbage_rejected = filter_buys_by_entry_ih_age(
            [_buy("Bad")],
            lambda chain, token, pair=None: "not-a-timestamp",
            now=NOW,
        )
        self.assertEqual(garbage, [])
        self.assertEqual(garbage_rejected, {REJECT_IH_HIST_MISSING: 1})

    def test_real_zero_age_is_below_not_missing(self) -> None:
        kept, rejected = filter_buys_by_entry_ih_age(
            [_buy("Now")],
            lambda chain, token, pair=None: _iso(NOW),
            now=NOW,
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected, {REJECT_IH_AGE_BELOW: 1})

    def test_non_buys_pass_without_lookup(self) -> None:
        sell = dict(_buy("X"))
        sell["action"] = "sell"

        def lookup(*_args, **_kwargs):
            raise AssertionError("lookup must not run for non-buys")

        kept, rejected = filter_buys_by_entry_ih_age([sell], lookup, now=NOW)
        self.assertEqual(kept, [sell])
        self.assertEqual(rejected, {})
        self.assertNotIn("entry_ih_age_h", sell)


class AdapterLookupTests(unittest.TestCase):
    def test_latest_token_row_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            _seed_selfbuilt(db, now=NOW)
            adapter = IndicatorDataAdapter(db)
            try:
                old = adapter.latest_indicator_history_recorded_at(
                    "solana", "OldIh", "pair-old"
                )
                missing = adapter.latest_indicator_history_recorded_at(
                    "solana", "MissingIh", "pair-missing"
                )
                unknown = adapter.latest_indicator_history_recorded_at(
                    "solana", "NoSuch", "pair-old"
                )
            finally:
                adapter.close()
            self.assertEqual(old, _iso(NOW - timedelta(hours=2)))
            self.assertIsNone(missing)
            self.assertIsNone(unknown)
            self.assertNotEqual(missing, 0)
            self.assertNotEqual(missing, "0")

    def test_pair_key_only_when_table_is_not_token_keyed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pair.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE indicator_history (
                    chain_id TEXT,
                    pair_address TEXT,
                    recorded_at TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO indicator_history VALUES ('solana', 'pair-k', ?)",
                (_iso(NOW - timedelta(hours=3)),),
            )
            conn.commit()
            conn.close()
            adapter = IndicatorDataAdapter(db)
            try:
                found = adapter.latest_indicator_history_recorded_at(
                    "solana", "token-unused", "pair-k"
                )
                absent = adapter.latest_indicator_history_recorded_at(
                    "solana", "token-unused", None
                )
            finally:
                adapter.close()
            self.assertEqual(found, _iso(NOW - timedelta(hours=3)))
            self.assertIsNone(absent)

    def test_token_key_does_not_borrow_another_pairs_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tok.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE indicator_history (
                    chain_id TEXT,
                    token_address TEXT,
                    primary_pair_address TEXT,
                    recorded_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO indicator_history VALUES
                    ('solana', 'Other', 'pair-shared', ?)
                """,
                (_iso(NOW - timedelta(hours=5)),),
            )
            conn.commit()
            conn.close()
            adapter = IndicatorDataAdapter(db)
            try:
                borrowed = adapter.latest_indicator_history_recorded_at(
                    "solana", "Wanted", "pair-shared"
                )
            finally:
                adapter.close()
            self.assertIsNone(borrowed)


class SidebookLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_handler = os.environ.get("LOG_HANDLER")
        os.environ["LOG_HANDLER"] = "stdout"
        self._flag = os.environ.get("DECISION_ENTRY_IH_SIDEBOOK")

    def tearDown(self) -> None:
        if self._log_handler is None:
            os.environ.pop("LOG_HANDLER", None)
        else:
            os.environ["LOG_HANDLER"] = self._log_handler
        if self._flag is None:
            os.environ.pop("DECISION_ENTRY_IH_SIDEBOOK", None)
        else:
            os.environ["DECISION_ENTRY_IH_SIDEBOOK"] = self._flag

    def test_main_execution_rejects_shadow_purpose(self) -> None:
        self.assertTrue(SIDEBOOK_PURPOSE.startswith("shadow_"))
        self.assertEqual(purpose_reject_reason(SIDEBOOK_PURPOSE), PURPOSE_REJECT_SHADOW)
        self.assertIsNone(purpose_reject_reason(PURPOSE_LIVE_CANDIDATE))

    def test_live_candidate_ignores_sidebook_flag(self) -> None:
        os.environ["DECISION_ENTRY_IH_SIDEBOOK"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dexscreener.db"
            _seed_selfbuilt(db, now=datetime.now(timezone.utc))
            rc = main(
                [
                    "--dry-run",
                    "--once",
                    "--db",
                    str(db),
                    "--strategy",
                    STRATEGY_ID,
                    "--purpose",
                    PURPOSE_LIVE_CANDIDATE,
                    "--position-pct",
                    "0.05",
                    "--token-cooldown-min",
                    "0",
                    "--limit",
                    "10",
                ]
            )
            self.assertEqual(rc, 0)
            rows = _rows(db)
            self.assertEqual(
                {row["token_address"] for row in rows},
                {"OldIh", "YoungIh", "MissingIh"},
            )
            for row in rows:
                self.assertEqual(row["purpose"], PURPOSE_LIVE_CANDIDATE)
                self.assertEqual(row["mode"], "dry_run")
                self.assertEqual(row["strategy_id"], STRATEGY_ID)
                extras = json.loads(row["raw_json"]).get("extras") or {}
                self.assertNotIn("entry_ih_age_h", extras)

    def test_engine_without_gate_keeps_young_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed_selfbuilt(db, now=datetime.now(timezone.utc))
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy(STRATEGY_ID, position_pct=0.05)],
                    risk=RiskManager(cooldown_sec=0),
                    hot_limit=10,
                )
                result = engine.tick()
            finally:
                bus.close()
                adapter.close()
            self.assertNotIn("entry_ih_rejected", result)
            self.assertEqual(
                {row["token_address"] for row in _rows(db)},
                {"OldIh", "YoungIh", "MissingIh"},
            )

    def test_shadow_loop_emits_only_ge_1h(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "indicators.db"
            out = Path(tmp) / "decision_entry_ih_ge1h.db"
            _seed_selfbuilt(src, now=datetime.now(timezone.utc))
            rc = main(
                [
                    "--dry-run",
                    "--once",
                    "--db",
                    str(src),
                    "--output-db",
                    str(out),
                    "--strategy",
                    STRATEGY_ID,
                    "--purpose",
                    SIDEBOOK_PURPOSE,
                    "--position-pct",
                    "0.05",
                    "--entry-ih-min-hours",
                    "1.0",
                    "--token-cooldown-min",
                    "0",
                    "--limit",
                    "10",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(_rows(src), [])
            rows = _rows(out)
            self.assertEqual([row["token_address"] for row in rows], ["OldIh"])
            row = rows[0]
            self.assertEqual(row["purpose"], SIDEBOOK_PURPOSE)
            self.assertEqual(row["mode"], "dry_run")
            self.assertEqual(row["strategy_id"], STRATEGY_ID)
            self.assertAlmostEqual(row["size_pct"], 0.05)
            extras = json.loads(row["raw_json"])["extras"]
            self.assertGreaterEqual(extras["entry_ih_age_h"], 1.0)
            self.assertNotEqual(extras["entry_ih_age_h"], 0)
            blob = Path(out).read_bytes()
            self.assertNotIn(b"YoungIh", blob)
            self.assertNotIn(b"MissingIh", blob)

    def test_shadow_purpose_gates_even_without_explicit_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "indicators.db"
            out = Path(tmp) / "out.db"
            _seed_selfbuilt(src, now=datetime.now(timezone.utc))
            rc = main(
                [
                    "--dry-run",
                    "--once",
                    "--db",
                    str(src),
                    "--output-db",
                    str(out),
                    "--strategy",
                    STRATEGY_ID,
                    "--purpose",
                    SIDEBOOK_PURPOSE,
                    "--position-pct",
                    "0.05",
                    "--token-cooldown-min",
                    "0",
                ]
            )
            self.assertEqual(rc, 0)
            self.assertEqual([row["token_address"] for row in _rows(out)], ["OldIh"])

    def test_refuses_to_filter_live_candidate_or_share_its_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dexscreener.db"
            _seed_selfbuilt(db, now=datetime.now(timezone.utc))
            filtered = main(
                [
                    "--dry-run",
                    "--once",
                    "--db",
                    str(db),
                    "--strategy",
                    STRATEGY_ID,
                    "--purpose",
                    PURPOSE_LIVE_CANDIDATE,
                    "--entry-ih-min-hours",
                    "1.0",
                    "--token-cooldown-min",
                    "0",
                ]
            )
            self.assertEqual(filtered, 2)
            self.assertEqual(_rows(db), [])
            shared = main(
                [
                    "--dry-run",
                    "--once",
                    "--db",
                    str(db),
                    "--strategy",
                    STRATEGY_ID,
                    "--purpose",
                    SIDEBOOK_PURPOSE,
                    "--token-cooldown-min",
                    "0",
                ]
            )
            self.assertEqual(shared, 2)
            self.assertEqual(_rows(db), [])


class LauncherTests(unittest.TestCase):
    def test_watchdog_starts_sidebook_only_when_flag_is_on(self) -> None:
        text = (_ROOT / "scripts" / "decision_watchdog.sh").read_text(encoding="utf-8")
        live = (
            "-m decision_engine.run --strategy selfbuilt_momentum_a_v1 "
            "--dry-run --interval 60 --purpose live_candidate --limit 2000 "
            "--token-cooldown-min 60 --position-pct 0.05"
        )
        self.assertIn(live, text)
        self.assertIn('DECISION_ENTRY_IH_SIDEBOOK:-0', text)
        self.assertIn("shadow_entry_ih_ge1h", text)
        self.assertIn("decision_entry_ih_ge1h.db", text)
        self.assertIn("--entry-ih-min-hours 1.0", text)
        self.assertLess(text.index("purpose live_candidate"), text.index("DECISION_ENTRY_IH_SIDEBOOK"))
        script = (_ROOT / "scripts" / "decision_entry_ih_sidebook.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("DECISION_ENTRY_IH_SIDEBOOK", script)
        self.assertIn("shadow_entry_ih_ge1h", script)
        self.assertIn("--position-pct 0.05", script)
        self.assertIn("--dry-run", script)
        self.assertIn("decision_entry_ih_ge1h.db", script)
        self.assertNotIn("live_candidate", script)

    def test_execution_note_names_isolated_paper_book(self) -> None:
        note = (_ROOT / "docs" / "ENTRY_IH_GE1H_SIDEBOOK_2026-10-30.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("shadow_entry_ih_ge1h", note)
        self.assertIn("paper_trading_entry_ih_ge1h.db", note)
        self.assertIn("SIZE_MULT=20", note)
        self.assertIn("MAX_OPEN=1", note)
        self.assertIn("10000", note)
        self.assertIn("DECISION_ENTRY_IH_SIDEBOOK", note)


if __name__ == "__main__":
    unittest.main()
