"""10-19 weekly evidence: two F-1 lines, shadow_c3, and ruin exports."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from execution.exits import DEFAULT_MAX_HOLD_MIN, DEFAULT_SL_PCT, DEFAULT_TP_PCT
from execution.risk.bankruptcy import (
    DEFAULT_CAPITAL_USD,
    DEFAULT_SIZE_PCT,
    bankruptcy_report,
)
from execution.streak import circuit_rejects_open
from review.data import DbPaths
from review.gates import (
    CONSECUTIVE_WEEKS,
    MIN_RAW_N,
    MIN_UNIQUE_TOKEN,
    UP_1H_DEGRADE,
    UP_1H_PROMOTE,
)
from review.report import render_weekly_md
from review.run import main
from review.weekly_evidence import DAILY_OPEN_PEAK_VETO, STOP_LOSS_SHARE_VETO, compute_weekly_evidence

START = "2026-10-12T00:00:00Z"
END = "2026-10-20T00:00:00Z"

# L,L,L,W, L,L,L,W, L,L,L → 3 hits / 5 trials under execution.conditional_third_loss.
_REALIZED_P60 = [-1.0, -1.0, -1.0, 1.0, -1.0, -1.0, -1.0, 1.0, -1.0, -1.0, -1.0]


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


def _streak_events(path: Path, rows: list[tuple[str, float]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE streak_events (
                id INTEGER PRIMARY KEY,
                strategy_id TEXT,
                pnl_usd REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO streak_events (strategy_id, pnl_usd) VALUES (?, ?)",
            rows,
        )
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
        # Net book -80, stop bucket -100 → share capped at 1.
        self.assertAlmostEqual(stop["total_pnl_usd"], -80.0)
        self.assertAlmostEqual(stop["stop_loss_pnl_usd"], -100.0)
        self.assertAlmostEqual(stop["loss_share"], 1.0)
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
        self.assertEqual(ev["step2_veto"]["reasons"], ["stop_loss_share"])
        self.assertIn("即使 1h≥55%", ev["step2_veto"]["line"])
        self.assertIn("不算有效性 / R2旁证", ev["step2_veto"]["line"])
        self.assertEqual(ev["c3_shadow"]["status"], "data_gap")
        self.assertIn("not invented", ev["c3_shadow"]["note"])
        self.assertEqual(ev["g3_ruin"]["kind"], "cli_defaults")
        self.assertEqual(ev["g3_ruin"]["ruin_count"], bankruptcy_report()["max_sequential_losses"])
        self.assertEqual(ev["f1_streak"]["up_1h"]["status"], "data_gap")
        self.assertEqual(ev["f1_streak"]["realized_pnl"]["status"], "data_gap")
        self.assertNotIn("by_strategy", ev["f1_streak"])
        self.assertNotIn("p_third_loss_given_two_loss", ev["f1_streak"])

    def test_net_positive_share_is_null(self) -> None:
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
                    ("tp", "closed", 40.0, "take_profit", "2026-10-14T02:00:00Z", "2026-10-14T00:30:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertIsNone(ev["stop_loss"]["loss_share"])
        self.assertAlmostEqual(ev["stop_loss"]["total_pnl_usd"], 30.0)
        self.assertFalse(ev["step2_veto"]["triggered"])

    def test_hk_tie_breaks_to_later_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, opened_at TEXT, meta_json TEXT
                );
                """,
                [
                    ("a", "open", "2026-10-18T02:00:00Z", "{}"),
                    ("b", "open", "2026-10-18T03:00:00Z", "{}"),
                    ("c", "open", "2026-10-18T16:30:00Z", "{}"),
                    ("d", "open", "2026-10-18T16:40:00Z", "{}"),
                ],
            )
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        daily = ev["daily_open_peak"]
        self.assertEqual(daily["peak"], 2)
        self.assertEqual(daily["peak_day"], "2026-10-19")

    def test_two_f1_lines_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _streak_events(
                root / "execution_ledger.db",
                [("alpha", pnl) for pnl in _REALIZED_P60] + [("beta", 5.0)],
            )
            # Paper wins must not replace the ledger line.
            _paper(
                root / "paper_trading.db",
                """
                CREATE TABLE positions (
                    id TEXT, status TEXT, pnl_usd REAL, exit_reason TEXT,
                    closed_at TEXT, opened_at TEXT, entry_price REAL, meta_json TEXT
                );
                """,
                [
                    (
                        "win", "closed", 9.0, "take_profit",
                        "2026-10-13T01:00:00Z", "2026-10-13T01:00:00Z", 1.0,
                        json.dumps({"strategy_id": "alpha"}),
                    ),
                ],
            )
            ev = compute_weekly_evidence(
                _paths(root),
                period_start=START,
                period_end=END,
                up_1h_rows=[
                    {"strategy_id": "alpha", "ts": "2026-10-13T01:00:00Z", "decision_id": 1, "fwd_1h": -0.02},
                    {"strategy_id": "alpha", "ts": "2026-10-13T02:00:00Z", "decision_id": 2, "fwd_1h": -0.01},
                    {"strategy_id": "alpha", "ts": "2026-10-13T03:00:00Z", "decision_id": 3, "fwd_1h": -0.03},
                    {"strategy_id": "beta", "ts": "2026-10-13T01:00:00Z", "decision_id": 4, "fwd_1h": -0.1},
                    {"strategy_id": "beta", "ts": "2026-10-13T02:00:00Z", "decision_id": 5, "fwd_1h": 0.0},
                    {"strategy_id": "beta", "ts": "2026-10-13T03:00:00Z", "decision_id": 6, "fwd_1h": -0.1},
                ],
            )
        up = ev["f1_streak"]["up_1h"]
        realized = ev["f1_streak"]["realized_pnl"]
        self.assertEqual(up["status"], "ok")
        self.assertEqual(up["source"], "decision_up_1h")
        self.assertEqual(realized["status"], "ok")
        self.assertEqual(realized["source"], "execution.f1_streak_report")
        self.assertAlmostEqual(up["by_strategy"]["alpha"]["p_third_loss_given_two_loss"], 1.0)
        self.assertEqual(up["by_strategy"]["alpha"]["n_given_two_loss"], 1)
        self.assertIsNone(up["by_strategy"]["beta"]["p_third_loss_given_two_loss"])
        self.assertEqual(up["by_strategy"]["beta"]["n_given_two_loss"], 0)
        self.assertAlmostEqual(realized["by_strategy"]["alpha"]["p_third_loss_given_two_loss"], 0.60)
        self.assertEqual(realized["by_strategy"]["alpha"]["n_given_two_loss"], 5)
        self.assertEqual(realized["by_strategy"]["alpha"]["n_third_loss"], 3)
        self.assertNotIn("p_third_loss_given_streak_ge2", realized)
        self.assertNotEqual(
            up["by_strategy"]["alpha"]["p_third_loss_given_two_loss"],
            realized["by_strategy"]["alpha"]["p_third_loss_given_two_loss"],
        )

    def test_realized_zero_pnl_is_not_a_loss_and_missing_up_1h_is_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _streak_events(
                root / "execution_ledger.db",
                [("alpha", -1.0), ("alpha", 0.0), ("alpha", -1.0)],
            )
            _touch_empty(root / "paper_trading.db")
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertEqual(ev["f1_streak"]["up_1h"]["status"], "data_gap")
        self.assertIn("up_1h unavailable", ev["f1_streak"]["up_1h"]["note"])
        row = ev["f1_streak"]["realized_pnl"]["by_strategy"]["alpha"]
        self.assertEqual(ev["f1_streak"]["realized_pnl"]["status"], "ok")
        self.assertEqual(row["n_given_two_loss"], 0)
        self.assertIsNone(row["p_third_loss_given_two_loss"])

    def test_c3_order_events_beat_table_and_paper_peak(self) -> None:
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
                """
                CREATE TABLE shadow_c3 (
                    id INTEGER PRIMARY KEY,
                    strategy_id TEXT,
                    peak_pnl_pct REAL,
                    giveback_from_peak_pct REAL,
                    actual_pnl_usd REAL,
                    actual_reason TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO order_events (event, event_ts, detail_json) VALUES (?, ?, ?)",
                (
                    "filled",
                    "2026-10-15T00:00:00Z",
                    json.dumps(
                        {
                            "shadow_c3": {
                                "actual_reason": "max_hold",
                                "peak_pnl_pct": 12.0,
                                "giveback_from_peak_pct": 4.0,
                                "actual_pnl_usd": 5.0,
                            }
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO order_events (event, event_ts, detail_json) VALUES (?, ?, ?)",
                (
                    "shadow_c3",
                    "2026-10-15T01:00:00Z",
                    json.dumps(
                        {
                            "actual_reason": "stop_loss",
                            "peak_pnl_pct": 2.0,
                            "giveback_from_peak_pct": 8.0,
                            "actual_pnl_usd": -8.0,
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO order_events (event, event_ts, detail_json) VALUES (?, ?, ?)",
                ("filled", "2026-10-15T02:00:00Z", json.dumps({"cash_usd": 250, "per_trade_risk": 40})),
            )
            conn.execute(
                """
                INSERT INTO shadow_c3 (
                    strategy_id, peak_pnl_pct, giveback_from_peak_pct, actual_pnl_usd, actual_reason
                ) VALUES ('alpha', 100, 100, 100, 'max_hold')
                """
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
        self.assertEqual(shadow["source"], "order_events.shadow_c3")
        self.assertAlmostEqual(shadow["all"]["mean_peak_pnl_pct"], 7.0)
        self.assertAlmostEqual(shadow["all"]["mean_giveback_from_peak_pct"], 6.0)
        self.assertAlmostEqual(shadow["all"]["mean_actual_pnl_usd"], -1.5)
        self.assertEqual(shadow["max_hold"]["n"], 1)
        self.assertAlmostEqual(shadow["max_hold"]["mean_peak_pnl_pct"], 12.0)
        self.assertEqual(ev["g3_ruin"]["kind"], "export")
        self.assertEqual(ev["g3_ruin"]["ruin_count"], 6)
        self.assertIn("floor(cash/per_trade_risk)=6", ev["g3_ruin"]["line"])

    def test_c3_table_when_order_events_have_no_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = sqlite3.connect(root / "execution_ledger.db")
            conn.execute(
                """
                CREATE TABLE shadow_c3 (
                    id INTEGER PRIMARY KEY,
                    strategy_id TEXT,
                    peak_pnl_pct REAL,
                    giveback_from_peak_pct REAL,
                    actual_pnl_usd REAL,
                    actual_reason TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO shadow_c3 (
                    strategy_id, peak_pnl_pct, giveback_from_peak_pct, actual_pnl_usd, actual_reason
                ) VALUES ('alpha', 10, 3, 1, 'max_hold')
                """
            )
            conn.commit()
            conn.close()
            _touch_empty(root / "paper_trading.db")
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertEqual(ev["c3_shadow"]["source"], "shadow_c3")
        self.assertEqual(ev["c3_shadow"]["max_hold"]["n"], 1)
        self.assertAlmostEqual(ev["c3_shadow"]["all"]["mean_giveback_from_peak_pct"], 3.0)
        self.assertIn("order_events", ev["c3_shadow"]["note"])

    def test_g3_export_beats_cli_defaults(self) -> None:
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
                ("filled", "2026-10-15T00:00:00Z", json.dumps({"max_sequential_losses": 7})),
            )
            conn.commit()
            conn.close()
            _touch_empty(root / "paper_trading.db")
            ev = compute_weekly_evidence(_paths(root), period_start=START, period_end=END)
        self.assertEqual(ev["g3_ruin"]["kind"], "export")
        self.assertEqual(ev["g3_ruin"]["ruin_count"], 7)
        self.assertEqual(bankruptcy_report()["max_sequential_losses"], 400)
        self.assertNotEqual(ev["g3_ruin"]["ruin_count"], 400)

    def test_missing_opened_at_does_not_use_executed_entries(self) -> None:
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
        self.assertEqual(ev["daily_open_peak"]["source"], "data_gap")
        self.assertIsNone(ev["daily_open_peak"]["peak"])
        self.assertIn("UTC date prefix", ev["daily_open_peak"]["note"])
        self.assertAlmostEqual(ev["stop_loss"]["loss_share"], 0.1)
        self.assertFalse(ev["step2_veto"]["triggered"])

    def test_opened_at_peak_veto_without_stop_share(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _touch_empty(root / "execution_ledger.db")
            rows = [
                ("sl", "closed", -10.0, "stop_loss", "2026-10-14T01:00:00Z", "2026-10-18T01:00:00Z", "{}"),
                ("mh", "closed", -90.0, "max_hold", "2026-10-14T02:00:00Z", "2026-10-18T01:10:00Z", "{}"),
            ]
            for index in range(20):
                rows.append(
                    (
                        f"o{index}", "open", None, None, None,
                        f"2026-10-18T16:{index:02d}:00Z", "{}",
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
        self.assertEqual(ev["daily_open_peak"]["source"], "paper.positions.opened_at")
        self.assertEqual(ev["daily_open_peak"]["peak"], 20)
        self.assertEqual(ev["daily_open_peak"]["peak_day"], "2026-10-19")
        self.assertAlmostEqual(ev["stop_loss"]["loss_share"], 0.1)
        self.assertTrue(ev["step2_veto"]["triggered"])
        self.assertEqual(ev["step2_veto"]["reasons"], ["daily_opens"])
        self.assertIn("单日开仓峰值 20", ev["step2_veto"]["line"])

    def test_veto_not_triggered_and_gates_unchanged(self) -> None:
        self.assertEqual(STOP_LOSS_SHARE_VETO, 0.50)
        self.assertEqual(DAILY_OPEN_PEAK_VETO, 20)
        self.assertEqual(MIN_RAW_N, 15)
        self.assertEqual(MIN_UNIQUE_TOKEN, 10)
        self.assertEqual(UP_1H_PROMOTE, 0.55)
        self.assertEqual(UP_1H_DEGRADE, 0.50)
        self.assertEqual(CONSECUTIVE_WEEKS, 2)
        self.assertEqual(DEFAULT_TP_PCT, 10.0)
        self.assertEqual(DEFAULT_SL_PCT, 5.0)
        self.assertEqual(DEFAULT_MAX_HOLD_MIN, 60.0)
        self.assertEqual(DEFAULT_CAPITAL_USD, 10000.0)
        self.assertEqual(DEFAULT_SIZE_PCT, 0.05)
        self.assertFalse(circuit_rejects_open("buy", 99))
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

    def test_report_section_renders_both_f1_lines(self) -> None:
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
            ev = compute_weekly_evidence(
                _paths(root),
                period_start=START,
                period_end=END,
                up_1h_rows=[
                    {"strategy_id": "alpha", "ts": "2026-10-13T01:00:00Z", "decision_id": 1, "fwd_1h": -0.01},
                    {"strategy_id": "alpha", "ts": "2026-10-13T02:00:00Z", "decision_id": 2, "fwd_1h": -0.02},
                    {"strategy_id": "alpha", "ts": "2026-10-13T03:00:00Z", "decision_id": 3, "fwd_1h": -0.03},
                ],
            )
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
        self.assertIn("F-1（a）up_1h / 决策口径", md)
        self.assertIn("F-1（b）已实现 PnL", md)
        self.assertIn("data gap", md)
        self.assertIn("即使 1h≥55%", md)
        self.assertIn('"loss_share"', md)
        self.assertIn('"realized_pnl"', md)
        self.assertIn("CLI defaults", md)

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
        self.assertIn("F-1（a）", text)
        self.assertIn("F-1（b）", text)


if __name__ == "__main__":
    unittest.main()
