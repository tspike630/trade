"""F-1 streak, C3/F-3 giveback shadow, G-3 bankruptcy count.

Exit defaults stay 10/5/60. F-1 does not reject orders.
Run from the repo root:

    python -m unittest execution.tests.test_f1_f3_g3
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

import execution.gateway as gateway_mod  # noqa: E402
from execution.tests.isolated_env import isolated_environ  # noqa: E402
from execution.close_observe import record_closed_position  # noqa: E402
from execution.config import f1_circuit_enabled  # noqa: E402
from execution.executors.paper_executor import (  # noqa: E402
    DEFAULT_MAX_HOLD_MIN as PAPER_HOLD,
)
from execution.executors.paper_executor import DEFAULT_SL_PCT as PAPER_SL  # noqa: E402
from execution.executors.paper_executor import DEFAULT_TP_PCT as PAPER_TP  # noqa: E402
from execution.executors.paper_executor import PaperExecutor  # noqa: E402
from execution.exits import (  # noqa: E402
    DEFAULT_MAX_HOLD_MIN,
    DEFAULT_SL_PCT,
    DEFAULT_TP_PCT,
    exit_reason,
)
from execution.f1_streak_report import build_report, main as report_main  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.risk import RISK_CHECKERS  # noqa: E402
from execution.risk.bankruptcy import (  # noqa: E402
    bankruptcy_report,
    max_sequential_losses,
    per_trade_risk_usd,
)
from execution.shadow_c3 import raise_peak, summarize_giveback  # noqa: E402
from execution.streak import (  # noqa: E402
    circuit_rejects_open,
    conditional_third_loss,
    next_streak,
    summarize_pnls,
)
from paper_trade import PaperLedger  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _py_env() -> dict[str, str]:
    env = isolated_environ(
        {"PYTHONPATH": os.pathsep.join([str(ROOT), str(CRAWLER)])}
    )
    return env


class _PriceDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(str(path))
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

    def add_price(self, ts: str, price: float, *, liquidity: float | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO pair_history (
                chain_id, pair_address, token_address, recorded_at, price_usd
            ) VALUES ('solana', 'pair1', 'token1', ?, ?)
            """,
            (ts, price),
        )
        raw: dict[str, object] = {"priceUsd": price}
        if liquidity is not None:
            raw["liquidity"] = {"usd": liquidity}
        self.conn.execute(
            """
            INSERT INTO pairs (
                chain_id, pair_address, token_address, raw_json, source, updated_at
            ) VALUES ('solana', 'pair1', 'token1', ?, 'test', ?)
            ON CONFLICT(chain_id, pair_address) DO UPDATE SET
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
            """,
            (json.dumps(raw), ts),
        )
        self.conn.commit()

    def add_decision(self, *, action: str, size_pct: float, strategy_id: str = "strat_a") -> int:
        raw = {
            "pair_address": "pair1",
            "extras": {},
            "purpose": "live_candidate",
        }
        cur = self.conn.execute(
            """
            INSERT INTO decision_log (
                ts, strategy_id, action, chain_id, token_address, size_pct,
                reason, mode, raw_json
            ) VALUES (?, ?, ?, 'solana', 'token1', ?, 'test', 'dry_run', ?)
            """,
            (_iso(_now()), strategy_id, action, size_pct, json.dumps(raw)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close(self) -> None:
        self.conn.close()


def _shadow_by_reason(db: Path, reason: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT * FROM shadow_c3 WHERE actual_reason = ? ORDER BY id",
                (reason,),
            ).fetchall()
        )
    finally:
        conn.close()


def _streak(db: Path, strategy_id: str) -> int | None:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT streak FROM strategy_streak WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return int(row[0])


class ExitDefaultTests(unittest.TestCase):
    def test_tp_sl_hold_defaults_unchanged(self) -> None:
        self.assertEqual(DEFAULT_TP_PCT, 10.0)
        self.assertEqual(DEFAULT_SL_PCT, 5.0)
        self.assertEqual(DEFAULT_MAX_HOLD_MIN, 60.0)
        self.assertEqual(PAPER_TP, 10.0)
        self.assertEqual(PAPER_SL, 5.0)
        self.assertEqual(PAPER_HOLD, 60.0)
        now = _now()
        opened = _iso(now)
        base = {
            "entry_price": 100.0,
            "tp_pct": 10.0,
            "sl_pct": 5.0,
            "max_hold_min": 60.0,
            "opened_at": opened,
        }
        self.assertEqual(exit_reason(base, 111.0, now), "take_profit")
        self.assertEqual(exit_reason(base, 94.0, now), "stop_loss")
        self.assertIsNone(exit_reason(base, 100.0, now))
        self.assertEqual(
            exit_reason(base, 100.0, now + timedelta(minutes=60)),
            "max_hold",
        )


class StreakMathTests(unittest.TestCase):
    def test_loss_increments_win_clears_flat_holds(self) -> None:
        self.assertEqual(next_streak(0, -1), 1)
        self.assertEqual(next_streak(2, -0.01), 3)
        self.assertEqual(next_streak(3, 0.5), 0)
        self.assertEqual(next_streak(4, 0.0), 4)
        replay = summarize_pnls([-1, -1, 0, -1, 2, -3])
        self.assertEqual(replay["max_streak"], 3)
        self.assertEqual(replay["current_streak"], 1)
        self.assertEqual(replay["n"], 6)

    def test_third_loss_given_two(self) -> None:
        # L L L W → windows (L,L,L) and (L,L,W)
        cond_n, hits, rate = conditional_third_loss([-1, -1, -1, 2])
        self.assertEqual(cond_n, 2)
        self.assertEqual(hits, 1)
        self.assertAlmostEqual(rate or 0.0, 0.5)
        empty_n, empty_hits, empty_rate = conditional_third_loss([-1, 1])
        self.assertEqual((empty_n, empty_hits, empty_rate), (0, 0, None))

    def test_persist_is_idempotent_per_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "exec.db"
            opened = _iso(_now() - timedelta(minutes=5))
            closed = _iso(_now())
            first = {
                "id": "p1",
                "meta_json": json.dumps({"strategy_id": "strat_a", "peak_price": 12}),
                "entry_price": 10.0,
                "exit_price": 8.0,
                "pnl_usd": -2.0,
                "opened_at": opened,
                "closed_at": closed,
                "exit_reason": "stop_loss",
            }
            wrote = record_closed_position(db, first, source="tick", mid_price=9.0)
            self.assertEqual(wrote["streak"], 1)
            self.assertTrue(wrote["inserted_streak"])
            again = record_closed_position(db, first, source="tick", mid_price=9.0)
            self.assertFalse(again["inserted_streak"])
            self.assertEqual(again["streak"], 1)
            second = dict(first)
            second["id"] = "p2"
            second["pnl_usd"] = -1.0
            record_closed_position(db, second, source="tick")
            self.assertEqual(_streak(db, "strat_a"), 2)
            win = dict(first)
            win["id"] = "p3"
            win["pnl_usd"] = 4.0
            win["exit_reason"] = "take_profit"
            record_closed_position(db, win, source="signal_sell")
            self.assertEqual(_streak(db, "strat_a"), 0)
            shadow = _shadow_by_reason(db, "stop_loss")
            self.assertEqual(len(shadow), 2)
            self.assertAlmostEqual(float(shadow[0]["peak_price"]), 12.0)
            self.assertAlmostEqual(float(shadow[0]["entry_price"]), 10.0)
            self.assertAlmostEqual(float(shadow[0]["exit_price"]), 8.0)
            self.assertAlmostEqual(float(shadow[0]["peak_pnl_pct"]), 20.0)
            self.assertAlmostEqual(float(shadow[0]["giveback_from_peak_pct"]), 40.0)
            self.assertAlmostEqual(float(shadow[0]["actual_pnl_usd"]), -2.0)
            self.assertAlmostEqual(float(shadow[0]["hold_min"]), 5.0, places=2)


class PeakTests(unittest.TestCase):
    def test_raise_peak_never_falls(self) -> None:
        meta, changed = raise_peak({}, 10)
        self.assertTrue(changed)
        self.assertEqual(meta["peak_price"], 10)
        higher, changed_up = raise_peak(meta, 12)
        self.assertTrue(changed_up)
        self.assertEqual(higher["peak_price"], 12)
        same, changed_same = raise_peak(higher, 11)
        self.assertFalse(changed_same)
        self.assertEqual(same["peak_price"], 12)

    def test_tick_updates_peak_and_close_writes_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = _PriceDB(root / "crawler.db")
            base = _now()
            prices.add_price(_iso(base), 105.0)
            prices.close()
            observe = root / "exec.db"
            with PaperExecutor(
                paper_db=root / "paper.db",
                crawler_db=root / "crawler.db",
                observe_db=observe,
            ) as ex:
                pid = ex.ledger.open_position(
                    chain_id="solana",
                    pair_address="pair1",
                    price=100.0,
                    size_usd=100.0,
                    tp_pct=10.0,
                    sl_pct=5.0,
                    max_hold_min=60.0,
                    meta={"strategy_id": "strat_a"},
                )
                self.assertEqual(ex.tick(), [])
                meta = json.loads(ex.ledger.get_position(pid)["meta_json"])
                self.assertAlmostEqual(meta["peak_price"], 105.0)

                writer = sqlite3.connect(str(root / "crawler.db"))
                writer.execute(
                    """
                    INSERT INTO pair_history (
                        chain_id, pair_address, recorded_at, price_usd
                    ) VALUES ('solana', 'pair1', ?, ?)
                    """,
                    (_iso(base + timedelta(seconds=1)), 103.0),
                )
                writer.commit()
                self.assertEqual(ex.tick(), [])
                meta = json.loads(ex.ledger.get_position(pid)["meta_json"])
                self.assertAlmostEqual(meta["peak_price"], 105.0)

                writer.execute(
                    """
                    INSERT INTO pair_history (
                        chain_id, pair_address, recorded_at, price_usd
                    ) VALUES ('solana', 'pair1', ?, ?)
                    """,
                    (_iso(base + timedelta(seconds=2)), 90.0),
                )
                writer.commit()
                closed = ex.tick()
                self.assertEqual(len(closed), 1)
                self.assertEqual(closed[0]["reason"], "stop_loss")
                self.assertEqual(_streak(observe, "strat_a"), 1)
                row = _shadow_by_reason(observe, "stop_loss")[0]
                self.assertAlmostEqual(float(row["peak_price"]), 105.0)
                self.assertAlmostEqual(float(row["entry_price"]), 100.0)
                self.assertAlmostEqual(float(row["peak_pnl_pct"]), 5.0)
                self.assertAlmostEqual(float(row["giveback_from_peak_pct"]), 16.8)
                self.assertLess(float(row["actual_pnl_usd"]), 0.0)
                self.assertIsNotNone(row["hold_min"])

                writer.execute(
                    """
                    INSERT INTO pair_history (
                        chain_id, pair_address, recorded_at, price_usd
                    ) VALUES ('solana', 'pair1', ?, ?)
                    """,
                    (_iso(base + timedelta(seconds=3)), 130.0),
                )
                writer.commit()
                ex.ledger.open_position(
                    chain_id="solana",
                    pair_address="pair1",
                    price=100.0,
                    size_usd=100.0,
                    tp_pct=10.0,
                    sl_pct=5.0,
                    max_hold_min=60.0,
                    meta={"strategy_id": "strat_a"},
                )
                took = ex.tick()
                writer.close()
                self.assertEqual(took[0]["reason"], "take_profit")
                self.assertEqual(_streak(observe, "strat_a"), 0)
                win = _shadow_by_reason(observe, "take_profit")[0]
                self.assertAlmostEqual(float(win["peak_price"]), 130.0)
                self.assertGreater(float(win["actual_pnl_usd"]), 0.0)


class CircuitOffTests(unittest.TestCase):
    def test_circuit_flag_never_rejects(self) -> None:
        os.environ.pop("EXECUTION_F1_CIRCUIT", None)
        self.assertFalse(f1_circuit_enabled())
        self.assertFalse(circuit_rejects_open("buy", 9))
        self.assertFalse(circuit_rejects_open("sell", 9))
        os.environ["EXECUTION_F1_CIRCUIT"] = "1"
        try:
            self.assertTrue(f1_circuit_enabled())
            self.assertFalse(circuit_rejects_open("buy", 99))
            self.assertFalse(circuit_rejects_open("sell", 99))
        finally:
            os.environ.pop("EXECUTION_F1_CIRCUIT", None)
        names = [name for name, _fn in RISK_CHECKERS]
        self.assertFalse(any("f1" in name or "streak" in name for name in names))

    def test_gateway_buy_and_sell_ignore_streak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = _PriceDB(root / "crawler.db")
            prices.add_price(_iso(_now()), 10.0, liquidity=1_000_000)
            buy_id = prices.add_decision(action="buy", size_pct=0.05)
            sell_id = prices.add_decision(action="sell", size_pct=1.0)
            prices.close()
            observe = root / "exec.db"
            opened = _iso(_now() - timedelta(minutes=1))
            closed_at = _iso(_now())
            for idx, pnl in enumerate((-1.0, -2.0)):
                record_closed_position(
                    observe,
                    {
                        "id": f"seed-{idx}",
                        "meta_json": json.dumps({"strategy_id": "strat_a"}),
                        "entry_price": 10.0,
                        "exit_price": 9.0,
                        "pnl_usd": pnl,
                        "opened_at": opened,
                        "closed_at": closed_at,
                        "exit_reason": "stop_loss",
                    },
                    source="tick",
                )
            self.assertEqual(_streak(observe, "strat_a"), 2)
            os.environ["EXECUTION_F1_CIRCUIT"] = "1"
            prev_risk = gateway_mod.EXECUTION_RISK_ENABLED
            prev_loss = gateway_mod.EXECUTION_DAILY_LOSS_USD
            gateway_mod.EXECUTION_RISK_ENABLED = True
            gateway_mod.EXECUTION_DAILY_LOSS_USD = None
            try:
                with ExecutionGateway(
                    decision_db=root / "crawler.db",
                    ledger_path=observe,
                    paper_enabled=True,
                    paper_db=root / "paper.db",
                    pm_enabled=False,
                ) as gw:
                    gw.process_pending(since_id=0)
                    rows = {r["decision_log_id"]: r for r in gw.ledger.recent(limit=10)}
                    paper = gw._get_paper()
                    opens = paper.ledger.list_open()
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev_risk
                gateway_mod.EXECUTION_DAILY_LOSS_USD = prev_loss
                os.environ.pop("EXECUTION_F1_CIRCUIT", None)
            buy = json.loads(rows[buy_id]["receipt_json"])
            sell = json.loads(rows[sell_id]["receipt_json"])
            self.assertEqual(buy["status"], "paper_executed")
            self.assertEqual(sell["status"], "paper_executed")
            self.assertNotIn("f1", buy.get("error") or "")
            self.assertNotIn("streak", json.dumps(buy))
            self.assertEqual(opens, [])
            self.assertEqual(rows[sell_id]["executor"], "paper")
            self.assertEqual(_streak(observe, "strat_a"), 3)
            sold = _shadow_by_reason(observe, "signal_sell")
            self.assertEqual(len(sold), 1)
            self.assertIsNotNone(sold[0]["peak_price"])
            self.assertIn("giveback_from_peak_pct", sold[0].keys())


class BankruptcyTests(unittest.TestCase):
    def test_floor_formula(self) -> None:
        self.assertEqual(max_sequential_losses(300, 30), 10)
        self.assertAlmostEqual(per_trade_risk_usd(500, 5), 25.0)
        self.assertEqual(max_sequential_losses(10_000, per_trade_risk_usd(500, 5)), 400)
        self.assertEqual(max_sequential_losses(29, 30), 0)
        self.assertEqual(max_sequential_losses(0, 30), 0)
        with self.assertRaises(ValueError):
            max_sequential_losses(100, 0)
        with self.assertRaises(ValueError):
            max_sequential_losses(-1, 30)
        row = bankruptcy_report()
        self.assertEqual(row["sl_pct"], 5.0)
        self.assertAlmostEqual(row["size_usd"], 500.0)
        self.assertEqual(row["max_sequential_losses"], 400)
        before = os.environ.get("EXECUTION_DAILY_LOSS_USD")
        bankruptcy_report(capital=300, size_usd=600, sl_pct=5)
        self.assertEqual(os.environ.get("EXECUTION_DAILY_LOSS_USD"), before)

    def test_cli_prints_count(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "execution.risk.bankruptcy",
                "--capital",
                "300",
                "--size-usd",
                "600",
                "--sl-pct",
                "5",
            ],
            cwd=str(ROOT),
            env=_py_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("max_sequential_losses=10", proc.stdout)
        self.assertIn("EXECUTION_DAILY_LOSS_USD unchanged", proc.stdout)


class ReportTests(unittest.TestCase):
    def test_report_and_shadow_summary_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "exec.db"
            paper = root / "paper.db"
            opened = "2026-10-14T00:00:00Z"
            closes = [-1.0, -1.0, -1.0, 2.0]
            book = PaperLedger(paper)
            try:
                for idx, pnl_sign in enumerate(closes):
                    # size 100, close price chosen so pnl matches the sign sequence.
                    exit_px = 9.0 if pnl_sign < 0 else 12.0
                    pid = book.open_position(
                        chain_id="solana",
                        pair_address=f"pair{idx}",
                        price=10.0,
                        size_usd=100.0,
                        meta={"strategy_id": "strat_a"},
                    )
                    book.close_position(pid, price=exit_px, reason="stop_loss")
                    record_closed_position(
                        ledger,
                        {
                            "id": f"e{idx}",
                            "meta_json": json.dumps(
                                {"strategy_id": "strat_a", "peak_price": 11.0}
                            ),
                            "entry_price": 10.0,
                            "exit_price": exit_px,
                            "pnl_usd": pnl_sign,
                            "opened_at": opened,
                            "closed_at": f"2026-10-14T00:0{idx}:00Z",
                            "exit_reason": "stop_loss" if pnl_sign < 0 else "take_profit",
                        },
                        source="tick",
                    )
            finally:
                book.close()
            report = build_report(ledger=ledger, paper=paper)
            self.assertFalse(report["rejects_orders"])
            self.assertFalse(report["f1_circuit_wired"])
            self.assertEqual(report["ledger"]["samples"], 4)
            self.assertEqual(report["ledger"]["longest_loss_streak"], 3)
            self.assertAlmostEqual(report["ledger"]["p_third_loss_given_streak_ge2"], 0.5)
            self.assertEqual(report["paper_crosscheck"]["samples"], 4)
            self.assertEqual(report["paper_crosscheck"]["longest_loss_streak"], 3)
            self.assertAlmostEqual(
                report["paper_crosscheck"]["p_third_loss_given_streak_ge2"],
                0.5,
            )
            buckets = summarize_giveback(
                [
                    {
                        "actual_reason": "stop_loss",
                        "giveback_from_peak_pct": 10.0,
                        "peak_pnl_pct": 5.0,
                        "actual_pnl_usd": -1.0,
                    },
                    {
                        "actual_reason": "stop_loss",
                        "giveback_from_peak_pct": 30.0,
                        "peak_pnl_pct": 15.0,
                        "actual_pnl_usd": -3.0,
                    },
                ]
            )
            self.assertEqual(buckets[0]["n"], 2)
            self.assertAlmostEqual(buckets[0]["mean_giveback_from_peak_pct"], 20.0)

            missing = root / "missing.db"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = report_main(["--ledger", str(missing), "--json"])
            self.assertEqual(code, 0)
            self.assertFalse(missing.exists())
            self.assertTrue(json.loads(buf.getvalue())["ledger"]["missing"])
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution.f1_streak_report",
                    "--ledger",
                    str(ledger),
                    "--paper",
                    str(paper),
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
            self.assertEqual(payload["ledger"]["longest_loss_streak"], 3)
            self.assertEqual(payload["ledger"]["samples"], 4)
            shadow = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execution.shadow_c3_report",
                    "--ledger",
                    str(ledger),
                    "--json",
                ],
                cwd=str(ROOT),
                env=_py_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(shadow.returncode, 0, shadow.stderr)
            body = json.loads(shadow.stdout)
            reasons = {row["actual_reason"]: row for row in body["by_reason"]}
            self.assertEqual(reasons["stop_loss"]["n"], 3)
            self.assertIn("mean_giveback_from_peak_pct", reasons["stop_loss"])


if __name__ == "__main__":
    unittest.main()
