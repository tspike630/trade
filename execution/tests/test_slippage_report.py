"""I-3 slippage_1h report: empty databases and sample fills do not crash.

The report is read-only. ``bot_trade_share`` prefers ``bot_fill_observations``.
It does not invent bot labels or skip orders.
Run from the repo root:

    python -m unittest execution.tests.test_slippage_report
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from execution.slippage_report import build_report, format_report, main as report_main  # noqa: E402
from execution.tests.isolated_env import isolated_environ  # noqa: E402


AS_OF = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _py_env() -> dict[str, str]:
    env = isolated_environ({"PYTHONPATH": str(ROOT)})
    return env


def _receipt(slippage: float, *, side: str = "buy", mid: float = 1.0, bot: object | None = None) -> str:
    fill = mid * (1.0 + slippage) if side == "buy" else mid * (1.0 - slippage)
    body: dict[str, object] = {
        "status": "paper_executed",
        "exec_result": {
            "ok": True,
            "side": side,
            "mid_price": mid,
            "fill_price": fill,
            "slippage": slippage,
        },
    }
    if bot is not None:
        assert isinstance(body["exec_result"], dict)
        body["exec_result"]["is_bot"] = bot
    return json.dumps(body)


def _write_ledger(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            decision_log_id INTEGER,
            strategy_id TEXT,
            action TEXT,
            executed INTEGER,
            receipt_json TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO execution_log
            (ts, decision_log_id, strategy_id, action, executed, receipt_json)
        VALUES (?, ?, 's', ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _write_paper(path: Path, positions: list[tuple], fills: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE positions (
            id TEXT PRIMARY KEY,
            meta_json TEXT,
            status TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT,
            side TEXT,
            price REAL,
            created_at TEXT,
            note TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO positions (id, meta_json, status) VALUES (?, ?, 'open')",
        positions,
    )
    conn.executemany(
        """
        INSERT INTO fills (position_id, side, price, created_at, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        fills,
    )
    conn.commit()
    conn.close()


