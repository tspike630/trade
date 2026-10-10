"""Offline checks for the read-only dashboard.

Builds temporary SQLite files, reads them through ``DataStore``, and checks that
missing databases degrade instead of raising. Run from the project root::

    python -m dashboard.smoke_test
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dashboard.auth import SecretFilter, verify_api_key
from dashboard.data import DataStore, DbPaths, _WRITE_RE, connect_ro
from dashboard.format_util import fmt_hold, fmt_liq, fmt_pct, fmt_usd
from dashboard.panels.registry import PANELS
from dashboard.parsing import bucket_key, extract_jev_strength
from dashboard.settings import bind_host, port, read_gates, refresh_seconds

_TOUCHED = (
    "API_SECRET",
    "ALLOW_INSECURE_API",
    "DASHBOARD_DB_ROOT",
    "DASHBOARD_PORT",
    "DASHBOARD_HOST",
    "DASHBOARD_REFRESH_SEC",
    "DASHBOARD_MAIN_DB",
    "DASHBOARD_EXECUTION_DB",
    "DASHBOARD_PAPER_DB",
    "DASHBOARD_BACKTEST_DB",
    "DASHBOARD_NEWS_DB",
    "DB_PATH",
    "PAPER_DB_PATH",
    "HEARTBEAT_STALE_SEC",
    "EXECUTION_PAPER_ENABLED",
    "PM_ENABLED",
    "LIVE_TRADING_ENABLED",
    "LIVE_TRADING_CONFIRM",
    "LIVE_KILL_SWITCH_PATH",
    "LIVE_EXCHANGE_API_KEY",
    "LIVE_EXCHANGE_API_SECRET",
)
_WRITE_SQL = re.compile(
    r"(?i)\b(insert\s+into|update\s+[a-z0-9_]+\s+set|delete\s+from|replace\s+into|"
    r"drop\s+table|alter\s+table|create\s+table)\b"
)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def build_fixture(root: Path) -> DbPaths:
    """Create a small readable snapshot. Writes stay in this test helper."""
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)
    stale = now - timedelta(days=2)
    paths = DbPaths(
        main=root / "dexscreener.db",
        execution=root / "execution_ledger.db",
        paper=root / "paper_trading.db",
        backtest=root / "backtest_ledger.db",
        news=root / "news_events.db",
    )
    main = _connect(paths.main)
    main.executescript(
        """
        CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            action TEXT NOT NULL,
            chain_id TEXT,
            token_address TEXT,
            size_pct REAL,
            reason TEXT,
            risk_passed INTEGER,
            mode TEXT NOT NULL,
            raw_json TEXT
        );
        CREATE TABLE pair_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT NOT NULL,
            pair_address TEXT NOT NULL,
            token_address TEXT,
            recorded_at TEXT NOT NULL,
            price_usd REAL,
            liquidity_usd REAL
        );
        CREATE TABLE jev_signals (
            chain_id TEXT NOT NULL,
            token_address TEXT NOT NULL,
            coverage_pct REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (chain_id, token_address)
        );
        CREATE TABLE indicator_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT NOT NULL,
            token_address TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            jev_strength REAL
        );
        CREATE TABLE tokens (
            chain_id TEXT NOT NULL,
            token_address TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (chain_id, token_address)
        );
        CREATE TABLE run_checkpoint (
            cycle_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            step TEXT NOT NULL
        );
        """
    )
    raw_t1 = json.dumps(
        {
            "extras": {"jev_strength": 0.81, "coverage": 12},
            "pair_address": "P1",
            "liquidity": {"usd": 1},
        }
    )
    raw_t2 = json.dumps({"pair_address": "P2"})
    main.executemany(
        """
        INSERT INTO decision_log
            (ts, strategy_id, action, chain_id, token_address, reason, mode, raw_json)
        VALUES (?, 'alpha', ?, 'solana', ?, ?, 'dry_run', ?)
        """,
        [
            (_iso(old), "buy", "T0", "old", json.dumps({"pair_address": "P0"})),
            (_iso(now), "buy", "T1", "strength ok", raw_t1),
            (_iso(now), "sell", "T2", "jev_strength=0.42 note", raw_t2),
        ],
    )
    main.executemany(
        """
        INSERT INTO pair_history
            (chain_id, pair_address, token_address, recorded_at, price_usd, liquidity_usd)
        VALUES ('solana', ?, ?, ?, ?, ?)
        """,
        [
            ("P1", "T1", _iso(stale), 9.0, 100.0),
            ("P1", "T1", _iso(now), 1.25, 12345.6),
            ("P2", "T2", _iso(now), 0.5, 999.0),
        ],
    )
    main.executemany(
        "INSERT INTO jev_signals (chain_id, token_address, coverage_pct, fetched_at) VALUES ('solana', ?, ?, ?)",
        [("T1", 88, _iso(stale)), ("T2", 55, _iso(stale))],
    )
    main.execute(
        """
        INSERT INTO indicator_history (chain_id, token_address, recorded_at, jev_strength)
        VALUES ('solana', 'T2', ?, 0.1)
        """,
        (_iso(now),),
    )
    main.execute(
        "INSERT INTO tokens (chain_id, token_address, raw_json) VALUES ('solana', 'T1', ?)",
        (json.dumps({"symbol": "BONK"}),),
    )
    main.execute(
        """
        INSERT INTO run_checkpoint (cycle_id, started_at, updated_at, status, step)
        VALUES ('c1', ?, ?, 'running', 'pairs')
        """,
        (_iso(now), _iso(now)),
    )
    main.commit()
    main.close()

    execution = _connect(paths.execution)
    execution.execute(
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
    execution.executemany(
        """
        INSERT INTO execution_log (
            ts, decision_log_id, strategy_id, action, chain_id, token_address, pair_address,
            validation_ok, validation_errors, executed, executor, receipt_json
        ) VALUES (?, ?, 'alpha', ?, 'solana', ?, ?, ?, ?, 0, 'dry_run', ?)
        """,
        [
            (_iso(old), 1, "buy", "T0", "P0", 0, "ancient_rule", "{}"),
            (
                _iso(now),
                2,
                "buy",
                "T1",
                "P1",
                1,
                "",
                json.dumps({"status": "skipped", "api_key": "supersecretvalue", "tx": "sig123"}),
            ),
            (_iso(now), 3, "sell", "T2", "P2", 0, "liq_too_low; wide spread", "{}"),
            (_iso(now), 3, "sell", "T2", "P2", 0, "liq_too_low; still wide", "{}"),
        ],
    )
    execution.commit()
    execution.close()

    paper = _connect(paths.paper)
    paper.executescript(
        """
        CREATE TABLE account (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cash_usd REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE positions (
            id TEXT PRIMARY KEY,
            chain_id TEXT NOT NULL,
            pair_address TEXT NOT NULL,
            token_address TEXT,
            qty REAL NOT NULL,
            entry_price REAL NOT NULL,
            size_usd REAL NOT NULL,
            opened_at TEXT NOT NULL,
            status TEXT NOT NULL,
            tp_pct REAL,
            sl_pct REAL,
            max_hold_min REAL,
            closed_at TEXT,
            pnl_usd REAL
        );
        """
    )
    paper.execute(
        "INSERT INTO account (id, cash_usd, updated_at) VALUES (1, 10000, ?)",
        (_iso(now),),
    )
    paper.executemany(
        """
        INSERT INTO positions (
            id, chain_id, pair_address, token_address, qty, entry_price, size_usd,
            opened_at, status, tp_pct, sl_pct, max_hold_min, closed_at
        ) VALUES (?, 'solana', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("pos-open", "P1", "T1", 100, 1.0, 100, _iso(now - timedelta(minutes=90)), "open", 25, 10, 240, None),
            ("pos-missing", "PX", "TX", 10, 2.0, 20, _iso(now - timedelta(minutes=10)), "open", None, None, None, None),
            ("pos-closed", "P9", "T9", 999, 1.0, 999, _iso(old), "closed", None, None, None, _iso(now)),
        ],
    )
    paper.commit()
    paper.close()

    backtest = _connect(paths.backtest)
    backtest.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            strategy_id TEXT,
            start_cash REAL,
            end_equity REAL,
            status TEXT,
            notes TEXT
        )
        """
    )
    backtest.execute(
        """
        INSERT INTO runs (run_id, started_at, finished_at, strategy_id, start_cash, end_equity, status, notes)
        VALUES ('bt-1', ?, ?, 'alpha', 10000, 11000, 'finished', 'baseline run')
        """,
        (_iso(now), _iso(now)),
    )
    backtest.commit()
    backtest.close()

    news = _connect(paths.news)
    news.execute(
        """
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_tier TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT,
            onchain_status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    news.execute(
        """
        INSERT INTO news_events (source, source_tier, title, url, published_at, onchain_status)
        VALUES ('rss', 'a', 'Solana validator update', 'https://example.com/solana', ?, 'pending')
        """,
        (_iso(now),),
    )
    news.commit()
    news.close()
    return paths


class DashboardSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _TOUCHED}
        for key in _TOUCHED:
            os.environ.pop(key, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="dash db ")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_fixture_numbers_match_review_definitions(self) -> None:
        paths = build_fixture(self.root)
        before = {path: path.stat().st_mtime_ns for path in (paths.main, paths.execution, paths.paper, paths.backtest, paths.news)}
        sizes = {path: path.stat().st_size for path in before}
        store = DataStore(paths)

        today = store.today_execution_stats()
        self.assertTrue(today.available, today.reason)
        self.assertEqual(today.data.n, 3)
        self.assertEqual(today.data.n_ok, 1)
        self.assertEqual(today.data.n_executed, 0)
        self.assertAlmostEqual(today.data.ok_rate, 1 / 3)
        self.assertEqual(today.data.executed_rate, 0.0)

        week = store.week_decision_stats()
        self.assertTrue(week.available, week.reason)
        self.assertEqual(week.data.n_decisions, 2)
        self.assertEqual(week.data.n_with_exec, 2)
        self.assertEqual(week.data.n_ok, 1)
        self.assertEqual(week.data.n_executed, 0)
        self.assertAlmostEqual(week.data.ok_rate, 0.5)
        self.assertEqual(week.data.executed_rate, 0.0)

        failures = store.recent_failures()
        self.assertTrue(failures.available, failures.reason)
        self.assertEqual(failures.data[0].id, 4)
        self.assertIn("liq_too_low", failures.data[0].validation_errors)

        report = store.rejection_buckets()
        self.assertTrue(report.available, report.reason)
        self.assertEqual(report.data.total, 2)
        self.assertEqual(report.data.buckets[0].key, "liq_too_low")
        self.assertEqual(report.data.buckets[0].count, 2)

        signals = store.recent_signals()
        self.assertTrue(signals.available, signals.reason)
        self.assertEqual(signals.data.exec_note, "")
        self.assertEqual(signals.data.rows[0].decision_id, 3)
        by_id = {row.decision_id: row for row in signals.data.rows}
        self.assertEqual(by_id[2].jev_strength, 0.81)
        self.assertEqual(by_id[2].coverage, 88)
        self.assertAlmostEqual(by_id[2].liquidity_usd or 0, 12345.6)
        self.assertEqual(by_id[2].validation_ok, 1)
        self.assertFalse(by_id[2].exec_dup)
        self.assertEqual(by_id[3].jev_strength, 0.42)
        self.assertEqual(by_id[3].coverage, 55)
        self.assertAlmostEqual(by_id[3].liquidity_usd or 0, 999)
        self.assertEqual(by_id[3].validation_ok, 0)
        self.assertTrue(by_id[3].exec_dup)
        self.assertNotIn("supersecretvalue", by_id[2].reason)

        executions = store.recent_executions()
        receipt = next(row for row in executions.data if row.id == 2)
        self.assertIn("skipped", receipt.receipt_summary)
        self.assertIn("sig123", receipt.receipt_summary)
        self.assertNotIn("supersecretvalue", receipt.receipt_summary)

        positions = store.open_positions()
        self.assertTrue(positions.available, positions.reason)
        payload = positions.data
        self.assertEqual(payload.open_count, 2)
        self.assertEqual(payload.cash_usd, 10000)
        self.assertAlmostEqual(payload.invested_usd, 145)
        self.assertAlmostEqual(payload.equity_usd, 10145)
        self.assertAlmostEqual(payload.position_ratio or 0, 145 / 10145)
        self.assertEqual(payload.missing_price, 1)
        open_row = next(row for row in payload.rows if row.pair_address == "P1")
        self.assertEqual(open_row.symbol, "BONK")
        self.assertAlmostEqual(open_row.mark_price or 0, 1.25)
        self.assertAlmostEqual(open_row.pnl_pct or 0, 25)
        self.assertAlmostEqual(open_row.pnl_usd or 0, 25)
        self.assertAlmostEqual(open_row.mtm_usd or 0, 125)
        self.assertGreater(open_row.hold_seconds or 0, 80 * 60)
        self.assertLess(open_row.hold_seconds or 0, 100 * 60)
        self.assertTrue(all(row.id != "pos-closed" for row in payload.rows))

        probes = {item.name: item for item in store.process_liveness().data}
        self.assertEqual(probes["主爬虫"].state, "alive")
        self.assertEqual(probes["指标"].state, "alive")
        self.assertEqual(probes["JEV"].state, "stale")

        run = store.latest_backtest().data
        self.assertEqual(run.run_id, "bt-1")
        self.assertEqual(run.end_equity, 11000)
        news = store.latest_news().data
        self.assertEqual(news[0].title, "Solana validator update")

        for path, stamp in before.items():
            self.assertEqual(path.stat().st_mtime_ns, stamp)
            self.assertEqual(path.stat().st_size, sizes[path])

    def test_read_only_connection_rejects_writes(self) -> None:
        paths = build_fixture(self.root)
        before = paths.main.stat().st_mtime_ns
        conn = connect_ro(paths.main)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute("INSERT INTO decision_log (ts, strategy_id, action, mode) VALUES ('t', 's', 'buy', 'dry')")
        finally:
            conn.close()
        self.assertEqual(paths.main.stat().st_mtime_ns, before)
        self.assertIsNone(_WRITE_RE.search("SELECT updated_at, created_at FROM run_checkpoint"))
        self.assertIsNotNone(_WRITE_RE.search("INSERT INTO decision_log VALUES (1)"))

    def test_missing_empty_corrupt_and_open_ts(self) -> None:
        empty = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        for result in (
            empty.today_execution_stats(),
            empty.recent_signals(),
            empty.open_positions(),
            empty.recent_executions(),
            empty.rejection_buckets(),
            empty.latest_backtest(),
            empty.latest_news(),
            empty.process_liveness(),
        ):
            self.assertFalse(result.available)
            self.assertIn("数据库文件不存在", result.reason)

        bare = self.root / "bare.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(
            DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare)
        )
        signals = bare_store.recent_signals()
        self.assertFalse(signals.available)
        self.assertIn("缺少数据表", signals.reason)

        corrupt = self.root / "corrupt.db"
        corrupt.write_text("this is not sqlite", encoding="utf-8")
        corrupt_store = DataStore(
            DbPaths(main=corrupt, execution=corrupt, paper=corrupt, backtest=corrupt, news=corrupt)
        )
        self.assertFalse(corrupt_store.recent_signals().available)

        exec_db = self.root / "execution_ledger.db"
        paper_db = self.root / "paper_trading.db"
        with sqlite3.connect(exec_db) as conn:
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    validation_ok INTEGER,
                    executed INTEGER,
                    validation_errors TEXT
                )
                """
            )
        with sqlite3.connect(paper_db) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 1000)")
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    chain_id TEXT,
                    pair_address TEXT,
                    token_address TEXT,
                    entry_price REAL,
                    size_usd REAL,
                    open_ts TEXT,
                    status TEXT
                )
                """
            )
            opened = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
            conn.execute(
                """
                INSERT INTO positions (id, chain_id, pair_address, token_address, entry_price, size_usd, open_ts, status)
                VALUES ('p', 'solana', 'P', 'T', 2, 50, ?, 'open')
                """,
                (opened,),
            )
        partial = DataStore(
            DbPaths(
                main=self.root / "no-main.db",
                execution=exec_db,
                paper=paper_db,
                backtest=self.root / "no-bt.db",
                news=self.root / "no-news.db",
            )
        )
        today = partial.today_execution_stats()
        self.assertTrue(today.available, today.reason)
        self.assertEqual(today.data.n, 0)
        self.assertIsNone(today.data.ok_rate)
        positions = partial.open_positions()
        self.assertTrue(positions.available, positions.reason)
        self.assertEqual(positions.data.open_count, 1)
        self.assertIsNone(positions.data.rows[0].mark_price)
        self.assertAlmostEqual(positions.data.rows[0].mtm_usd or 0, 50)
        self.assertIn("不存在", positions.data.price_note)
        self.assertIsNotNone(positions.data.rows[0].hold_seconds)

    def test_signal_limit_and_secret_scrub(self) -> None:
        main = self.root / "dexscreener.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    strategy_id TEXT,
                    action TEXT,
                    chain_id TEXT,
                    token_address TEXT,
                    reason TEXT,
                    mode TEXT,
                    raw_json TEXT
                )
                """
            )
            secret = "Zx9-secret-value-77"
            os.environ["API_SECRET"] = secret
            rows = [
                (index, "2026-10-10T00:00:00Z", "alpha", "buy", "solana", f"T{index}", f"leak {secret}", "dry_run", "{}")
                for index in range(1, 56)
            ]
            conn.executemany(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address, reason, mode, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        result = store.recent_signals(50)
        self.assertTrue(result.available, result.reason)
        self.assertEqual(len(result.data.rows), 50)
        self.assertEqual(result.data.rows[0].decision_id, 55)
        self.assertIn("不存在", result.data.exec_note)
        self.assertNotIn(secret, result.data.rows[0].reason)
        self.assertIn("[redacted]", result.data.rows[0].reason)

    def test_gates_auth_and_formatting(self) -> None:
        # Isolate from the live project's LIVE_TRADING_DISABLED kill-switch file.
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        os.environ.pop("LIVE_KILL_SWITCH_PATH", None)
        self.assertFalse(read_gates().paper_enabled)
        self.assertFalse(read_gates().pm_enabled)
        self.assertFalse(read_gates().live_effective)
        os.environ["EXECUTION_PAPER_ENABLED"] = "YES"
        os.environ["PM_ENABLED"] = "on"
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        status = read_gates()
        self.assertTrue(status.paper_enabled)
        self.assertTrue(status.pm_enabled)
        self.assertFalse(status.live_effective)
        self.assertEqual(status.live_block_reason, "确认短语未匹配")

        (self.root / "LIVE_TRADING_DISABLED").write_text("", encoding="utf-8")
        blocked = read_gates()
        self.assertTrue(blocked.kill_switch_present)
        self.assertEqual(blocked.live_block_reason, "杀开关文件存在")
        self.assertFalse(blocked.live_effective)

        os.environ["LIVE_TRADING_ENABLED"] = "0"
        self.assertEqual(read_gates().live_block_reason, "环境变量关闭")
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        os.environ["LIVE_KILL_SWITCH_PATH"] = str(self.root / "absent-switch")
        os.environ["LIVE_TRADING_CONFIRM"] = "I_UNDERSTAND_THE_RISKS"
        armed = read_gates()
        self.assertTrue(armed.live_effective)
        self.assertEqual(armed.confirm_state, "matched")
        self.assertNotIn("I_UNDERSTAND_THE_RISKS", armed.live_block_reason)

        os.environ["DASHBOARD_HOST"] = "0.0.0.0"
        self.assertEqual(bind_host(), "127.0.0.1")
        os.environ["DASHBOARD_PORT"] = "nope"
        self.assertEqual(port(), 8081)
        os.environ["DASHBOARD_PORT"] = "9091"
        self.assertEqual(port(), 9091)
        os.environ["DASHBOARD_REFRESH_SEC"] = "10"
        self.assertEqual(refresh_seconds(), 30)
        os.environ["DASHBOARD_REFRESH_SEC"] = "90"
        self.assertEqual(refresh_seconds(), 60)
        os.environ["DASHBOARD_REFRESH_SEC"] = "45"
        self.assertEqual(refresh_seconds(), 45)

        os.environ["API_SECRET"] = "abcdefghijklmnop"
        self.assertTrue(verify_api_key("abcdefghijklmnop"))
        self.assertFalse(verify_api_key("abcdefghijklmnoP"))
        self.assertFalse(verify_api_key("short"))
        self.assertFalse(verify_api_key(""))
        self.assertFalse(verify_api_key(None))

        record = logging.LogRecord("dashboard", logging.INFO, __file__, 1, "leak abcdefghijklmnop", None, None)
        self.assertTrue(SecretFilter().filter(record))
        self.assertNotIn("abcdefghijklmnop", record.getMessage())

        self.assertEqual(bucket_key("liq_too_low; wide\nmore"), "liq_too_low")
        self.assertEqual(bucket_key("   "), "(empty)")
        self.assertEqual(bucket_key(None), "(empty)")
        self.assertEqual(len(bucket_key("a" * 100)), 80)
        self.assertEqual(extract_jev_strength({"extras": {"jev_strength": "0.81"}}, None), 0.81)
        self.assertEqual(extract_jev_strength({}, "jev_strength=0.42 tail"), 0.42)
        self.assertEqual(fmt_pct(25), "+25.00%")
        self.assertEqual(fmt_pct(None), "—")
        self.assertEqual(fmt_usd(10145), "$10,145.00")
        self.assertEqual(fmt_usd(-2, signed=True), "-$2.00")
        self.assertEqual(fmt_liq(12345.6), "$12.3K")
        self.assertEqual(fmt_hold(5400), "1小时30分")
        self.assertEqual([item.id for item in PANELS], ["risk", "positions", "signals", "execution"])

    def test_package_has_no_write_sql_or_trade_controls(self) -> None:
        root = Path(__file__).resolve().parent
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "smoke_test.py":
                continue
            text = path.read_text(encoding="utf-8")
            if _WRITE_SQL.search(text):
                offenders.append(f"write sql in {path.name}")
            if "sqlite3.connect" in text and path.name != "data.py":
                offenders.append(f"sqlite connect in {path.name}")
            assignments = re.findall(r"os\.environ\[[^\]]+\]\s*=", text)
            allowed_storage = path.name == "__init__.py" and assignments == ['os.environ["NICEGUI_STORAGE_PATH"] =']
            if assignments and not allowed_storage:
                offenders.append(f"env assignment in {path.name}: {assignments}")
            for banned in ("ui.switch", "ui.toggle", "ui.checkbox", "ui.html(", "place_order"):
                if banned in text:
                    offenders.append(f"{banned} in {path.name}")
        data = (root / "data.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", data)
        self.assertIn("query_only=ON", data)
        common = (root / "panels" / "common.py").read_text(encoding="utf-8")
        self.assertIn("数据不可用", common)
        for name in ("risk.py", "positions.py", "signals.py", "execution.py"):
            self.assertIn("show_unavailable", (root / "panels" / name).read_text(encoding="utf-8"))
        self.assertEqual(offenders, [])

    def test_http_gate_hides_fixture_data(self) -> None:
        build_fixture(self.root)
        secret = "dashboard-demo-key-2026"
        env = os.environ.copy()
        env["API_SECRET"] = secret
        env["DASHBOARD_DB_ROOT"] = str(self.root)
        env["DASHBOARD_PORT"] = "8766"
        env["NICEGUI_STORAGE_PATH"] = str(self.root / "sessions")
        env.pop("ALLOW_INSECURE_API", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "dashboard.app"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output = ""
        try:
            deadline = time.time() + 25
            health = ""
            while time.time() < deadline:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    self.fail(f"server exited early:\n{output}")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8766/health", timeout=1) as response:
                        health = response.read().decode()
                    break
                except Exception:
                    time.sleep(0.25)
            self.assertIn("trading-dashboard", health)
            self.assertIn("true", health)
            self.assertNotIn(secret, health)
            request = urllib.request.Request("http://127.0.0.1:8766/", method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:
                html = response.read().decode("utf-8", errors="replace")
            self.assertNotIn(secret, html)
            self.assertNotIn("BONK", html)
            self.assertNotIn("supersecretvalue", html)
            self.assertNotIn("liq_too_low", html)
        finally:
            proc.terminate()
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                output, _ = proc.communicate(timeout=5)
        self.assertNotIn(secret, output or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
