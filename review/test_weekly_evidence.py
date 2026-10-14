"""10-19 weekly evidence: closed-trade metrics, exports, and honest gaps."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from review.data import DbPaths
from review.gates import CONSECUTIVE_WEEKS, MIN_RAW_N, MIN_UNIQUE_TOKEN, UP_1H_PROMOTE
from review.report import render_weekly_md
from review.run import main
from review.weekly_evidence import STOP_LOSS_SHARE_VETO, compute_weekly_evidence

START = "2026-10-12T00:00:00Z"
END = "2026-10-20T00:00:00Z"


def _paths(root: Path) -> DbPaths:
    return DbPaths(
        main=str(root / "dexscreener.db"),
        execution=str(root / "execution_ledger.db"),
        paper=str(root / "paper_trading.db"),
        backtest=str(root / "backtest_ledger.db"),
    )


def _touch_empty(path: Path) -> None:
    sqlite3.connect(path).close()


def _paper(path: Path, sql: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
        if rows:
            placeholders = ", ".join("?" for _ in rows[0])
            conn.executemany(f"INSERT INTO positions VALUES ({placeholders})", rows)
        conn.commit()
    finally:
        conn.close()


class WeeklyEvidenceTest(unittest.TestCase):
    def test_stop_loss_share_worst_stop_and_hk_peak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, entry_price REAL,
                    qty REAL, size_usd REAL, meta_json TEXT
                );
                """,
                [
                    (
                        "sl-worst", "closed", -80.0, "stop_loss",
                        "2026-10-14T01:00:00Z", "2026-10-18T16:30:00Z",
                        1.0, 10.0, 100.0,
                        json.dumps({"strategy_id": "alpha"}),
                    ),
                    (
                        "sl-small", "closed", -20.0, "stop_loss",
                        "2026-10-14T02:00:00Z", "2026-10-18T16:40:00Z",
                        1.0, 10.0, 100.0,
                        json.dumps({"strategy_id": "alpha"}),
                    ),
                    (
                        "hold", "closed", -10.0, "max_hold",
                        "2026-10-14T03:00:00Z", "2026-10-18T17:00:00Z",
                        1.0, 10.0, 100.0,
                        json.dumps({"strategy_id": "alpha"}),
                    ),
                    (
                        "tp", "closed", 30.0, "take_profit",
                        "2026-10-14T04:00:00Z", "2026-10-18T02:00:00Z",
                        1.0, 10.0, 100.0,
                        json.dumps({"strategy_id": "alpha"}),
                    ),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        stop = ev["stop_loss"]
        self.assertAlmostEqual(stop["loss_share"], 100 / 110)
        self.assertAlmostEqual(stop["negative_pnl_usd"], -110.0)
        self.assertAlmostEqual(stop["stop_loss_negative_pnl_usd"], -100.0)
        self.assertEqual(stop["worst_stop"]["position_id"], "sl-worst")
        self.assertAlmostEqual(stop["worst_stop"]["pnl_usd"], -80.0)
        daily = ev["daily_open_peak"]
        self.assertEqual(daily["source"], "paper.positions.opened_at")
        self.assertEqual(daily["tz"], "Asia/Hong_Kong")
        # 16:30Z is 00:30 the next calendar day in Hong Kong; 02:00Z stays on the 18th.
        self.assertEqual(daily["peak"], 3)
        self.assertEqual(daily["peak_day"], "2026-10-19")
        self.assertEqual(daily["by_day"]["2026-10-18"], 1)
        self.assertTrue(ev["step2_veto"]["triggered"])
        self.assertIn("stop_loss_share", ev["step2_veto"]["reasons"])
        self.assertIn("即使 1h≥55%", ev["step2_veto"]["line"])
        self.assertIn("不算有效性 / R2旁证", ev["step2_veto"]["line"])
        self.assertEqual(ev["c3_shadow"]["status"], "data_gap")
        self.assertIn("not invented", ev["c3_shadow"]["note"])
        self.assertEqual(ev["g3_ruin"]["status"], "data_gap")

    def test_f1_from_closes_matches_two_loss_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            pnls = [-1.0, -1.0, -1.0, 1.0, -1.0, -1.0, -1.0]
            rows = []
            for index, pnl in enumerate(pnls):
                rows.append(
                    (
                        f"a{index}", "closed", pnl, "max_hold",
                        f"2026-10-13T0{index}:00:00Z", f"2026-10-13T0{index}:00:00Z",
                        json.dumps({"strategy_id": "alpha"}),
                    )
                )
            rows.append(
                (
                    "b0", "closed", 2.0, "take_profit",
                    "2026-10-13T08:00:00Z", "2026-10-13T08:00:00Z",
                    json.dumps({"strategy_id": "beta"}),
                )
            )
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                rows,
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        alpha = ev["f1_streak"]["by_strategy"]["alpha"]
        self.assertEqual(ev["f1_streak"]["source"], "closed_trades")
        self.assertEqual(alpha["n_given_two_loss"], 3)
        self.assertEqual(alpha["n_third_loss"], 2)
        self.assertAlmostEqual(alpha["p_third_loss_given_two_loss"], 2 / 3)
        beta = ev["f1_streak"]["by_strategy"]["beta"]
        self.assertEqual(beta["n_given_two_loss"], 0)
        self.assertIsNone(beta["p_third_loss_given_two_loss"])

    def test_zero_pnl_counts_as_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("z0", "closed", -1.0, "stop_loss", "2026-10-13T01:00:00Z", "2026-10-13T01:00:00Z", "{}"),
                    ("z1", "closed", 0.0, "max_hold", "2026-10-13T02:00:00Z", "2026-10-13T02:00:00Z", "{}"),
                    ("z2", "closed", -1.0, "stop_loss", "2026-10-13T03:00:00Z", "2026-10-13T03:00:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        row = ev["f1_streak"]["by_strategy"]["(unknown)"]
        self.assertEqual(row["n_given_two_loss"], 1)
        self.assertEqual(row["n_third_loss"], 1)
        self.assertAlmostEqual(row["p_third_loss_given_two_loss"], 1.0)

    def test_streak_observe_export_beats_closed_trades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("a0", "closed", -1.0, "stop_loss", "2026-10-13T01:00:00Z", "2026-10-13T01:00:00Z",
                     json.dumps({"strategy_id": "alpha"})),
                    ("a1", "closed", -1.0, "stop_loss", "2026-10-13T02:00:00Z", "2026-10-13T02:00:00Z",
                     json.dumps({"strategy_id": "alpha"})),
                    ("a2", "closed", -1.0, "stop_loss", "2026-10-13T03:00:00Z", "2026-10-13T03:00:00Z",
                     json.dumps({"strategy_id": "alpha"})),
                ],
            )
            export = root / "exports"
            export.mkdir()
            (export / "streak.json").write_text(
                json.dumps(
                    {
                        "streak_observe": {
                            "by_strategy": {
                                "alpha": {
                                    "n_closed": 4,
                                    "cond_n": 4,
                                    "n_third_loss": 1,
                                    "cond_third_loss_rate": 0.25,
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            ev = compute_weekly_evidence(
                _paths(root),
                period_start=START,
                period_end=END,
                export_roots=[export],
            )
        self.assertEqual(ev["f1_streak"]["source"], "streak_observe")
        alpha = ev["f1_streak"]["by_strategy"]["alpha"]
        self.assertEqual(alpha["n_given_two_loss"], 4)
        self.assertEqual(alpha["n_third_loss"], 1)
        self.assertAlmostEqual(alpha["p_third_loss_given_two_loss"], 0.25)

    def test_c3_peak_versus_max_hold_and_g3_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = sqlite3.connect(root / "execution_ledger.db")
            conn.execute(
                """
                CREATE TABLE order_events (
                    id INTEGER PRIMARY KEY, event TEXT, event_ts TEXT, detail_json TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO order_events (event, event_ts, detail_json) VALUES (?, ?, ?)",
                (
                    "filled",
                    "2026-10-15T00:00:00Z",
                    json.dumps({"cash_usd": 250, "per_trade_risk": 40}),
                ),
            )
            conn.commit()
            conn.close()
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, entry_price REAL,
                    qty REAL, size_usd REAL, peak_price REAL, meta_json TEXT
                );
                """,
                [
                    (
                        "hold", "closed", 5.0, "max_hold",
                        "2026-10-14T01:00:00Z", "2026-10-14T00:00:00Z",
                        1.0, 100.0, 100.0, 1.2, "{}",
                    ),
                    (
                        "sl", "closed", -8.0, "stop_loss",
                        "2026-10-14T02:00:00Z", "2026-10-14T00:30:00Z",
                        1.0, 100.0, 100.0, 1.1, "{}",
                    ),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        shadow = ev["c3_shadow"]
        self.assertEqual(shadow["status"], "ok")
        self.assertAlmostEqual(shadow["all"]["peak_mfe_usd_sum"], 30.0)
        self.assertAlmostEqual(shadow["all"]["exit_pnl_usd_sum"], -3.0)
        self.assertAlmostEqual(shadow["all"]["giveback_usd_sum"], 33.0)
        self.assertEqual(shadow["max_hold"]["n"], 1)
        self.assertAlmostEqual(shadow["max_hold"]["peak_mfe_usd_sum"], 20.0)
        self.assertAlmostEqual(shadow["max_hold"]["exit_pnl_usd_sum"], 5.0)
        self.assertAlmostEqual(shadow["max_hold"]["giveback_usd_sum"], 15.0)
        self.assertEqual(ev["g3_ruin"]["status"], "ok")
        self.assertEqual(ev["g3_ruin"]["ruin_count"], 6)
        self.assertIn("floor(cash/per_trade_risk)=6", ev["g3_ruin"]["line"])

    def test_daily_open_fallback_and_peak_veto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = sqlite3.connect(root / "execution_ledger.db")
            conn.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY, ts TEXT, executed INTEGER, strategy_id TEXT
                )
                """
            )
            for index in range(20):
                conn.execute(
                    "INSERT INTO execution_log (ts, executed, strategy_id) VALUES (?, 1, 'alpha')",
                    (f"2026-10-18T16:{index:02d}:00Z",),
                )
            conn.execute(
                "INSERT INTO execution_log (ts, executed, strategy_id) VALUES (?, 1, 'alpha')",
                ("2026-10-18T01:00:00Z",),
            )
            conn.commit()
            conn.close()
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("sl", "closed", -10.0, "stop_loss", "2026-10-14T01:00:00Z", "{}"),
                    ("mh", "closed", -90.0, "max_hold", "2026-10-14T02:00:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertEqual(ev["daily_open_peak"]["source"], "execution_log.executed")
        self.assertEqual(ev["daily_open_peak"]["peak"], 20)
        self.assertEqual(ev["daily_open_peak"]["peak_day"], "2026-10-19")
        self.assertAlmostEqual(ev["stop_loss"]["loss_share"], 0.1)
        self.assertTrue(ev["step2_veto"]["triggered"])
        self.assertEqual(ev["step2_veto"]["reasons"], ["daily_opens"])
        self.assertIn("单日开仓峰值 20", ev["step2_veto"]["line"])

    def test_veto_not_triggered_and_gates_unchanged(self) -> None:
        self.assertEqual(STOP_LOSS_SHARE_VETO, 0.50)
        self.assertEqual(MIN_RAW_N, 15)
        self.assertEqual(MIN_UNIQUE_TOKEN, 10)
        self.assertEqual(UP_1H_PROMOTE, 0.55)
        self.assertEqual(CONSECUTIVE_WEEKS, 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("sl", "closed", -10.0, "stop_loss", "2026-10-14T01:00:00Z", "2026-10-14T00:00:00Z", "{}"),
                    ("mh", "closed", -90.0, "max_hold", "2026-10-14T02:00:00Z", "2026-10-14T00:30:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertFalse(ev["step2_veto"]["triggered"])
        self.assertIn("未触发", ev["step2_veto"]["line"])
        self.assertIn("R2旁证", ev["step2_veto"]["line"])
        self.assertFalse(ev["counts_as_validity"])

    def test_report_section_renders_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("sl", "closed", -50.0, "stop_loss", "2026-10-14T01:00:00Z", "2026-10-14T00:00:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        md = render_weekly_md(
            period_start=START,
            period_end=END,
            chain=[],
            metrics={},
            baseline={},
            exec_summary={},
            weekly_evidence=ev,
        )
        self.assertIn("### 10-19 硬证据（不算有效性 / R2旁证）", md)
        self.assertIn("止损亏损占比", md)
        self.assertIn("100.0%", md)
        self.assertIn("data gap", md)
        self.assertIn("即使 1h≥55%", md)
        self.assertIn('"loss_share"', md)

    def test_run_report_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_db = sqlite3.connect(root / "dexscreener.db")
            main_db.executescript(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                    chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                    risk_passed INTEGER, mode TEXT, raw_json TEXT
                );
                CREATE TABLE indicator_history (
                    change_1h REAL, change_4h REAL, tier TEXT, recorded_at TEXT
                );
                """
            )
            main_db.commit()
            main_db.close()
            _touch_empty(root / "execution_ledger.db")
            _touch_empty(root / "backtest_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, entry_price REAL, meta_json TEXT
                );
                """,
                [
                    ("sl", "closed", -40.0, "stop_loss", "2026-10-14T01:00:00Z", "2026-10-14T00:00:00Z",
                     1.0, json.dumps({"strategy_id": "alpha"})),
                    ("tp", "closed", 10.0, "take_profit", "2026-10-14T02:00:00Z", "2026-10-14T00:10:00Z",
                     1.0, json.dumps({"strategy_id": "alpha"})),
                ],
            )
            out = root / "weekly.md"
            code = main(
                [
                    "--db-root", str(root),
                    "--start", START,
                    "--end", END,
                    "--report",
                    "--out", str(out),
                ]
            )
            text = out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("10-19 硬证据", text)
        self.assertIn("不算有效性 / R2旁证", text)
        self.assertIn("100.0%", text)


if __name__ == "__main__":
    unittest.main()