def _write_bot_fills(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE bot_fill_observations (
            chain_id TEXT NOT NULL,
            signature TEXT NOT NULL,
            wallet TEXT NOT NULL,
            token_address TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            bot_reason TEXT,
            PRIMARY KEY (chain_id, signature, wallet, token_address)
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO bot_fill_observations
            (chain_id, signature, wallet, token_address, recorded_at, bot_reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


TOKEN_A = "So11111111111111111111111111111111111111112"
TOKEN_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_WALLET = "Wallet1111111111111111111111111111111111111"


class SlippageReportTests(unittest.TestCase):
    def test_missing_and_empty_databases_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_ledger = root / "no-ledger.db"
            missing_paper = root / "no-paper.db"
            report = build_report(
                ledger=missing_ledger,
                paper=missing_paper,
                as_of=AS_OF,
            )
            self.assertFalse(missing_ledger.exists())
            self.assertFalse(missing_paper.exists())
            self.assertEqual(report["slippage_1h"]["n"], 0)
            self.assertIsNone(report["slippage_1h"]["mean"])
            self.assertEqual(report["daily"], [])
            self.assertEqual(report["bot_trade_share"]["status"], "no_samples")
            self.assertIsNone(report["bot_trade_share"]["share"])
            self.assertFalse(report["rejects_orders"])
            self.assertFalse(report["high_bot_skip"]["wired"])
            self.assertIn("data gap", report["bot_trade_share"]["note"])

            empty_ledger = root / "empty-ledger.db"
            empty_paper = root / "empty-paper.db"
            sqlite3.connect(empty_ledger).close()
            sqlite3.connect(empty_paper).close()
            empty = build_report(ledger=empty_ledger, paper=empty_paper, as_of=AS_OF)
            self.assertTrue(empty["sources"]["ledger"]["present"])
            self.assertEqual(empty["slippage_1h"]["n"], 0)
            self.assertEqual(empty["bot_trade_share"]["status"], "no_samples")

            schema = root / "schema-only.db"
            _write_ledger(schema, [])
            _write_paper(root / "schema-paper.db", [], [])
            bare = build_report(
                ledger=schema,
                paper=root / "schema-paper.db",
                as_of=AS_OF,
            )
            self.assertEqual(bare["slippage_1h"]["n"], 0)
            self.assertEqual(bare["samples"]["skipped_missing_fields"], 0)

    def test_samples_window_percentiles_and_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "execution_ledger.db"
            paper = root / "paper.db"
            _write_ledger(
                ledger,
                [
                    ("2026-10-14T11:30:00Z", 1, "buy", 1, _receipt(0.02)),
                    ("2026-10-14T11:00:00Z", 2, "buy", 1, _receipt(0.01, mid=2.0)),
                    ("2026-10-14T11:45:00Z", 3, "sell", 1, _receipt(0.003, side="sell", mid=3.0)),
                    ("2026-10-14T10:00:00Z", 4, "buy", 1, _receipt(0.05)),
                    ("2026-10-01T00:00:00Z", 5, "buy", 1, _receipt(0.9)),
                    ("2026-10-14T11:50:00Z", 6, "buy", 0, _receipt(0.02)),
                    ("2026-10-14T11:55:00Z", 7, "buy", 1, "not-json"),
                ],
            )
            _write_paper(
                paper,
                [
                    (
                        "pos-dup",
                        json.dumps({"decision_log_id": 1, "mid_price": 1.0, "slippage": 0.99}),
                    ),
                    (
                        "pos-paper",
                        json.dumps({"decision_log_id": 8, "mid_price": 10.0, "slippage": 0.02}),
                    ),
                    (
                        "pos-sell",
                        json.dumps({"decision_log_id": 9, "mid_price": 10.0, "slippage": 0.02}),
                    ),
                ],
                [
                    ("pos-dup", "buy", 1.02, "2026-10-14T11:30:00Z", "open"),
                    ("pos-paper", "buy", 10.2, "2026-10-14T11:20:00Z", "open"),
                    ("pos-sell", "sell", 9.8, "2026-10-14T11:40:00Z", "stop_loss"),
                ],
            )
            before = ledger.read_bytes()
            report = build_report(ledger=ledger, paper=paper, as_of=AS_OF, daily_days=7)
            self.assertEqual(ledger.read_bytes(), before)
            hour = report["slippage_1h"]
            # 0.02, 0.01, 0.003 from receipts, plus the paper-only buy 0.02.
            # The duplicate paper buy is dropped. The sell fill has no own
            # slip fields. The unfilled receipt and the old row stay out.
            self.assertEqual(hour["n"], 4)
            self.assertAlmostEqual(hour["mean"], (0.02 + 0.01 + 0.003 + 0.02) / 4)
            self.assertAlmostEqual(hour["p50"], 0.015)
            self.assertEqual(hour["by_side"]["buy"]["n"], 3)
            self.assertEqual(hour["by_side"]["sell"]["n"], 1)
            self.assertAlmostEqual(hour["by_side"]["sell"]["mean"], 0.003)
            self.assertEqual(report["samples"]["deduped"], 1)
            self.assertGreaterEqual(report["samples"]["skipped_missing_fields"], 2)
            self.assertEqual(report["samples"]["skipped_not_filled"], 1)
            self.assertEqual(report["bot_trade_share"]["status"], "data_gap")
            self.assertIsNone(report["bot_trade_share"]["share"])
            self.assertFalse(report["bot_trade_share"]["pattern_recognition"])
            days = {row["date"]: row for row in report["daily"]}
            self.assertEqual(set(days), {"2026-10-14"})
            self.assertEqual(days["2026-10-14"]["n"], 5)
            self.assertNotIn("2026-10-01", days)

    def test_bot_tag_is_optional_and_skip_stays_unwired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution_ledger.db"
            _write_ledger(
                ledger,
                [
                    ("2026-10-14T11:10:00Z", 1, "buy", 1, _receipt(0.02, bot=True)),
                    ("2026-10-14T11:20:00Z", 2, "buy", 1, _receipt(0.01, bot=False)),
                    ("2026-10-14T11:30:00Z", 3, "sell", 1, _receipt(0.003, side="sell")),
                ],
            )
            report = build_report(ledger=ledger, paper=None, as_of=AS_OF)
            bot = report["bot_trade_share"]
            self.assertEqual(bot["status"], "partial")
            self.assertAlmostEqual(bot["share"], 0.5)
            self.assertEqual(bot["tagged_n"], 2)
            self.assertEqual(bot["bot_n"], 1)
            self.assertEqual(bot["untagged_n"], 1)
            self.assertFalse(report["high_bot_skip"]["wired"])
            self.assertFalse(report["high_bot_skip"]["pm_gateway"])
            self.assertIn("R2/R3", report["high_bot_skip"]["note"])

            paper = Path(tmp) / "paper.db"
            _write_paper(
                paper,
                [("pos", json.dumps({"mid_price": 2.0, "slippage": 0.01}))],
                [
                    (
                        "pos",
                        "sell",
                        1.96,
                        "2026-10-14T11:50:00Z",
                        json.dumps(
                            {
                                "mid_price": 2.0,
                                "fill_price": 1.96,
                                "slippage": 0.02,
                                "bot_tag": "collector",
                            }
                        ),
                    )
                ],
            )
            tagged = build_report(ledger=None, paper=paper, as_of=AS_OF)
            self.assertEqual(tagged["slippage_1h"]["n"], 1)
            self.assertAlmostEqual(tagged["slippage_1h"]["mean"], 0.02)
            self.assertEqual(tagged["bot_trade_share"]["status"], "observed")
            self.assertAlmostEqual(tagged["bot_trade_share"]["share"], 1.0)
            self.assertFalse(tagged["rejects_orders"])

    def test_derives_cost_and_reads_reserved_bot_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution_ledger.db"
            conn = sqlite3.connect(ledger)
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT,
                    decision_log_id INTEGER,
                    action TEXT,
                    executed INTEGER,
                    receipt_json TEXT,
                    bot_tag TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO execution_log
                    (ts, decision_log_id, action, executed, receipt_json, bot_tag)
                VALUES (?, 1, 'buy', 1, ?, 'collector')
                """,
                (
                    "2026-10-14T11:15:00Z",
                    json.dumps(
                        {
                            "status": "paper_executed",
                            "exec_result": {
                                "ok": True,
                                "side": "buy",
                                "mid_price": 1.0,
                                "fill_price": 1.02,
                            },
                        }
                    ),
                ),
            )
            conn.commit()
            conn.close()
            report = build_report(ledger=ledger, paper=None, as_of=AS_OF)
            self.assertEqual(report["slippage_1h"]["n"], 1)
            self.assertAlmostEqual(report["slippage_1h"]["mean"], 0.02)
            self.assertEqual(report["bot_trade_share"]["status"], "observed")
            self.assertAlmostEqual(report["bot_trade_share"]["share"], 1.0)
            self.assertFalse(report["high_bot_skip"]["wired"])

    def test_bot_fill_observations_drive_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            crawler = root / "dexscreener.db"
            _write_bot_fills(
                crawler,
                [
                    ("solana", "sig-a", _WALLET, TOKEN_A, "2026-10-14T11:10:00Z", "same_slot_repeat"),
                    ("solana", "sig-b", _WALLET, TOKEN_A, "2026-10-14T11:20:00Z", "priority_fee"),
                    ("solana", "sig-c", _WALLET, TOKEN_A, "2026-10-14T11:40:00Z", None),
                    ("solana", "sig-d", _WALLET, TOKEN_B, "2026-10-14T11:50:00Z", ""),
                    ("solana", "sig-old", _WALLET, TOKEN_A, "2026-10-14T10:00:00Z", "fixed_interval"),
                    ("solana", "sig-new", _WALLET, TOKEN_A, "2026-10-14T12:30:00Z", "priority_fee"),
                ],
            )
            ledger = root / "execution_ledger.db"
            _write_ledger(
                ledger,
                [("2026-10-14T11:15:00Z", 1, "buy", 1, _receipt(0.02, bot=True))],
            )
            before = crawler.read_bytes()
            report = build_report(
                ledger=ledger,
                paper=None,
                crawler_db=crawler,
                as_of=AS_OF,
            )
            self.assertEqual(crawler.read_bytes(), before)
            bot = report["bot_trade_share"]
            self.assertEqual(bot["source"], "bot_fill_observations")
            self.assertEqual(bot["status"], "observed")
            self.assertAlmostEqual(bot["share"], 0.5)
            self.assertEqual(bot["total_fills"], 4)
            self.assertEqual(bot["bot_fills"], 2)
            self.assertTrue(bot["available"])
            self.assertIsNone(bot["gap"])
            self.assertFalse(bot["pattern_recognition"])
            by_token = {row["token_address"]: row for row in bot["by_token"]}
            self.assertEqual(set(by_token), {TOKEN_A, TOKEN_B})
            self.assertEqual(by_token[TOKEN_A]["total_fills"], 3)
            self.assertEqual(by_token[TOKEN_A]["bot_fills"], 2)
            self.assertAlmostEqual(by_token[TOKEN_A]["share"], 2 / 3)
            self.assertEqual(by_token[TOKEN_B]["total_fills"], 1)
            self.assertEqual(by_token[TOKEN_B]["bot_fills"], 0)
            self.assertEqual(by_token[TOKEN_B]["share"], 0.0)
            self.assertEqual(bot["receipt_fallback"]["source"], "receipt_tag")
            self.assertAlmostEqual(bot["receipt_fallback"]["share"], 1.0)
            self.assertFalse(report["high_bot_skip"]["wired"])
            self.assertFalse(report["high_bot_skip"]["pm_gateway"])
            self.assertFalse(report["rejects_orders"])
            text = format_report(report)
            self.assertIn("source=bot_fill_observations", text)
            self.assertIn("wired=false", text)
            self.assertIn("secondary", text)

            quiet = root / "unflagged.db"
            _write_bot_fills(
                quiet,
                [
                    ("solana", "sig-1", _WALLET, TOKEN_A, "2026-10-14T11:05:00Z", None),
                    ("solana", "sig-2", _WALLET, TOKEN_A, "2026-10-14T11:06:00Z", None),
                ],
            )
            zero = build_report(ledger=None, paper=None, crawler_db=quiet, as_of=AS_OF)
            share = zero["bot_trade_share"]
            self.assertEqual(share["status"], "observed")
            self.assertEqual(share["share"], 0.0)
            self.assertEqual(share["total_fills"], 2)
            self.assertEqual(share["bot_fills"], 0)
            self.assertFalse(zero["high_bot_skip"]["wired"])

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = report_main(
                    [
                        "--ledger",
                        str(ledger),
                        "--crawler-db",
                        str(crawler),
                        "--as-of",
                        "2026-10-14T12:00:00Z",
                        "--json",
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertAlmostEqual(payload["bot_trade_share"]["share"], 0.5)
            self.assertFalse(payload["high_bot_skip"]["wired"])

            previous_crawler = os.environ.get("CRAWLER_DB")
            previous_db = os.environ.get("DB_PATH")
            os.environ["DB_PATH"] = str(quiet)
            os.environ["CRAWLER_DB"] = str(crawler)
            try:
                preferred = io.StringIO()
                with contextlib.redirect_stdout(preferred):
                    preferred_code = report_main(
                        [
                            "--ledger",
                            str(root / "no-ledger.db"),
                            "--as-of",
                            "2026-10-14T12:00:00Z",
                            "--json",
                        ]
                    )
                os.environ.pop("CRAWLER_DB", None)
                from_db_path = io.StringIO()
                with contextlib.redirect_stdout(from_db_path):
                    db_path_code = report_main(
                        [
                            "--ledger",
                            str(root / "no-ledger.db"),
                            "--as-of",
                            "2026-10-14T12:00:00Z",
                            "--json",
                        ]
                    )
            finally:
                if previous_crawler is None:
                    os.environ.pop("CRAWLER_DB", None)
                else:
                    os.environ["CRAWLER_DB"] = previous_crawler
                if previous_db is None:
                    os.environ.pop("DB_PATH", None)
                else:
                    os.environ["DB_PATH"] = previous_db
            self.assertEqual(preferred_code, 0)
            self.assertAlmostEqual(
                json.loads(preferred.getvalue())["bot_trade_share"]["share"],
                0.5,
            )
            self.assertEqual(db_path_code, 0)
            self.assertEqual(
                json.loads(from_db_path.getvalue())["bot_trade_share"]["share"],
                0.0,
            )

    def test_missing_or_empty_observation_table_is_data_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "no-crawler.db"
            ledger = root / "execution_ledger.db"
            _write_ledger(
                ledger,
                [("2026-10-14T11:15:00Z", 1, "buy", 1, _receipt(0.02, bot=True))],
            )
            absent = build_report(
                ledger=ledger,
                paper=None,
                crawler_db=missing,
                as_of=AS_OF,
            )
            self.assertFalse(missing.exists())
            bot = absent["bot_trade_share"]
            self.assertEqual(bot["status"], "data_gap")
            self.assertEqual(bot["gap"], "no_table")
            self.assertIsNone(bot["share"])
            self.assertFalse(bot["available"])
            self.assertIn("missing crawler db", bot["error"])
            self.assertAlmostEqual(bot["receipt_fallback"]["share"], 1.0)
            self.assertFalse(absent["high_bot_skip"]["wired"])

            bare = root / "other-tables.db"
            conn = sqlite3.connect(bare)
            conn.execute("CREATE TABLE tokens (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            no_table = build_report(ledger=None, paper=None, crawler_db=bare, as_of=AS_OF)
            gap = no_table["bot_trade_share"]
            self.assertEqual(gap["status"], "data_gap")
            self.assertEqual(gap["gap"], "no_table")
            self.assertIsNone(gap["share"])
            self.assertIn("missing table bot_fill_observations", gap["error"])
            self.assertFalse(no_table["high_bot_skip"]["wired"])
            self.assertFalse(no_table["high_bot_skip"]["pm_gateway"])

            empty = root / "empty-fills.db"
            _write_bot_fills(empty, [])
            empty_report = build_report(
                ledger=ledger,
                paper=None,
                crawler_db=empty,
                as_of=AS_OF,
            )
            empty_bot = empty_report["bot_trade_share"]
            self.assertEqual(empty_bot["status"], "data_gap")
            self.assertEqual(empty_bot["gap"], "empty")
            self.assertIsNone(empty_bot["share"])
            self.assertEqual(empty_bot["total_fills"], 0)
            self.assertEqual(empty_bot["bot_fills"], 0)
            self.assertTrue(empty_bot["available"])
            self.assertNotEqual(empty_bot["share"], 0)
            self.assertAlmostEqual(empty_bot["receipt_fallback"]["share"], 1.0)
            self.assertFalse(empty_report["high_bot_skip"]["wired"])

    def test_cli_missing_and_sample_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.db"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = report_main(
                    ["--ledger", str(missing), "--paper", str(root / "missing-paper.db"), "--json"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["slippage_1h"]["n"], 0)
            self.assertFalse(missing.exists())

            ledger = root / "execution_ledger.db"
            _write_ledger(
                ledger,
                [("2026-10-14T11:30:00Z", 1, "buy", 1, _receipt(0.02))],
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution.slippage_report",
                    "--ledger",
                    str(ledger),
                    "--paper",
                    str(root / "absent-paper.db"),
                    "--as-of",
                    "2026-10-14T12:00:00Z",
                    "--json",
                ],
                cwd=str(ROOT),
                env=_py_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["slippage_1h"]["n"], 1)
            self.assertAlmostEqual(payload["slippage_1h"]["mean"], 0.02)
            self.assertEqual(payload["bot_trade_share"]["status"], "data_gap")
            self.assertFalse(payload["high_bot_skip"]["wired"])
            self.assertFalse((root / "absent-paper.db").exists())


if __name__ == "__main__":
    unittest.main()
