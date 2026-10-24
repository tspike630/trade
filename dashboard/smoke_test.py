"""Offline checks for the read-only dashboard.

Builds temporary SQLite files, reads them through ``DataStore``, and checks that
missing databases degrade instead of raising. Run from the project root::

    python -m dashboard.smoke_test
"""

from __future__ import annotations

import csv
from decimal import Decimal
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
from unittest.mock import patch
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
    KPI_DAILY_LOSS_LINE_USD,
    KPI_WINDOW_DAYS,
    SLIP_SUSPICIOUS_TABLES,
    SOURCE_TABLE_DAMAGE_BANNER,
    DataStore,
    source_table_banner,
    DbPaths,
    _WRITE_RE,
    connect_ro,
    dedup_token_count,
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
import dashboard.c_exit_shadow as c_exit_module
import dashboard.entry_c_exit_joint as joint_module
import dashboard.phase1_counterfactual as phase1_module
from dashboard.c_exit_shadow import (
    C_EXIT_DISCIPLINE,
    C_EXIT_NAIL,
    C_EXIT_ROLLBACK,
    C_EXIT_THRESHOLD,
    C_EXIT_VERDICT,
    load_c_exit_shadow,
    tag_line as c_exit_tag_line,
)
from dashboard.entry_c_exit_joint import (
    JOINT_DISCIPLINE,
    JOINT_LIQ_LIVE,
    JOINT_LIQ_NAIL,
    JOINT_NAIL,
    JOINT_PNL_NOTE,
    JOINT_VERDICT,
    contrast_cells as joint_contrast_cells,
    liq_risk_line as joint_liq_risk_line,
    load_entry_c_exit_joint,
    nail_line as joint_nail_line,
    nine_line as joint_nine_line,
    peak_line as joint_peak_line,
    verdict_line as joint_verdict_line,
)
from dashboard.phase1_counterfactual import (
    PHASE1_CANDIDATE,
    PHASE1_CF_NAIL,
    PHASE1_WINDOW,
    load_phase1_counterfactual,
)
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
    "PHASE1_CF_CSV",
    "C_EXIT_SHADOW_DB",
    "C_EXIT_SHADOW010_DB",
    "C_EXIT_SELL_SLIP_CSV",
    "ENTRY_C_EXIT_JOINT_CSV",
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
        self.assertEqual(week.data.n_decisions_dedup, 2)
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
        self.assertEqual(report.data.first_hit_dedup, 2)
        self.assertEqual(report.data.first_hit_live_dedup, 2)

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
        self.assertEqual(buckets.data.rows[0].n_dedup, 2)

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
        self.assertEqual(data.total_dedup, 11)
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

        texts = _rendered_card_texts(lambda: app_module._mount_goal_20k(store, now=start))
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
        self.assertIn("保守 $8,283.14 · 缺口 ≈$11.7k", joined)
        self.assertIn(app_module.KPI_GAP_PRIMARY_NOTE, joined)
        self.assertIn("原账 $8,704.00 · 缺口 ≈$11.3k", joined)
        book_texts = _rendered_card_texts(lambda: app_module._v2_equity_book_row(data))
        book_joined = "\n".join(book_texts)
        self.assertEqual(book_texts[book_texts.index("原账") + 1], "$8,704.00")
        self.assertEqual(book_texts[book_texts.index("保守") + 1], "$8,283.14 · 待定")
        self.assertIn("$8,530.57（代理 $8,356.48 参考）", book_texts)
        self.assertIn("$8,886.49", book_texts)
        self.assertNotIn("仅剔向下", book_joined)
        self.assertIn(app_module.V_MARK_NOTE, book_texts)
        self.assertIn(app_module.EQUITY_RULER_NOTE, book_texts)
        self.assertIn("真止盈 0/143（3 笔止盈均为向上假报价）", book_joined)
        mount_texts = _rendered_card_texts(lambda: app_module._mount_goal_20k(store))
        mount_joined = "\n".join(mount_texts)
        self.assertIn("距", mount_texts)
        self.assertIn("$20k", mount_texts)
        self.assertIn("10日目标旁证", mount_joined)
        self.assertIn("起点 2026-10-20", mount_joined)
        self.assertIn("截止 2026-10-30 HKT", mount_joined)
        self.assertIn(app_module._GOAL_NAIL, mount_joined)
        self.assertIn("目标跟踪≠会赚会亏；不改闸", mount_joined)
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

    def test_kpi_reverse_card_renders(self) -> None:
        self.assertEqual(KPI_DAILY_LOSS_LINE_USD, 200.0)
        self.assertEqual(KPI_WINDOW_DAYS, 7)
        self.assertEqual(
            app_module._KPI_NAIL,
            "只读倒推 · 不改闸 · 不改 .env · 开杠杆由五层互批",
        )
        cases = [name for name in DashboardSmokeTest.__dict__ if name.startswith("test_")]
        self.assertEqual(len(cases), 32)

        missing = DataStore(
            DbPaths(
                main=self.root / "missing-kpi-main.db",
                execution=self.root / "missing-kpi-exec.db",
                paper=self.root / "missing-kpi-paper.db",
                backtest=self.root / "missing-kpi-bt.db",
                news=self.root / "missing-kpi-news.db",
            )
        )
        absent = missing.paper_kpi_reverse()
        self.assertFalse(absent.available)
        self.assertIn("数据库文件不存在", absent.reason)
        gap_texts = _rendered_card_texts(lambda: app_module._mount_kpi_reverse(missing))
        self.assertIn("数据库文件不存在", "\n".join(gap_texts))
        self.assertIn(app_module._KPI_NAIL, gap_texts)
        self.assertNotIn("负期望 × 杠杆 = 放大亏损", gap_texts)

        bare = self.root / "bare-kpi.db"
        sqlite3.connect(bare).close()
        bare_store = DataStore(
            DbPaths(main=bare, execution=bare, paper=bare, backtest=bare, news=bare)
        )
        bare_kpi = bare_store.paper_kpi_reverse()
        self.assertFalse(bare_kpi.available, bare_kpi.reason)
        self.assertIn("缺少数据表", bare_kpi.reason)

        corrupt = self.root / "corrupt-kpi.db"
        corrupt.write_text("{not json and not sqlite", encoding="utf-8")
        corrupt_store = DataStore(
            DbPaths(main=corrupt, execution=corrupt, paper=corrupt, backtest=corrupt, news=corrupt)
        )
        corrupt_kpi = corrupt_store.paper_kpi_reverse()
        self.assertFalse(corrupt_kpi.available, corrupt_kpi.reason)
        self.assertIn("查询失败", corrupt_kpi.reason)

        def _store(name: str, rows: list[tuple[object, ...]], *, cash: float = 8000.0) -> DataStore:
            path = self.root / name
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
                conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, ?)", (cash,))
                conn.execute(
                    """
                    CREATE TABLE positions (
                        id TEXT PRIMARY KEY,
                        size_usd REAL,
                        status TEXT,
                        closed_at TEXT,
                        pnl_usd REAL,
                        sl_pct REAL
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO positions (id, size_usd, status, closed_at, pnl_usd, sl_pct)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            return DataStore(DbPaths(main=path, execution=path, paper=path, backtest=path, news=path))

        hkt = ZoneInfo("Asia/Hong_Kong")
        start = datetime(2026, 10, 20, 4, 0, tzinfo=hkt)
        happy_rows = [
            ("open-a", 450, "open", None, None, 5),
            ("open-b", 254, "OPEN", "", None, 4),
            ("c1", 4000, "closed", "2026-10-19T16:30:00Z", -30, 5),
            ("c2", 100, "closed", "2026-10-13T16:00:00Z", -10, 5),
            ("c3", 80, "closed", "2026-10-13T15:59:00Z", 5, 5),
            ("c4", 80, "closed", "2026-10-20T15:00:00Z", -5, 5),
            ("cancel", 9999, "cancelled", None, -999, 50),
        ]
        store = _store("kpi-happy.db", happy_rows)
        paper = self.root / "kpi-happy.db"
        before = paper.stat().st_mtime_ns
        report = store.paper_kpi_reverse(now=start)
        self.assertTrue(report.available, report.reason)
        self.assertEqual(paper.stat().st_mtime_ns, before)
        data = report.data
        self.assertAlmostEqual(data.gap_usd, 11_296)
        self.assertEqual(data.days_left, 10)
        self.assertAlmostEqual(data.daily_need_usd or 0, 1129.6)
        self.assertEqual(data.as_of_day, "2026-10-20")
        self.assertEqual(data.closed_n, 4)
        self.assertAlmostEqual(data.pnl_sum_usd or 0, -40)
        self.assertAlmostEqual(data.avg_pnl_usd or 0, -10)
        self.assertAlmostEqual(data.avg_closes_per_day or 0, round(3 / 7, 4))
        self.assertEqual(data.window_days, 7)
        self.assertTrue(data.negative_expectancy)
        self.assertEqual(data.daily_loss_line_usd, 200.0)
        self.assertAlmostEqual(data.max_stop_usd or 0, 200.0)
        self.assertEqual(data.gap_note, "")
        labels = [row.label for row in data.pace_rows]
        self.assertEqual(labels, ["近7日", "5", "10", "20"])
        by_label = {row.label: row for row in data.pace_rows}
        self.assertAlmostEqual(by_label["近7日"].closes_per_day or 0, 0.4286)
        self.assertAlmostEqual(by_label["近7日"].need_per_trade_usd or 0, round(1129.6 / 0.4286, 4))
        self.assertEqual(by_label["近7日"].contrast, "符号相反")
        self.assertAlmostEqual(by_label["5"].need_per_trade_usd or 0, 225.92)
        self.assertEqual(by_label["5"].contrast, "符号相反")
        self.assertAlmostEqual(by_label["10"].need_per_trade_usd or 0, 112.96)
        self.assertAlmostEqual(by_label["20"].need_per_trade_usd or 0, 56.48)
        self.assertEqual(by_label["20"].live_avg_pnl_usd, -10)
        steps = {row.multiple: row for row in data.leverage_rows}
        self.assertEqual(sorted(steps), [1, 2, 3])
        self.assertAlmostEqual(steps[1].stop_usd or 0, 200.0)
        self.assertEqual(steps[1].vs_line, "触线")
        self.assertAlmostEqual(steps[2].stop_usd or 0, 400.0)
        self.assertEqual(steps[2].vs_line, "超线")
        self.assertAlmostEqual(steps[3].stop_usd or 0, 600.0)
        self.assertEqual(steps[3].vs_line, "超线")

        eve = store.paper_kpi_reverse(now=datetime(2026, 10, 13, 15, 59, tzinfo=timezone.utc))
        self.assertEqual(eve.data.as_of_day, "2026-10-13")
        self.assertAlmostEqual(eve.data.avg_closes_per_day or 0, round(1 / 7, 4))
        opened = store.paper_kpi_reverse(now=datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(opened.data.as_of_day, "2026-10-14")
        self.assertAlmostEqual(opened.data.avg_closes_per_day or 0, round(2 / 7, 4))

        texts = _rendered_card_texts(lambda: app_module._mount_kpi_reverse(store, now=start))
        self.assertIn("KPI", texts)
        self.assertIn("倒推", texts)
        self.assertEqual(texts[texts.index("缺口") + 1], "+$11,296.00")
        self.assertEqual(texts[texts.index("剩余天") + 1], "10")
        self.assertEqual(texts[texts.index("日需") + 1], "+$1,129.60")
        self.assertEqual(texts[texts.index("已平") + 1], "4")
        self.assertEqual(texts[texts.index("每笔均") + 1], "-$10.00")
        self.assertEqual(texts[texts.index("近7日均笔") + 1], "0.4286")
        near = texts.index("近7日 0.4286")
        self.assertEqual(texts[near + 1], "+$2,635.56")
        self.assertEqual(texts[near + 2], "-$10.00")
        self.assertEqual(texts[near + 3], "符号相反")
        five = texts.index("5 笔")
        self.assertEqual(texts[five + 1], "+$225.92")
        self.assertEqual(texts[five + 3], "符号相反")
        joined = "\n".join(texts)
        self.assertIn("负期望 × 杠杆 = 放大亏损", joined)
        self.assertIn("单笔最大止损 $200.00", joined)
        self.assertIn("1× $200.00 · 日亏线 $200 · 触线", joined)
        self.assertIn("2× $400.00 · 日亏线 $200 · 超线", joined)
        self.assertIn("3× $600.00 · 日亏线 $200 · 超线", joined)
        self.assertIn("止损 = size_usd×sl_pct/100 · 不含撤单", joined)
        self.assertIn("近7个 HKT 日含零成交日", joined)
        self.assertIn("口径日 2026-10-20 HKT", joined)
        self.assertIn("保守 $8,283.14 · 缺口 ≈$11.7k", joined)
        self.assertIn(app_module.KPI_GAP_PRIMARY_NOTE, joined)
        self.assertIn("硬 KPI 倒推", joined)
        self.assertIn("截止 2026-10-30 HKT", joined)
        self.assertIn(app_module._KPI_NAIL, joined)
        self.assertNotIn("$999", joined)

        deadline = datetime(2026, 10, 30, 0, 0, tzinfo=hkt)
        ended = store.paper_kpi_reverse(now=deadline)
        self.assertEqual(ended.data.days_left, 0)
        self.assertIsNone(ended.data.daily_need_usd)
        self.assertTrue(all(row.need_per_trade_usd is None for row in ended.data.pace_rows))
        self.assertTrue(all(row.contrast == "" for row in ended.data.pace_rows))
        self.assertTrue(ended.data.negative_expectancy)
        ended_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(store, now=deadline))
        self.assertEqual(ended_texts[ended_texts.index("剩余天") + 1], "0")
        self.assertEqual(ended_texts[ended_texts.index("日需") + 1], "—")
        self.assertIn("负期望 × 杠杆 = 放大亏损", ended_texts)
        self.assertIn(app_module._KPI_NAIL, ended_texts)

        positive_rows = [
            ("open-a", 450, "open", None, None, 5),
            ("open-b", 254, "OPEN", "", None, 4),
            ("p1", 50, "closed", "2026-10-20T01:00:00Z", 30, 5),
            ("p2", 50, "closed", "2026-10-14T00:00:00Z", 10, 5),
        ]
        positive = _store("kpi-positive.db", positive_rows)
        pos = positive.paper_kpi_reverse(now=start)
        self.assertTrue(pos.available, pos.reason)
        self.assertAlmostEqual(pos.data.avg_pnl_usd or 0, 20)
        self.assertFalse(pos.data.negative_expectancy)
        self.assertAlmostEqual(pos.data.avg_closes_per_day or 0, round(2 / 7, 4))
        self.assertEqual(pos.data.pace_rows[1].contrast, "差 11.30 倍")
        self.assertAlmostEqual(pos.data.max_stop_usd or 0, 22.5)
        self.assertEqual(pos.data.leverage_rows[0].vs_line, "低于线")
        self.assertEqual(pos.data.leverage_rows[2].vs_line, "低于线")
        pos_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(positive, now=start))
        self.assertNotIn("负期望 × 杠杆 = 放大亏损", pos_texts)
        self.assertIn("差 11.30 倍", pos_texts)
        self.assertIn("1× $22.50 · 日亏线 $200 · 低于线", pos_texts)
        self.assertIn("3× $67.50 · 日亏线 $200 · 低于线", pos_texts)

        quiet = _store(
            "kpi-quiet.db",
            [
                ("open-a", 450, "open", None, None, 5),
                ("open-b", 254, "OPEN", "", None, 4),
            ],
        )
        quiet_report = quiet.paper_kpi_reverse(now=start)
        self.assertEqual(quiet_report.data.closed_n, 0)
        self.assertIsNone(quiet_report.data.avg_pnl_usd)
        self.assertIsNone(quiet_report.data.pnl_sum_usd)
        self.assertEqual(quiet_report.data.avg_closes_per_day, 0.0)
        self.assertIsNone(quiet_report.data.negative_expectancy)
        self.assertIsNone(quiet_report.data.pace_rows[0].need_per_trade_usd)
        self.assertAlmostEqual(quiet_report.data.pace_rows[1].need_per_trade_usd or 0, 225.92)
        self.assertEqual(quiet_report.data.pace_rows[1].contrast, "")
        self.assertAlmostEqual(quiet_report.data.max_stop_usd or 0, 22.5)
        quiet_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(quiet, now=start))
        self.assertEqual(quiet_texts[quiet_texts.index("已平") + 1], "0")
        self.assertEqual(quiet_texts[quiet_texts.index("每笔均") + 1], "—")
        self.assertEqual(quiet_texts[quiet_texts.index("近7日均笔") + 1], "0")
        self.assertEqual(quiet_texts[quiet_texts.index("近7日 0") + 1], "—")
        self.assertNotIn("负期望 × 杠杆 = 放大亏损", quiet_texts)

        thin_pnl = _store(
            "kpi-thin-pnl.db",
            [
                ("open-a", 450, "open", None, None, 5),
                ("c1", 80, "closed", "2026-10-20T01:00:00Z", None, 5),
                ("c2", 80, "closed", "2026-10-19T01:00:00Z", -8, 5),
            ],
        )
        thin = thin_pnl.paper_kpi_reverse(now=start)
        self.assertEqual(thin.data.closed_n, 2)
        self.assertIsNone(thin.data.avg_pnl_usd)
        self.assertIsNone(thin.data.pnl_sum_usd)
        self.assertIsNone(thin.data.negative_expectancy)
        self.assertIn("1 笔已平缺 pnl_usd", thin.data.gap_note)
        self.assertTrue(all(row.contrast == "" for row in thin.data.pace_rows))
        self.assertIsNotNone(thin.data.pace_rows[1].need_per_trade_usd)
        thin_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(thin_pnl, now=start))
        self.assertEqual(thin_texts[thin_texts.index("每笔均") + 1], "—")
        self.assertIn("1 笔已平缺 pnl_usd", thin_texts)
        self.assertNotIn("负期望 × 杠杆 = 放大亏损", thin_texts)
        self.assertNotIn("$0.00", "\n".join(thin_texts))

        undated = _store(
            "kpi-undated.db",
            [
                ("open-a", 450, "open", None, None, 5),
                ("c1", 80, "closed", "not-a-day", -10, 5),
                ("c2", 80, "closed", "2026-10-20T01:00:00Z", -10, 5),
            ],
        )
        undated_report = undated.paper_kpi_reverse(now=start)
        self.assertEqual(undated_report.data.closed_n, 2)
        self.assertAlmostEqual(undated_report.data.avg_pnl_usd or 0, -10)
        self.assertIsNone(undated_report.data.avg_closes_per_day)
        self.assertIn("1 笔已平无法按 HKT 切日", undated_report.data.gap_note)
        self.assertIsNone(undated_report.data.pace_rows[0].closes_per_day)
        self.assertIsNone(undated_report.data.pace_rows[0].need_per_trade_usd)
        self.assertEqual(undated_report.data.pace_rows[1].contrast, "符号相反")
        undated_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(undated, now=start))
        self.assertEqual(undated_texts[undated_texts.index("近7日均笔") + 1], "—")
        self.assertIn("近7日 —", undated_texts)
        self.assertIn("1 笔已平无法按 HKT 切日", undated_texts)

        partial = _store(
            "kpi-partial-sl.db",
            happy_rows + [("open-gap", 100, "open", None, None, None)],
        )
        partial_report = partial.paper_kpi_reverse(now=start)
        self.assertAlmostEqual(partial_report.data.max_stop_usd or 0, 200.0)
        self.assertIn("未计入最大", partial_report.data.gap_note)
        self.assertIn("1 笔缺 size_usd 或 sl_pct", partial_report.data.gap_note)

        no_sl = self.root / "kpi-no-sl.db"
        with sqlite3.connect(no_sl) as conn:
            conn.execute("CREATE TABLE account (id INTEGER PRIMARY KEY, cash_usd REAL)")
            conn.execute("INSERT INTO account (id, cash_usd) VALUES (1, 8000)")
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    size_usd REAL,
                    status TEXT,
                    closed_at TEXT,
                    pnl_usd REAL
                )
                """
            )
            conn.execute(
                "INSERT INTO positions (id, size_usd, status, closed_at, pnl_usd)"
                " VALUES ('c1', 100, 'closed', '2026-10-20T01:00:00Z', -4)"
            )
        no_sl_store = DataStore(
            DbPaths(main=no_sl, execution=no_sl, paper=no_sl, backtest=no_sl, news=no_sl)
        )
        no_sl_report = no_sl_store.paper_kpi_reverse(now=start)
        self.assertTrue(no_sl_report.available, no_sl_report.reason)
        self.assertIsNone(no_sl_report.data.max_stop_usd)
        self.assertIn("缺少字段：sl_pct", no_sl_report.data.gap_note)
        self.assertTrue(all(row.stop_usd is None for row in no_sl_report.data.leverage_rows))
        no_sl_texts = _rendered_card_texts(lambda: app_module._v2_kpi_reverse(no_sl_store, now=start))
        self.assertIn("单笔最大止损 —", no_sl_texts)
        self.assertIn("1× — · 日亏线 $200 · —", no_sl_texts)
        self.assertIn("缺少字段：sl_pct", no_sl_texts)
        self.assertNotIn("低于线", no_sl_texts)

        signed = _store("kpi-signed-sl.db", [("open-neg", 100, "open", None, None, -5)], cash=1000)
        signed_report = signed.paper_kpi_reverse(now=start)
        self.assertAlmostEqual(signed_report.data.max_stop_usd or 0, 5.0)
        self.assertEqual(signed_report.data.leverage_rows[0].vs_line, "低于线")

        overview = inspect.getsource(app_module._render_dashboard)
        goal_at = overview.index("_mount_goal_20k")
        kpi_at = overview.index("_mount_kpi_reverse")
        phase_at = overview.index("_mount_phase0_shadow")
        self.assertLess(goal_at, kpi_at)
        self.assertLess(kpi_at, phase_at)
        mount_src = inspect.getsource(app_module._mount_kpi_reverse)
        self.assertIn("span-6", mount_src)
        self.assertIn("_KPI_NAIL", mount_src)
        card_src = inspect.getsource(app_module._v2_kpi_reverse)
        self.assertIn("负期望 × 杠杆 = 放大亏损", card_src)
        self.assertIn("日亏线", card_src)
        self.assertNotIn("place_order", card_src)
        self.assertNotIn("os.environ", card_src)
        read_src = inspect.getsource(DataStore._paper_kpi_reverse)
        self.assertIn("ro_connect", read_src)
        self.assertNotIn("os.environ", read_src)
        mirror = Path(__file__).resolve().parent.parent / "crawler" / "dashboard"
        if mirror.is_dir():
            for name in ("data.py", "app.py", "smoke_test.py", "board_truth.py"):
                self.assertEqual(
                    (Path(__file__).resolve().parent / name).read_bytes(),
                    (mirror / name).read_bytes(),
                    name,
                )

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
        self.assertIn(app_module._ENTRY_JEV_BANNER, gap_texts)
        self.assertIn("不 soft-skip", "\n".join(gap_texts))
        phase_joined = "\n".join(gap_texts)
        self.assertIn("比随机好多少 · TP10/SL5/h60 · 扣成本前 −1.46% · 扣1.2% −2.65% · 分档成本 −4.92%", phase_joined)
        self.assertIn("新币低liq(<5万)切片 已淘汰", phase_joined)
        self.assertIn("exports/random_entry_shared_seed20261021_v2_ulj.csv", phase_joined)
        self.assertIn(app_module.RETIRED_GRID_LINE, gap_texts)
        self.assertIn(app_module.REMAINING_ENTRY_LINE, gap_texts)
        self.assertIn(app_module.C4_FLOW_BLIND_LINE, gap_texts)

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

    def test_phase1_counterfactual_card_copy_and_csv_fallback(self) -> None:
        baseline = "基线：closed=142 · ΣPnL≈−$1295.71 · 权益≈$8704"
        line_a = "A R3 skip30：抓 SL 5/10、TP 0 → 剩余 Σ≈−$474（Δ+$822）"
        line_b = "B liq<150k skip27：抓 SL 5/10、TP 0 → 剩余 Σ≈−$450（Δ+$846）"
        line_c = "C 并集 skip41：抓 SL 5/10、TP 0 → 剩余 Σ≈−$425（Δ+$871）"
        honest = "即使最优 C 权益也只≈$9.6k，填不满 $20k"
        packaged_lines = (baseline, line_a, line_b, line_c, honest)

        missing = self.root / "no-such-phase1.csv"
        os.environ["PHASE1_CF_CSV"] = str(missing)
        absent = load_phase1_counterfactual()
        self.assertEqual(absent.source, "packaged")
        self.assertEqual(absent.closed, 142)
        self.assertIn("CSV 缺失", absent.note)
        self.assertIn("回退", absent.note)
        gap_texts = _rendered_card_texts(lambda: app_module._mount_phase1_counterfactual())
        gap_joined = "\n".join(gap_texts)
        for line in packaged_lines:
            self.assertIn(line, gap_texts)
        self.assertIn("Phase1", gap_texts)
        self.assertIn("soft-skip 反事实", gap_texts)
        self.assertIn(PHASE1_CF_NAIL, gap_texts)
        self.assertIn(app_module._ENTRY_JEV_BANNER, gap_texts)
        self.assertIn(PHASE1_WINDOW, gap_texts)
        self.assertIn(PHASE1_CANDIDATE, gap_texts)
        self.assertIn("候选", gap_joined)
        self.assertIn("填不满 $20k", gap_joined)
        self.assertIn("$9.6k", gap_joined)
        self.assertIn("5–6h", gap_joined)
        self.assertIn("不 soft-skip", gap_joined)
        self.assertIn("#71/权0/日亏200 不动", gap_joined)
        self.assertNotIn("已接", gap_joined)

        empty = self.root / "empty-phase1.csv"
        empty.write_text("", encoding="utf-8")
        os.environ["PHASE1_CF_CSV"] = str(empty)
        blank = load_phase1_counterfactual()
        self.assertEqual(blank.source, "packaged")
        self.assertIn("CSV 空", blank.note)
        self.assertIn("回退", blank.note)
        empty_texts = _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual())
        empty_joined = "\n".join(empty_texts)
        self.assertIn("CSV 空", empty_joined)
        self.assertIn(line_a, empty_texts)
        self.assertIn(PHASE1_CF_NAIL, empty_texts)

        bad = self.root / "bad-phase1.csv"
        bad.write_bytes(b"\xff\xfe not-utf8")
        os.environ["PHASE1_CF_CSV"] = str(bad)
        broken = load_phase1_counterfactual()
        self.assertEqual(broken.source, "packaged")
        self.assertEqual(broken.note, "CSV 不可读 · 回退证据包总表 2026-10-20")
        bad_texts = _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual())
        bad_joined = "\n".join(bad_texts)
        self.assertIn("CSV 不可读", bad_joined)
        self.assertIn("回退", bad_joined)
        self.assertIn(line_b, bad_texts)
        self.assertIn(line_c, bad_texts)
        self.assertIn(honest, bad_texts)
        self.assertIn(PHASE1_CF_NAIL, bad_texts)

        alias_only = self.root / "alias-phase1.csv"
        alias_only.write_text(
            "status,exit_reason,pnl_usd,shadow_skip_stop_prone\n"
            "closed,stop_loss,-10,1\n",
            encoding="utf-8",
        )
        os.environ["PHASE1_CF_CSV"] = str(alias_only)
        alias = load_phase1_counterfactual()
        self.assertEqual(alias.source, "packaged")
        self.assertIn("CSV 缺列", alias.note)
        self.assertEqual(alias.closed, 142)

        garbage = self.root / "garbage-phase1.csv"
        garbage.write_text(
            "status,exit_reason,pnl_usd,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"
            "closed,stop_loss,nope,1000,1,0\n",
            encoding="utf-8",
        )
        os.environ["PHASE1_CF_CSV"] = str(garbage)
        junk = load_phase1_counterfactual()
        self.assertEqual(junk.source, "packaged")
        self.assertEqual(junk.note, "CSV 坏数据 · 回退证据包总表 2026-10-20")
        junk_texts = _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual())
        junk_joined = "\n".join(junk_texts)
        self.assertIn("CSV 坏数据", junk_joined)
        self.assertIn("回退", junk_joined)
        self.assertIn(baseline, junk_texts)
        self.assertIn(line_c, junk_texts)
        self.assertIn(PHASE1_CANDIDATE, junk_texts)
        self.assertNotIn("已接", junk_joined)

        header_only = self.root / "header-phase1.csv"
        header_only.write_text(
            "status,exit_reason,pnl_usd,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n",
            encoding="utf-8",
        )
        os.environ["PHASE1_CF_CSV"] = str(header_only)
        bare = load_phase1_counterfactual()
        self.assertEqual(bare.note, "CSV 无 closed · 回退证据包总表 2026-10-20")
        self.assertIn(line_a, _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual()))

        null_liq = self.root / "null-liq-phase1.csv"
        null_liq.write_text(
            "status,exit_reason,pnl_usd,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"
            "closed,stop_loss,-10,,1,1\n"
            "open,stop_loss,-999,1000,1,1\n",
            encoding="utf-8",
        )
        os.environ["PHASE1_CF_CSV"] = str(null_liq)
        guarded = load_phase1_counterfactual()
        self.assertEqual(guarded.source, "csv")
        self.assertEqual(guarded.closed, 1)
        self.assertEqual(guarded.cases[0].key, "A")
        self.assertEqual(guarded.cases[0].skip_n, 1)
        self.assertEqual(guarded.cases[1].key, "B")
        self.assertEqual(guarded.cases[1].skip_n, 0)
        self.assertEqual(guarded.cases[2].key, "C")
        self.assertEqual(guarded.cases[2].skip_n, 1)
        self.assertEqual(guarded.note, "与证据包总表不一致 · 以 CSV 重算")
        null_texts = _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual())
        null_joined = "\n".join(null_texts)
        self.assertIn("以 CSV 重算", null_joined)
        self.assertIn("B liq<150k skip0", null_joined)
        self.assertIn(PHASE1_CF_NAIL, null_texts)
        self.assertIn("填不满 $20k", null_joined)
        self.assertNotIn("closed=142", null_joined)
        self.assertNotIn("回退证据包总表", null_joined)
        self.assertNotIn("已接", null_joined)

        anchor = self.root / "anchor-phase1.csv"
        lines = ["status,exit_reason,pnl_usd,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"]

        def add(count: int, reason: str, pnl: str, liq: str, r3: int, liq_flag: int) -> None:
            for index in range(count):
                cell = pnl if index == 0 else "0"
                lines.append(f"closed,{reason},{cell},{liq},{r3},{liq_flag}\n")

        add(5, "stop_loss", "-854.70", "10000", 1, 1)
        add(11, "max_hold", "58.48", "10000", 1, 1)
        add(14, "max_hold", "-25.38", "200000", 1, 0)
        add(11, "max_hold", "-49.29", "10000", 0, 1)
        add(5, "stop_loss", "-686.19", "200000", 0, 0)
        add(3, "take_profit", "437.66", "200000", 0, 0)
        add(93, "max_hold", "-176.29", "200000", 0, 0)
        lines.append("open,stop_loss,-99999,10000,1,1\n")
        anchor.write_text("".join(lines), encoding="utf-8")
        before = anchor.stat().st_mtime_ns
        os.environ.pop("PHASE1_CF_CSV", None)
        os.environ["SHADOW_PHASE0_CSV"] = str(anchor)
        report = load_phase1_counterfactual()
        self.assertEqual(anchor.stat().st_mtime_ns, before)
        self.assertEqual(report.source, "csv")
        self.assertEqual(report.note, "CSV 与证据包总表一致")
        self.assertEqual(report.closed, 142)
        matched = _rendered_card_texts(lambda: app_module._mount_phase1_counterfactual())
        matched_joined = "\n".join(matched)
        for line in packaged_lines:
            self.assertIn(line, matched)
        self.assertIn("CSV 与证据包总表一致", matched_joined)
        self.assertNotIn("以 CSV 重算", matched_joined)
        self.assertNotIn("回退", matched_joined)
        self.assertIn(PHASE1_CF_NAIL, matched)
        self.assertIn("候选", matched_joined)
        self.assertNotIn("已接", matched_joined)

        shifted = self.root / "shifted-phase1.csv"
        shifted.write_text(
            "status,exit_reason,pnl_usd,liq_usd,shadow_R3_vol_chg_rsi,shadow_liq_lt_150k\n"
            "closed,max_hold,-1.00,200000,1,0\n",
            encoding="utf-8",
        )
        os.environ["PHASE1_CF_CSV"] = str(shifted)
        moved = load_phase1_counterfactual()
        self.assertEqual(moved.source, "csv")
        self.assertEqual(moved.note, "与证据包总表不一致 · 以 CSV 重算")
        self.assertEqual(moved.cases[0].skip_n, 1)
        self.assertEqual(moved.cases[1].skip_n, 0)
        shifted_texts = _rendered_card_texts(lambda: app_module._v2_phase1_counterfactual())
        shifted_joined = "\n".join(shifted_texts)
        self.assertIn("A R3 skip1：抓 SL 0/0、TP 0 → 剩余 Σ≈$0（Δ+$1）", shifted_joined)
        self.assertIn("以 CSV 重算", shifted_joined)
        self.assertNotIn("skip30", shifted_joined)
        self.assertIn("填不满 $20k", shifted_joined)
        self.assertIn(PHASE1_CF_NAIL, shifted_texts)

        overview = inspect.getsource(app_module._render_dashboard)
        phase0_at = overview.index("_mount_phase0_shadow")
        card_at = overview.index("_mount_phase1_counterfactual")
        m7_at = overview.index("_mount_m7_shadow")
        self.assertLess(phase0_at, card_at)
        self.assertLess(card_at, m7_at)
        card_src = inspect.getsource(app_module._v2_phase1_counterfactual)
        mount_src = inspect.getsource(app_module._mount_phase1_counterfactual)
        self.assertIn("honest_line", card_src)
        self.assertIn("PHASE1_CF_NAIL", card_src)
        self.assertIn("PHASE1_CANDIDATE", card_src)
        self.assertNotIn("已接", card_src)
        self.assertNotIn("已接", mount_src)
        self.assertNotIn("place_order", card_src)
        self.assertNotIn("place_order", mount_src)
        module_src = inspect.getsource(phase1_module)
        self.assertNotIn("sqlite3", module_src)
        self.assertNotIn("DECISION_MIN_LIQ", module_src)
        self.assertNotIn("place_order", module_src)
        self.assertNotIn("paper_trading", module_src)
        self.assertEqual(
            PHASE1_CF_NAIL,
            "影簿/反事实≠已改闸 · 不算有效性 · 不 soft-skip · 薄样本勿批改闸 · "
            "与 Phase0 双列并存 · #71/权0/日亏200 不动",
        )
        self.assertIn("候选", PHASE1_CANDIDATE)
        self.assertNotIn("已接", PHASE1_CANDIDATE)

    def test_c_exit_shadow_card_loads_and_nails(self) -> None:
        names = [name for name in dir(DashboardSmokeTest) if name.startswith("test_")]
        self.assertGreaterEqual(len(names), 26)
        self.assertEqual(C_EXIT_NAIL, "假设旁簿≠已改主闸")
        self.assertIn("不改 .env", C_EXIT_DISCIPLINE)
        self.assertIn("不升主", C_EXIT_DISCIPLINE)
        module_src = inspect.getsource(c_exit_module)
        self.assertIn(C_EXIT_NAIL, module_src)
        self.assertNotIn("sqlite3.connect", module_src)
        self.assertNotIn("place_order", module_src)
        self.assertNotIn("LIVE_TRADING_ENABLED", module_src)
        self.assertNotIn("paper_trading.db", module_src)

        line_60 = "c_sl4_h60 · n=141 · Σpnl −$1144.01 · 止损占负 81.6% · 单笔SL峰 −$491.67"
        line_90 = "c_sl5_h90 · n=142 · Σpnl −$546.98 · 止损占负 86.1% · 单笔SL峰 −$491.67"
        missing = self.root / "no-such-c-exit.db"
        self.assertFalse(missing.exists())
        absent = load_c_exit_shadow(missing)
        self.assertFalse(missing.exists())
        self.assertEqual(absent.source, "packaged")
        self.assertIn("旁簿库缺失", absent.note)
        self.assertIn("路径重放非旁簿实盘", absent.note)
        self.assertFalse(absent.rollback_single)
        self.assertFalse(absent.rollback_cum)
        self.assertEqual(c_exit_module.verdict_line(absent), C_EXIT_VERDICT)
        self.assertEqual(
            [row.tag for row in absent.tags],
            ["c_sl4_h60", "c_sl5_h60", "c_sl4_h90", "c_sl5_h90"],
        )
        self.assertEqual(c_exit_tag_line(absent.tags[0]), line_60)
        self.assertEqual(c_exit_tag_line(absent.tags[3]), line_90)

        fixture = Path(__file__).resolve().parent / "fixtures" / "c_exit_grid_offline_2026-10-20.csv"
        with fixture.open(newline="", encoding="utf-8") as handle:
            sheet = list(csv.DictReader(handle))
        self.assertEqual(len(sheet), 4)
        for row, item in zip(absent.tags, sheet, strict=True):
            self.assertEqual(item["tag"], row.tag)
            self.assertEqual(int(item["n"]), row.n)
            self.assertEqual(Decimal(item["sum_pnl"]), row.sum_pnl)
            self.assertEqual(Decimal(item["sl_share_pct"]), row.sl_share)
            self.assertEqual(Decimal(item["sl_peak"]), row.sl_peak)

        os.environ["C_EXIT_SHADOW_DB"] = str(missing)
        gap_texts = _rendered_card_texts(lambda: app_module._mount_c_exit_shadow())
        gap_joined = "\n".join(gap_texts)
        self.assertIn("C 出口", gap_texts)
        self.assertIn("旁簿", gap_texts)
        self.assertIn(C_EXIT_NAIL, gap_texts)
        self.assertIn(C_EXIT_DISCIPLINE, gap_texts)
        self.assertIn(app_module._ENTRY_JEV_BANNER, gap_texts)
        self.assertIn(app_module.bonferroni_grid_label(), gap_texts)
        self.assertIn(app_module.RETIRED_GRID_LINE, gap_texts)
        self.assertIn(app_module.REMAINING_ENTRY_LINE, gap_texts)
        self.assertIn(app_module.C4_FLOW_BLIND_LINE, gap_texts)
        self.assertNotIn("样本不足", gap_joined)
        self.assertIn(C_EXIT_THRESHOLD, gap_texts)
        self.assertIn(C_EXIT_ROLLBACK, gap_texts)
        self.assertIn(line_60, gap_texts)
        self.assertIn(line_90, gap_texts)
        self.assertIn("≈ −$1296", gap_joined)
        self.assertIn("−$1295.71", gap_joined)
        self.assertIn(C_EXIT_VERDICT, gap_texts)
        self.assertIn("不升主", gap_joined)
        self.assertIn("−$500", gap_joined)
        self.assertIn("−$400", gap_joined)
        self.assertIn("污染主簿", gap_joined)
        self.assertNotIn("停旁簿", gap_joined)
        self.assertNotIn("place_order", gap_joined)

        corrupt = self.root / "corrupt-c-exit.db"
        corrupt.write_bytes(b"not a database")
        broken = load_c_exit_shadow(corrupt)
        self.assertEqual(broken.source, "packaged")
        self.assertIn("旁簿库不可读", broken.note)
        self.assertIn(line_60, c_exit_tag_line(broken.tags[0]))

        empty = self.root / "empty-c-exit.db"
        sqlite3.connect(empty).close()
        blank = load_c_exit_shadow(empty)
        self.assertIn("旁簿库空", blank.note)
        self.assertEqual(blank.tags[0].n, 141)

        shapeless = self.root / "shapeless-c-exit.db"
        conn = sqlite3.connect(shapeless)
        conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, status TEXT, pnl_usd REAL)")
        conn.execute("INSERT INTO positions (status, pnl_usd) VALUES ('closed', -1)")
        conn.commit()
        conn.close()
        self.assertIn("旁簿库缺列", load_c_exit_shadow(shapeless).note)

        opens = self.root / "open-c-exit.db"
        conn = sqlite3.connect(opens)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        conn.execute(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) "
            "VALUES ('c_sl4_h60', 'open', 'stop_loss', -999)"
        )
        conn.commit()
        conn.close()
        self.assertIn("旁簿无 closed", load_c_exit_shadow(opens).note)

        bad = self.root / "bad-c-exit.db"
        conn = sqlite3.connect(bad)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        conn.execute(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) "
            "VALUES ('c_sl4_h60', 'closed', 'stop_loss', NULL)"
        )
        conn.commit()
        conn.close()
        self.assertIn("旁簿坏数据", load_c_exit_shadow(bad).note)

        joined = self.root / "account-c-exit.db"
        conn = sqlite3.connect(joined)
        conn.execute(
            "CREATE TABLE account (id INTEGER PRIMARY KEY, grid_tag TEXT NOT NULL, cash_usd REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, status TEXT NOT NULL, "
            "exit_reason TEXT, pnl_usd REAL)"
        )
        conn.executemany(
            "INSERT INTO account (id, grid_tag, cash_usd) VALUES (?, ?, 10000)",
            [(1, "c_sl4_h60"), (2, "c_sl5_h60"), (3, "c_sl4_h90"), (4, "c_sl5_h90")],
        )
        conn.executemany(
            "INSERT INTO positions (account_id, status, exit_reason, pnl_usd) VALUES (?, ?, ?, ?)",
            [
                (1, "Closed", "stop_loss", -1),
                (2, "closed", "max_hold", -2),
                (3, "closed", "take_profit", 3),
                (4, "open", "stop_loss", -800),
            ],
        )
        conn.commit()
        conn.close()
        thin = load_c_exit_shadow(joined)
        self.assertEqual(thin.source, "packaged")
        self.assertEqual(thin.tags[0].n, 141)
        self.assertEqual(thin.tags[0].live_n, 1)
        self.assertEqual(thin.tags[3].live_n, 0)
        self.assertIn("c_sl4_h60=1", thin.note)
        self.assertIn("未满 30", thin.note)
        self.assertFalse(thin.rollback_single)
        self.assertIn(line_60, c_exit_tag_line(thin.tags[0]))

        meta = self.root / "meta-c-exit.db"
        conn = sqlite3.connect(meta)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, status TEXT, exit_reason TEXT, pnl_usd REAL, meta_json TEXT)"
        )
        conn.execute(
            "INSERT INTO positions (status, exit_reason, pnl_usd, meta_json) "
            "VALUES ('closed', 'stop_loss', -3, ?)",
            ('{"grid_tag":"c_sl5_h90"}',),
        )
        conn.commit()
        conn.close()
        meta_report = load_c_exit_shadow(meta)
        self.assertEqual(meta_report.tags[3].live_n, 1)
        self.assertEqual(meta_report.tags[0].live_n, 0)
        self.assertEqual(meta_report.tags[3].n, 142)

        mixed = self.root / "mixed-c-exit.db"
        conn = sqlite3.connect(mixed)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        mixed_rows: list[tuple[str, str, str, float]] = []
        mixed_rows.append(("c_sl4_h60", "closed", "stop_loss", -40))
        mixed_rows.extend(("c_sl4_h60", "closed", "max_hold", 0) for _ in range(29))
        mixed_rows.append(("c_sl5_h60", "closed", "stop_loss", -1))
        mixed_rows.append(("c_sl4_h90", "closed", "max_hold", -1))
        mixed_rows.append(("c_sl5_h90", "closed", "max_hold", -1))
        conn.executemany(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) VALUES (?, ?, ?, ?)",
            mixed_rows,
        )
        conn.commit()
        conn.close()
        mixed_report = load_c_exit_shadow(mixed)
        self.assertEqual(mixed_report.source, "mixed")
        self.assertEqual(mixed_report.tags[0].origin, "live")
        self.assertEqual(mixed_report.tags[0].n, 30)
        self.assertEqual(mixed_report.tags[0].sum_pnl, Decimal("-40.00"))
        self.assertEqual(mixed_report.tags[1].origin, "packaged")
        self.assertEqual(mixed_report.tags[1].n, 141)
        self.assertIn("未满 30", mixed_report.note)

        full = self.root / "full-c-exit.db"
        conn = sqlite3.connect(full)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        full_rows: list[tuple[str, str, str, float]] = []
        for tag, sl in (
            ("c_sl4_h60", -40),
            ("c_sl5_h60", -50),
            ("c_sl4_h90", -60),
            ("c_sl5_h90", -70),
        ):
            full_rows.append((tag, "closed", "stop_loss", sl))
            full_rows.extend((tag, "closed", "max_hold", 0) for _ in range(29))
            full_rows.append((tag, "open", "stop_loss", -999))
        conn.executemany(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) VALUES (?, ?, ?, ?)",
            full_rows,
        )
        conn.commit()
        conn.close()
        before = full.stat().st_mtime_ns
        os.environ["C_EXIT_SHADOW_DB"] = str(full)
        live = load_c_exit_shadow()
        self.assertEqual(full.stat().st_mtime_ns, before)
        self.assertEqual(live.source, "live")
        self.assertIn("旁簿实盘", live.note)
        self.assertNotIn("回退离线", live.note)
        self.assertFalse(live.rollback_single)
        self.assertFalse(live.rollback_cum)
        self.assertEqual(
            c_exit_tag_line(live.tags[0]),
            "c_sl4_h60 · n=30 · Σpnl −$40.00 · 止损占负 100.0% · 单笔SL峰 −$40.00",
        )
        self.assertEqual(live.tags[3].sum_pnl, Decimal("-70.00"))
        self.assertNotEqual(c_exit_module.verdict_line(live), C_EXIT_VERDICT)
        self.assertIn("不升主", c_exit_module.verdict_line(live))
        live_texts = _rendered_card_texts(lambda: app_module._v2_c_exit_shadow())
        live_joined = "\n".join(live_texts)
        self.assertIn(C_EXIT_NAIL, live_texts)
        self.assertIn("n=30", live_joined)
        self.assertNotIn("−$1144.01", live_joined)
        self.assertNotIn("路径重放", live_joined)

        breach = self.root / "breach-c-exit.db"
        conn = sqlite3.connect(breach)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        conn.execute(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) "
            "VALUES ('c_sl4_h60', 'closed', 'stop_loss', -501)"
        )
        conn.commit()
        conn.close()
        os.environ["C_EXIT_SHADOW_DB"] = str(breach)
        tripped = load_c_exit_shadow()
        self.assertTrue(tripped.rollback_single)
        self.assertTrue(tripped.rollback_cum)
        self.assertEqual(tripped.tags[0].n, 141)
        self.assertEqual(tripped.tags[0].sl_peak, Decimal("-491.67"))
        breach_texts = _rendered_card_texts(lambda: app_module._mount_c_exit_shadow())
        breach_joined = "\n".join(breach_texts)
        self.assertIn("实盘单笔≤−$500", breach_joined)
        self.assertIn("实盘累计≤−$400", breach_joined)
        self.assertIn("停旁簿", breach_joined)
        self.assertIn(line_60, breach_texts)
        self.assertIn(C_EXIT_NAIL, breach_texts)
        self.assertIn("c_sl4_h60 · 样本不足", breach_joined)

        dupes = self.root / "dup-token-c-exit.db"
        conn = sqlite3.connect(dupes)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, "
            "pnl_usd REAL, token_address TEXT)"
        )
        dup_rows: list[tuple[str, str, str, float, str]] = []
        for index in range(40):
            dup_rows.append(("c_sl4_h60", "closed", "max_hold", -1, f"tok-{index % 10}"))
        for index in range(35):
            dup_rows.append(("c_sl5_h60", "closed", "max_hold", -1, f"wide-{index}"))
        dup_rows.append(("c_sl5_h60", "closed", "max_hold", -1, "wide-0"))
        conn.executemany(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd, token_address) "
            "VALUES (?, ?, ?, ?, ?)",
            dup_rows,
        )
        conn.commit()
        conn.close()
        distinct = load_c_exit_shadow(dupes)
        self.assertEqual(distinct.tags[0].live_n, 10)
        self.assertEqual(distinct.tags[0].origin, "packaged")
        self.assertEqual(distinct.tags[0].n, 141)
        self.assertEqual(distinct.tags[1].live_n, 35)
        self.assertEqual(distinct.tags[1].origin, "live")
        self.assertEqual(distinct.tags[1].n, 35)
        os.environ["C_EXIT_SHADOW_DB"] = str(dupes)
        dup_joined = "\n".join(_rendered_card_texts(lambda: app_module._v2_c_exit_shadow()))
        self.assertIn("c_sl4_h60 · 样本不足", dup_joined)
        self.assertNotIn("c_sl5_h60 · 样本不足", dup_joined)
        self.assertIn("n=35", dup_joined)

        overview = inspect.getsource(app_module._render_dashboard)
        phase1_at = overview.index("_mount_phase1_counterfactual")
        card_at = overview.index("_mount_c_exit_shadow")
        m7_at = overview.index("_mount_m7_shadow")
        self.assertLess(phase1_at, card_at)
        self.assertLess(card_at, m7_at)
        mount_src = inspect.getsource(app_module._mount_c_exit_shadow)
        card_src = inspect.getsource(app_module._v2_c_exit_shadow)
        self.assertIn("C_EXIT_NAIL", mount_src)
        self.assertIn("C_EXIT_NAIL", card_src)
        self.assertNotIn("place_order", mount_src)
        self.assertNotIn("place_order", card_src)

    def test_entry_c_exit_joint_card_sample_in_and_liq_gap(self) -> None:
        names = [name for name in dir(DashboardSmokeTest) if name.startswith("test_")]
        self.assertGreaterEqual(len(names), 27)
        self.assertEqual(JOINT_NAIL, "样本内假设 · 旁簿成交=0 · ≠升主/≠抬权/≠Phase1")
        self.assertEqual(JOINT_LIQ_NAIL, "liq：NULL/missing ≠ 真<150k · R3 优先")
        self.assertIn("38.9%", JOINT_LIQ_LIVE)
        self.assertIn("采集缺口", JOINT_LIQ_LIVE)
        self.assertIn("不改 .env", JOINT_DISCIPLINE)
        self.assertIn("不升主", JOINT_DISCIPLINE)
        self.assertIn("不 Phase1", JOINT_DISCIPLINE)
        module_src = inspect.getsource(joint_module)
        self.assertIn(JOINT_NAIL, module_src)
        self.assertIn(JOINT_LIQ_NAIL, module_src)
        self.assertNotIn("sqlite3.connect", module_src)
        self.assertNotIn("place_order", module_src)
        self.assertNotIn("LIVE_TRADING_ENABLED", module_src)
        self.assertNotIn("sum_pnl", joint_module.JointReport.__dataclass_fields__)

        here = Path(__file__).resolve()
        repo = here.parents[2] if here.parent.parent.name == "crawler" else here.parents[1]
        for name in (
            "entry_c_exit_joint.py",
            "app.py",
            "smoke_test.py",
            "fixtures/shadow_entry_x_c_exit_joint_2026-10-20.csv",
        ):
            left = repo / "dashboard" / name
            right = repo / "crawler" / "dashboard" / name
            self.assertEqual(left.read_bytes(), right.read_bytes(), name)

        missing = self.root / "no-such-joint.csv"
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(missing)
        empty_side = self.root / "empty-side-joint.db"
        side_conn = sqlite3.connect(empty_side)
        side_conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, status TEXT)")
        side_conn.commit()
        side_conn.close()
        os.environ["C_EXIT_SHADOW_DB"] = str(empty_side)
        report = load_entry_c_exit_joint()
        self.assertEqual(report.source, "packaged")
        self.assertTrue(report.sample_in)
        self.assertEqual(report.side_fills, 0)
        self.assertEqual(report.closed, 142)
        self.assertIn("旁簿/live joint 空", report.note)
        self.assertEqual(joint_nail_line(report), JOINT_NAIL)
        self.assertEqual(joint_verdict_line(report), JOINT_VERDICT)
        self.assertEqual(report.ge_count, 9)
        self.assertEqual(report.key_ge_count, 4)
        self.assertEqual(report.key_n, 4)
        self.assertEqual(report.r3_n, 30)
        self.assertEqual(report.liq_true_n, 27)
        self.assertEqual(report.liq_missing_n, 0)
        self.assertEqual(report.liq_excluded_n, 0)
        self.assertTrue(report.liq_column)
        self.assertEqual(report.main_sl_r3, 5)
        self.assertEqual(report.main_sl_liq, 5)
        self.assertEqual(report.main_sl_total, 10)
        self.assertEqual(report.main_peak1, Decimal("-491.72"))
        self.assertEqual(report.main_peak0, Decimal("-313.91"))
        by_key = {(row.flag, row.tag): row for row in report.contrasts}
        r3_h90 = by_key[("R3", "c_sl5_h90")]
        r3_h60 = by_key[("R3", "c_sl5_h60")]
        liq_h90 = by_key[("liq", "c_sl5_h90")]
        liq_h60 = by_key[("liq", "c_sl5_h60")]
        self.assertEqual(r3_h90.n1, 30)
        self.assertEqual(r3_h90.n0, 112)
        self.assertEqual(r3_h90.share1, Decimal("97.3"))
        self.assertEqual(r3_h90.share0, Decimal("79.2"))
        self.assertEqual(r3_h90.delta_pp, Decimal("18.1"))
        self.assertEqual(r3_h90.sl_n1, 5)
        self.assertEqual(r3_h90.sl_n0, 10)
        self.assertEqual(r3_h60.share1, Decimal("93.6"))
        self.assertEqual(r3_h60.share0, Decimal("69.2"))
        self.assertEqual(r3_h60.delta_pp, Decimal("24.5"))
        self.assertEqual(liq_h90.share1, Decimal("93.4"))
        self.assertEqual(liq_h90.share0, Decimal("81.3"))
        self.assertEqual(liq_h60.share1, Decimal("96.8"))
        self.assertEqual(liq_h60.share0, Decimal("67.1"))
        self.assertEqual(joint_contrast_cells(r3_h60)[0], "★ R3")
        self.assertEqual(joint_contrast_cells(liq_h60)[0], "liq†")
        self.assertEqual(joint_contrast_cells(by_key[("R3", "c_sl4_h90")])[0], "R3")
        self.assertIn("真<150k 27", joint_liq_risk_line(report))
        self.assertIn("NULL/missing 0", joint_liq_risk_line(report))
        self.assertEqual(
            joint_nine_line(report),
            "止损占负 flag=1 ≥ flag=0：9/9 · R3/liq × c_sl5_h60、c_sl5_h90 为 4/4",
        )
        self.assertEqual(
            joint_peak_line(report),
            "峰 −491 vs −314 · 重放 −$491.67 vs −$313.95 · "
            "主簿 −$491.72 vs −$313.91 · n_SL 薄（主簿 R3 5/10 · liq 5/10）",
        )
        self.assertNotIn("515.61", joint_peak_line(report))
        self.assertNotIn("sum_pnl", module_src)

        gap_texts = _rendered_card_texts(lambda: app_module._mount_entry_c_exit_joint())
        gap_joined = "\n".join(gap_texts)
        self.assertIn("实验A", gap_texts)
        self.assertIn("入口×C出口", gap_texts)
        self.assertIn(JOINT_NAIL, gap_texts)
        self.assertIn(JOINT_LIQ_NAIL, gap_texts)
        self.assertIn(JOINT_LIQ_LIVE, gap_texts)
        self.assertIn(JOINT_DISCIPLINE, gap_texts)
        self.assertIn("9/9", gap_joined)
        self.assertIn("4/4", gap_joined)
        self.assertIn("−491 vs −314", gap_joined)
        self.assertIn("n_SL 薄", gap_joined)
        self.assertIn(JOINT_VERDICT, gap_texts)
        self.assertIn("不升主、不抬权、不 Phase1", gap_joined)
        self.assertIn("旁簿攒成交后再重跑", gap_joined)
        self.assertIn("97.3%", gap_joined)
        self.assertIn("93.6%", gap_joined)
        self.assertIn("96.8%", gap_joined)
        self.assertIn("★ R3", gap_joined)
        self.assertIn("liq†", gap_joined)
        self.assertIn("真<150k", gap_joined)
        self.assertIn("NULL/missing", gap_joined)
        self.assertIn("liq flag=1 旁注", gap_joined)
        self.assertIn(JOINT_PNL_NOTE, gap_texts)
        self.assertIn("旁簿/live joint 空", gap_joined)
        self.assertNotIn("只读对照", gap_joined)
        self.assertIn(app_module._ENTRY_JEV_BANNER, gap_texts)
        self.assertNotIn("515.61", gap_joined)
        self.assertNotIn("761.71", gap_joined)
        self.assertNotIn("place_order", gap_joined)

        def write_joint(path: Path, records: list[dict[str, object]], *, liq_column: bool) -> None:
            tags = ("c_sl5_h60", "c_sl5_h90", "c_sl4_h90")
            fields = [
                "opened_at",
                "tagged_at",
                "shadow_R3_vol_chg_rsi",
                "shadow_liq_lt_150k",
                "main_exit_reason",
                "main_pnl_usd",
            ]
            if liq_column:
                fields.append("liq_usd")
            for tag in tags:
                fields.extend((f"{tag}_hit", f"{tag}_exit_reason", f"{tag}_pnl_usd"))
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for record in records:
                    row = {
                        "opened_at": record.get("opened_at", "2026-10-14T00:00:00Z"),
                        "tagged_at": record.get("tagged_at", "2026-10-20T05:09:46Z"),
                        "shadow_R3_vol_chg_rsi": record["r3"],
                        "shadow_liq_lt_150k": record["liq"],
                        "main_exit_reason": record.get("main_reason", "max_hold"),
                        "main_pnl_usd": record.get("main_pnl", "-1"),
                    }
                    if liq_column:
                        row["liq_usd"] = record.get("liq_usd", "")
                    reason = str(record.get("reason", "max_hold"))
                    pnl = record.get("pnl", "-1")
                    for tag in tags:
                        row[f"{tag}_hit"] = record.get("hit", "1")
                        row[f"{tag}_exit_reason"] = reason
                        row[f"{tag}_pnl_usd"] = pnl
                    row["opened_at"] = record.get("opened_at", row["opened_at"])
                    writer.writerow(row)

        blank = self.root / "blank-joint.csv"
        blank.write_text("", encoding="utf-8")
        empty = load_entry_c_exit_joint(blank)
        self.assertEqual(empty.source, "packaged")
        self.assertIn("联合表空", empty.note)
        self.assertEqual(empty.closed, 142)
        self.assertEqual(joint_nail_line(empty), JOINT_NAIL)

        bad = self.root / "bad-joint.csv"
        write_joint(
            bad,
            [{"r3": "1", "liq": "1", "liq_usd": "1000", "hit": "1", "pnl": "", "reason": "stop_loss"}],
            liq_column=True,
        )
        broken = load_entry_c_exit_joint(bad)
        self.assertIn("联合表坏数据", broken.note)
        self.assertEqual(broken.liq_true_n, 27)

        shaped = self.root / "columns-joint.csv"
        shaped.write_text("position_id\n1\n", encoding="utf-8")
        self.assertIn("联合表缺列", load_entry_c_exit_joint(shaped).note)

        corrupt = self.root / "corrupt-joint.csv"
        corrupt.write_bytes(b"\xff\xfe not csv")
        self.assertIn("联合表不可读", load_entry_c_exit_joint(corrupt).note)

        gap_only = self.root / "null-liq-joint.csv"
        write_joint(
            gap_only,
            [
                {
                    "r3": "1",
                    "liq": "1",
                    "liq_usd": "",
                    "reason": "stop_loss",
                    "pnl": "-20",
                    "main_reason": "stop_loss",
                    "main_pnl": "-20",
                },
                {
                    "r3": "0",
                    "liq": "1",
                    "liq_usd": "10000",
                    "reason": "max_hold",
                    "pnl": "-4",
                    "main_reason": "max_hold",
                    "main_pnl": "-4",
                },
                {
                    "r3": "0",
                    "liq": "0",
                    "liq_usd": "400000",
                    "reason": "stop_loss",
                    "pnl": "-8",
                    "main_reason": "stop_loss",
                    "main_pnl": "-8",
                },
            ],
            liq_column=True,
        )
        guarded = load_entry_c_exit_joint(gap_only)
        self.assertEqual(guarded.source, "export")
        self.assertEqual(guarded.r3_n, 1)
        self.assertEqual(guarded.liq_true_n, 1)
        self.assertEqual(guarded.liq_missing_n, 1)
        self.assertEqual(guarded.liq_excluded_n, 1)
        guarded_flags = {(row.flag, row.tag): row for row in guarded.contrasts}
        self.assertEqual(guarded_flags[("R3", "c_sl5_h90")].n1, 1)
        self.assertEqual(guarded_flags[("liq", "c_sl5_h90")].n1, 1)
        self.assertEqual(guarded_flags[("R3∨liq", "c_sl5_h90")].n1, 2)
        self.assertIn("NULL/missing 1 不入列", joint_liq_risk_line(guarded))
        self.assertIn("剔出 1", joint_liq_risk_line(guarded))
        self.assertIn(JOINT_LIQ_NAIL, _rendered_card_texts(lambda: app_module._v2_entry_c_exit_joint()))

        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(gap_only)
        null_texts = _rendered_card_texts(lambda: app_module._v2_entry_c_exit_joint())
        null_joined = "\n".join(null_texts)
        self.assertIn(JOINT_LIQ_NAIL, null_texts)
        self.assertIn(JOINT_LIQ_LIVE, null_texts)
        self.assertIn("liq†", null_joined)
        self.assertIn("NULL/missing", null_joined)
        self.assertNotIn("97.3%", null_joined)

        no_value = self.root / "no-liq-column.csv"
        write_joint(
            no_value,
            [
                {"r3": "1", "liq": "1", "reason": "stop_loss", "pnl": "-5", "main_pnl": "-5"},
                {"r3": "0", "liq": "1", "reason": "max_hold", "pnl": "-2", "main_pnl": "-2"},
            ],
            liq_column=False,
        )
        unverified = load_entry_c_exit_joint(no_value)
        self.assertFalse(unverified.liq_column)
        self.assertEqual(unverified.r3_n, 1)
        self.assertEqual(unverified.liq_true_n, 0)
        self.assertEqual(unverified.liq_missing_n, 2)
        self.assertEqual(unverified.liq_excluded_n, 2)
        self.assertIn("无 liq_usd", joint_liq_risk_line(unverified))
        self.assertIn(JOINT_LIQ_NAIL, joint_liq_risk_line(unverified))

        weak = self.root / "weak-joint.csv"
        write_joint(
            weak,
            [
                {
                    "r3": "1",
                    "liq": "1",
                    "liq_usd": "1000",
                    "reason": "max_hold",
                    "pnl": "-10",
                    "main_pnl": "-10",
                },
                {
                    "r3": "0",
                    "liq": "0",
                    "liq_usd": "800000",
                    "reason": "stop_loss",
                    "pnl": "-10",
                    "main_reason": "stop_loss",
                    "main_pnl": "-10",
                },
            ],
            liq_column=True,
        )
        opposed = load_entry_c_exit_joint(weak)
        self.assertEqual(opposed.ge_count, 0)
        self.assertNotIn("部分支持", joint_verdict_line(opposed))
        self.assertIn("不升主、不抬权、不 Phase1", joint_verdict_line(opposed))
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(weak)
        weak_joined = "\n".join(_rendered_card_texts(lambda: app_module._v2_entry_c_exit_joint()))
        self.assertIn("0/9", weak_joined)
        self.assertIn(JOINT_LIQ_NAIL, weak_joined)
        self.assertNotIn("部分支持", weak_joined)

        forward = self.root / "forward-joint.csv"
        write_joint(
            forward,
            [
                {
                    "r3": "1",
                    "liq": "0",
                    "liq_usd": "900000",
                    "opened_at": "2026-10-21T00:00:00Z",
                    "tagged_at": "2026-10-20T00:00:00Z",
                    "reason": "stop_loss",
                    "pnl": "-3",
                    "main_reason": "stop_loss",
                    "main_pnl": "-3",
                },
                {
                    "r3": "0",
                    "liq": "0",
                    "liq_usd": "900000",
                    "opened_at": "2026-10-21T00:00:00Z",
                    "tagged_at": "2026-10-20T00:00:00Z",
                    "reason": "max_hold",
                    "pnl": "-1",
                    "main_pnl": "-1",
                },
            ],
            liq_column=True,
        )
        later = load_entry_c_exit_joint(forward)
        self.assertFalse(later.sample_in)
        self.assertNotEqual(joint_nail_line(later), JOINT_NAIL)
        self.assertIn("含打标后新开", joint_nail_line(later))
        self.assertIn("不升主", joint_verdict_line(later))

        side = self.root / "side-joint.db"
        conn = sqlite3.connect(side)
        conn.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("INSERT INTO positions (status) VALUES ('closed')")
        conn.commit()
        conn.close()
        before = side.stat().st_mtime_ns
        os.environ["C_EXIT_SHADOW_DB"] = str(side)
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(missing)
        held = load_entry_c_exit_joint()
        self.assertEqual(side.stat().st_mtime_ns, before)
        self.assertEqual(held.side_fills, 1)
        self.assertEqual(held.closed, 142)
        self.assertEqual(held.liq_true_n, 27)
        self.assertIn("旁簿/live joint 空", held.note)
        self.assertIn("旁簿库 1 笔", held.note)
        self.assertNotEqual(joint_nail_line(held), JOINT_NAIL)
        self.assertIn("旁簿成交=1", joint_nail_line(held))
        self.assertIn("样本内假设", joint_nail_line(held))
        self.assertEqual(joint_verdict_line(held), JOINT_VERDICT)
        compare_texts = _rendered_card_texts(lambda: app_module._v2_entry_c_exit_joint())
        compare_joined = "\n".join(compare_texts)
        self.assertIn("只读对照 · 旁簿成交=1", compare_texts)
        self.assertIn("旁簿成交=1", compare_joined)
        self.assertNotIn(JOINT_NAIL, compare_texts)
        self.assertIn(JOINT_DISCIPLINE, compare_texts)
        self.assertIn(app_module._ENTRY_JEV_BANNER, compare_texts)

        export_root = self.root / "export-root"
        older = export_root / "exports" / "shadow_entry_x_c_exit_joint_2026-10-01.csv"
        newer = export_root / "exports" / "shadow_entry_x_c_exit_joint_2026-11-01.csv"
        older.parent.mkdir(parents=True)
        write_joint(
            older,
            [{"r3": "0", "liq": "0", "liq_usd": "900000", "reason": "max_hold", "pnl": "-1", "main_pnl": "-1"}],
            liq_column=True,
        )
        write_joint(
            newer,
            [
                {"r3": "1", "liq": "0", "liq_usd": "900000", "reason": "stop_loss", "pnl": "-2", "main_pnl": "-2"},
                {"r3": "1", "liq": "0", "liq_usd": "900000", "reason": "stop_loss", "pnl": "-2", "main_pnl": "-2"},
            ],
            liq_column=True,
        )
        os.utime(older, (1_700_000_000, 1_700_000_000))
        os.utime(newer, (1_800_000_000, 1_800_000_000))
        os.environ.pop("ENTRY_C_EXIT_JOINT_CSV", None)
        os.environ["DASHBOARD_DB_ROOT"] = str(export_root)
        picked = load_entry_c_exit_joint()
        self.assertEqual(picked.source, "export")
        self.assertEqual(picked.closed, 2)
        self.assertEqual(picked.r3_n, 2)
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(missing)
        forced = load_entry_c_exit_joint()
        self.assertEqual(forced.source, "packaged")
        self.assertEqual(forced.closed, 142)
        self.assertIn("旁簿/live joint 空", forced.note)

        overview = inspect.getsource(app_module._render_dashboard)
        c_exit_at = overview.index("_mount_c_exit_shadow")
        joint_at = overview.index("_mount_entry_c_exit_joint")
        m7_at = overview.index("_mount_m7_shadow")
        self.assertLess(c_exit_at, joint_at)
        self.assertLess(joint_at, m7_at)
        card_src = inspect.getsource(app_module._v2_entry_c_exit_joint)
        mount_src = inspect.getsource(app_module._mount_entry_c_exit_joint)
        self.assertIn("JOINT_NAIL", card_src)
        self.assertIn("JOINT_LIQ_NAIL", card_src)
        self.assertIn("JOINT_LIQ_LIVE", card_src)
        self.assertNotIn("place_order", card_src)
        self.assertNotIn("place_order", mount_src)

    def test_sell_slip_note_shows_db_book_and_pr137_annotation(self) -> None:
        """#137 snapshot is a display note. The book line is the unread DB sum."""
        c_exit_note = (
            "#137 c6016d7 卖出滑点补扣旁注 · 只读重算 c_exit 4 笔已平 "
            "账面 +$22.66 → 补扣后 −$17.79（Δ −$40.45）· 历史库未回写 · 旁注常量"
        )
        shadow_note = (
            "#137 c6016d7 卖出滑点补扣旁注 · 只读重算 shadow010 20 笔已平 "
            "账面 −$201.30 → 补扣后 −$274.47（Δ −$73.17）· 历史库未回写 · 旁注常量"
        )
        os.environ["C_EXIT_SELL_SLIP_CSV"] = str(self.root / "no-such-sell-slip.csv")
        os.environ["C_EXIT_SHADOW_DB"] = str(self.root / "no-such-c-exit.db")
        os.environ["C_EXIT_SHADOW010_DB"] = str(self.root / "no-such-shadow010.db")
        lines = c_exit_module.sell_slip_card_lines()
        joined = "\n".join(lines)
        self.assertIn(c_exit_note, lines)
        self.assertIn(shadow_note, lines)
        self.assertIn("c_exit 旁簿账面 Σpnl 不可读（库不存在）", lines)
        self.assertIn("shadow010 旁簿账面 Σpnl 不可读（库不存在）", lines)
        self.assertIn("历史库未回写", joined)
        self.assertNotIn("22.67", joined)
        self.assertNotIn("73.16", joined)

        c_exit_db = self.root / "c-exit-book.db"
        conn = sqlite3.connect(c_exit_db)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, exit_reason TEXT, pnl_usd REAL)"
        )
        conn.executemany(
            "INSERT INTO positions (grid_tag, status, exit_reason, pnl_usd) VALUES (?, ?, ?, ?)",
            [
                ("c_sl5_h60", "closed", "take_profit", 30),
                ("c_sl5_h60", "closed", "stop_loss", -7.34),
                ("c_sl4_h90", "open", "stop_loss", -999),
            ],
        )
        conn.commit()
        conn.close()
        shadow_db = self.root / "shadow010-book.db"
        conn = sqlite3.connect(shadow_db)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, pnl_usd REAL, closed_at TEXT)"
        )
        conn.execute(
            "INSERT INTO positions (grid_tag, status, pnl_usd, closed_at) "
            "VALUES ('c_sl5_h60', 'closed', -201.3, '2026-10-21T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        before = (c_exit_db.stat().st_mtime_ns, shadow_db.stat().st_mtime_ns)
        os.environ["C_EXIT_SHADOW_DB"] = str(c_exit_db)
        os.environ["C_EXIT_SHADOW010_DB"] = str(shadow_db)
        live = c_exit_module.sell_slip_card_lines()
        self.assertEqual((c_exit_db.stat().st_mtime_ns, shadow_db.stat().st_mtime_ns), before)
        self.assertIn("c_exit 旁簿账面 Σpnl +$22.66 · 已平 2", live)
        self.assertIn("shadow010 旁簿账面 Σpnl −$201.30 · 已平 1", live)
        self.assertIn(c_exit_note, live)
        self.assertIn(shadow_note, live)

        export = self.root / "c_exit_sell_slip_recalc.csv"
        export.write_text(
            "book,closed_n,book_pnl_usd,adjusted_pnl_usd,delta_usd\n"
            "shadow010,20,-201.30,-274.47,-73.17\n"
            "c_exit,4,22.66,-17.79,-40.45\n",
            encoding="utf-8",
        )
        os.environ["C_EXIT_SELL_SLIP_CSV"] = str(export)
        exported = "\n".join(c_exit_module.sell_slip_card_lines())
        self.assertIn("补扣后 −$17.79（Δ −$40.45）", exported)
        self.assertIn("补扣后 −$274.47（Δ −$73.17）", exported)
        self.assertIn("历史库未回写", exported)
        self.assertIn("导出 c_exit_sell_slip_recalc.csv", exported)
        self.assertNotIn("旁注常量", exported)
        self.assertIn("c_exit 旁簿账面 Σpnl +$22.66 · 已平 2", exported)

        bad = self.root / "bad-sell-slip.csv"
        bad.write_text("book,closed_n\nc_exit,4\n", encoding="utf-8")
        os.environ["C_EXIT_SELL_SLIP_CSV"] = str(bad)
        fallback = "\n".join(c_exit_module.sell_slip_card_lines())
        self.assertIn("旁注常量（导出不可读）", fallback)
        self.assertIn("补扣后 −$17.79", fallback)
        self.assertIn("历史库未回写", fallback)

        null_pnl = self.root / "null-pnl-c-exit.db"
        conn = sqlite3.connect(null_pnl)
        conn.execute(
            "CREATE TABLE positions ("
            "id INTEGER PRIMARY KEY, grid_tag TEXT, status TEXT, pnl_usd REAL)"
        )
        conn.execute(
            "INSERT INTO positions (grid_tag, status, pnl_usd) VALUES ('c_sl5_h60', 'closed', NULL)"
        )
        conn.commit()
        conn.close()
        os.environ["C_EXIT_SHADOW_DB"] = str(null_pnl)
        os.environ["C_EXIT_SELL_SLIP_CSV"] = str(self.root / "no-such-sell-slip.csv")
        unreadable = "\n".join(c_exit_module.sell_slip_card_lines())
        self.assertIn("c_exit 旁簿账面 Σpnl 不可读（坏数据）", unreadable)
        self.assertIn(c_exit_note, unreadable)

        note_src = inspect.getsource(c_exit_module.sell_slip_card_lines)
        reader_src = inspect.getsource(c_exit_module.load_book_cash)
        for src in (note_src, reader_src, inspect.getsource(c_exit_module)):
            self.assertNotIn("place_order", src)
            self.assertNotIn("PAPER_OPEN_PAUSED", src)
            self.assertNotIn("LIVE_TRADING_ENABLED", src)
            self.assertNotIn("sqlite3.connect", src)

        os.environ["C_EXIT_SHADOW_DB"] = str(self.root / "no-such-c-exit.db")
        os.environ["C_EXIT_SHADOW010_DB"] = str(self.root / "no-such-shadow010.db")
        os.environ["C_EXIT_SELL_SLIP_CSV"] = str(self.root / "no-such-sell-slip.csv")
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(self.root / "no-such-joint.csv")
        for build in (app_module._v2_c_exit_shadow, app_module._v2_entry_c_exit_joint):
            rendered = "\n".join(_rendered_card_texts(build))
            self.assertIn(c_exit_note, rendered)
            self.assertIn(shadow_note, rendered)
            self.assertIn("历史库未回写", rendered)
            self.assertIn("#137", rendered)
            self.assertIn("c6016d7", rendered)
            self.assertIn("c_exit 旁簿账面 Σpnl 不可读（库不存在）", rendered)
            self.assertIn("shadow010 旁簿账面 Σpnl 不可读（库不存在）", rendered)

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
        self.assertEqual(texts[texts.index("S1") + 1], app_module._raw_dedup(538, 1))
        self.assertEqual(texts[texts.index("S0") + 1], app_module._raw_dedup(8, 1))
        self.assertEqual(texts[texts.index("liq_missing") + 1], app_module._raw_dedup(68, 1))
        self.assertEqual(texts[texts.index("would_open_S0") + 1], app_module._raw_dedup(546, 2))
        self.assertEqual(texts[texts.index("would_open_S1") + 1], app_module._raw_dedup(538, 1))
        self.assertEqual(texts[texts.index("leak") + 1], app_module._raw_dedup(3, 1))
        self.assertEqual(texts[texts.index("Phase0") + 1], app_module._raw_dedup(2, 2))
        self.assertIn(app_module._ENTRY_JEV_BANNER, texts)
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

    def test_dedup_window_pause_split_and_closed_banner(self) -> None:
        t0 = datetime(2026, 10, 20, 1, 0, tzinfo=timezone.utc)
        events = [
            (t0, "A"),
            (t0 + timedelta(minutes=10), "A"),
            (t0 + timedelta(minutes=59), "A"),
            (t0 + timedelta(minutes=60), "A"),
            (t0 + timedelta(minutes=10), "B"),
            (t0, ""),
            (t0 + timedelta(minutes=1), ""),
            (None, "C"),
        ]
        self.assertEqual(dedup_token_count(events), 6)
        ledger = self.root / "CELL_LEDGER.csv"
        ledger.write_text(
            "cell_id,cell_id_end,date,layer,hypothesis,status,ref\n"
            "1,267,2026-10-20,all,历史累计、明细待补,历史累计、明细待补,r\n"
            "268,,2026-10-21,entry,a,rejected,x\n"
            "269,,2026-10-21,entry,b,rejected,x\n"
            "270,,2026-10-21,exit,c,rejected,x\n"
            "271,,2026-10-21,entry,jev d,暂定拒绝,y\n",
            encoding="utf-8",
        )
        os.environ["CELL_LEDGER_CSV"] = str(ledger)
        self.assertEqual(
            app_module.bonferroni_grid_label(),
            "累计共测 271 格 · Bonferroni 按 271（CELL_LEDGER 最大 cell_id）",
        )
        self.assertFalse(hasattr(app_module, "BONFERRONI_GRIDS"))
        label_src = inspect.getsource(app_module.bonferroni_grid_label)
        self.assertNotIn("268", label_src)
        self.assertNotIn("271", label_src)
        jev_texts = _rendered_card_texts(app_module._v2_jev_gate)
        self.assertTrue(any("暂定拒绝" in t for t in jev_texts))
        os.environ["CELL_LEDGER_CSV"] = str(self.root / "no-ledger.csv")
        self.assertEqual(app_module.bonferroni_grid_label(), "格数：总账缺失")
        jev_texts = _rendered_card_texts(app_module._v2_jev_gate)
        self.assertIn(app_module.bonferroni_grid_label(), jev_texts)
        self.assertIn(app_module.RETIRED_GRID_LINE, jev_texts)
        self.assertIn(app_module.REMAINING_ENTRY_LINE, jev_texts)
        self.assertIn(app_module.C4_FLOW_BLIND_LINE, jev_texts)

        now = datetime.now(timezone.utc)
        execution = self.root / "dedup-exec.db"
        with sqlite3.connect(execution) as conn:
            conn.execute(
                "CREATE TABLE execution_log ("
                "id INTEGER PRIMARY KEY, ts TEXT, decision_log_id INTEGER, "
                "validation_ok INTEGER, executed INTEGER)"
            )
        main = self.root / "dedup-main.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    token_address TEXT,
                    mode TEXT NOT NULL,
                    raw_json TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO decision_log (ts, strategy_id, action, token_address, mode, raw_json) "
                "VALUES (?, 'alpha', 'buy', ?, 'dry_run', '{}')",
                [
                    (_iso(now - timedelta(minutes=50)), "SAME"),
                    (_iso(now - timedelta(minutes=20)), "SAME"),
                    (_iso(now - timedelta(minutes=5)), "SAME"),
                    (_iso(now - timedelta(minutes=70)), "SAME"),
                    (_iso(now - timedelta(minutes=10)), "OTHER"),
                ],
            )
        store = DataStore(
            DbPaths(
                main=main,
                execution=execution,
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        week = store.week_decision_stats()
        self.assertTrue(week.available, week.reason)
        self.assertEqual(week.data.n_decisions, 5)
        self.assertEqual(week.data.n_decisions_dedup, 3)
        buckets = store.strategy_buckets()
        self.assertEqual(buckets.data.rows[0].n, 5)
        self.assertEqual(buckets.data.rows[0].n_dedup, 3)
        week_texts = _rendered_card_texts(lambda: app_module._v2_week(store))
        self.assertIn(app_module._raw_dedup(5, 3), week_texts)

        paper = self.root / "pause-paper.db"
        before_open = "2026-10-20T08:00:00Z"
        after_open = "2026-10-21T01:00:00Z"
        with sqlite3.connect(paper) as conn:
            conn.execute(
                """
                CREATE TABLE account (
                    id INTEGER PRIMARY KEY,
                    cash_usd REAL NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO account (id, cash_usd, updated_at) VALUES (1, 10000, ?)",
                (before_open,),
            )
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    chain_id TEXT,
                    pair_address TEXT,
                    token_address TEXT,
                    qty REAL,
                    entry_price REAL,
                    size_usd REAL,
                    opened_at TEXT,
                    status TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO positions (id, chain_id, pair_address, token_address, qty, entry_price, "
                "size_usd, opened_at, status) VALUES (?, 'solana', ?, ?, 1, 1, 10, ?, 'open')",
                [
                    ("early", "P1", "T1", before_open),
                    ("late", "P2", "T2", after_open),
                ],
            )
        pause_store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=paper,
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        split = pause_store.paper_pause_split()
        self.assertTrue(split.available, split.reason)
        self.assertEqual(split.data.before, 1)
        self.assertEqual(split.data.after, 1)
        self.assertIn("PAPER_OPEN_PAUSED", split.data.flag_note)
        held = _rendered_card_texts(lambda: app_module._v2_positions(pause_store))
        held_joined = "\n".join(held)
        self.assertIn("暂停前 1 · 暂停后 1", held)
        self.assertIn("暂停后出现新开仓 · 挂卡拦", held)
        self.assertIn("v2-note-bad", inspect.getsource(app_module._v2_pause_line))
        self.assertIn("PAPER_OPEN_PAUSED", held_joined)

        quiet = self.root / "quiet-paper.db"
        with sqlite3.connect(quiet) as conn:
            conn.execute(
                "CREATE TABLE positions (id TEXT PRIMARY KEY, pair_address TEXT, opened_at TEXT, status TEXT)"
            )
            conn.execute(
                "INSERT INTO positions (id, pair_address, opened_at, status) VALUES ('only', 'P', ?, 'open')",
                (before_open,),
            )
        quiet_store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=quiet,
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        calm = quiet_store.paper_pause_split()
        self.assertEqual(calm.data.before, 1)
        self.assertEqual(calm.data.after, 0)
        calm_texts = _rendered_card_texts(lambda: app_module._v2_positions(quiet_store))
        self.assertIn("暂停前 1 · 暂停后 0", calm_texts)
        self.assertNotIn("暂停后出现新开仓 · 挂卡拦", calm_texts)

        closed_paper = self.root / "closed-banner.db"
        with sqlite3.connect(closed_paper) as conn:
            conn.execute(
                """
                CREATE TABLE positions (
                    id TEXT PRIMARY KEY,
                    pair_address TEXT,
                    token_address TEXT,
                    status TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    exit_reason TEXT,
                    pnl_usd REAL,
                    meta_json TEXT
                )
                """
            )
            stamp = _iso(now)
            conn.executemany(
                "INSERT INTO positions (id, pair_address, token_address, status, opened_at, closed_at, "
                "exit_reason, pnl_usd, meta_json) VALUES (?, ?, ?, 'closed', ?, ?, 'max_hold', ?, ?)",
                [
                    ("a", "pairA", "TOK", stamp, stamp, 1, json.dumps({"decision_log_id": 1})),
                    (
                        "b",
                        "pairB",
                        "TOK",
                        _iso(now - timedelta(minutes=10)),
                        _iso(now - timedelta(minutes=10)),
                        -1,
                        json.dumps({"decision_log_id": 2}),
                    ),
                ],
            )
        with sqlite3.connect(main) as conn:
            conn.execute("ALTER TABLE decision_log ADD COLUMN purpose TEXT")
            conn.execute("UPDATE decision_log SET purpose = 'live_candidate'")
        closed_store = DataStore(
            DbPaths(
                main=main,
                execution=self.root / "missing-exec.db",
                paper=closed_paper,
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        closed = closed_store.paper_closed_buckets(days=14)
        self.assertEqual(closed.data.first_hit_n, 2)
        self.assertEqual(closed.data.first_hit_dedup, 1)
        self.assertEqual(closed.data.first_hit_live_n, 2)
        self.assertEqual(closed.data.first_hit_live_dedup, 1)
        closed_texts = _rendered_card_texts(lambda: app_module._v2_closed(closed_store))
        self.assertIn(app_module._ENTRY_JEV_BANNER, closed_texts)
        self.assertIn(app_module._raw_dedup(2, 1), "\n".join(closed_texts))

    def test_malformed_holder_and_bot_tables_show_gap_not_zero(self) -> None:
        banner = SOURCE_TABLE_DAMAGE_BANNER
        slip_banner = source_table_banner(SLIP_SUSPICIOUS_TABLES)
        self.assertIn("索引可疑", banner)
        self.assertIn("token_transfer_edges", banner)
        self.assertIn("address_labels", banner)
        self.assertIn("token_holder_snapshots", banner)
        self.assertIn("bot_fill_observations", slip_banner)
        self.assertNotIn("token_transfer_edges", slip_banner)
        holder_src = inspect.getsource(DataStore._intel_holder_observe)
        slip_src = inspect.getsource(DataStore._intel_slippage_observe)
        self.assertIn("except sqlite3.DatabaseError", holder_src)
        self.assertIn("except sqlite3.DatabaseError", slip_src)
        self.assertIn("token_holder_snapshots", holder_src)
        self.assertIn("bot_fill_observations", slip_src)

        class _RaisingConnect:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                return None

            def __enter__(self) -> object:
                raise sqlite3.DatabaseError("database disk image is malformed")

            def __exit__(self, *_args: object) -> bool:
                return False

        live = self.root / "mocked-dex.db"
        with sqlite3.connect(live) as conn:
            conn.execute(
                "CREATE TABLE token_holder_snapshots (chain_id TEXT, token_address TEXT, recorded_at TEXT)"
            )
            conn.execute(
                "INSERT INTO token_holder_snapshots (chain_id, token_address, recorded_at) "
                "VALUES ('solana', 'mint', '2026-10-21T00:00:00Z')"
            )
            conn.execute(
                "CREATE TABLE bot_fill_observations (chain_id TEXT, token_address TEXT, recorded_at TEXT, bot_reason TEXT)"
            )
            conn.execute(
                "INSERT INTO bot_fill_observations (chain_id, token_address, recorded_at, bot_reason) "
                "VALUES ('solana', 'mint', '2026-10-21T00:00:00Z', 'repeat')"
            )
        mocked = DataStore(
            DbPaths(
                main=live,
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        with patch("dashboard.data.ro_connect", _RaisingConnect):
            held = mocked.intel_holder_observe()
            slipped = mocked.intel_slippage_observe()
            holder_texts = _rendered_card_texts(lambda: app_module._v2_intel_holders(mocked))
            slip_texts = _rendered_card_texts(lambda: app_module._v2_intel_slippage(mocked))
        self.assertFalse(held.available, held.reason)
        self.assertIsNone(held.data)
        self.assertEqual(held.reason, banner)
        self.assertFalse(slipped.available, slipped.reason)
        self.assertIsNone(slipped.data)
        self.assertEqual(slipped.reason, slip_banner)
        self.assertIn(banner, holder_texts)
        self.assertIn("data gap", holder_texts)
        self.assertNotIn("快照样本", holder_texts)
        self.assertIn(slip_banner, slip_texts)
        self.assertIn("data gap", slip_texts)
        self.assertNotIn("1h n", slip_texts)

        corrupt = self.root / "corrupt-dex.db"
        corrupt.write_bytes(b"not a database")
        damaged = DataStore(
            DbPaths(
                main=corrupt,
                execution=self.root / "missing-exec.db",
                paper=self.root / "missing-paper.db",
                backtest=self.root / "missing-bt.db",
                news=self.root / "missing-news.db",
            )
        )
        corrupt_holder = damaged.intel_holder_observe()
        corrupt_slip = damaged.intel_slippage_observe()
        self.assertFalse(corrupt_holder.available, corrupt_holder.reason)
        self.assertIsNone(corrupt_holder.data)
        self.assertEqual(corrupt_holder.reason, banner)
        self.assertFalse(corrupt_slip.available, corrupt_slip.reason)
        self.assertIsNone(corrupt_slip.data)
        self.assertEqual(corrupt_slip.reason, slip_banner)
        corrupt_holder_texts = _rendered_card_texts(lambda: app_module._v2_intel_holders(damaged))
        corrupt_slip_texts = _rendered_card_texts(lambda: app_module._v2_intel_slippage(damaged))
        self.assertIn(banner, corrupt_holder_texts)
        self.assertIn("data gap", corrupt_holder_texts)
        self.assertNotIn("快照样本", corrupt_holder_texts)
        self.assertIn(slip_banner, corrupt_slip_texts)
        self.assertIn("data gap", corrupt_slip_texts)
        self.assertNotIn("1h n", corrupt_slip_texts)

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

    def test_c4_order_flow_stays_blind(self) -> None:
        root = Path(__file__).resolve().parent
        chunks: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "smoke_test.py":
                continue
            chunks.append(path.read_text(encoding="utf-8"))
        prod = "\n".join(chunks)
        for column in (
            "txns_m5_buys",
            "txns_m5_sells",
            "txns_h1_buys",
            "txns_h1_sells",
            "volume_m5",
        ):
            self.assertNotIn(column, prod, column)
        self.assertEqual(prod.count("C4 单流"), 2)
        self.assertEqual(prod.count("C4"), 4)
        self.assertEqual(
            app_module.C4_FLOW_BLIND_LINE,
            "C4 单流：预登记 10-21 12:00 HKT 锁定·窗口 10-21 07:08–10-24 07:00·固定切点 10-23 02:15（前60/后40）·截止前不看数",
        )
        self.assertNotIn("数据自 07:08", prod)
        self.assertNotIn("切 60/40", prod)
        self.assertNotRegex(prod, r"闪烁.{0,24}21")
        self.assertNotRegex(prod, r"21.{0,24}闪烁")
        self.assertNotIn("tp_not_held", prod)
        self.assertNotIn("闪烁率", prod)

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



class BoardTruthSmokeTest(unittest.TestCase):
    """Display-only truth cards: missing never reads as 0."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_supply_gaps_partial_and_missing(self) -> None:
        from dashboard import board_truth as bt
        self.assertIsNone(bt.load_supply_gaps(self.root / "none.csv"))
        self.assertEqual(bt.day_coverage("2026-10-21", None), "断供记录缺失")
        csv_path = self.root / "supply_gaps.csv"
        csv_path.write_text(
            "component,start_at,end_at,start_hkt,end_hkt,reason,source\n"
            "execution_main_loop,2026-10-20T18:03:17Z,2026-10-21T00:05:00Z,,,stop,execution\n"
            "shadow010_book,2026-10-20T17:50:11Z,,,,halt,execution\n",
            encoding="utf-8",
        )
        gaps = bt.load_supply_gaps(csv_path)
        now = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)
        cov = bt.day_coverage("2026-10-21", gaps, now=now)
        self.assertIn("execution_main_loop 部分 02:03–08:05", cov)
        self.assertIn("shadow010_book 部分 01:50–进行中", cov)
        self.assertEqual(bt.day_coverage("2026-10-19", gaps, now=now), "")

    def test_side_book_missing_is_not_zero(self) -> None:
        from dashboard import entry_c_exit_joint as ej
        os.environ["C_EXIT_SHADOW_DB"] = str(self.root / "absent.db")
        os.environ["ENTRY_C_EXIT_JOINT_CSV"] = str(self.root / "absent.csv")
        count, reason = ej.side_book_count_or_missing()
        self.assertIsNone(count)
        self.assertTrue(reason)
        report = ej.load_entry_c_exit_joint()
        self.assertIn("缺失", ej.nail_line(report))

    def test_watchdog_heartbeat(self) -> None:
        from dashboard import board_truth as bt
        self.assertEqual(bt.WATCHDOG_HEARTBEAT_REL, Path("logs") / "heartbeat_gap_watch.alive")
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        os.environ.pop("WATCHDOG_HEARTBEAT_FILE", None)
        self.assertEqual(bt.watchdog_status()[1], "看门未上线/缺失")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "heartbeat_gap_watch.alive").write_text("x")
        self.assertEqual(bt.watchdog_status()[0], "ok")
        hb = self.root / "hb"
        os.environ["WATCHDOG_HEARTBEAT_FILE"] = str(hb)
        self.assertEqual(bt.watchdog_status()[1], "看门未上线/缺失")
        self.assertEqual(bt.watchdog_status(hb)[1], "看门未上线/缺失")
        hb.write_text("x")
        self.assertEqual(bt.watchdog_status(hb)[0], "ok")
        self.assertEqual(bt.watchdog_status(hb, now=time.time() + 600)[0], "warn")

    def test_cell_ledger_range_rows(self) -> None:
        from dashboard import board_truth as bt
        head = "cell_id,date,layer,hypothesis,status,ref\n"
        singles = "".join(f"{i},2026-10-21,entry,h,rejected,r\n" for i in range(268, 272))
        for dash in ("-", "–"):
            path = self.root / f"ledger{ord(dash)}.csv"
            path.write_text(
                head + f"1{dash}267,2026-10-20,all,历史累计、明细待补,历史累计、明细待补,r\n"
                + singles + "bad,x,x,x,x,x\n,x,x,x,x,x\n9-3,x,x,x,x,x\n",
                encoding="utf-8",
            )
            led = bt.load_cell_ledger(path)
            self.assertEqual(led.max_cell_id, 271)
            self.assertEqual(led.skipped, 3)
            self.assertIn("3 行无法解析已跳过", bt.grid_count_label(led))
        only = self.root / "only_range.csv"
        only.write_text(head + "1–267,x,x,x,x,x\n", encoding="utf-8")
        self.assertEqual(bt.load_cell_ledger(only).max_cell_id, 267)

    def test_deploy_components_include_indicators(self) -> None:
        from dashboard import board_truth as bt
        self.assertIn(("indicators", "indicators.run"), bt.DEPLOY_COMPONENTS)
        self.assertEqual(bt.DEPLOY_REPO_PATHS["indicators"], "crawler/indicators")
        (self.root / "indicators").mkdir()
        (self.root / "indicators" / "a.py").write_text("x=1\n")
        seen: list[str] = []

        def fake_tree(_repo, _rev, sub):
            seen.append(sub)
            return {"a.py": bt._blob_sha(b"x=1\n")}

        with patch.object(bt, "_tree", side_effect=fake_tree):
            row = bt.compare_component(self.root, self.root, "indicators", "no-such-proc-xyz")
        self.assertEqual(seen, ["crawler/indicators"])
        self.assertIn("0 文件不同", row.detail)

    def test_cell_ledger_reserved_rows(self) -> None:
        from dashboard import board_truth as bt
        head = "cell_id,cell_id_end,date,layer,hypothesis,status,ref\n"
        rng = "1,267,,multi,历史累计,mixed,r\n"
        only = self.root / "only.csv"
        only.write_text(head + rng, encoding="utf-8")
        self.assertEqual(bt.load_cell_ledger(only).max_cell_id, 267)
        full = self.root / "full.csv"
        full.write_text(
            head + rng
            + "".join(f"{i},,2026-10-21,x,h,registered,r\n" for i in range(268, 272))
            + "272,,2026-10-21,decision,迁移,reserved,r\n",
            encoding="utf-8",
        )
        led = bt.load_cell_ledger(full)
        self.assertEqual(led.max_cell_id, 271)
        self.assertEqual(led.reserved, (272,))
        label = bt.grid_count_label(led)
        self.assertIn("累计共测 271 格", label)
        self.assertIn("另有 1 行 reserved（272）", label)

    def test_cell_ledger_end_column_bad_row(self) -> None:
        from dashboard import board_truth as bt
        path = self.root / "l.csv"
        path.write_text(
            "cell_id,cell_id_end,date,layer,hypothesis,status,ref\n"
            "1,267,x,x,x,x,x\n270,,x,x,x,x,x\n5,zz,x,x,x,x,x\n",
            encoding="utf-8",
        )
        led = bt.load_cell_ledger(path)
        self.assertEqual((led.max_cell_id, led.skipped), (270, 1))

    def test_deploy_no_comparable_files(self) -> None:
        from dashboard import board_truth as bt
        (self.root / "indicators").mkdir()
        (self.root / "indicators" / "run.py").write_text("x=1\n")
        with patch.object(bt, "_tree", return_value={}):
            row = bt.compare_component(self.root, self.root, "indicators", "no-such-proc-xyz")
        self.assertIn("无可比文件", row.detail)
        self.assertNotIn("0 文件不同", row.detail)
        self.assertIn(row.status, {"unknown", "unreported"})
        self.assertEqual(app_module._DEPLOY_TONE[row.status], "mute")

    def _git_repo_with_ledger(self, content: str | None) -> Path:
        origin = self.root / "origin"
        work = self.root / "work"
        run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
        run("git", "init", "-q", "-b", "main", str(origin))
        run("git", "-C", str(origin), "config", "user.email", "t@t")
        run("git", "-C", str(origin), "config", "user.name", "t")
        (origin / "docs").mkdir()
        if content is not None:
            (origin / "docs" / "CELL_LEDGER.csv").write_text(content, encoding="utf-8")
        else:
            (origin / "docs" / "x.md").write_text("x")
        run("git", "-C", str(origin), "add", "-A")
        run("git", "-C", str(origin), "commit", "-qm", "c")
        run("git", "clone", "-q", str(origin), str(work))
        run("git", "-C", str(work), "fetch", "-q", "origin")
        return work

    def test_ledger_from_git(self) -> None:
        from dashboard import board_truth as bt
        head = "cell_id,cell_id_end,date,layer,hypothesis,status,ref\n"
        body = head + "1,267,,m,h,mixed,r\n" + "".join(
            f"{i},,d,l,h,registered,r\n" for i in range(268, 272)
        ) + "272,,d,l,h,reserved,r\n"
        work = self._git_repo_with_ledger(body)
        os.environ.pop("CELL_LEDGER_CSV", None)
        os.environ["DEPLOY_COMPARE_REPO"] = str(work)
        bt._LEDGER_CACHE = None
        led, src = bt.load_ledger_with_source()
        self.assertEqual(led.max_cell_id, 271)
        self.assertEqual(src.kind, "git")
        sha = subprocess.run(["git", "-C", str(work), "rev-parse", "origin/main"],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(src.sha, sha)
        self.assertFalse(src.stale)
        text, warn = bt.ledger_source_line(src)
        self.assertIn(sha[:8], text)
        self.assertIn("HKT", text)
        self.assertFalse(warn)
        _led, old = bt.load_ledger_with_source(now=time.time() + 7200)
        self.assertTrue(old.stale)
        self.assertTrue(bt.ledger_source_line(old)[1])
        self.assertTrue(bt.fetch_compare_repo_once())
        # explicit override wins and is labelled 本地覆盖 <path>
        over = self.root / "over.csv"
        over.write_text(head + "5,,d,l,h,x,r\n", encoding="utf-8")
        os.environ["CELL_LEDGER_CSV"] = str(over)
        led, src = bt.cached_ledger()
        self.assertEqual(led.max_cell_id, 5)
        self.assertIn(f"来源：本地覆盖 {over}", bt.ledger_source_line(src)[0])

    def test_ledger_git_missing_file_and_repo(self) -> None:
        from dashboard import board_truth as bt
        os.environ.pop("CELL_LEDGER_CSV", None)
        work = self._git_repo_with_ledger(None)
        os.environ["DEPLOY_COMPARE_REPO"] = str(work)
        led, _src = bt.load_ledger_with_source()
        self.assertIsNone(led.max_cell_id)
        self.assertTrue(bt.grid_count_label(led).startswith("格数：总账缺失（"))
        os.environ["DEPLOY_COMPARE_REPO"] = str(self.root / "nope")
        led, _src = bt.load_ledger_with_source()
        self.assertTrue(bt.grid_count_label(led).startswith("格数：总账缺失（"))
        bt._FETCH_STATE["last_error"] = None
        self.assertFalse(bt.fetch_compare_repo_once())
        bt._FETCH_STATE["last_error"] = None

    def test_deploy_diff_is_yellow_even_if_declared_matches(self) -> None:
        from dashboard import board_truth as bt
        folder = self.root / "execution"
        folder.mkdir()
        (folder / "a.py").write_text("x=1\n")
        (folder / "DEPLOY_SHA").write_text("abc123\n")
        good = {"a.py": bt._blob_sha(b"x=1\n")}

        def fake_tree(_repo, rev, _sub):
            return dict(good) if rev == "abc123" else {"a.py": good["a.py"], "b.py": "f" * 40}

        with patch.object(bt, "_tree", side_effect=fake_tree):
            row = bt.compare_component(self.root, self.root, "execution", "no-such-proc-xyz")
        self.assertIn("与文件一致", row.detail)
        self.assertEqual(row.status, "yellow")

    def test_main_diff_nonzero_exact(self) -> None:
        from dashboard import board_truth as bt
        self.assertFalse(bt.main_diff_nonzero("未报 · 对 main：0 文件不同 · 0 缺"))
        self.assertTrue(bt.main_diff_nonzero("对 main：1 文件不同 · 0 缺"))
        self.assertTrue(bt.main_diff_nonzero("对 main：10 文件不同 · 0 缺"))
        self.assertTrue(bt.main_diff_nonzero("对 main：0 文件不同 · 1 缺"))
        self.assertFalse(bt.main_diff_nonzero("无可比文件"))

    def test_ledger_missing_always_warns(self) -> None:
        from dashboard import board_truth as bt
        src = bt.LedgerSource(
            "missing", "origin/main@deadbeef", "deadbeef" * 5,
            datetime.now(timezone.utc), False, reason="origin/main 无 docs/CELL_LEDGER.csv",
        )
        text, warn = bt.ledger_source_line(src)
        self.assertTrue(warn)
        self.assertIn("缺失", text)

    def test_ledger_show_uses_resolved_sha(self) -> None:
        from dashboard import board_truth as bt
        head = "cell_id,cell_id_end,date,layer,hypothesis,status,ref\n1,,d,l,h,x,r\n"
        work = self._git_repo_with_ledger(head)
        os.environ.pop("CELL_LEDGER_CSV", None)
        os.environ["DEPLOY_COMPARE_REPO"] = str(work)
        sha = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "origin/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        seen: list[str] = []
        real = bt._git

        def spy(repo, *args):
            seen.append(args)
            return real(repo, *args)

        with patch.object(bt, "_git", side_effect=spy):
            bt.load_ledger_with_source()
        shows = [a for a in seen if a and a[0] == "show"]
        self.assertEqual(len(shows), 1)
        self.assertEqual(shows[0], ("show", f"{sha}:docs/CELL_LEDGER.csv"))
        self.assertNotIn("origin/main:", shows[0][1])

    def test_deploy_unreported(self) -> None:
        from dashboard import board_truth as bt
        (self.root / "execution").mkdir()
        (self.root / "execution" / "a.py").write_text("x=1\n")
        row = bt.compare_component(self.root, None, "execution", "no-such-proc-xyz")
        self.assertEqual(row.status, "unreported")
        self.assertIn("未报", row.detail)

    def test_stamp_lag_with_tip_blobs_is_yellow(self) -> None:
        from dashboard import board_truth as bt
        folder = self.root / "execution"
        folder.mkdir()
        (folder / "a.py").write_text("x=1\n")
        (folder / "DEPLOY_SHA").write_text("oldsha12\n")
        tip_blob = bt._blob_sha(b"x=1\n")

        def fake_tree(_repo: object, rev: str, _sub: str) -> dict[str, str]:
            if rev == "origin/main":
                return {"a.py": tip_blob}
            if rev == "oldsha12":
                return {"a.py": "ab" * 20}
            return {}

        with patch.object(bt, "_tree", side_effect=fake_tree), patch.object(
            bt, "declared_lags_tip", return_value=True
        ):
            row = bt.compare_component(self.root, self.root, "execution", "no-such-proc-xyz")
        self.assertEqual(row.status, "yellow")
        self.assertIn("落后 tip", row.detail)
        self.assertIn("文件与 tip 一致", row.detail)
        self.assertNotIn("与文件不符", row.detail)
        self.assertNotIn("进程已退出", row.detail)
        self.assertNotIn("执行中断", row.detail)
        self.assertEqual(app_module._DEPLOY_TONE[row.status], "warn")
        self.assertIn("落后 tip", app_module._DEPLOY_FOOT)
        self.assertIn("非进程退出", app_module._DEPLOY_FOOT)
        self.assertIn("声明落后 tip", inspect.getsource(app_module._v2_deploy_sha))

    def test_stamp_mismatch_stays_red_when_tip_blobs_differ(self) -> None:
        from dashboard import board_truth as bt
        folder = self.root / "execution"
        folder.mkdir()
        (folder / "a.py").write_text("x=1\n")
        (folder / "DEPLOY_SHA").write_text("oldsha12\n")

        def fake_tree(_repo: object, rev: str, _sub: str) -> dict[str, str]:
            if rev == "origin/main":
                return {"a.py": "ff" * 20}
            if rev == "oldsha12":
                return {"a.py": "ab" * 20}
            return {}

        with patch.object(bt, "_tree", side_effect=fake_tree), patch.object(
            bt, "declared_lags_tip", return_value=True
        ):
            row = bt.compare_component(self.root, self.root, "execution", "no-such-proc-xyz")
        self.assertEqual(row.status, "red")
        self.assertIn("与文件不符", row.detail)
        self.assertNotIn("落后 tip", row.detail)

    def test_stamp_lag_not_yellow_without_ancestor(self) -> None:
        from dashboard import board_truth as bt
        folder = self.root / "execution"
        folder.mkdir()
        (folder / "a.py").write_text("x=1\n")
        (folder / "DEPLOY_SHA").write_text("oldsha12\n")
        tip_blob = bt._blob_sha(b"x=1\n")

        def fake_tree(_repo: object, rev: str, _sub: str) -> dict[str, str]:
            if rev == "origin/main":
                return {"a.py": tip_blob}
            if rev == "oldsha12":
                return {"a.py": "ab" * 20}
            return {}

        with patch.object(bt, "_tree", side_effect=fake_tree), patch.object(
            bt, "declared_lags_tip", return_value=False
        ):
            row = bt.compare_component(self.root, self.root, "execution", "no-such-proc-xyz")
        self.assertEqual(row.status, "red")
        self.assertIn("与文件不符", row.detail)

    def test_declared_lags_tip_on_real_history(self) -> None:
        from dashboard import board_truth as bt
        work = self._git_repo_with_ledger("a")
        origin = self.root / "origin"
        old = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "origin/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (origin / "docs" / "x.md").write_text("newer\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(origin), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(origin), "commit", "-qm", "newer"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], check=True, capture_output=True)
        tip = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "origin/main"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(old, tip)
        self.assertTrue(bt.declared_lags_tip(work, old))
        self.assertFalse(bt.declared_lags_tip(work, tip))
        self.assertIsNone(bt.declared_lags_tip(work, "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"))

    def test_supply_wal_missing_and_parent_heartbeat(self) -> None:
        from dashboard import board_truth as bt
        blank = bt.supply_wal_lock(self.root, now=1_000.0)
        self.assertEqual(blank.tone, "mute")
        self.assertIsNone(blank.age_seconds)
        self.assertIsNone(blank.status)
        self.assertIsNone(blank.wal_bytes)
        self.assertIn("心跳文件缺失", blank.note)
        self.assertIn("WAL 无", blank.note)

        dbroot = self.root / "dbroot"
        dbroot.mkdir()
        parent_logs = self.root / "logs"
        parent_logs.mkdir()
        heartbeat = parent_logs / "heartbeat.json"
        heartbeat.write_text(
            json.dumps({"status": "ok", "last_error": "supersecretvalue"}),
            encoding="utf-8",
        )
        os.utime(heartbeat, (1_000, 1_000))
        fresh = bt.supply_wal_lock(dbroot, now=1_030.0)
        self.assertEqual(fresh.tone, "ok")
        self.assertEqual(fresh.status, "ok")
        self.assertEqual(fresh.where, "上级/logs/heartbeat.json")
        self.assertAlmostEqual(fresh.age_seconds or 0, 30.0, delta=0.1)
        self.assertNotIn("supersecretvalue", fresh.note)
        self.assertIsNone(fresh.wal_bytes)
        stale = bt.supply_wal_lock(dbroot, now=1_000.0 + bt.SUPPLY_HEARTBEAT_STALE_SEC + 5)
        self.assertEqual(stale.tone, "warn")

        direct_logs = dbroot / "logs"
        direct_logs.mkdir()
        direct = direct_logs / "heartbeat.json"
        direct.write_text(json.dumps({"status": "degraded"}), encoding="utf-8")
        degraded = bt.supply_wal_lock(dbroot, now=time.time())
        self.assertEqual(degraded.where, "DASHBOARD_DB_ROOT/logs/heartbeat.json")
        self.assertEqual(degraded.status, "degraded")
        self.assertEqual(degraded.tone, "warn")
        (dbroot / "dexscreener.db-wal").write_bytes(b"abc")
        sized = bt.supply_wal_lock(dbroot, now=time.time())
        self.assertEqual(sized.wal_bytes, 3)
        direct.write_text(json.dumps({"status": "error"}), encoding="utf-8")
        self.assertEqual(bt.supply_wal_lock(dbroot, now=time.time()).tone, "warn")
        direct.write_text("{", encoding="utf-8")
        broken = bt.supply_wal_lock(dbroot, now=time.time())
        self.assertEqual(broken.tone, "warn")
        self.assertIsNone(broken.status)
        self.assertIn("心跳 JSON 不可读", broken.note)


