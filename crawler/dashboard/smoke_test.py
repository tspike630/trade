"""Offline checks for the read-only dashboard.

Builds temporary SQLite files, reads them through ``DataStore``, and checks that
missing databases degrade instead of raising. Run from the project root::

    python -m dashboard.smoke_test
"""

from __future__ import annotations

import csv
import inspect
from collections.abc import Callable
import io
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
from zoneinfo import ZoneInfo
from pathlib import Path

from dashboard.auth import SecretFilter, attach_secret_filters, verify_api_key
from dashboard.chat.sessions import SessionStore
from dashboard.data import (
    GOAL_DEADLINE_DAY,
    GOAL_START_DAY,
    GOAL_TARGET_USD,
    DataStore,
    DbPaths,
    _WRITE_RE,
    connect_ro,
)
from dashboard.format_util import fmt_hold, fmt_liq, fmt_pct, fmt_usd
from dashboard.panels.evolution import (
    find_latest_weekly_report,
    load_evolution_suggestions,
    parse_evolution_section,
    status_class,
)
from dashboard.panels.markets import _row as market_table_row
from dashboard.panels.registry import PANELS
from dashboard.panels.shell import (
    TOP_TABS,
    db_status_line,
    heartbeat_summary,
    host_visible,
    live_caption,
    panel_group,
    refresh_age_label,
    select_top_tab,
)
import dashboard.app as app_module
from dashboard.shadow_phase0 import FLAG_LIQ, FLAG_R3, PHASE0_NAIL, load_shadow_phase0
from nicegui.client import Client
from nicegui.page import page as ng_page
from dashboard.app import backtest_equity_tone, jev_gate_view, prior_pulse_tone
from dashboard.parsing import bucket_key, extract_jev_strength, liq_bucket_label, rejection_key
from dashboard.settings import (
    bind_host,
    gateway_stale_seconds,
    port,
    read_gates,
    refresh_seconds,
    stale_seconds,
)

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
    "JEV_TREND_CSV",
    "SHADOW_PHASE0_CSV",
    "DB_PATH",
    "PAPER_DB_PATH",
    "HEARTBEAT_STALE_SEC",
    "GATEWAY_HEARTBEAT_STALE_SEC",
    "EXECUTION_LEDGER_PATH",
    "EXECUTION_PAPER_ENABLED",
    "PM_ENABLED",
    "LIVE_TRADING_ENABLED",
    "LIVE_TRADING_CONFIRM",
    "LIVE_KILL_SWITCH_PATH",
    "LIVE_EXCHANGE_API_KEY",
    "LIVE_EXCHANGE_API_SECRET",
    "LIVE_EXCHANGE_API_URL",
)
_WRITE_SQL = re.compile(
    r"(?i)\b(insert\s+into|update\s+[a-z0-9_]+\s+set|delete\s+from|replace\s+into|"
    r"drop\s+table|alter\s+table|create\s+table)\b"
)


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rendered_card_texts(build: Callable[[], None]) -> list[str]:
    """Render one overview card in a NiceGUI client and collect label text."""
    client = Client(ng_page("/_goal_20k_smoke"), request=None)
    try:
        with client:
            build()
        texts: list[str] = []
        for element in client.elements.values():
            text = getattr(element, "text", None)
            if isinstance(text, str) and text.strip():
                texts.append(text)
        return texts
    finally:
        client.delete()


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
        self.assertEqual(
            [item.name for item in store.process_liveness().data],
            ["主爬虫", "指标", "JEV", "决策引擎", "执行网关"],
        )
        self.assertEqual(probes["主爬虫"].state, "alive")
        self.assertEqual(probes["指标"].state, "alive")
        self.assertEqual(probes["JEV"].state, "stale")
        self.assertEqual(probes["决策引擎"].state, "alive")
        self.assertEqual(probes["执行网关"].state, "alive")

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
            empty.paper_closed_buckets(),
            empty.intel_holder_observe(),
            empty.intel_slippage_observe(),
            empty.intel_crowding_note(),
            empty.token_age_observe(),
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
        idle = read_gates()
        self.assertFalse(idle.paper_enabled)
        self.assertFalse(idle.pm_enabled)
        self.assertFalse(idle.live_effective)
        self.assertEqual(idle.paper_origin, "看板进程")
        self.assertEqual(idle.pm_origin, "看板进程")
        self.assertFalse(idle.gateway_stale)
        self.assertFalse(idle.exchange_url_set)
        os.environ["EXECUTION_PAPER_ENABLED"] = "YES"
        os.environ["PM_ENABLED"] = "on"
        os.environ["LIVE_TRADING_ENABLED"] = "1"
        status = read_gates()
        self.assertTrue(status.paper_enabled)
        self.assertTrue(status.pm_enabled)
        self.assertEqual(status.paper_origin, "看板进程")
        self.assertEqual(status.origin_hint("paper"), "看板进程")
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
        missing_url = read_gates()
        self.assertFalse(missing_url.live_effective)
        self.assertEqual(missing_url.live_block_reason, "交易所 URL 未设置")
        self.assertEqual(missing_url.confirm_state, "matched")
        os.environ["LIVE_EXCHANGE_API_URL"] = "https://exchange.example/orders"
        armed = read_gates()
        self.assertTrue(armed.live_effective)
        self.assertTrue(armed.exchange_url_set)
        self.assertEqual(armed.confirm_state, "matched")
        self.assertNotIn("I_UNDERSTAND_THE_RISKS", armed.live_block_reason)
        self.assertNotIn("exchange.example", armed.live_block_reason)

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
        self.assertEqual(bucket_key('["liq_too_low", "wide spread"]'), "liq_too_low")
        self.assertEqual(bucket_key("   "), "(empty)")
        self.assertEqual(bucket_key(None), "(empty)")
        self.assertEqual(len(bucket_key("a" * 100)), 80)
        self.assertEqual(rejection_key(1, "", "rejected", {"status": "pm_rejected"}), "pm_rejected")
        self.assertEqual(
            rejection_key(
                1,
                "",
                "rejected",
                {
                    "status": "pm_rejected",
                    "pm_check": {"allowed": False, "reason": "pm_rejected:max_open_per_strategy"},
                },
            ),
            "pm_rejected:max_open_per_strategy",
        )
        self.assertEqual(
            rejection_key(
                1,
                "",
                "rejected",
                {
                    "status": "pm_rejected",
                    "pm_check": {"allowed": False, "reason": "pm_rejected:token_dedup"},
                },
            ),
            "pm_rejected:token_dedup",
        )
        self.assertEqual(rejection_key(1, "", "validated", {"status": "paper_failed"}), "paper_failed")
        self.assertEqual(
            rejection_key(
                1,
                "",
                "rejected",
                {
                    "status": "risk_rejected",
                    "risk": [{"ok": False, "reason": "daily_loss:2026-10-14:-316.5<-200.0"}],
                },
            ),
            "risk_rejected:daily_loss",
        )
        self.assertEqual(rejection_key(0, '["slippage"]', "rejected", None), "slippage")
        self.assertIsNone(rejection_key(1, "", "validated", {"status": "dry_run_acked"}))
        self.assertEqual(liq_bucket_label(11994.57), "liq_<50k")
        self.assertEqual(liq_bucket_label(50_000), "liq_>=50k")
        self.assertEqual(liq_bucket_label(None), "liq_unknown")
        self.assertEqual(backtest_equity_tone(11000, 10000), "up")
        self.assertEqual(backtest_equity_tone(9000, 10000), "down")
        self.assertEqual(backtest_equity_tone(15000, None), "mute")
        self.assertEqual(backtest_equity_tone(10000, 10000), "mute")
        self.assertEqual(extract_jev_strength({"extras": {"jev_strength": "0.81"}}, None), 0.81)
        self.assertEqual(extract_jev_strength({}, "jev_strength=0.42 tail"), 0.42)
        self.assertEqual(fmt_pct(25), "+25.00%")
        self.assertEqual(fmt_pct(None), "—")
        self.assertEqual(fmt_usd(10145), "$10,145.00")
        self.assertEqual(fmt_usd(-2, signed=True), "-$2.00")
        self.assertEqual(fmt_liq(12345.6), "$12.3K")
        self.assertEqual(fmt_hold(5400), "1小时30分")
        self.assertEqual([item.id for item in PANELS], ["risk", "positions", "signals", "execution", "market", "evolution"])
        self.assertEqual(panel_group("risk"), "overview")
        self.assertEqual(panel_group("positions"), "overview")
        self.assertEqual(panel_group("signals"), "signals")
        self.assertEqual(panel_group("execution"), "execution")
        self.assertEqual(panel_group("evolution"), "overview")
        self.assertEqual(panel_group("later"), "overview")
        self.assertEqual(heartbeat_summary(["alive", "stale", "unknown"]), ("进程 1/3 存活", "ok"))
        self.assertEqual(heartbeat_summary(["stale"]), ("进程 0/1 存活", "warn"))
        self.assertEqual(heartbeat_summary([]), ("进程 —", "muted"))
        self.assertEqual(live_caption("ok"), "● 实时")
        self.assertEqual(live_caption("warn"), "● 停滞")
        self.assertEqual(db_status_line([("主库", "a.db", True), ("新闻", "b.db", False)]), "库 1/2 缺失:新闻")
        self.assertEqual(refresh_age_label(None), "刷新 —")
        self.assertEqual(refresh_age_label(3), "刷新 3s 前")
        app_text = Path(__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertNotIn("end >= 10000", app_text)
        self.assertIn("attach_secret_filters", app_text)
        for label in (
            "SPIKE'S",
            "CRYPTO",
            "TERMINER",
            "链上新币投研终端",
            "DASHBOARD",
            "MARKETS",
            "AI CHAT",
            "OVERVIEW",
            "SIGNALS",
            "EXECUTION",
            "/chat",
        ):
            self.assertIn(label, app_text)
        self.assertNotIn("TERMINAL", app_text)
        for marker in ("v2-topbar", "v2-subbar", "v2-statusbar", "v2-grid", "v2-kpi", "span-8"):
            self.assertIn(marker, app_text)
        self.assertNotIn("gv2-", app_text)
        self.assertIn("grid-auto-rows: minmax(84px, auto)", app_text)
        self.assertIn("min-height: 84px", app_text)
        self.assertIn(".v2-warnbar", app_text)
        self.assertIn("|x|<=500", app_text)
        self.assertNotIn("|x|≤", app_text)
        self.assertNotIn("不授权", app_text)
        self.assertIn("不调权", app_text)
        self.assertIn("延迟 --ms", app_text)
        self.assertIn('classes("v2-statusdot ok right")', app_text)
        self.assertIn("flex-direction: row !important", app_text)
        render_src = inspect.getsource(app_module._render_dashboard)
        module_src = inspect.getsource(app_module._module_tabs)
        self.assertIn("v2-grid", render_src)
        self.assertNotIn("v2-subbar", render_src)
        self.assertIn("v2-subbar", module_src)
        self.assertNotIn("v2-grid", module_src)
        self.assertIn("v2-warnbar", render_src)
        self.assertEqual(prior_pulse_tone("4H 存活", "75.3%"), "up")
        self.assertEqual(prior_pulse_tone("4H 存活", "59%"), "")
        self.assertEqual(prior_pulse_tone("当天死", "68.67%"), "down")
        self.assertEqual(prior_pulse_tone("当天死", "49%"), "")
        self.assertEqual(prior_pulse_tone("毕业率", "0.2%"), "mute")
        self.assertEqual(prior_pulse_tone("见顶 P50", "1.0 min"), "mute")
        view = jev_gate_view(
            {
                "filtered_n": "14",
                "unique_token": "9",
                "up_1h_rate": "0.714",
                "up_4h_rate": "0.571",
            }
        )
        assert view is not None
        self.assertEqual([item[1] for item in view], ["14", "9", "71.4%", "57.1%"])
        self.assertEqual(view[2][2], "up")
        self.assertIsNone(jev_gate_view(None))
        trend = self.root / "jev_validity_trend.csv"
        trend.write_text(
            "date,filtered_n,unique_token,up_1h_rate,up_4h_rate\n"
            "2026-10-01,1,1,0.100000,0.100000\n"
            "2026-10-08,14,9,0.714000,0.571000\n",
            encoding="utf-8",
        )
        os.environ["JEV_TREND_CSV"] = str(trend)
        try:
            loaded = jev_gate_view(app_module._jev_trend_row())
        finally:
            os.environ.pop("JEV_TREND_CSV", None)
        assert loaded is not None
        self.assertEqual(loaded[0][1], "14")
        self.assertEqual(loaded[2][1], "71.4%")
        market_src = inspect.getsource(DataStore._market_rows)
        for banned in ("rsi", "macd", "boll"):
            self.assertNotIn(banned, market_src.lower())

    def test_fresh_receipt_overrides_process_env(self) -> None:
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        os.environ["EXECUTION_PAPER_ENABLED"] = "0"
        os.environ["PM_ENABLED"] = "1"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger = self.root / "execution_ledger.db"
        with sqlite3.connect(ledger) as conn:
            conn.execute(
                "CREATE TABLE execution_log (id INTEGER PRIMARY KEY, ts TEXT, receipt_json TEXT)"
            )
            conn.execute(
                "INSERT INTO execution_log (ts, receipt_json) VALUES (?, ?)",
                (now, json.dumps({"paper_enabled": True, "pm_enabled": False})),
            )
        status = read_gates()
        self.assertTrue(status.paper_enabled)
        self.assertFalse(status.pm_enabled)
        self.assertEqual(status.paper_origin, "网关回执")
        self.assertEqual(status.pm_origin, "网关回执")
        self.assertFalse(status.gateway_stale)
        self.assertEqual(status.origin_hint("paper"), "网关回执")

    def test_stale_receipt_falls_back_and_is_labeled(self) -> None:
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        os.environ["EXECUTION_PAPER_ENABLED"] = "0"
        os.environ["PM_ENABLED"] = "0"
        old = (datetime.now(timezone.utc) - timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger = self.root / "execution_ledger.db"
        with sqlite3.connect(ledger) as conn:
            conn.execute(
                "CREATE TABLE execution_log (id INTEGER PRIMARY KEY, ts TEXT, receipt_json TEXT)"
            )
            conn.execute(
                "INSERT INTO execution_log (ts, receipt_json) VALUES (?, ?)",
                (old, json.dumps({"paper_enabled": True, "pm_enabled": True})),
            )
        status = read_gates()
        self.assertFalse(status.paper_enabled)
        self.assertFalse(status.pm_enabled)
        self.assertTrue(status.gateway_stale)
        self.assertEqual(status.paper_origin, "看板进程")
        self.assertEqual(status.origin_hint("pm"), "网关陈旧 · 看板进程")

    def test_execution_ledger_path_and_gateway_window(self) -> None:
        self.assertEqual(stale_seconds(), 900.0)
        self.assertEqual(gateway_stale_seconds(), 120.0)
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        ledger = self.root / "from-execution.db"
        os.environ["EXECUTION_LEDGER_PATH"] = str(ledger)
        self.assertEqual(DbPaths.from_env().execution, ledger)
        override = self.root / "override.db"
        os.environ["DASHBOARD_EXECUTION_DB"] = str(override)
        self.assertEqual(DbPaths.from_env().execution, override)

        aged = datetime.now(timezone.utc) - timedelta(seconds=200)
        main = self.root / "dexscreener.db"
        execution = self.root / "execution_ledger.db"
        with sqlite3.connect(main) as conn:
            conn.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT)")
            conn.execute("INSERT INTO decision_log (ts) VALUES (?)", (_iso(aged),))
        with sqlite3.connect(execution) as conn:
            conn.execute("CREATE TABLE execution_log (id INTEGER PRIMARY KEY, ts TEXT)")
            conn.execute("INSERT INTO execution_log (ts) VALUES (?)", (_iso(aged),))
        store = DataStore(
            DbPaths(
                main=main,
                execution=execution,
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        probes = {item.name: item for item in store.process_liveness().data}
        self.assertEqual(probes["决策引擎"].state, "alive")
        self.assertEqual(probes["执行网关"].state, "stale")

    def test_rejection_buckets_include_status_and_json_errors(self) -> None:
        now = datetime.now(timezone.utc)
        execution = self.root / "execution_ledger.db"
        with sqlite3.connect(execution) as conn:
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    validation_ok INTEGER,
                    validation_errors TEXT,
                    status TEXT,
                    receipt_json TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO execution_log
                    (id, ts, validation_ok, validation_errors, status, receipt_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        1,
                        _iso(now),
                        1,
                        "",
                        "rejected",
                        json.dumps(
                            {
                                "status": "pm_rejected",
                                "pm_check": {
                                    "allowed": False,
                                    "reason": "pm_rejected:max_open_per_strategy",
                                },
                            }
                        ),
                    ),
                    (2, _iso(now), 1, "", "rejected", json.dumps({"status": "risk_rejected"})),
                    (3, _iso(now), 1, "", "validated", json.dumps({"status": "paper_failed"})),
                    (4, _iso(now), 0, '["slippage", "wide"]', "rejected", "{}"),
                    (5, _iso(now), 1, "", "validated", json.dumps({"status": "dry_run_acked"})),
                ],
            )
        store = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=execution,
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        report = store.rejection_buckets()
        self.assertTrue(report.available, report.reason)
        counts = {item.key: item.count for item in report.data.buckets}
        self.assertEqual(report.data.total, 4)
        self.assertEqual(counts, {"pm_rejected:max_open_per_strategy": 1, "risk_rejected": 1, "paper_failed": 1, "slippage": 1})
        failures = store.recent_failures()
        self.assertEqual([item.id for item in failures.data], [4, 3, 2, 1])
        self.assertIn("slippage", failures.data[0].validation_errors)

    def test_paper_closed_buckets_by_exit_and_liq(self) -> None:
        now = datetime.now(timezone.utc)
        paper = self.root / "paper_trading.db"
        with sqlite3.connect(paper) as conn:
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    pair_address TEXT,
                    status TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    exit_reason TEXT,
                    pnl_usd REAL,
                    meta_json TEXT
                )
                """
            )
            t0 = _iso(now - timedelta(hours=2))
            t1 = _iso(now - timedelta(hours=1))
            t2 = _iso(now)
            conn.executemany(
                """
                INSERT INTO positions
                    (id, pair_address, status, opened_at, closed_at, exit_reason, pnl_usd, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "a",
                        "pairA",
                        "closed",
                        t0,
                        t0,
                        "stop_loss",
                        -84.3,
                        json.dumps({"liquidity_usd": 11994.57, "decision_log_id": 1}),
                    ),
                    (
                        "b",
                        "pairB",
                        "closed",
                        t1,
                        t1,
                        "max_hold",
                        10.5,
                        json.dumps({"liquidity_usd": 191062.78, "decision_log_id": 2}),
                    ),
                    (
                        "c",
                        "pairB",
                        "closed",
                        t2,
                        t2,
                        "max_hold",
                        -4.3,
                        json.dumps({"liquidity_usd": 386822.33, "decision_log_id": 3}),
                    ),
                    (
                        "d",
                        "pairC",
                        "open",
                        None,
                        None,
                        None,
                        None,
                        "{}",
                    ),
                ],
            )
        main = self.root / "main.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                "CREATE TABLE decision_log (id INTEGER PRIMARY KEY, purpose TEXT)"
            )
            conn.executemany(
                "INSERT INTO decision_log (id, purpose) VALUES (?, ?)",
                [(1, "live_candidate"), (2, "live_candidate"), (3, "sample")],
            )
        store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=paper,
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        report = store.paper_closed_buckets(days=14)
        self.assertTrue(report.available, report.reason)
        self.assertEqual(report.data.closed, 3)
        keys = {(item.exit_reason, item.liq_bucket): item for item in report.data.buckets}
        self.assertIn(("stop_loss", "liq_<50k"), keys)
        self.assertEqual(keys[("stop_loss", "liq_<50k")].count, 1)
        self.assertAlmostEqual(keys[("stop_loss", "liq_<50k")].pnl_usd, -84.3, places=2)
        self.assertEqual(keys[("max_hold", "liq_>=50k")].count, 2)
        self.assertEqual(report.data.win_count, 1)
        self.assertEqual(report.data.loss_count, 2)
        self.assertAlmostEqual(report.data.win_rate or 0, 1 / 3, places=4)
        self.assertAlmostEqual(report.data.worst_pnl_usd or 0, -84.3, places=2)
        self.assertTrue(report.data.single_wipe)
        # first closed per pair: pairA loss, pairB win (earliest of B is +10.5)
        self.assertEqual(report.data.first_hit_n, 2)
        self.assertEqual(report.data.first_hit_wins, 1)
        self.assertAlmostEqual(report.data.first_hit_rate or 0, 0.5, places=4)
        self.assertEqual(report.data.first_hit_live_n, 2)
        self.assertEqual(report.data.first_hit_live_wins, 1)

    def test_chat_context_uses_newest_messages(self) -> None:
        sessions = SessionStore(self.root / "dashboard_state.db")
        info = sessions.create_session("sweep")
        for index in range(205):
            sessions.append_message(info.id, "user", f"m{index}")
        msgs = sessions.list_messages(info.id, limit=200)
        self.assertEqual(len(msgs), 200)
        self.assertEqual(msgs[0].content, "m5")
        self.assertEqual(msgs[-1].content, "m204")
        self.assertLess(msgs[0].id, msgs[-1].id)

    def test_secret_filter_scrubs_propagated_handler_records(self) -> None:
        os.environ["API_SECRET"] = "abcdefghijklmnop"
        attach_secret_filters()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        child = logging.getLogger("dashboard.probe.secret")
        child.setLevel(logging.INFO)
        child.propagate = True
        try:
            child.info("leak abcdefghijklmnop")
            text = stream.getvalue()
            self.assertNotIn("abcdefghijklmnop", text)
            self.assertIn("[redacted]", text)
            self.assertTrue(any(isinstance(item, SecretFilter) for item in handler.filters))
        finally:
            root.removeHandler(handler)

    def test_market_rows_keep_indicator_columns(self) -> None:
        missing = DataStore(
            DbPaths(
                main=self.root / "no-market.db",
                execution=self.root / "no-market-exec.db",
                paper=self.root / "no-market-paper.db",
                backtest=self.root / "no-market-bt.db",
                news=self.root / "no-market-news.db",
            )
        )
        absent = missing.market_rows()
        self.assertFalse(absent.available)
        self.assertIn("不存在", absent.reason)

        main = self.root / "market.db"
        with sqlite3.connect(main) as conn:
            conn.executescript(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT,
                    token_address TEXT,
                    change_5m REAL,
                    change_1h REAL,
                    jev_strength REAL,
                    jev_confidence REAL,
                    tier TEXT,
                    primary_pair_address TEXT,
                    updated_at TEXT
                );
                CREATE TABLE pair_history (
                    id INTEGER PRIMARY KEY,
                    chain_id TEXT,
                    pair_address TEXT,
                    recorded_at TEXT,
                    price_usd REAL,
                    liquidity_usd REAL
                );
                CREATE TABLE jev_signals (
                    chain_id TEXT,
                    token_address TEXT,
                    coverage_pct REAL,
                    fetched_at TEXT
                );
                CREATE TABLE tokens (
                    chain_id TEXT,
                    token_address TEXT,
                    raw_json TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, change_5m, change_1h, jev_strength, jev_confidence,
                     tier, primary_pair_address, updated_at)
                VALUES ('solana', 'T1', 12.5, -1.25, 0.81, 0.2, 'hot', 'P1', '2026-10-10T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, change_5m, jev_confidence, tier, updated_at)
                VALUES ('ethereum', 'T2', 1.0, 0.25, 'warm', '2026-10-10T00:00:00Z')
                """
            )
            conn.execute(
                """
                INSERT INTO pair_history (chain_id, pair_address, recorded_at, price_usd, liquidity_usd)
                VALUES ('solana', 'P1', '2026-10-10T00:00:00Z', 1.25, 50000)
                """
            )
            conn.execute(
                """
                INSERT INTO jev_signals (chain_id, token_address, coverage_pct, fetched_at)
                VALUES ('solana', 'T1', 88, '2026-10-10T00:00:00Z')
                """
            )
            conn.execute(
                "INSERT INTO tokens (chain_id, token_address, raw_json) VALUES ('solana', 'T1', ?)",
                (json.dumps({"symbol": "BONK"}),),
            )
        before = main.stat().st_mtime_ns
        store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "market-exec-missing.db",
                paper=self.root / "market-paper-missing.db",
                backtest=self.root / "market-bt-missing.db",
                news=self.root / "market-news-missing.db",
            )
        )
        result = store.market_rows()
        self.assertTrue(result.available, result.reason)
        self.assertEqual([row.token_address for row in result.data], ["T1", "T2"])
        first = result.data[0]
        self.assertEqual(first.symbol, "BONK")
        self.assertEqual(first.tier, "hot")
        self.assertAlmostEqual(first.change_5m or 0, 12.5)
        self.assertAlmostEqual(first.change_1h or 0, -1.25)
        self.assertIsNone(first.change_15m)
        self.assertAlmostEqual(first.liquidity_usd or 0, 50000)
        self.assertAlmostEqual(first.jev_strength or 0, 0.81)
        self.assertAlmostEqual(first.coverage or 0, 88)
        second = result.data[1]
        self.assertAlmostEqual(second.coverage or 0, 25)
        self.assertIsNone(second.liquidity_usd)
        self.assertEqual(first.tech_at, "")
        self.assertEqual(first.fresh_at, "2026-10-10T00:00:00Z")
        self.assertEqual(first.fresh_state, "stale")
        self.assertIn("陈旧", str(market_table_row(first)["tech"]))
        probes = {item.name: item for item in store.process_liveness().data}
        self.assertEqual(probes["指标"].state, "stale")
        sol = store.market_rows(chain="solana")
        self.assertEqual([row.token_address for row in sol.data], ["T1"])
        self.assertEqual(list(store.market_rows(chain="not-a-chain").data or ()), [])
        self.assertEqual(main.stat().st_mtime_ns, before)

    def test_indicator_clock_with_and_without_tech_column(self) -> None:
        """Follow tech_updated_at when present; legacy updated_at when the column is absent."""
        now = datetime.now(timezone.utc)
        tech_fresh = _iso(now)
        tech_old = _iso(now - timedelta(days=2))
        shared_fresh = _iso(now)
        shared_recent = _iso(now - timedelta(minutes=1))
        shared_mid = _iso(now - timedelta(hours=2))
        shared_old = _iso(now - timedelta(days=10))

        legacy = self.root / "legacy-indicators.db"
        with sqlite3.connect(legacy) as conn:
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT,
                    token_address TEXT,
                    tier TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO token_indicators (chain_id, token_address, tier, updated_at)
                VALUES ('solana', 'OLD', 'warm', ?)
                """,
                (shared_old,),
            )
            conn.execute(
                """
                INSERT INTO token_indicators (chain_id, token_address, tier, updated_at)
                VALUES ('solana', 'NEW', 'warm', ?)
                """,
                (shared_fresh,),
            )
        legacy_store = self._store(legacy)
        legacy_rows = legacy_store.market_rows()
        self.assertTrue(legacy_rows.available, legacy_rows.reason)
        self.assertEqual([row.token_address for row in legacy_rows.data], ["NEW", "OLD"])
        newest = legacy_rows.data[0]
        self.assertEqual(newest.tech_at, "")
        self.assertEqual(newest.fresh_at, shared_fresh)
        self.assertEqual(newest.fresh_state, "alive")
        self.assertEqual(market_table_row(newest)["tech_class"], "pnl-flat")
        self.assertNotIn("陈旧", str(market_table_row(newest)["tech"]))
        legacy_probe = {item.name: item for item in legacy_store.process_liveness().data}
        self.assertEqual(legacy_probe["指标"].state, "alive")
        self.assertEqual(legacy_probe["指标"].detail, "")

        tech = self.root / "tech-indicators.db"
        with sqlite3.connect(tech) as conn:
            conn.executescript(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT,
                    token_address TEXT,
                    tier TEXT,
                    updated_at TEXT,
                    tech_updated_at TEXT
                );
                CREATE TABLE indicator_history (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, tier, updated_at, tech_updated_at)
                VALUES ('solana', ?, ?, ?, ?)
                """,
                [
                    ("FUND", "warm", shared_fresh, tech_old),
                    ("TECH", "warm", shared_old, tech_fresh),
                    ("GAP", "hot", shared_mid, None),
                    ("BLANK", "cold", shared_recent, "  "),
                ],
            )
            conn.execute(
                "INSERT INTO indicator_history (recorded_at) VALUES (?)",
                (shared_fresh,),
            )
        before = tech.stat().st_mtime_ns
        tech_store = self._store(tech)
        tech_rows = tech_store.market_rows()
        self.assertTrue(tech_rows.available, tech_rows.reason)
        self.assertEqual(
            [row.token_address for row in tech_rows.data],
            ["GAP", "TECH", "BLANK", "FUND"],
        )
        by_token = {row.token_address: row for row in tech_rows.data}
        self.assertEqual(by_token["TECH"].tech_at, tech_fresh)
        self.assertEqual(by_token["TECH"].fresh_at, tech_fresh)
        self.assertEqual(by_token["TECH"].fresh_state, "alive")
        self.assertEqual(by_token["FUND"].tech_at, tech_old)
        self.assertEqual(by_token["FUND"].fresh_at, tech_old)
        self.assertEqual(by_token["FUND"].fresh_state, "stale")
        self.assertIn("陈旧", str(market_table_row(by_token["FUND"])["tech"]))
        self.assertEqual(by_token["GAP"].tech_at, "")
        self.assertEqual(by_token["GAP"].fresh_at, shared_mid)
        self.assertEqual(by_token["GAP"].fresh_state, "stale")
        self.assertEqual(by_token["BLANK"].tech_at, "")
        self.assertEqual(by_token["BLANK"].fresh_at, shared_recent)
        self.assertEqual(by_token["BLANK"].fresh_state, "alive")
        tech_probe = {item.name: item for item in tech_store.process_liveness().data}
        self.assertEqual(tech_probe["指标"].state, "alive")
        self.assertEqual(tech_probe["指标"].detail, "tech_updated_at")
        self.assertEqual(tech.stat().st_mtime_ns, before)

        stale = self.root / "stale-tech.db"
        with sqlite3.connect(stale) as conn:
            conn.executescript(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT,
                    token_address TEXT,
                    tier TEXT,
                    updated_at TEXT,
                    tech_updated_at TEXT
                );
                CREATE TABLE indicator_history (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, tier, updated_at, tech_updated_at)
                VALUES ('solana', 'FROZEN', 'hot', ?, ?)
                """,
                (shared_fresh, tech_old),
            )
            conn.execute(
                "INSERT INTO indicator_history (recorded_at) VALUES (?)",
                (shared_fresh,),
            )
        stale_store = self._store(stale)
        stale_probe = {item.name: item for item in stale_store.process_liveness().data}
        self.assertEqual(stale_probe["指标"].state, "stale")
        self.assertEqual(stale_probe["指标"].detail, "tech_updated_at")
        self.assertGreater(stale_probe["指标"].age_seconds or 0, 86400)
        os.environ["HEARTBEAT_STALE_SEC"] = "10000000"
        wide = {item.name: item for item in stale_store.process_liveness().data}
        self.assertEqual(wide["指标"].state, "alive")
        widened_row = stale_store.market_rows().data[0]
        self.assertEqual(widened_row.fresh_state, "alive")
        os.environ["HEARTBEAT_STALE_SEC"] = "10"
        narrow = {item.name: item for item in stale_store.process_liveness().data}
        self.assertEqual(narrow["指标"].state, "stale")

        empty_tech = self.root / "null-tech.db"
        with sqlite3.connect(empty_tech) as conn:
            conn.executescript(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT,
                    token_address TEXT,
                    tier TEXT,
                    updated_at TEXT,
                    tech_updated_at TEXT
                );
                CREATE TABLE indicator_history (
                    id INTEGER PRIMARY KEY,
                    recorded_at TEXT
                );
                """
            )
            conn.execute(
                """
                INSERT INTO token_indicators
                    (chain_id, token_address, tier, updated_at, tech_updated_at)
                VALUES ('solana', 'UNSTAMPED', 'hot', ?, NULL)
                """,
                (shared_fresh,),
            )
            conn.execute(
                "INSERT INTO token_indicators (chain_id, token_address, tier) VALUES ('solana', 'BARE', 'warm')"
            )
            conn.execute(
                "INSERT INTO indicator_history (recorded_at) VALUES (?)",
                (shared_fresh,),
            )
        os.environ.pop("HEARTBEAT_STALE_SEC", None)
        empty_store = self._store(empty_tech)
        empty_probe = {item.name: item for item in empty_store.process_liveness().data}
        self.assertEqual(empty_probe["指标"].state, "unknown")
        self.assertEqual(empty_probe["指标"].detail, "tech_updated_at")
        bare = {row.token_address: row for row in empty_store.market_rows().data}
        self.assertEqual(bare["UNSTAMPED"].tech_at, "")
        self.assertEqual(bare["UNSTAMPED"].fresh_at, shared_fresh)
        self.assertEqual(bare["BARE"].fresh_at, "")
        self.assertEqual(bare["BARE"].fresh_state, "unknown")
        self.assertEqual(market_table_row(bare["BARE"])["tech"], "—")

    def _store(self, main: Path) -> DataStore:
        return DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )

    def test_market_base_and_strategy_buckets_are_read_only(self) -> None:
        paths = build_fixture(self.root)
        store = DataStore(paths)
        missing = store.market_base_stats()
        self.assertFalse(missing.available)
        self.assertIn("change_1h", missing.reason)
        buckets = store.strategy_buckets()
        self.assertTrue(buckets.available, buckets.reason)
        self.assertEqual([(row.strategy_id, row.n, row.ok_rate) for row in buckets.data.rows], [("alpha", 2, "50.0%")])

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=3)
        fresh = now - timedelta(hours=1)
        with sqlite3.connect(paths.main) as conn:
            conn.execute("DROP TABLE indicator_history")
            conn.execute(
                """
                CREATE TABLE indicator_history (
                    id INTEGER PRIMARY KEY,
                    tier TEXT,
                    recorded_at TEXT,
                    change_1h REAL,
                    change_4h REAL
                )
                """
            )
            conn.executemany(
                "INSERT INTO indicator_history (tier, recorded_at, change_1h, change_4h) VALUES (?, ?, ?, ?)",
                [
                    ("hot", _iso(fresh), 1.0, 2.0),
                    ("hot", _iso(fresh), 3.0, 6.0),
                    ("hot", _iso(fresh), 600.0, None),
                    ("warm", _iso(fresh), 50.0, 50.0),
                    ("hot", _iso(old), 9.0, 9.0),
                ],
            )
        before = paths.main.stat().st_mtime_ns
        stats = store.market_base_stats()
        self.assertTrue(stats.available, stats.reason)
        self.assertEqual(stats.data.n_1h, 3)
        self.assertEqual(stats.data.med_1h, "+3.00%")
        self.assertEqual(stats.data.med_4h, "+4.00%")
        self.assertEqual(stats.data.mean_1h_w, "+2.00%")
        self.assertEqual(paths.main.stat().st_mtime_ns, before)

    def test_top_tabs_match_skeleton_and_skip_dead_routes(self) -> None:
        labels = [tab.label for tab in TOP_TABS]
        self.assertEqual(
            labels,
            [
                "DASHBOARD",
                "MARKETS",
                "CRYPTO",
                "PORTFOLIO",
                "SIGNALS",
                "EXECUTION",
                "NEWS",
                "AI CHAT",
                "BACKTEST",
                "ALGO",
                "NODES",
                "QUANT LAB",
                "SETTINGS",
            ],
        )
        self.assertEqual(
            [tab.label for tab in TOP_TABS if tab.kind == "live"],
            ["DASHBOARD", "MARKETS", "SIGNALS", "EXECUTION", "AI CHAT"],
        )
        self.assertEqual(
            [tab.label for tab in TOP_TABS if tab.kind == "placeholder"],
            ["CRYPTO", "PORTFOLIO", "NEWS", "BACKTEST", "ALGO", "NODES", "QUANT LAB", "SETTINGS"],
        )
        self.assertEqual(select_top_tab("DASHBOARD", "CRYPTO"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "PORTFOLIO"), "DASHBOARD")
        self.assertEqual(select_top_tab("MARKETS", "NEWS"), "MARKETS")
        self.assertEqual(select_top_tab("AI CHAT", "BACKTEST"), "AI CHAT")
        self.assertEqual(select_top_tab("DASHBOARD", "ALGO"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "NODES"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "QUANT LAB"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "SETTINGS"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "no-such-tab"), "DASHBOARD")
        self.assertEqual(select_top_tab("DASHBOARD", "MARKETS"), "MARKETS")
        self.assertEqual(select_top_tab("DASHBOARD", "SIGNALS"), "SIGNALS")
        self.assertEqual(select_top_tab("DASHBOARD", "EXECUTION"), "EXECUTION")
        self.assertEqual(select_top_tab("MARKETS", "AI CHAT"), "AI CHAT")
        self.assertEqual(select_top_tab("AI CHAT", "AI CHAT"), "AI CHAT")
        self.assertTrue(host_visible("DASHBOARD", "OVERVIEW", "OVERVIEW"))
        self.assertFalse(host_visible("DASHBOARD", "OVERVIEW", "SIGNALS"))
        self.assertTrue(host_visible("DASHBOARD", "SIGNALS", "SIGNALS"))
        self.assertTrue(host_visible("SIGNALS", "OVERVIEW", "SIGNALS"))
        self.assertFalse(host_visible("SIGNALS", "OVERVIEW", "OVERVIEW"))
        self.assertTrue(host_visible("EXECUTION", "OVERVIEW", "EXECUTION"))
        self.assertTrue(host_visible("DASHBOARD", "EXECUTION", "EXECUTION"))
        self.assertFalse(host_visible("EXECUTION", "OVERVIEW", "SIGNALS"))
        self.assertTrue(host_visible("MARKETS", "OVERVIEW", "MARKETS"))
        self.assertTrue(host_visible("AI CHAT", "OVERVIEW", "AI CHAT"))
        self.assertFalse(host_visible("NEWS", "OVERVIEW", "MARKETS"))
        self.assertFalse(host_visible("BACKTEST", "OVERVIEW", "EXECUTION"))
        app_text = Path(__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn('classes("v2-tab placeholder")', app_text)
        self.assertIn("select_top_tab", app_text)
        self.assertEqual(app_text.count("@ui.page("), 2)
        for dead in ("/crypto", "/portfolio", "/news", "/backtest", "/algo", "/nodes", "/settings", "/quant"):
            self.assertNotIn(f'@ui.page("{dead}', app_text)
        self.assertIn("v2-subbar", inspect.getsource(app_module._module_tabs))
        self.assertNotIn("v2-subbar", inspect.getsource(app_module._render_dashboard))


    def test_evolution_section_parse_and_registry(self) -> None:
        sample = (
            "# weekly\n\n"
            "## 9. 其他\n\nignore\n\n"
            "## 10. 进化建议\n\n"
            "> note\n\n"
            "| 层 | 项 | 状态 | 证据 | 行动提示 |\n"
            "|---|---|---|---|---|\n"
            "| 采集 | age 覆盖 | **观察中** | unknown=0/40 evidence long text | 继续观察；落地须走 R4 |\n"
            "| 决策 | JEV 门槛 | **建议落地** | n=20 unique=12 | 交 R4 人工确认 |\n"
            "| 复盘 | 退役规则 | **裁决** | 连续 2 周弱 | 止损退役 |\n\n"
            "共 3 条；\n\n"
            "### 基线滚动快照\n\n"
            "| 层 | 项 | 状态 | 证据 | 行动提示 |\n"
            "|---|---|---|---|---|\n"
            "| 假 | 不该解析 | **观察中** | x | y |\n"
        )
        items = parse_evolution_section(sample)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].layer, "采集")
        self.assertEqual(items[0].status, "观察中")
        self.assertEqual(items[1].status, "建议落地")
        self.assertEqual(items[2].status, "裁决")
        self.assertEqual(status_class("观察中"), "muted")
        self.assertEqual(status_class("**建议落地**"), "warn")
        self.assertEqual(status_class("裁决"), "ok")
        self.assertEqual(parse_evolution_section("# no section"), [])

        reports = self.root / "exports" / "reports"
        reports.mkdir(parents=True)
        (reports / "review_weekly_2026-10-01.md").write_text(
            "## 10. 进化建议\n\n_本期无进化建议（各层数据未交来）。_\n",
            encoding="utf-8",
        )
        (reports / "review_weekly_2026-10-12.md").write_text(sample, encoding="utf-8")
        latest = find_latest_weekly_report(self.root)
        assert latest is not None
        self.assertEqual(latest.name, "review_weekly_2026-10-12.md")
        loaded, reason, source = load_evolution_suggestions(self.root)
        self.assertIsNone(reason)
        assert source is not None
        self.assertEqual(source.name, "review_weekly_2026-10-12.md")
        self.assertEqual(len(loaded), 3)
        empty_root = self.root / "empty-exports"
        empty_root.mkdir()
        missing, miss_reason, miss_path = load_evolution_suggestions(empty_root)
        self.assertEqual(missing, [])
        self.assertIsNotNone(miss_reason)
        self.assertIsNone(miss_path)
        self.assertIn("evolution", [item.id for item in PANELS])
        self.assertEqual(next(item.title for item in PANELS if item.id == "evolution"), "进化建议")
        app_text = Path(__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn('("进化", "建议")', app_text)
        self.assertIn("span-12", app_text)
        self.assertIn("render_evolution_body", app_text)

    def test_intel_observe_cards_gap_and_numbers(self) -> None:
        missing = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        for result in (
            missing.intel_holder_observe(),
            missing.intel_slippage_observe(),
            missing.intel_crowding_note(),
        ):
            self.assertFalse(result.available, result.reason)
            self.assertIn("数据库文件不存在", result.reason)

        bare = self.root / "bare-intel.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(
            DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare)
        )
        holder_gap = bare_store.intel_holder_observe()
        self.assertFalse(holder_gap.available, holder_gap.reason)
        self.assertIn("缺少数据表", holder_gap.reason)
        slip_gap = bare_store.intel_slippage_observe()
        self.assertFalse(slip_gap.available, slip_gap.reason)
        self.assertIn("缺少数据表", slip_gap.reason)
        self.assertNotIn("store", sys.modules)

        empty_main = self.root / "empty-main.db"
        empty_paper = self.root / "empty-paper.db"
        empty_exec = self.root / "empty-exec.db"
        with sqlite3.connect(empty_main) as conn:
            conn.executescript(
                """
                CREATE TABLE token_holder_snapshots (
                    chain_id TEXT,
                    token_address TEXT,
                    snapshot_id TEXT,
                    recorded_at TEXT,
                    rank INTEGER,
                    holder_address TEXT,
                    amount_ui REAL,
                    pct_of_supply REAL,
                    total_supply_ui REAL
                );
                CREATE TABLE address_labels (
                    chain_id TEXT,
                    address TEXT,
                    label_type TEXT,
                    token_mint TEXT
                );
                CREATE TABLE token_transfer_edges (
                    chain_id TEXT,
                    token_address TEXT,
                    from_address TEXT,
                    to_address TEXT,
                    amount_ui REAL,
                    recorded_at TEXT
                );
                CREATE TABLE bot_fill_observations (
                    chain_id TEXT,
                    signature TEXT,
                    wallet TEXT,
                    token_address TEXT,
                    recorded_at TEXT,
                    bot_reason TEXT
                );
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    strategy_id TEXT,
                    action TEXT,
                    purpose TEXT,
                    chain_id TEXT,
                    token_address TEXT
                );
                """
            )
        with sqlite3.connect(empty_paper) as conn:
            conn.executescript(
                """
                CREATE TABLE positions (id TEXT PRIMARY KEY, meta_json TEXT, status TEXT);
                CREATE TABLE fills (
                    id INTEGER PRIMARY KEY,
                    position_id TEXT,
                    side TEXT,
                    price REAL,
                    created_at TEXT,
                    note TEXT
                );
                """
            )
        with sqlite3.connect(empty_exec) as conn:
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    decision_log_id TEXT,
                    action TEXT,
                    executed INTEGER,
                    receipt_json TEXT
                )
                """
            )
        empty_store = DataStore(
            DbPaths(
                main=empty_main,
                execution=empty_exec,
                paper=empty_paper,
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        empty_holder = empty_store.intel_holder_observe()
        self.assertTrue(empty_holder.available, empty_holder.reason)
        self.assertEqual(empty_holder.data.snapshot_n, 0)
        self.assertEqual(empty_holder.data.non_null_share_n, 0)
        self.assertEqual(empty_holder.data.non_null_delta_n, 0)
        self.assertEqual(empty_holder.data.weight, 0.0)
        self.assertFalse(empty_holder.data.wired)
        self.assertEqual(empty_holder.data.gap, "empty")
        self.assertEqual(empty_holder.data.latest, ())
        empty_slip = empty_store.intel_slippage_observe()
        self.assertTrue(empty_slip.available, empty_slip.reason)
        self.assertEqual(empty_slip.data.slippage_n, 0)
        self.assertIsNone(empty_slip.data.mean)
        self.assertIsNone(empty_slip.data.bot_share)
        self.assertFalse(empty_slip.data.high_bot_skip_wired)
        self.assertEqual(empty_slip.data.gap, "both")
        empty_crowd = empty_store.intel_crowding_note()
        self.assertTrue(empty_crowd.available, empty_crowd.reason)
        self.assertEqual(empty_crowd.data.live_n, 0)
        self.assertEqual(empty_crowd.data.sample_n, 0)
        self.assertIsNone(empty_crowd.data.live_peak_hour_density)
        self.assertIn("不否决", empty_crowd.data.line)
        self.assertIn("不算有效性", empty_crowd.data.line)

        now = datetime.now(timezone.utc)
        recent = _iso(now - timedelta(minutes=5))
        mint = "mint111111111111111111111111111111111111111"
        dev = "dev1111111111111111111111111111111111111111"
        other = "oth1111111111111111111111111111111111111111"
        with sqlite3.connect(empty_main) as conn:
            conn.executemany(
                """
                INSERT INTO token_holder_snapshots (
                    chain_id, token_address, snapshot_id, recorded_at, rank,
                    holder_address, amount_ui, pct_of_supply, total_supply_ui
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("solana", mint, "early", "2026-10-14T00:00:00Z", 1, "a", None, None, 100.0),
                    ("solana", mint, "late", recent, 1, "a", 20.0, 0.20, 100.0),
                    ("solana", mint, "late", recent, 2, "b", 5.0, 0.05, 100.0),
                ],
            )
            conn.execute(
                "INSERT INTO address_labels (chain_id, address, label_type, token_mint) VALUES (?, ?, ?, ?)",
                ("solana", dev, "dev", mint),
            )
            conn.execute(
                """
                INSERT INTO token_transfer_edges (
                    chain_id, token_address, from_address, to_address, amount_ui, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("solana", mint, dev, other, 6.0, recent),
            )
            conn.executemany(
                """
                INSERT INTO bot_fill_observations (
                    chain_id, signature, wallet, token_address, recorded_at, bot_reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("solana", "sig-bot", "w1", mint, recent, "repeat"),
                    ("solana", "sig-quiet", "w2", mint, recent, None),
                ],
            )
            conn.executemany(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, purpose, chain_id, token_address)
                VALUES (?, 'jev_strength_v1', 'buy', ?, 'solana', ?)
                """,
                [
                    (recent, "live_candidate", mint),
                    (_iso(now - timedelta(minutes=4)), "live_candidate", other),
                    (_iso(now - timedelta(minutes=3)), "sample", mint),
                ],
            )
        with sqlite3.connect(empty_paper) as conn:
            conn.execute(
                "INSERT INTO positions (id, meta_json, status) VALUES (?, ?, 'open')",
                ("pos-1", json.dumps({"mid_price": 1.0, "slippage": 0.02})),
            )
            conn.execute(
                "INSERT INTO positions (id, meta_json, status) VALUES (?, ?, 'open')",
                ("pos-2", json.dumps({"mid_price": 2.0, "slippage": 0.04})),
            )
            conn.executemany(
                """
                INSERT INTO fills (position_id, side, price, created_at, note)
                VALUES (?, 'buy', ?, ?, 'open')
                """,
                [("pos-1", 1.02, recent), ("pos-2", 2.08, recent)],
            )
        before = {
            path: path.stat().st_mtime_ns
            for path in (empty_main, empty_paper, empty_exec)
        }
        filled = empty_store.intel_holder_observe()
        self.assertTrue(filled.available, filled.reason)
        self.assertEqual(filled.data.weight, 0.0)
        self.assertFalse(filled.data.wired)
        self.assertEqual(filled.data.snapshot_n, 2)
        self.assertEqual(filled.data.non_null_share_n, 1)
        self.assertEqual(filled.data.non_null_delta_n, 1)
        self.assertEqual(filled.data.gap, "")
        by_snap = {item.snapshot_id: item for item in filled.data.latest}
        self.assertIsNone(by_snap["early"].top10_share)
        self.assertAlmostEqual(by_snap["late"].top10_share or 0.0, 0.25)
        self.assertAlmostEqual(by_snap["late"].dev_balance_delta or 0.0, -6.0)
        self.assertIsNone(by_snap["early"].top10_share)
        slipped = empty_store.intel_slippage_observe()
        self.assertTrue(slipped.available, slipped.reason)
        self.assertEqual(slipped.data.slippage_n, 2)
        self.assertAlmostEqual(slipped.data.mean or 0.0, 0.03)
        self.assertAlmostEqual(slipped.data.bot_share or -1.0, 0.5)
        self.assertEqual(slipped.data.bot_status, "observed")
        self.assertEqual(slipped.data.bot_source, "bot_fill_observations")
        self.assertEqual(slipped.data.bot_fills, 1)
        self.assertEqual(slipped.data.total_fills, 2)
        self.assertFalse(slipped.data.high_bot_skip_wired)
        self.assertEqual(slipped.data.gap, "")
        crowd = empty_store.intel_crowding_note()
        self.assertTrue(crowd.available, crowd.reason)
        self.assertEqual(crowd.data.live_n, 2)
        self.assertEqual(crowd.data.sample_n, 1)
        self.assertIsNotNone(crowd.data.live_peak_hour_density)
        self.assertIn("不否决", crowd.data.line)
        self.assertIn("不算有效性", crowd.data.line)
        for path, stamp in before.items():
            self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertNotIn("store", sys.modules)

        # Receipt bot tags are not the #81 headline when the crawler table is absent.
        tagged = self.root / "tagged-exec.db"
        with sqlite3.connect(tagged) as conn:
            conn.execute(
                """
                CREATE TABLE execution_log (
                    ts TEXT,
                    decision_log_id INTEGER,
                    action TEXT,
                    executed INTEGER,
                    receipt_json TEXT
                )
                """
            )
            receipt = json.dumps(
                {
                    "paper_enabled": True,
                    "executed": True,
                    "exec_result": {
                        "side": "buy",
                        "mid_price": 1.0,
                        "fill_price": 1.02,
                        "slippage": 0.02,
                        "bot": True,
                    },
                }
            )
            conn.execute(
                """
                INSERT INTO execution_log (ts, decision_log_id, action, executed, receipt_json)
                VALUES (?, 1, 'buy', 1, ?)
                """,
                (recent, receipt),
            )
        tag_store = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=tagged,
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        tagged_slip = tag_store.intel_slippage_observe()
        self.assertTrue(tagged_slip.available, tagged_slip.reason)
        self.assertEqual(tagged_slip.data.slippage_n, 1)
        self.assertIsNone(tagged_slip.data.bot_share)
        self.assertEqual(tagged_slip.data.bot_status, "data_gap")
        self.assertFalse(tagged_slip.data.high_bot_skip_wired)

        app_text = Path(__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
        data_text = Path(__file__).resolve().parent.joinpath("data.py").read_text(encoding="utf-8")
        self.assertIn('("持仓", "集中")', app_text)
        self.assertIn('("滑点", "bot")', app_text)
        self.assertIn("权重0 / 不接决策", app_text)
        self.assertIn("信号或胜率方向≠盈亏；旁证只记分布缝，不算有效性", app_text)
        self.assertEqual(
            app_module._EVIDENCE_NAIL,
            "信号或胜率方向≠盈亏；旁证只记分布缝，不算有效性",
        )
        self.assertIn("不进盘中参 · 高 bot 不跳单", app_text)
        self.assertIn("半胜仍亏：止损可抹盈", app_text)
        self.assertIn("v2-note-warn", app_text)
        self.assertIn("多样本可取，但日开↑常伴随损耗↑——观察峰，不解读为应加仓频", app_text)
        self.assertIn("_EVIDENCE_NAIL", inspect.getsource(app_module._v2_closed))
        self.assertIn("_EVIDENCE_NAIL", inspect.getsource(app_module._v2_trade_freq))
        self.assertIn("_EVIDENCE_NAIL", inspect.getsource(app_module._v2_signal_streak))
        self.assertIn("read_top10_share_history", data_text)
        self.assertIn("read_dev_balance_delta", data_text)
        self.assertIn("build_slippage_report", data_text)
        self.assertNotIn("PositionManager", data_text)
        self.assertNotIn("PositionManager", app_text)
        holder_src = inspect.getsource(app_module._v2_intel_holders)
        slip_src = inspect.getsource(app_module._v2_intel_slippage)
        self.assertIn("data gap", holder_src)
        self.assertIn("data gap", slip_src)
        self.assertIn("不跳单", slip_src)

    def test_token_age_observe_buckets(self) -> None:
        missing = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        absent = missing.token_age_observe()
        self.assertFalse(absent.available)
        self.assertIn("数据库文件不存在", absent.reason)

        bare = self.root / "bare-age.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(
            DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare)
        )
        bare_age = bare_store.token_age_observe()
        self.assertFalse(bare_age.available, bare_age.reason)
        self.assertIn("缺少数据表", bare_age.reason)

        thin = self.root / "thin-age.db"
        with sqlite3.connect(thin) as conn:
            conn.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT)")
        thin_store = DataStore(
            DbPaths(main=thin, execution=thin, paper=thin, backtest=thin, news=thin)
        )
        thin_age = thin_store.token_age_observe()
        self.assertFalse(thin_age.available, thin_age.reason)
        self.assertIn("缺少字段", thin_age.reason)
        self.assertIn("raw_json", thin_age.reason)

        corrupt = self.root / "corrupt-age.db"
        corrupt.write_text("{not json and not sqlite", encoding="utf-8")
        corrupt_store = DataStore(
            DbPaths(main=corrupt, execution=corrupt, paper=corrupt, backtest=corrupt, news=corrupt)
        )
        corrupt_age = corrupt_store.token_age_observe()
        self.assertFalse(corrupt_age.available, corrupt_age.reason)
        self.assertIn("查询失败", corrupt_age.reason)

        signal = datetime(2026, 10, 18, tzinfo=timezone.utc)
        signal_ms = int(signal.timestamp() * 1000)

        def stamped(age_days: float, *, seconds: bool = False, as_text: bool = False) -> str:
            delta_ms = int(round(age_days * 86_400_000))
            created_ms = signal_ms - delta_ms
            value: object = int(created_ms / 1000) if seconds else created_ms
            if as_text:
                value = str(value)
            return json.dumps({"extras": {"pairCreatedAt": value}})

        main = self.root / "age-main.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO decision_log (id, ts, raw_json) VALUES (?, ?, ?)",
                [
                    (1, _iso(signal), "{}"),
                    (2, _iso(signal), stamped(0.5, as_text=True)),
                    (3, _iso(signal), stamped(1)),
                    (4, _iso(signal), stamped(7)),
                    (5, _iso(signal), stamped(7 + 1 / 24)),
                    (6, _iso(signal), "not-json"),
                    (7, _iso(signal), json.dumps({"pairCreatedAt": signal_ms - 86_400_000})),
                    (8, _iso(signal), json.dumps({"extras": {"pairCreatedAt": ""}})),
                    (9, _iso(signal), json.dumps({"extras": {"pairCreatedAt": 0}})),
                    (10, _iso(signal), json.dumps({"extras": {"pairCreatedAt": signal_ms + 3_600_000}})),
                    (11, _iso(signal), stamped(2, seconds=True)),
                ],
            )
        store = DataStore(
            DbPaths(main=main, execution=main, paper=main, backtest=main, news=main)
        )
        before = main.stat().st_mtime_ns
        report = store.token_age_observe()
        self.assertTrue(report.available, report.reason)
        data = report.data
        self.assertEqual(data.total, 11)
        self.assertEqual(data.with_field, 6)
        self.assertEqual(data.unknown, 6)
        self.assertNotEqual(data.unknown, data.total)
        self.assertAlmostEqual(data.unknown_ratio or 0.0, round(6 / 11, 4))
        counts = {item.label: item.count for item in data.buckets}
        self.assertEqual(list(counts), ["<1d", "1-7d", ">7d", "unknown"])
        self.assertEqual(counts["<1d"], 1)
        self.assertEqual(counts["1-7d"], 3)
        self.assertEqual(counts[">7d"], 1)
        self.assertEqual(counts["unknown"], 6)
        self.assertEqual(sum(counts.values()), data.total)
        self.assertEqual([item.decision_id for item in data.samples], [11, 5, 4, 3, 2])
        by_id = {item.decision_id: item.age_days for item in data.samples}
        self.assertAlmostEqual(by_id[2], 0.5, places=4)
        self.assertAlmostEqual(by_id[3], 1.0, places=4)
        self.assertAlmostEqual(by_id[4], 7.0, places=4)
        self.assertAlmostEqual(by_id[11], 2.0, places=4)
        self.assertGreater(by_id[5], 7.0)
        self.assertEqual(main.stat().st_mtime_ns, before)

        narrowed = store.token_age_observe(limit=2)
        self.assertTrue(narrowed.available, narrowed.reason)
        self.assertEqual(narrowed.data.total, 2)
        self.assertEqual(narrowed.data.with_field, 2)
        self.assertEqual(narrowed.data.unknown, 1)
        self.assertNotEqual(narrowed.data.unknown, narrowed.data.total)
        narrow_counts = {item.label: item.count for item in narrowed.data.buckets}
        self.assertEqual(narrow_counts["1-7d"], 1)
        self.assertEqual(narrow_counts["unknown"], 1)
        self.assertEqual(narrowed.data.samples[0].decision_id, 11)

        empty = self.root / "empty-age.db"
        with sqlite3.connect(empty) as conn:
            conn.execute(
                "CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT, raw_json TEXT)"
            )
        empty_store = DataStore(
            DbPaths(main=empty, execution=empty, paper=empty, backtest=empty, news=empty)
        )
        empty_age = empty_store.token_age_observe()
        self.assertTrue(empty_age.available, empty_age.reason)
        self.assertEqual(empty_age.data.total, 0)
        self.assertEqual(empty_age.data.with_field, 0)
        self.assertEqual(empty_age.data.unknown, 0)
        self.assertIsNone(empty_age.data.unknown_ratio)
        self.assertEqual(empty_age.data.samples, ())

        app_text = Path(__file__).resolve().parent.joinpath("app.py").read_text(encoding="utf-8")
        self.assertIn('("代币", "年龄")', app_text)
        self.assertIn("extras.pairCreatedAt", app_text)
        age_src = inspect.getsource(app_module._v2_token_age)
        self.assertIn("_EVIDENCE_NAIL", age_src)
        self.assertIn("不算有效性", app_module._EVIDENCE_NAIL)
        self.assertIn("不进盘中参", age_src)
        self.assertIn("pairCreatedAt", age_src)
        self.assertIn("unknown", age_src)

    def test_goal_20k_pace_card_renders(self) -> None:
        missing = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        absent = missing.paper_goal_pace()
        self.assertFalse(absent.available)
        self.assertIn("数据库文件不存在", absent.reason)

        bare = self.root / "bare-goal.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(
            DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare)
        )
        bare_goal = bare_store.paper_goal_pace()
        self.assertFalse(bare_goal.available, bare_goal.reason)
        self.assertIn("缺少数据表", bare_goal.reason)

        cash_only = self.root / "cash-only-goal.db"
        with sqlite3.connect(cash_only) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 1000)")
        cash_store = DataStore(
            DbPaths(main=cash_only, execution=cash_only, paper=cash_only, backtest=cash_only, news=cash_only)
        )
        cash_goal = cash_store.paper_goal_pace()
        self.assertFalse(cash_goal.available, cash_goal.reason)
        self.assertIn("positions", cash_goal.reason)

        thin = self.root / "thin-goal.db"
        with sqlite3.connect(thin) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 1000)")
            conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, status TEXT)")
            conn.execute("INSERT INTO positions (id, status) VALUES (1, 'open')")
        thin_store = DataStore(
            DbPaths(main=thin, execution=thin, paper=thin, backtest=thin, news=thin)
        )
        thin_goal = thin_store.paper_goal_pace()
        self.assertFalse(thin_goal.available, thin_goal.reason)
        self.assertIn("size_usd", thin_goal.reason)

        corrupt = self.root / "corrupt-goal.db"
        corrupt.write_text("{not json and not sqlite", encoding="utf-8")
        corrupt_store = DataStore(
            DbPaths(main=corrupt, execution=corrupt, paper=corrupt, backtest=corrupt, news=corrupt)
        )
        corrupt_goal = corrupt_store.paper_goal_pace()
        self.assertFalse(corrupt_goal.available, corrupt_goal.reason)
        self.assertIn("查询失败", corrupt_goal.reason)

        paper = self.root / "goal-paper.db"
        with sqlite3.connect(paper) as conn:
            conn.execute(
                "CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL, updated_at TEXT)"
            )
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    size_usd REAL,
                    status TEXT,
                    closed_at TEXT
                )
                """
            )
            conn.execute("INSERT INTO account (id, cash_usd, updated_at) VALUES (1, 8000, '2026-10-20')")
            conn.execute("INSERT INTO account (id, cash_usd, updated_at) VALUES (2, 1, '2026-10-20')")
            conn.executemany(
                "INSERT INTO positions (id, size_usd, status, closed_at) VALUES (?, ?, ?, ?)",
                [
                    ("open-a", 450, "open", None),
                    ("open-b", 254, "OPEN", ""),
                    ("closed", 999, "closed", "2026-10-19T00:00:00Z"),
                    ("closed-at", 500, "open", "2026-10-19T00:00:00Z"),
                    ("cancelled", 80, "cancelled", None),
                ],
            )
        store = DataStore(
            DbPaths(main=paper, execution=paper, paper=paper, backtest=paper, news=paper)
        )
        hkt = ZoneInfo("Asia/Hong_Kong")
        start = datetime(2026, 10, 20, 4, 0, tzinfo=hkt)
        before = paper.stat().st_mtime_ns
        report = store.paper_goal_pace(now=start)
        self.assertTrue(report.available, report.reason)
        data = report.data
        self.assertEqual(data.cash_usd, 8000)
        self.assertEqual(data.open_count, 2)
        self.assertAlmostEqual(data.open_size_usd, 704)
        self.assertAlmostEqual(data.equity_usd, 8704)
        self.assertEqual(data.target_usd, GOAL_TARGET_USD)
        self.assertEqual(data.target_usd, 20_000)
        self.assertAlmostEqual(data.gap_usd, 11_296)
        self.assertEqual(data.as_of_day, "2026-10-20")
        self.assertEqual(data.start_day, GOAL_START_DAY.isoformat())
        self.assertEqual(data.deadline_day, GOAL_DEADLINE_DAY.isoformat())
        self.assertEqual(data.days_left, 10)
        self.assertAlmostEqual(data.daily_need_usd or 0, 1129.6)
        self.assertFalse(data.mark_to_market)
        self.assertEqual(data.size_gap_count, 0)
        self.assertEqual(data.note, "")
        self.assertEqual(paper.stat().st_mtime_ns, before)

        just_before_hkt = datetime(2026, 10, 19, 15, 59, tzinfo=timezone.utc)
        eve = store.paper_goal_pace(now=just_before_hkt)
        self.assertTrue(eve.available, eve.reason)
        self.assertEqual(eve.data.as_of_day, "2026-10-19")
        self.assertEqual(eve.data.days_left, 11)

        hkt_midnight = datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc)
        opened = store.paper_goal_pace(now=hkt_midnight)
        self.assertEqual(opened.data.as_of_day, "2026-10-20")
        self.assertEqual(opened.data.days_left, 10)

        last_day = datetime(2026, 10, 29, 15, 59, tzinfo=timezone.utc)
        near = store.paper_goal_pace(now=last_day)
        self.assertEqual(near.data.as_of_day, "2026-10-29")
        self.assertEqual(near.data.days_left, 1)
        self.assertAlmostEqual(near.data.daily_need_usd or 0, 11296)

        deadline = datetime(2026, 10, 30, 0, 0, tzinfo=hkt)
        ended = store.paper_goal_pace(now=deadline)
        self.assertTrue(ended.available, ended.reason)
        self.assertEqual(ended.data.days_left, 0)
        self.assertIsNone(ended.data.daily_need_usd)
        self.assertEqual(ended.data.as_of_day, "2026-10-30")

        ahead_paper = self.root / "ahead-goal.db"
        with sqlite3.connect(ahead_paper) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 21000)")
            conn.execute("CREATE TABLE positions (id TEXT PRIMARY KEY, size_usd REAL, status TEXT)")
        ahead_store = DataStore(
            DbPaths(
                main=ahead_paper,
                execution=ahead_paper,
                paper=ahead_paper,
                backtest=ahead_paper,
                news=ahead_paper,
            )
        )
        ahead = ahead_store.paper_goal_pace(now=start)
        self.assertTrue(ahead.available, ahead.reason)
        self.assertAlmostEqual(ahead.data.gap_usd, -1000)
        self.assertAlmostEqual(ahead.data.daily_need_usd or 0, -100)

        gap_paper = self.root / "gap-goal.db"
        with sqlite3.connect(gap_paper) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 1000)")
            conn.execute(
                "CREATE TABLE positions (id TEXT PRIMARY KEY, size_usd REAL, status TEXT, closed_at TEXT)"
            )
            conn.execute(
                "INSERT INTO positions (id, size_usd, status, closed_at) VALUES ('x', NULL, 'open', NULL)"
            )
        gap_store = DataStore(
            DbPaths(main=gap_paper, execution=gap_paper, paper=gap_paper, backtest=gap_paper, news=gap_paper)
        )
        partial = gap_store.paper_goal_pace(now=start)
        self.assertTrue(partial.available, partial.reason)
        self.assertEqual(partial.data.open_count, 1)
        self.assertEqual(partial.data.size_gap_count, 1)
        self.assertAlmostEqual(partial.data.equity_usd, 1000)
        self.assertIn("未计入名义", partial.data.note)

        texts = _rendered_card_texts(lambda: app_module._mount_goal_20k(store))
        self.assertIn("距", texts)
        self.assertIn("$20k", texts)
        self.assertEqual(texts[texts.index("权益粗算") + 1], "$8,704")
        self.assertEqual(texts[texts.index("目标") + 1], "$20,000")
        self.assertEqual(texts[texts.index("缺口") + 1], "+$11,296")
        self.assertEqual(texts[texts.index("剩余天") + 1], "10")
        self.assertEqual(texts[texts.index("日均需") + 1], "+$1,130")
        joined = "\n".join(texts)
        self.assertIn("未盯市 · cash + 未平 size_usd", joined)
        self.assertIn("口径日 2026-10-20 HKT", joined)
        self.assertIn("10日目标旁证", joined)
        self.assertIn("起点 2026-10-20", joined)
        self.assertIn("截止 2026-10-30 HKT", joined)
        self.assertIn(app_module._GOAL_NAIL, joined)
        self.assertIn("目标跟踪≠会赚会亏；不改闸", joined)
        self.assertEqual(app_module._GOAL_NAIL, "目标跟踪≠会赚会亏；不改闸")

        ended_texts = _rendered_card_texts(lambda: app_module._v2_goal_20k(ahead_store))
        ended_store_texts = _rendered_card_texts(lambda: app_module._v2_goal_20k(store, now=deadline))
        self.assertEqual(ended_store_texts[ended_store_texts.index("剩余天") + 1], "0")
        self.assertEqual(ended_store_texts[ended_store_texts.index("日均需") + 1], "—")
        self.assertIn(app_module._GOAL_NAIL, ended_store_texts)

        gap_texts = _rendered_card_texts(lambda: app_module._v2_goal_20k(missing))
        self.assertIn("数据库文件不存在", "\n".join(gap_texts))
        self.assertIn(app_module._GOAL_NAIL, gap_texts)

        overview = inspect.getsource(app_module._render_dashboard)
        self.assertIn("_mount_goal_20k", overview)
        goal_src = inspect.getsource(app_module._v2_goal_20k)
        self.assertIn("_GOAL_NAIL", goal_src)
        self.assertIn("未盯市", goal_src)
        self.assertIn("日均需", goal_src)
        self.assertNotIn("place_order", goal_src)
        self.assertNotIn("MAX_OPEN", goal_src)
        self.assertIn("权益粗算", ended_texts)
        self.assertEqual(ended_texts[ended_texts.index("缺口") + 1], "-$1,000")
        self.assertIn(app_module._GOAL_NAIL, ended_texts)

    def test_phase0_shadow_card_reads_csv_and_gaps(self) -> None:
        missing = self.root / "no-such-shadow.csv"
        os.environ["SHADOW_PHASE0_CSV"] = str(missing)
        absent = load_shadow_phase0()
        self.assertFalse(absent.available)
        self.assertEqual(absent.reason, "CSV 缺失")
        self.assertIsNone(absent.both)
        gap_texts = _rendered_card_texts(lambda: app_module._v2_phase0_shadow())
        self.assertIn("CSV 缺失", gap_texts)
        self.assertIn(PHASE0_NAIL, gap_texts)
        self.assertIn("不 soft-skip", "\n".join(gap_texts))

        empty = self.root / "empty-shadow.csv"
        empty.write_text("", encoding="utf-8")
        os.environ["SHADOW_PHASE0_CSV"] = str(empty)
        blank = load_shadow_phase0()
        self.assertFalse(blank.available)
        self.assertEqual(blank.reason, "CSV 空")

        bad = self.root / "bad-shadow.csv"
        bad.write_bytes(b"\xff\xfe not-utf8")
        os.environ["SHADOW_PHASE0_CSV"] = str(bad)
        broken = load_shadow_phase0()
        self.assertFalse(broken.available)
        self.assertEqual(broken.reason, "CSV 不可读")
        bad_texts = _rendered_card_texts(lambda: app_module._v2_phase0_shadow())
        self.assertIn("CSV 不可读", bad_texts)
        self.assertIn(PHASE0_NAIL, bad_texts)

        alias_only = self.root / "alias-shadow.csv"
        alias_only.write_text(
            "status,exit_reason,liq_usd,shadow_R3,shadow_skip_stop_prone\n"
            "closed,stop_loss,1000,1,1\n",
            encoding="utf-8",
        )
        os.environ["SHADOW_PHASE0_CSV"] = str(alias_only)
        alias = load_shadow_phase0()
        self.assertTrue(alias.available, alias.reason)
        self.assertEqual(alias.closed, 1)
        self.assertFalse(alias.r3.present)
        self.assertFalse(alias.liq.present)
        self.assertIsNone(alias.both)
        self.assertEqual(alias.r3.name, FLAG_R3)
        self.assertEqual(alias.liq.name, FLAG_LIQ)

        null_liq = self.root / "null-liq-shadow.csv"
        null_liq.write_text(
            "status,exit_reason,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"
            "closed,stop_loss,,1,1\n"
            "closed,take_profit,NULL,0,1\n"
            "closed,max_hold,1000,0,1\n"
            "open,stop_loss,,1,1\n",
            encoding="utf-8",
        )
        os.environ["SHADOW_PHASE0_CSV"] = str(null_liq)
        guarded = load_shadow_phase0()
        self.assertTrue(guarded.available, guarded.reason)
        self.assertEqual(guarded.closed, 3)
        self.assertEqual(guarded.liq_null, 2)
        self.assertEqual(guarded.r3.n, 1)
        self.assertEqual(guarded.r3.sl_hit, 1)
        self.assertEqual(guarded.r3.sl_total, 1)
        self.assertEqual(guarded.r3.tp_false, 0)
        self.assertEqual(guarded.liq.n, 1)
        self.assertEqual(guarded.liq.sl_hit, 0)
        self.assertEqual(guarded.liq.tp_false, 0)
        self.assertEqual(guarded.liq.tp_total, 1)
        self.assertEqual(guarded.both, 0)

        anchor = self.root / "anchor-shadow.csv"
        lines = ["status,exit_reason,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"]

        def add(count: int, status: str, reason: str, liq: str, r3: int, liq_flag: int) -> None:
            for _ in range(count):
                lines.append(f"{status},{reason},{liq},{r3},{liq_flag}\n")

        add(5, "closed", "stop_loss", "10000", 1, 1)
        add(5, "closed", "stop_loss", "200000", 0, 0)
        add(3, "closed", "take_profit", "200000", 0, 0)
        add(11, "closed", "max_hold", "10000", 1, 1)
        add(14, "closed", "max_hold", "200000", 1, 0)
        add(11, "closed", "max_hold", "10000", 0, 1)
        add(93, "closed", "max_hold", "200000", 0, 0)
        add(1, "open", "stop_loss", "10000", 1, 1)
        anchor.write_text("".join(lines), encoding="utf-8")
        before = anchor.stat().st_mtime_ns
        os.environ["SHADOW_PHASE0_CSV"] = str(anchor)
        report = load_shadow_phase0()
        self.assertEqual(anchor.stat().st_mtime_ns, before)
        self.assertTrue(report.available, report.reason)
        self.assertEqual(report.closed, 142)
        self.assertEqual(report.liq_null, 0)
        self.assertEqual(report.r3.n, 30)
        self.assertEqual((report.r3.sl_hit, report.r3.sl_total), (5, 10))
        self.assertEqual(report.r3.tp_false, 0)
        self.assertEqual(report.r3.tp_total, 3)
        self.assertEqual(report.liq.n, 27)
        self.assertEqual((report.liq.sl_hit, report.liq.sl_total), (5, 10))
        self.assertEqual(report.liq.tp_false, 0)
        self.assertEqual(report.both, 16)

        texts = _rendered_card_texts(lambda: app_module._mount_phase0_shadow())
        self.assertIn("止损", texts)
        self.assertIn("影子", texts)
        self.assertIn(FLAG_R3, texts)
        self.assertIn(FLAG_LIQ, texts)
        self.assertNotIn("shadow_R3", texts)
        self.assertEqual(texts[texts.index("命中 n") + 1], "30")
        self.assertEqual(texts[texts.index("命中 n") + 2], "27")
        self.assertEqual(texts[texts.index("抓 SL") + 1], "5/10")
        self.assertEqual(texts[texts.index("抓 SL") + 2], "5/10")
        self.assertEqual(texts[texts.index("误伤 TP") + 1], "0/3")
        self.assertEqual(texts[texts.index("误伤 TP") + 2], "0/3")
        joined = "\n".join(texts)
        self.assertIn("同为 1：16", joined)
        self.assertIn("closed 142", joined)
        self.assertIn("NULL liq 0 不打 liq 标", joined)
        self.assertIn("不 soft-skip", joined)
        self.assertIn(PHASE0_NAIL, joined)
        self.assertEqual(
            PHASE0_NAIL,
            "Phase0 只打标≠有效性；NULL liq 不打 liq 标；48h 后再批 Phase1",
        )

        overview = inspect.getsource(app_module._render_dashboard)
        self.assertIn("_mount_phase0_shadow", overview)
        self.assertIn("_mount_m7_shadow", overview)
        self.assertIn("_mount_goal_20k", overview)
        self.assertIn('("平仓", "旁证")', overview)
        phase_src = inspect.getsource(app_module._mount_phase0_shadow)
        goal_src = inspect.getsource(app_module._mount_goal_20k)
        self.assertNotIn("_mount_goal_20k", phase_src)
        self.assertNotIn("_v2_phase0_shadow", goal_src)
        self.assertNotIn("place_order", inspect.getsource(app_module._v2_phase0_shadow))
        card_src = inspect.getsource(app_module._v2_phase0_shadow)
        self.assertIn("FLAG_R3", card_src)
        self.assertIn("FLAG_LIQ", card_src)
        self.assertNotIn("soft_skip", card_src)
        self.assertNotIn('"shadow_R3"', card_src)
        self.assertNotIn("'shadow_R3'", card_src)

    def test_m7_s0_s1_shadow_card(self) -> None:
        missing_csv = self.root / "no-such-m7.csv"
        missing = DataStore(
            DbPaths(
                main=self.root / "missing-main.db",
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        absent = missing.m7_shadow_register(csv_path=missing_csv)
        self.assertFalse(absent.available)
        self.assertIn("CSV 缺", absent.reason)
        self.assertIn("数据库文件不存在", absent.reason)
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root / "empty-root")
        gap_texts = _rendered_card_texts(lambda: app_module._v2_m7_shadow(missing))
        os.environ.pop("DASHBOARD_DB_ROOT", None)
        gap_joined = "\n".join(gap_texts)
        self.assertIn("CSV 缺", gap_joined)
        self.assertIn(app_module._M7_NAIL, gap_texts)
        self.assertIn("S0 历史仅 8 笔对照极薄", gap_joined)
        self.assertIn("勿用薄样本批改闸", gap_joined)
        self.assertIn(app_module._M7_PHASE0_NOTE, gap_texts)

        bare = self.root / "bare-m7.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare))
        bare_report = bare_store.m7_shadow_register(csv_path=missing_csv)
        self.assertFalse(bare_report.available, bare_report.reason)
        self.assertIn("缺少数据表", bare_report.reason)

        thin = self.root / "thin-m7.db"
        with sqlite3.connect(thin) as conn:
            conn.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, action TEXT)")
        thin_store = DataStore(DbPaths(main=thin, execution=thin, paper=thin, backtest=thin, news=thin))
        thin_report = thin_store.m7_shadow_register(csv_path=missing_csv)
        self.assertFalse(thin_report.available, thin_report.reason)
        self.assertIn("raw_json", thin_report.reason)

        def pack(liq: object = "OMIT", purpose: str = "live_candidate", phase0: bool = False) -> str:
            extras: dict[str, object] = {"purpose": purpose}
            if liq != "OMIT":
                extras["liquidity_usd"] = liq
            if phase0:
                extras["shadow_liq_lt_150k"] = 1
                extras["shadow_R3"] = True
            return json.dumps({"extras": extras})

        main = self.root / "m7-decision.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    action TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO decision_log (id, ts, action, raw_json) VALUES (?, ?, ?, ?)",
                [
                    (1, "2026-10-20T00:00:01Z", "buy", pack(100_000)),
                    (2, "2026-10-20T00:00:02Z", "buy", pack(100_000.5)),
                    (3, "2026-10-20T00:00:03Z", "buy", pack(" 150000 ")),
                    (4, "2026-10-20T00:00:04Z", "buy", pack(50_000)),
                    (5, "2026-10-20T00:00:05Z", "buy", pack(99_999.99)),
                    (6, "2026-10-20T00:00:06Z", "buy", pack(49_999.99)),
                    (7, "2026-10-20T00:00:07Z", "buy", pack(None)),
                    (8, "2026-10-20T00:00:08Z", "buy", pack()),
                    (9, "2026-10-20T00:00:09Z", "buy", pack("abc")),
                    (10, "2026-10-20T00:00:10Z", "buy", pack("")),
                    (11, "2026-10-20T00:00:11Z", "buy", pack(True)),
                    (12, "2026-10-20T00:00:12Z", "buy", pack("n/a")),
                    (13, "2026-10-20T00:00:13Z", "buy", pack(120_000, phase0=True)),
                    (14, "2026-10-20T00:00:14Z", "buy", pack(200_000, purpose="sample")),
                    (15, "2026-10-20T00:00:15Z", "sell", pack(200_000)),
                    (16, "2026-10-20T00:00:16Z", "buy", pack(1)),
                    (17, "2026-10-20T00:00:17Z", "buy", pack(phase0=True)),
                    (
                        18,
                        "2026-10-20T00:00:18Z",
                        "buy",
                        json.dumps(
                            {
                                "purpose": "live_candidate",
                                "liquidity": {"usd": 500_000},
                                "extras": {},
                            }
                        ),
                    ),
                    (19, "2026-10-20T00:00:19Z", "BUY", pack(180_000)),
                ],
            )
        store = DataStore(DbPaths(main=main, execution=main, paper=main, backtest=main, news=main))
        before = main.stat().st_mtime_ns
        fallback = store.m7_shadow_register(csv_path=missing_csv)
        self.assertTrue(fallback.available, fallback.reason)
        data = fallback.data
        self.assertEqual(data.n_s1, 5)
        self.assertEqual(data.n_s0, 2)
        self.assertEqual(data.n_missing, 8)
        self.assertEqual(data.n_leak, 2)
        self.assertEqual(data.n_phase0, 2)
        self.assertEqual(data.would_open_s0, 7)
        self.assertEqual(data.would_open_s1, 5)
        self.assertEqual(data.cohort, "live_candidate buy")
        self.assertEqual(data.source_kind, "decision_log")
        self.assertIn("decision_log.raw_json.extras.liquidity_usd", data.source)
        self.assertIn("CSV 缺", data.source)
        self.assertEqual(data.n_s1 + data.n_s0 + data.n_missing, data.counted - data.n_leak)
        self.assertEqual(main.stat().st_mtime_ns, before)

        def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            fields = [
                "ts",
                "decision_log_id",
                "chain",
                "token",
                "liq_usd",
                "bucket",
                "would_open_S0",
                "would_open_S1",
                "extras",
            ]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)

        def extras_for(purpose: str, **flags: object) -> str:
            payload: dict[str, object] = {"purpose": purpose}
            payload.update(flags)
            return json.dumps(payload, separators=(",", ":"))

        register = self.root / "exports" / "m7_s0_s1_shadow_register.csv"
        csv_rows: list[dict[str, object]] = []
        for index in range(538):
            flags = {"shadow_liq_lt_150k": 1} if index == 0 else {}
            csv_rows.append(
                {
                    "ts": "2026-10-20T00:44:22Z",
                    "decision_log_id": str(index + 1),
                    "chain": "solana",
                    "token": "tok",
                    "liq_usd": "150000" if index else "100,000",
                    "bucket": "S1",
                    "would_open_S0": "1",
                    "would_open_S1": "1",
                    "extras": extras_for("live_candidate", **flags),
                }
            )
        for index in range(8):
            csv_rows.append(
                {
                    "ts": "2026-10-20T00:50:00Z",
                    "decision_log_id": str(1000 + index),
                    "chain": "solana",
                    "token": "s0",
                    "liq_usd": "50000" if index == 0 else "99999.99",
                    "bucket": "S0",
                    "would_open_S0": "1",
                    "would_open_S1": "0",
                    "extras": extras_for("live_candidate", S1_skip=1),
                }
            )
        for index in range(68):
            flags = {"shadow_R3": 1} if index == 0 else {}
            csv_rows.append(
                {
                    "ts": "2026-10-20T00:55:00Z",
                    "decision_log_id": str(2000 + index),
                    "chain": "solana",
                    "token": "gap",
                    "liq_usd": "",
                    "bucket": "S1" if index == 0 else "",
                    "would_open_S0": "1",
                    "would_open_S1": "1",
                    "extras": extras_for("live_candidate", **flags),
                }
            )
        for index, liq in enumerate(("1000", "49999.99", "0")):
            csv_rows.append(
                {
                    "ts": "2026-10-20T01:00:00Z",
                    "decision_log_id": str(3000 + index),
                    "chain": "solana",
                    "token": "leak",
                    "liq_usd": liq,
                    "bucket": "liq_below_min",
                    "would_open_S0": "0",
                    "would_open_S1": "0",
                    "extras": extras_for("live_candidate", leak_below_S0=1),
                }
            )
        csv_rows.append(
            {
                "ts": "2026-10-20T01:01:00Z",
                "decision_log_id": "4001",
                "chain": "solana",
                "token": "sample",
                "liq_usd": "250000",
                "bucket": "S1",
                "would_open_S0": "1",
                "would_open_S1": "1",
                "extras": extras_for("sample"),
            }
        )
        csv_rows.append(
            {
                "ts": "2026-10-20T01:02:00Z",
                "decision_log_id": "4002",
                "chain": "solana",
                "token": "sell",
                "liq_usd": "250000",
                "bucket": "S1",
                "would_open_S0": "1",
                "would_open_S1": "1",
                "extras": extras_for("live_candidate", action="sell"),
            }
        )
        write_csv(register, csv_rows)
        before_csv = main.stat().st_mtime_ns
        cross = store.m7_shadow_register(csv_path=register)
        self.assertTrue(cross.available, cross.reason)
        book = cross.data
        self.assertEqual(book.n_s1, 538)
        self.assertEqual(book.n_s0, 8)
        self.assertEqual(book.n_missing, 68)
        self.assertEqual(book.n_leak, 3)
        self.assertEqual(book.would_open_s0, 546)
        self.assertEqual(book.would_open_s1, 538)
        self.assertEqual(book.n_phase0, 2)
        self.assertEqual(book.cohort, "live_candidate buy")
        self.assertEqual(book.source_kind, "csv")
        self.assertEqual(book.source, "exports/m7_s0_s1_shadow_register.csv")
        self.assertEqual(main.stat().st_mtime_ns, before_csv)

        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        discovered = store.m7_shadow_register()
        self.assertTrue(discovered.available, discovered.reason)
        self.assertEqual(discovered.data.n_s1, 538)
        self.assertEqual(discovered.data.n_s0, 8)
        self.assertEqual(discovered.data.n_missing, 68)
        self.assertEqual(discovered.data.source_kind, "csv")

        nested = self.root / "pkg"
        os.environ["DASHBOARD_DB_ROOT"] = str(nested)
        from_parent = store.m7_shadow_register()
        self.assertEqual(from_parent.data.source_kind, "csv")
        self.assertEqual(from_parent.data.n_s1, 538)
        nearer = nested / "exports" / "m7_s0_s1_shadow_register.csv"
        write_csv(
            nearer,
            [
                {
                    "ts": "2026-10-20T02:00:00Z",
                    "decision_log_id": "1",
                    "chain": "solana",
                    "token": "only",
                    "liq_usd": "60000",
                    "bucket": "S1",
                    "would_open_S0": "1",
                    "would_open_S1": "1",
                    "extras": extras_for("live_candidate"),
                }
            ],
        )
        preferred = store.m7_shadow_register()
        self.assertEqual(preferred.data.n_s0, 1)
        self.assertEqual(preferred.data.n_s1, 0)
        self.assertEqual(preferred.data.would_open_s1, 0)
        self.assertEqual(preferred.data.source, "exports/m7_s0_s1_shadow_register.csv")
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)

        header_only = self.root / "empty-m7.csv"
        header_only.write_text(
            "ts,decision_log_id,liq_usd,bucket,extras\n",
            encoding="utf-8",
        )
        empty = store.m7_shadow_register(csv_path=header_only)
        self.assertTrue(empty.available, empty.reason)
        self.assertEqual(empty.data.n_s1, 0)
        self.assertEqual(empty.data.n_s0, 0)
        self.assertEqual(empty.data.n_missing, 0)
        self.assertEqual(empty.data.n_leak, 0)
        self.assertEqual(empty.data.source_kind, "csv")
        self.assertEqual(main.stat().st_mtime_ns, before_csv)

        broken = self.root / "broken-m7.csv"
        broken.write_bytes(b"\xff\xfe not a csv")
        rescued = store.m7_shadow_register(csv_path=broken)
        self.assertTrue(rescued.available, rescued.reason)
        self.assertEqual(rescued.data.source_kind, "decision_log")
        self.assertIn("CSV 无法读取", rescued.data.source)
        self.assertEqual(rescued.data.n_s1, 5)

        hinted = self.root / "hint-m7.csv"
        hinted.write_text(
            "decision_log_id,bucket,extras\n"
            '1,S0,"{""purpose"":""live_candidate""}"\n'
            '2,leak,"{""purpose"":""live_candidate""}"\n'
            '3,liq_missing,"{""purpose"":""live_candidate""}"\n'
            '4,S1,"{""purpose"":""live_candidate""}"\n'
            '5,liq_<50k,"{""purpose"":""live_candidate""}"\n',
            encoding="utf-8",
        )
        hints = store.m7_shadow_register(csv_path=hinted)
        self.assertEqual(hints.data.n_s0, 1)
        self.assertEqual(hints.data.n_s1, 1)
        self.assertEqual(hints.data.n_missing, 1)
        self.assertEqual(hints.data.n_leak, 2)
        self.assertIn("CSV 无 action 列", hints.data.note)

        duped = self.root / "dupe-m7.csv"
        write_csv(
            duped,
            [
                {
                    "ts": "2026-10-20T03:00:00Z",
                    "decision_log_id": "9",
                    "chain": "solana",
                    "token": "a",
                    "liq_usd": "60000",
                    "bucket": "S0",
                    "would_open_S0": "1",
                    "would_open_S1": "0",
                    "extras": extras_for("live_candidate"),
                },
                {
                    "ts": "2026-10-20T03:01:00Z",
                    "decision_log_id": "9",
                    "chain": "solana",
                    "token": "a",
                    "liq_usd": "150000",
                    "bucket": "S0",
                    "would_open_S0": "1",
                    "would_open_S1": "0",
                    "extras": extras_for("live_candidate"),
                },
            ],
        )
        deduped = store.m7_shadow_register(csv_path=duped)
        self.assertEqual(deduped.data.n_s1, 1)
        self.assertEqual(deduped.data.n_s0, 0)
        self.assertEqual(deduped.data.dupes, 1)
        self.assertIn("保留末行", deduped.data.note)

        corrupt = self.root / "corrupt-m7.db"
        corrupt.write_text("{not json and not sqlite", encoding="utf-8")
        corrupt_store = DataStore(
            DbPaths(main=corrupt, execution=corrupt, paper=corrupt, backtest=corrupt, news=corrupt)
        )
        corrupt_report = corrupt_store.m7_shadow_register(csv_path=missing_csv)
        self.assertFalse(corrupt_report.available, corrupt_report.reason)
        self.assertIn("查询失败", corrupt_report.reason)
        self.assertIn("CSV 缺", corrupt_report.reason)

        texts = _rendered_card_texts(lambda: app_module._mount_m7_shadow(store))
        self.assertIn("M7", texts)
        self.assertIn("S0|S1|liq_missing", texts)
        self.assertEqual(texts[texts.index("S1") + 1], "538")
        self.assertEqual(texts[texts.index("S0") + 1], "8")
        self.assertEqual(texts[texts.index("liq_missing") + 1], "68")
        self.assertEqual(texts[texts.index("would_open_S0") + 1], "546")
        self.assertEqual(texts[texts.index("would_open_S1") + 1], "538")
        self.assertEqual(texts[texts.index("leak") + 1], "3")
        self.assertEqual(texts[texts.index("Phase0") + 1], "2")
        joined = "\n".join(texts)
        self.assertIn("live_candidate buy", joined)
        self.assertIn("exports/m7_s0_s1_shadow_register.csv", joined)
        self.assertIn("leak 不并进 S0 / S1 / liq_missing", joined)
        self.assertIn(app_module._M7_NAIL, joined)
        self.assertIn("影簿≠已改闸", joined)
        self.assertIn("不算有效性", joined)
        self.assertNotIn("低于门槛", joined)

        overview = inspect.getsource(app_module._render_dashboard)
        self.assertIn("_mount_m7_shadow", overview)
        self.assertIn("_mount_phase0_shadow", overview)
        card_src = inspect.getsource(app_module._v2_m7_shadow)
        self.assertIn("_M7_NAIL", card_src)
        self.assertIn("liq_missing", card_src)
        self.assertIn("would_open_S0", card_src)
        self.assertNotIn("place_order", card_src)
        self.assertNotIn("低于门槛", card_src)
        self.assertEqual(
            app_module._M7_NAIL,
            "影簿≠已改闸 · S0 历史仅 8 笔对照极薄 · 勿用薄样本批改闸",
        )
        os.environ.pop("DASHBOARD_DB_ROOT", None)

    def test_package_has_no_write_sql_or_trade_controls(self) -> None:
        root = Path(__file__).resolve().parent
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "smoke_test.py":
                continue
            text = path.read_text(encoding="utf-8")
            if _WRITE_SQL.search(text) and path.name != "sessions.py":
                offenders.append(f"write sql in {path.name}")
            if "sqlite3.connect" in text and path.name not in ("data.py", "sessions.py"):
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
