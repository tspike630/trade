"""Temp-db checks for the execution sweep (P0-4, P1-04/05/06, P2-06/07/09/10).

Run from the repo root:

    python -m unittest execution.tests.test_code_sweep
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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

import execution.gateway as gateway_mod  # noqa: E402
from dashboard.parsing import rejection_key  # noqa: E402
from execution.executors.paper_executor import PaperExecutor  # noqa: E402
from execution.exits import exit_reason  # noqa: E402
from execution.gateway import ExecutionGateway, _risk_rejected_event_detail  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402
from execution.order_events import event_chain  # noqa: E402
from execution.risk.limits import check_daily_loss  # noqa: E402
from execution.position_manager import (  # noqa: E402
    PositionManager,
    PositionManagerConfig,
    _pct_to_fraction,
)
from paper_trade import PaperLedger, latest_price, tick  # noqa: E402
from store import Store, fetch_latest_pair_price, fetch_pair_spot  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hkt_today() -> str:
    return _now().astimezone(ZoneInfo("Asia/Hong_Kong")).date().isoformat()


class _PriceDB:
    """Minimal crawler DB: pair_history + pairs. Not a business library."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
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
            CREATE INDEX idx_pair_history_lookup
                ON pair_history (chain_id, pair_address, recorded_at);
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
        )
        self.conn.commit()

    def add_history(self, ts: str, price: float, *, chain: str = "solana", pair: str = "pair1") -> None:
        self.conn.execute(
            """
            INSERT INTO pair_history (chain_id, pair_address, recorded_at, price_usd)
            VALUES (?, ?, ?, ?)
            """,
            (chain, pair, ts, price),
        )

    def add_spot(
        self,
        ts: str,
        price: float,
        *,
        liquidity: float | None = None,
        chain: str = "solana",
        pair: str = "pair1",
    ) -> None:
        raw: dict = {"priceUsd": price}
        if liquidity is not None:
            raw["liquidity"] = {"usd": liquidity}
        self.conn.execute(
            """
            INSERT INTO pairs (chain_id, pair_address, raw_json, source, updated_at)
            VALUES (?, ?, ?, 'test', ?)
            ON CONFLICT(chain_id, pair_address) DO UPDATE SET
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (chain, pair, json.dumps(raw), ts),
        )

    def add_decision(self, **kwargs) -> int:
        # Sweep rows are executable dry-runs. Purpose lives in raw_json because
        # this fixture predates the decision_log.purpose column (P2-38).
        raw = {
            "pair_address": kwargs.get("pair_address", "pair1"),
            "extras": kwargs.get("extras") or {},
            "purpose": kwargs.get("purpose", "live_candidate"),
        }
        cur = self.conn.execute(
            """
            INSERT INTO decision_log (
                ts, strategy_id, action, chain_id, token_address, size_pct,
                reason, mode, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                kwargs.get("ts") or _iso(_now()),
                kwargs["strategy_id"],
                kwargs["action"],
                kwargs.get("chain_id", "solana"),
                kwargs.get("token_address", "token1"),
                kwargs.get("size_pct", 0.05),
                kwargs.get("reason", "test"),
                kwargs.get("mode", "dry_run"),
                json.dumps(raw),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


class LatestPriceTests(unittest.TestCase):
    def test_desc_limit_1_not_first_500(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawler.db"
            db = _PriceDB(path)
            start = _now() - timedelta(minutes=600)
            for i in range(600):
                db.add_history(_iso(start + timedelta(minutes=i)), float(i + 1))
            db.close()
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            quote = fetch_latest_pair_price(conn, "solana", "pair1")
            conn.close()
            self.assertIsNotNone(quote)
            assert quote is not None
            self.assertEqual(quote["price_usd"], 600.0)

            class _Facade:
                def __init__(self, p: Path) -> None:
                    self._conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    self._conn.row_factory = sqlite3.Row

                def latest_pair_price(self, chain: str, pair: str):
                    return fetch_latest_pair_price(self._conn, chain, pair)

                def pair_spot(self, chain: str, pair: str):
                    return None

                def close(self) -> None:
                    self._conn.close()

            facade = _Facade(path)
            try:
                px = latest_price(facade, "solana", "pair1", max_age_sec=10**9)
            finally:
                facade.close()
            self.assertEqual(px, 600.0)

    def test_newer_spot_wins_and_stale_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawler.db"
            db = _PriceDB(path)
            fresh = _now() - timedelta(seconds=30)
            older = fresh - timedelta(seconds=120)
            db.add_history(_iso(older), 1.0)
            db.add_spot(_iso(fresh), 9.0, liquidity=1_000_000)
            db.close()

            class _Facade:
                def __init__(self, p: Path) -> None:
                    self._conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    self._conn.row_factory = sqlite3.Row

                def latest_pair_price(self, chain: str, pair: str):
                    return fetch_latest_pair_price(self._conn, chain, pair)

                def pair_spot(self, chain: str, pair: str):
                    return fetch_pair_spot(self._conn, chain, pair)

                def close(self) -> None:
                    self._conn.close()

            facade = _Facade(path)
            try:
                self.assertEqual(
                    latest_price(facade, "solana", "pair1", max_age_sec=3600),
                    9.0,
                )
                self.assertIsNone(
                    latest_price(facade, "solana", "pair1", max_age_sec=10)
                )
            finally:
                facade.close()

    def test_store_method_on_temp_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "crawler.db"
            store = Store(path)
            try:
                now = _iso(_now())
                store._conn.execute(
                    """
                    INSERT INTO pair_history (
                        chain_id, pair_address, recorded_at, price_usd
                    ) VALUES ('solana', 'pair1', ?, 1.5)
                    """,
                    (now,),
                )
                store._conn.commit()
                quote = store.latest_pair_price("solana", "pair1")
            finally:
                store.close()
            self.assertIsNotNone(quote)
            assert quote is not None
            self.assertEqual(quote["price_usd"], 1.5)


class LedgerIdempotencyTests(unittest.TestCase):
    def _seed_prices(self, db: _PriceDB) -> None:
        now = _now()
        db.add_history(_iso(now), 2.0)
        db.add_spot(_iso(now), 2.0, liquidity=1_000_000)

    def test_duplicate_decision_does_not_double_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            self._seed_prices(crawler)
            did = crawler.add_decision(
                strategy_id="strat_a",
                action="buy",
                size_pct=0.05,
            )
            crawler.close()
            with ExecutionGateway(
                decision_db=root / "crawler.db",
                ledger_path=root / "exec.db",
                paper_enabled=True,
                paper_db=root / "paper.db",
                pm_enabled=False,
            ) as gw:
                row = gw.fetch_pending(0)[0]
                first = gw.process_one(row)
                second = gw.process_one(row)
                opens = gw._get_paper().ledger.list_open()
                n_log = gw.ledger.count()
                events = event_chain(gw.ledger.path, first["execution_log_id"])
            self.assertTrue(first["executed"])
            self.assertTrue(second.get("duplicate"))
            self.assertEqual(second["executed"], 0)
            self.assertEqual(len(opens), 1)
            self.assertEqual(n_log, 1)
            self.assertEqual([e["event"] for e in events], ["new", "validated", "submitted", "filled"])

    def test_crash_after_fill_resumes_without_second_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            self._seed_prices(crawler)
            did = crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            crawler.close()
            paper = PaperLedger(root / "paper.db")
            paper.open_position(
                chain_id="solana",
                pair_address="pair1",
                token_address="token1",
                price=2.0,
                size_usd=100.0,
                meta={"strategy_id": "strat_a", "decision_log_id": did},
            )
            paper.close()
            with ExecutionGateway(
                decision_db=root / "crawler.db",
                ledger_path=root / "exec.db",
                paper_enabled=True,
                paper_db=root / "paper.db",
                pm_enabled=False,
            ) as gw:
                gw.ledger.claim_decision(
                    decision_log_id=did,
                    strategy_id="strat_a",
                    action="buy",
                    chain_id="solana",
                    token_address="token1",
                    pair_address="pair1",
                    size_pct=0.05,
                    reason="test",
                    mode="dry_run",
                )
                row = gw.fetch_pending(0)[0]
                result = gw.process_one(row)
                opens = gw._get_paper().ledger.list_open()
                stored = gw.ledger.get_by_decision_log_id(did)
            self.assertEqual(result["executed"], 1)
            self.assertEqual(len(opens), 1)
            self.assertEqual(stored["status"], "executed")
            self.assertEqual(int(stored["executed"]), 1)

    def test_stale_claim_without_position_fills_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            self._seed_prices(crawler)
            did = crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            crawler.close()
            with ExecutionGateway(
                decision_db=root / "crawler.db",
                ledger_path=root / "exec.db",
                paper_enabled=True,
                paper_db=root / "paper.db",
                pm_enabled=False,
            ) as gw:
                gw.ledger.claim_decision(
                    decision_log_id=did,
                    strategy_id="strat_a",
                    action="buy",
                    chain_id="solana",
                    token_address="token1",
                    pair_address="pair1",
                    size_pct=0.05,
                    reason="test",
                    mode="dry_run",
                )
                old = _iso(_now() - timedelta(seconds=120))
                gw.ledger.conn.execute(
                    "UPDATE execution_log SET status_updated_at = ? WHERE decision_log_id = ?",
                    (old, did),
                )
                row = gw.fetch_pending(0)[0]
                result = gw.process_one(row)
                opens = gw._get_paper().ledger.list_open()
            self.assertEqual(result["executed"], 1)
            self.assertFalse(result.get("duplicate"))
            self.assertEqual(len(opens), 1)

    def test_since_id_does_not_rewind_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            crawler.conn.execute(
                """
                INSERT INTO decision_log (
                    id, ts, strategy_id, action, chain_id, token_address,
                    size_pct, reason, mode, raw_json
                ) VALUES (4, ?, 'strat_a', 'buy', 'solana', 'token1', 0.05, 't', 'dry_run', ?)
                """,
                (
                    _iso(_now()),
                    json.dumps({"pair_address": "pair1", "purpose": "live_candidate"}),
                ),
            )
            crawler.conn.commit()
            crawler.close()
            with ExecutionGateway(
                decision_db=root / "crawler.db",
                ledger_path=root / "exec.db",
                paper_enabled=False,
                pm_enabled=False,
            ) as gw:
                gw.ledger.set_last_processed_id(100)
                stats = gw.process_pending(since_id=0)
                cursor = gw.ledger.get_last_processed_id()
            self.assertEqual(stats.processed, 1)
            self.assertEqual(cursor, 100)

    def test_bad_message_rejected_and_next_row_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            crawler.close()
            real = gateway_mod.validate_signal
            calls = {"n": 0}

            def boom(sig):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("bad message")
                return real(sig)

            gateway_mod.validate_signal = boom
            try:
                with ExecutionGateway(
                    decision_db=root / "crawler.db",
                    ledger_path=root / "exec.db",
                    paper_enabled=False,
                    pm_enabled=False,
                ) as gw:
                    stats = gw.process_pending(since_id=0)
                    cursor = gw.ledger.get_last_processed_id()
                    rows = gw.ledger.recent(limit=5)
            finally:
                gateway_mod.validate_signal = real
            self.assertEqual(stats.processed, 2)
            self.assertEqual(cursor, 2)
            statuses = {r["decision_log_id"]: r["status"] for r in rows}
            self.assertEqual(statuses[1], "rejected")
            self.assertEqual(statuses[2], "validated")

    def test_finalize_rolls_back_receipt_cursor_and_events(self) -> None:
        import execution.ledger as ledger_mod

        with tempfile.TemporaryDirectory() as tmp:
            led = ExecutionLedger(Path(tmp) / "exec.db")
            try:
                claim = led.claim_decision(
                    decision_log_id=3,
                    strategy_id="strat_a",
                    action="buy",
                    chain_id="solana",
                    token_address="token1",
                    pair_address="pair1",
                    size_pct=0.05,
                    reason="t",
                    mode="dry_run",
                )
                real = ledger_mod.record_event

                def boom(*_a, **_k):
                    raise RuntimeError("event failed")

                ledger_mod.record_event = boom
                try:
                    with self.assertRaises(RuntimeError):
                        led.finalize_receipt(
                            claim["execution_log_id"],
                            decision_log_id=3,
                            strategy_id="strat_a",
                            action="buy",
                            chain_id="solana",
                            token_address="token1",
                            pair_address="pair1",
                            size_pct=0.05,
                            reason="t",
                            mode="dry_run",
                            validation_ok=True,
                            validation_errors=None,
                            executed=1,
                            executor="paper",
                            receipt={"status": "paper_executed"},
                            status="executed",
                            events=[("new", {"decision_log_id": 3})],
                        )
                finally:
                    ledger_mod.record_event = real
                row = led.get_by_decision_log_id(3)
                self.assertEqual(row["status"], "claimed")
                self.assertEqual(led.get_last_processed_id(), 0)
                self.assertEqual(event_chain(led.path, claim["execution_log_id"]), [])
            finally:
                led.close()

    def test_unique_index_on_existing_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "exec.db"
            conn = sqlite3.connect(str(path))
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    decision_log_id INTEGER NOT NULL,
                    strategy_id TEXT NOT NULL,
                    action TEXT,
                    chain_id TEXT,
                    token_address TEXT,
                    pair_address TEXT,
                    size_pct REAL,
                    reason TEXT,
                    mode TEXT,
                    validation_ok INTEGER NOT NULL,
                    validation_errors TEXT,
                    executed INTEGER NOT NULL DEFAULT 0,
                    executor TEXT,
                    receipt_json TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO execution_log (
                    ts, decision_log_id, strategy_id, validation_ok, executed
                ) VALUES ('t', 7, 's', 1, 0)
                """
            )
            conn.commit()
            conn.close()
            led = ExecutionLedger(path)
            try:
                idx = led.conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='ux_exec_log_decision'"
                ).fetchone()
                self.assertIsNotNone(idx)
                with self.assertRaises(sqlite3.IntegrityError):
                    led.conn.execute(
                        """
                        INSERT INTO execution_log (
                            ts, decision_log_id, strategy_id, validation_ok, executed
                        ) VALUES ('t2', 7, 's', 1, 0)
                        """
                    )
            finally:
                led.close()


class PaperLedgerTests(unittest.TestCase):
    def test_wal_and_close_is_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            led = PaperLedger(Path(tmp) / "paper.db")
            try:
                mode = led.conn.execute("PRAGMA journal_mode").fetchone()[0]
                busy = led.conn.execute("PRAGMA busy_timeout").fetchone()[0]
                self.assertEqual(str(mode).lower(), "wal")
                self.assertGreaterEqual(int(busy), 30_000)
                start = led.get_cash()
                pid = led.open_position(
                    chain_id="solana",
                    pair_address="pair1",
                    price=1.0,
                    size_usd=100.0,
                )
                after_open = led.get_cash()
                self.assertAlmostEqual(after_open, start - 100.0)
                led.close_position(pid, price=1.5, reason="take_profit")
                after_close = led.get_cash()
                self.assertAlmostEqual(after_close, after_open + 150.0)
                with self.assertRaises(ValueError):
                    led.close_position(pid, price=1.5, reason="take_profit")
                self.assertAlmostEqual(led.get_cash(), after_close)
            finally:
                led.close()


class SellMatchTests(unittest.TestCase):
    def test_strategy_and_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            now = _iso(_now())
            crawler.add_history(now, 10.0)
            crawler.add_spot(now, 10.0, liquidity=1_000_000)
            crawler.close()
            with PaperExecutor(paper_db=root / "paper.db", crawler_db=root / "crawler.db") as ex:
                ex.ledger.open_position(
                    chain_id="solana",
                    pair_address="pair1",
                    token_address="token1",
                    price=10.0,
                    size_usd=100.0,
                    meta={"strategy_id": "strat_a", "decision_log_id": 1},
                )
                other = ex.execute(
                    {
                        "action": "sell",
                        "strategy_id": "strat_b",
                        "chain_id": "solana",
                        "token_address": "token1",
                        "pair_address": "pair1",
                        "size_pct": 1.0,
                        "decision_log_id": 2,
                    }
                )
                partial = ex.execute(
                    {
                        "action": "sell",
                        "strategy_id": "strat_a",
                        "chain_id": "solana",
                        "token_address": "token1",
                        "pair_address": "pair1",
                        "size_pct": 0.5,
                        "decision_log_id": 3,
                    }
                )
                full = ex.execute(
                    {
                        "action": "sell",
                        "strategy_id": "strat_a",
                        "chain_id": "solana",
                        "token_address": "token1",
                        "pair_address": "pair1",
                        "size_pct": 1.0,
                        "decision_log_id": 4,
                    }
                )
                opens = ex.ledger.list_open()
            self.assertEqual(other["error"], "strategy_mismatch")
            self.assertEqual(partial["error"], "partial_sell_rejected")
            self.assertTrue(full["ok"])
            self.assertEqual(opens, [])
            self.assertAlmostEqual(full["fill_price"], 10.0 * (1.0 - 0.003))


class ExitAndTickTests(unittest.TestCase):
    def test_z_timestamp_max_hold(self) -> None:
        opened = _iso(_now() - timedelta(minutes=5))
        self.assertTrue(opened.endswith("Z"))
        reason = exit_reason(
            {
                "entry_price": 1.0,
                "tp_pct": 10.0,
                "sl_pct": 5.0,
                "max_hold_min": 1.0,
                "opened_at": opened,
            },
            1.0,
            now=_now(),
        )
        self.assertEqual(reason, "max_hold")

    def test_tick_uses_scan_exits_and_sell_slippage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            # unknown liquidity → 2% slip. mid 120 vs entry fill 100, tp 10%.
            crawler.add_history(_iso(_now()), 120.0)
            crawler.close()
            led = PaperLedger(root / "paper.db")
            store = Store(root / "crawler.db")
            try:
                # Store() migrates; history was inserted on the other connection
                # before Store opened. Re-read via the price facade Store sees.
                pid = led.open_position(
                    chain_id="solana",
                    pair_address="pair1",
                    price=100.0,
                    size_usd=100.0,
                    tp_pct=10.0,
                    sl_pct=5.0,
                    max_hold_min=60.0,
                    meta={"strategy_id": "strat_a"},
                )
                actions = tick(led, store)
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["reason"], "take_profit")
                self.assertAlmostEqual(actions[0]["mid_price"], 120.0)
                self.assertAlmostEqual(actions[0]["fill_price"], 120.0 * 0.98)
                self.assertEqual(led.list_open(), [])
                self.assertEqual(pid, actions[0]["id"])
            finally:
                led.close()
                store.close()

    def test_hold_not_yet_does_not_close(self) -> None:
        reason = exit_reason(
            {
                "entry_price": 100.0,
                "tp_pct": 10.0,
                "sl_pct": 5.0,
                "max_hold_min": 60.0,
                "opened_at": _iso(_now()),
            },
            100.0,
            now=_now(),
        )
        self.assertIsNone(reason)


class RiskAndPmTests(unittest.TestCase):
    def test_pct_is_percent_points_only(self) -> None:
        self.assertAlmostEqual(_pct_to_fraction(1), 0.01)
        self.assertAlmostEqual(_pct_to_fraction(30), 0.30)

    def test_no_ledger_fail_closed_only_when_pm_on(self) -> None:
        on = PositionManager(config=PositionManagerConfig(enabled=True))
        off = PositionManager(config=PositionManagerConfig(enabled=False))
        sig = {
            "action": "buy",
            "strategy_id": "strat_a",
            "chain_id": "solana",
            "token_address": "token1",
            "pair_address": "pair1",
            "size_pct": 0.05,
        }
        denied = on.check_open(sig)
        allowed = off.check_open(sig)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "pm_rejected:no_ledger")
        self.assertTrue(allowed.allowed)

    def test_sell_skips_max_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            now = _iso(_now())
            crawler.add_history(now, 2.0)
            crawler.add_spot(now, 2.0, liquidity=1_000_000)
            buy_id = crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            sell_id = crawler.add_decision(
                strategy_id="strat_a", action="sell", size_pct=1.0
            )
            crawler.close()
            paper = PaperLedger(root / "paper.db")
            for _ in range(5):
                paper.open_position(
                    chain_id="solana",
                    pair_address="pair_other",
                    token_address="other",
                    price=1.0,
                    size_usd=10.0,
                    meta={"strategy_id": "strat_a"},
                )
            paper.close()
            prev = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = True
            try:
                with ExecutionGateway(
                    decision_db=root / "crawler.db",
                    ledger_path=root / "exec.db",
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    pm_enabled=False,
                ) as gw:
                    stats = gw.process_pending(since_id=0)
                    rows = {r["decision_log_id"]: r for r in gw.ledger.recent(limit=10)}
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev
            self.assertEqual(rows[buy_id]["status"], "rejected")
            self.assertIn("max_open", rows[buy_id]["receipt_json"])
            self.assertNotEqual(rows[sell_id]["status"], "risk_rejected")
            self.assertEqual(rows[sell_id]["executor"], "paper")
            self.assertGreaterEqual(stats.processed, 2)

    def test_daily_loss_reason_is_stable_key(self) -> None:
        today = _hkt_today()

        class _Closed:
            def __init__(self, rows: list[dict]) -> None:
                self._rows = rows

            def list_closed(self, limit: int = 20) -> list[dict]:
                return list(self._rows)[: int(limit)] if limit else []

        skipped = check_daily_loss(
            None, {"action": "sell"}, daily_loss_limit_usd=None
        )
        self.assertTrue(skipped["ok"])
        self.assertNotIn("reason", skipped)

        within = check_daily_loss(
            _Closed(
                [
                    {"closed_at": f"{today}T01:00:00Z", "pnl_usd": -200.0},
                    {"closed_at": "2020-01-01T00:00:00Z", "pnl_usd": -999.0},
                ]
            ),
            {"action": "buy"},
            daily_loss_limit_usd=200.0,
        )
        self.assertTrue(within["ok"])
        self.assertNotEqual(within.get("reason"), "daily_loss")

        denied = check_daily_loss(
            _Closed(
                [
                    {"closed_at": f"{today}T01:00:00Z", "pnl_usd": -201.0},
                    {"closed_at": "2020-01-01T00:00:00Z", "pnl_usd": -999.0},
                ]
            ),
            {"action": "sell"},
            daily_loss_limit_usd=200.0,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "daily_loss")
        self.assertTrue(denied["reason"].startswith("daily_loss"))
        self.assertNotIn(":", denied["reason"])
        self.assertEqual(
            denied["detail"],
            {"today": today, "pnl": -201.0, "limit": 200.0},
        )
        self.assertNotIn(today, denied["reason"])

    def test_daily_loss_gateway_receipt_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            now = _iso(_now())
            crawler.add_history(now, 2.0)
            crawler.add_spot(now, 2.0, liquidity=1_000_000)
            buy_id = crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            sell_id = crawler.add_decision(
                strategy_id="strat_a", action="sell", size_pct=1.0
            )
            crawler.close()
            paper = PaperLedger(root / "paper.db")
            lost = paper.open_position(
                chain_id="solana",
                pair_address="pair_lost",
                token_address="lost",
                price=1.0,
                size_usd=400.0,
                meta={"strategy_id": "strat_a"},
            )
            closed = paper.close_position(lost, price=0.1, reason="stop_loss")
            paper.close()
            self.assertLess(float(closed["pnl_usd"]), -200.0)
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            prev_loss = gateway_mod.EXECUTION_DAILY_LOSS_USD
            gateway_mod.EXECUTION_RISK_ENABLED = True
            gateway_mod.EXECUTION_DAILY_LOSS_USD = 200.0
            try:
                with ExecutionGateway(
                    decision_db=root / "crawler.db",
                    ledger_path=root / "exec.db",
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    pm_enabled=False,
                ) as gw:
                    gw.process_pending(since_id=0)
                    rows = {r["decision_log_id"]: r for r in gw.ledger.recent(limit=10)}
                    buy_events = event_chain(gw.ledger.path, rows[buy_id]["id"])
                    sell_events = event_chain(gw.ledger.path, rows[sell_id]["id"])
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
                gateway_mod.EXECUTION_DAILY_LOSS_USD = prev_loss

            for decision_id, events in ((buy_id, buy_events), (sell_id, sell_events)):
                receipt = json.loads(rows[decision_id]["receipt_json"])
                self.assertEqual(receipt["status"], "risk_rejected")
                self.assertEqual(receipt["error"], "daily_loss")
                item = receipt["risk"][0]
                self.assertEqual(item["reason"], "daily_loss")
                self.assertNotIn(":", item["reason"])
                self.assertEqual(item["detail"]["limit"], 200.0)
                self.assertLess(item["detail"]["pnl"], -200.0)
                self.assertEqual(item["detail"]["today"], _hkt_today())
                self.assertEqual(
                    rejection_key(1, "", "rejected", receipt),
                    "risk_rejected:daily_loss",
                )
                rejected = [event for event in events if event["event"] == "rejected"]
                self.assertEqual(len(rejected), 1)
                self.assertEqual(rejected[0]["detail"]["stage"], "risk")
                self.assertEqual(rejected[0]["detail"]["reason"], "daily_loss")
                self.assertEqual(rejected[0]["detail"]["detail"]["limit"], 200.0)
                self.assertLess(rejected[0]["detail"]["detail"]["pnl"], -200.0)

    def test_daily_loss_unset_does_not_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = _PriceDB(root / "crawler.db")
            now = _iso(_now())
            crawler.add_history(now, 2.0)
            crawler.add_spot(now, 2.0, liquidity=1_000_000)
            buy_id = crawler.add_decision(strategy_id="strat_a", action="buy", size_pct=0.05)
            sell_id = crawler.add_decision(
                strategy_id="strat_a", action="sell", size_pct=1.0
            )
            crawler.close()
            paper = PaperLedger(root / "paper.db")
            lost = paper.open_position(
                chain_id="solana",
                pair_address="pair_lost",
                token_address="lost",
                price=1.0,
                size_usd=400.0,
                meta={"strategy_id": "strat_a"},
            )
            paper.close_position(lost, price=0.1, reason="stop_loss")
            paper.close()
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            prev_loss = gateway_mod.EXECUTION_DAILY_LOSS_USD
            gateway_mod.EXECUTION_RISK_ENABLED = True
            gateway_mod.EXECUTION_DAILY_LOSS_USD = None
            try:
                with ExecutionGateway(
                    decision_db=root / "crawler.db",
                    ledger_path=root / "exec.db",
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    pm_enabled=False,
                ) as gw:
                    gw.process_pending(since_id=0)
                    rows = {r["decision_log_id"]: r for r in gw.ledger.recent(limit=10)}
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
                gateway_mod.EXECUTION_DAILY_LOSS_USD = prev_loss
            buy_receipt = json.loads(rows[buy_id]["receipt_json"])
            sell_receipt = json.loads(rows[sell_id]["receipt_json"])
            self.assertNotEqual(buy_receipt["status"], "risk_rejected")
            self.assertNotEqual(sell_receipt["status"], "risk_rejected")
            self.assertEqual(rows[buy_id]["executor"], "paper")
            self.assertEqual(rows[sell_id]["executor"], "paper")
            self.assertNotIn("daily_loss", rows[buy_id]["receipt_json"])
            self.assertNotIn("daily_loss", rows[sell_id]["receipt_json"])

    def test_risk_event_keeps_max_open_reason(self) -> None:
        detail = _risk_rejected_event_detail(
            [{"ok": False, "reason": "max_open:strat_a:5>=5"}]
        )
        self.assertEqual(
            detail,
            {"stage": "risk", "reason": "max_open:strat_a:5>=5"},
        )


class DefaultGatesTests(unittest.TestCase):
    def test_paper_pm_risk_default_off(self) -> None:
        env = os.environ.copy()
        for key in (
            "EXECUTION_PAPER_ENABLED",
            "PM_ENABLED",
            "EXECUTION_RISK_ENABLED",
            "EXECUTION_PAPER_TICK",
        ):
            env.pop(key, None)
        code = (
            "import sys\n"
            f"sys.path[:0] = [{str(ROOT)!r}, {str(CRAWLER)!r}]\n"
            "import importlib\n"
            "import execution.config as cfg\n"
            "importlib.reload(cfg)\n"
            "from execution.gateway import _env_bool\n"
            "assert cfg.EXECUTION_RISK_ENABLED is False, cfg.EXECUTION_RISK_ENABLED\n"
            "assert cfg.EXECUTION_DAILY_LOSS_USD is None\n"
            "assert _env_bool('EXECUTION_PAPER_ENABLED', False) is False\n"
            "assert _env_bool('PM_ENABLED', False) is False\n"
            "assert _env_bool('EXECUTION_RISK_ENABLED', False) is False\n"
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