class ObserveAsideSmokeTest(unittest.TestCase):
    """JEV clock, WAL card, and realized-equity aside. Missing stays blank."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _paths(self) -> DbPaths:
        return DbPaths(
            main=self.root / "dexscreener.db",
            execution=self.root / "execution_ledger.db",
            paper=self.root / "paper_trading.db",
            backtest=self.root / "backtest_ledger.db",
            news=self.root / "news_events.db",
        )

    def test_jev_clock_missing_degrades(self) -> None:
        store = DataStore(self._paths())
        result = store.jev_signal_clock()
        self.assertFalse(result.available)
        self.assertIsNone(result.data)
        texts = _rendered_card_texts(lambda: app_module._v2_jev_clock(store))
        self.assertTrue(any("dexscreener.db" in text for text in texts))
        self.assertTrue(any(app_module._JEV_CLOCK_NAIL in text for text in texts))
        empty = self.root / "dexscreener.db"
        sqlite3.connect(empty).close()
        store = DataStore(self._paths())
        gap = store.jev_signal_clock()
        self.assertFalse(gap.available)
        self.assertIn("token_indicators", gap.reason)
        gap_texts = _rendered_card_texts(lambda: app_module._v2_jev_clock(store))
        self.assertTrue(any("缺少数据表" in text for text in gap_texts))

    def test_jev_clock_counts_without_inventing_bullish(self) -> None:
        as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        fresh = "2026-10-24T11:00:00Z"
        old = "2026-10-20T00:00:00Z"
        main = self.root / "dexscreener.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                "CREATE TABLE token_indicators ("
                "chain_id TEXT, token_address TEXT, jev_strength REAL, jev_updated_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO token_indicators VALUES (?, ?, ?, ?)",
                [
                    ("solana", "a", 0.4, fresh),
                    ("solana", "b", 0, fresh),
                    ("solana", "c", -0.2, fresh),
                    ("solana", "d", 0.2, old),
                    ("solana", "e", None, None),
                ],
            )
            conn.execute(
                "CREATE TABLE jev_signals ("
                "chain_id TEXT, token_address TEXT, stance TEXT, fetched_at TEXT)"
            )
            conn.executemany(
                "INSERT INTO jev_signals VALUES (?, ?, ?, ?)",
                [
                    ("solana", "a", "BULLISH", fresh),
                    ("solana", "b", "NOT BULLISH", fresh),
                    ("solana", "c", "NEUTRAL", old),
                    ("solana", "d", "BUY", fresh),
                    ("solana", "e", "not bullish", fresh),
                ],
            )
        report = DataStore(self._paths()).jev_signal_clock(now=as_of)
        self.assertTrue(report.available)
        clock = report.data
        self.assertEqual(clock.max_jev_updated_at, fresh)
        self.assertEqual(clock.clock_source, "token_indicators.jev_updated_at")
        self.assertEqual(clock.strength_positive, 2)
        self.assertEqual(clock.strength_column, "jev_strength")
        self.assertEqual(clock.bullish_24h, 2)
        labels = dict(clock.stance_counts)
        self.assertEqual(labels["BULLISH"], 1)
        self.assertEqual(labels["NOT BULLISH"], 1)
        self.assertEqual(labels["NEUTRAL"], 1)
        texts = _rendered_card_texts(lambda: app_module._v2_jev_clock(DataStore(self._paths())))
        self.assertTrue(any(app_module._JEV_CLOCK_NAIL in text for text in texts))
        self.assertIn("ro_connect", inspect.getsource(DataStore._jev_signal_clock))
        self.assertNotIn("os.environ", inspect.getsource(DataStore._jev_signal_clock))

    def test_jev_clock_zero_bullish_is_a_real_zero(self) -> None:
        as_of = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        fresh = "2026-10-24T11:00:00Z"
        main = self.root / "dexscreener.db"
        with sqlite3.connect(main) as conn:
            conn.execute(
                "CREATE TABLE token_indicators (jev_strength REAL, jev_updated_at TEXT)"
            )
            conn.execute("INSERT INTO token_indicators VALUES (0.1, ?)", (fresh,))
            conn.execute("CREATE TABLE jev_signals (stance TEXT, fetched_at TEXT)")
            conn.execute("INSERT INTO jev_signals VALUES ('NEUTRAL', ?)", (fresh,))
        clock = DataStore(self._paths()).jev_signal_clock(now=as_of).data
        self.assertEqual(clock.bullish_24h, 0)
        self.assertEqual(clock.strength_positive, 1)
        pinned = DataStore(self._paths())

        def _render() -> None:
            original = pinned.jev_signal_clock

            def _fixed(*, now: datetime | None = None) -> object:
                return original(now=as_of)

            pinned.jev_signal_clock = _fixed  # type: ignore[method-assign]
            app_module._v2_jev_clock(pinned)

        texts = _rendered_card_texts(_render)
        self.assertIn("0", texts)
        self.assertTrue(any(app_module._JEV_CLOCK_NAIL in text for text in texts))

    def test_equity_aside_drawdown_and_missing(self) -> None:
        store = DataStore(self._paths())
        missing = store.paper_equity_aside()
        self.assertFalse(missing.available)
        texts = _rendered_card_texts(lambda: app_module._v2_equity_aside(store))
        self.assertTrue(any("paper_trading.db" in text for text in texts))
        self.assertTrue(any(app_module._EQUITY_ASIDE_NAIL in text for text in texts))
        paper = self.root / "paper_trading.db"
        with sqlite3.connect(paper) as conn:
            conn.execute(
                "CREATE TABLE positions ("
                "id TEXT, status TEXT, closed_at TEXT, pnl_usd REAL, exit_reason TEXT, size_usd REAL)"
            )
            conn.executemany(
                "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("1", "closed", "2026-10-20T00:00:00Z", 100, "tp", 10),
                    ("2", "closed", "2026-10-21T00:00:00Z", -40, "stop_loss", 10),
                    ("3", "closed", "2026-10-22T00:00:00Z", -10, "stop_loss", 10),
                    ("4", "closed", "2026-10-23T00:00:00Z", 5, "tp", 10),
                    ("5", "open", None, None, None, 10),
                ],
            )
        as_of = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)
        report = DataStore(self._paths()).paper_equity_aside(now=as_of)
        self.assertTrue(report.available)
        aside = report.data
        self.assertEqual(aside.closed_n, 4)
        self.assertAlmostEqual(aside.max_drawdown_usd or 0, 50.0)
        self.assertAlmostEqual(aside.max_drawdown_pct or 0, 0.5)
        self.assertAlmostEqual(aside.stop_of_neg or 0, 1.0)
        self.assertEqual(aside.days_since_last_close, 1)
        rendered = _rendered_card_texts(lambda: app_module._v2_equity_aside(DataStore(self._paths())))
        self.assertTrue(any("不改保守账" in text for text in rendered))
        self.assertIn("ro_connect", inspect.getsource(DataStore._paper_equity_aside))
        goal = inspect.getsource(DataStore._paper_goal_pace)
        self.assertIn("equity_conservative_usd", goal)
        overview = inspect.getsource(app_module._render_dashboard)
        self.assertLess(overview.index("_mount_equity_aside"), overview.index("_mount_goal_20k"))
        self.assertLess(overview.index("_mount_jev_clock"), overview.index("_mount_supply_wal"))
        self.assertIn("span-4", inspect.getsource(app_module._mount_jev_clock))
        self.assertIn("span-4", inspect.getsource(app_module._mount_supply_wal))
        self.assertIn("span-4", inspect.getsource(app_module._mount_equity_aside))

    def test_supply_wal_card_renders_missing(self) -> None:
        os.environ["DASHBOARD_DB_ROOT"] = str(self.root)
        texts = _rendered_card_texts(app_module._v2_supply_wal)
        self.assertTrue(any("心跳文件缺失" in text for text in texts))
        self.assertTrue(any(app_module._WAL_NAIL in text for text in texts))
        self.assertNotIn("0 B", texts)
        self.assertNotIn("0 秒", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
