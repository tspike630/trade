"""Read-only freshness dump: tech≤10m by tier, hot age p50/p90."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

from indicators.freshness_report import (
    HOT_TECH_FRESH_SEC,
    linear_percentile,
    main,
    summarize,
    write_report,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PercentileTest(unittest.TestCase):
    def test_linear_inclusive(self) -> None:
        ages = [60.0, 120.0, 600.0, 1200.0]
        p50 = linear_percentile(ages, 50)
        p90 = linear_percentile(ages, 90)
        self.assertIsNotNone(p50)
        self.assertIsNotNone(p90)
        assert p50 is not None and p90 is not None
        self.assertAlmostEqual(p50, 360.0)
        self.assertAlmostEqual(p90, 1020.0)
        self.assertIsNone(linear_percentile([], 50))
        self.assertEqual(linear_percentile([12.0], 90), 12.0)


class SummarizeTest(unittest.TestCase):
    def test_fresh_counts_by_tier_and_hot_age(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        rows = [
            ("hot", _iso(now - timedelta(seconds=60))),
            ("hot", _iso(now - timedelta(seconds=120))),
            ("hot", _iso(now - timedelta(seconds=600))),
            ("hot", _iso(now - timedelta(seconds=1200))),
            ("hot", None),
            ("warm", _iso(now - timedelta(seconds=30))),
            ("warm", _iso(now - timedelta(seconds=601))),
            ("cold", _iso(now - timedelta(seconds=10))),
            ("(blank)", _iso(now - timedelta(seconds=5))),
        ]
        summary = summarize(rows, now=now)
        self.assertEqual(HOT_TECH_FRESH_SEC, 600)
        self.assertEqual(summary["by_tier"]["hot"]["n"], 5)
        self.assertEqual(summary["by_tier"]["hot"]["fresh"], 3)
        self.assertEqual(summary["by_tier"]["hot"]["missing"], 1)
        self.assertEqual(summary["by_tier"]["warm"]["fresh"], 1)
        self.assertEqual(summary["by_tier"]["warm"]["n"], 2)
        self.assertEqual(summary["by_tier"]["cold"]["fresh"], 1)
        self.assertEqual(summary["by_tier"]["(blank)"]["fresh"], 1)
        self.assertEqual(summary["hot_n_with_tech"], 4)
        self.assertAlmostEqual(summary["hot_age_p50_sec"], 360.0)
        self.assertAlmostEqual(summary["hot_age_p90_sec"], 1020.0)


class WriteReportTest(unittest.TestCase):
    def _seed(self, path: Path, *, with_tech: bool) -> None:
        conn = sqlite3.connect(path)
        try:
            if with_tech:
                conn.execute(
                    """
                    CREATE TABLE token_indicators (
                        chain_id TEXT,
                        token_address TEXT,
                        tier TEXT,
                        updated_at TEXT,
                        tech_updated_at TEXT
                    )
                    """
                )
            else:
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
                "INSERT INTO token_indicators VALUES (?, ?, ?, ?, ?)"
                if with_tech
                else "INSERT INTO token_indicators VALUES (?, ?, ?, ?)",
                ("solana", "aaa", "hot", "2026-10-14T11:59:00Z", "2026-10-14T11:59:00Z")
                if with_tech
                else ("solana", "aaa", "hot", "2026-10-14T11:59:00Z"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_markdown_uses_tech_column_and_optional_pid(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "dexscreener.db"
            self._seed(db, with_tech=True)
            out = root / "indicators_freshness_20261014.md"
            write_report(db, out, now=now, pids=[4242], scan_pids=False)
            text = out.read_text(encoding="utf-8")
            self.assertIn("loop_pid: `4242`", text)
            self.assertIn("| hot | 1 | 1 | 1 | 0 |", text)
            self.assertIn("age_p50_sec: 60.0", text)
            self.assertIn("供流正常 ≠ 下游能用", text)
            self.assertIn("tech_updated_at", text)

    def test_missing_tech_column_does_not_use_updated_at(self) -> None:
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "dexscreener.db"
            self._seed(db, with_tech=False)
            out = root / "out.md"
            write_report(db, out, now=now, pids=None, scan_pids=False)
            text = out.read_text(encoding="utf-8")
            self.assertIn("tech_updated_at_absent", text)
            self.assertIn("不改用 `updated_at`", text)
            self.assertNotIn("| hot |", text)

    def test_cli_writes_dated_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "dexscreener.db"
            self._seed(db, with_tech=True)
            out = root / "indicators_freshness_20261014.md"
            buf = StringIO()
            with redirect_stdout(buf):
                code = main(
                    [
                        "--db",
                        str(db),
                        "--out",
                        str(out),
                        "--now",
                        "2026-10-14T12:00:00Z",
                        "--no-pid",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertIn(str(out), buf.getvalue())
            text = out.read_text(encoding="utf-8")
            self.assertIn("loop_pid: `—`", text)
            self.assertIn("| hot | 1 | 1 | 1 | 0 |", text)


if __name__ == "__main__":
    unittest.main()
