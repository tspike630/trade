"""Fade rolling buy frequency. Not a concurrent-holdings cap. No live HTTP.

    python -m unittest execution.tests.test_fade_buy_frequency
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    classify_fade_slot,
)
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402


class _Probe:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result or {
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "error": "probe",
            "consumer_allowed": False,
        }
        self.buy_blocks: list[dict[str, Any]] = []
        self.peer_buy_blocks: list[dict[str, Any]] = []
        self.canonical_rows: list[dict[str, Any]] = []

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(sig)
        return dict(self.result)


def _row(*, decision_id: int, purpose: str, action: str = "buy", mode: str = "live_stub", symbol: str = "BTCUSDT") -> dict[str, Any]:
    extras = {"binance_symbol": symbol, "quote_order_qty": "25"}
    return {
        "id": decision_id,
        "ts": "2026-11-07T00:00:00Z",
        "strategy_id": "tech_boll_fade_v1",
        "action": action,
        "chain_id": "binance",
        "token_address": symbol,
        "pair_address": f"{symbol[:-4]}/USDT",
        "size_pct": 0.05,
        "reason": "frequency",
        "risk_passed": 1,
        "mode": mode,
        "purpose": purpose,
        "raw_json": json.dumps({"pair_address": f"{symbol[:-4]}/USDT", "purpose": purpose, "extras": extras}),
    }


def _env(root: Path) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "LIVE_KILL_SWITCH_PATH": str(root / "absent-kill"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "BINANCE_SPOT_PRODUCTION_BOOK": "",
        "BINANCE_SPOT_SYMBOL_COOLDOWN_MIN": "",
        "BINANCE_SPOT_WINDOW_MIN": "",
        "BINANCE_SPOT_MAX_OPEN": "",
    }


class FadeFrequencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _ledger(self) -> ExecutionLedger:
        return ExecutionLedger(self.root / "execution_ledger_live_bnb_boll_fade.db")

    def _seed(self, ledger: ExecutionLedger, decision_id: int, symbol: str, state: str, age: timedelta) -> None:
        when = self.now - age
        self.assertIsNone(
            ledger.admit_fade_buy_frequency(
                decision_log_id=decision_id, symbol=symbol, now=when
            )
        )
        ledger.settle_fade_buy_slot(decision_id, state, now=when)

    def _gate(self, adapter: _Probe, purpose: str) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
            paper_enabled=False,
            pm_enabled=False,
            accept_purposes=frozenset({purpose}),
            binance_spot=adapter,
        )

    def test_three_fills_in_two_hours_and_same_symbol_in_four_hours(self) -> None:
        ledger = self._ledger()
        for index, symbol in enumerate(("ETHUSDT", "BNBUSDT", "SOLUSDT"), start=1):
            self._seed(ledger, index, symbol, "filled", timedelta(minutes=30))
        probe = _Probe()
        env = _env(self.root)
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                blocked = gw.process_one(
                    _row(decision_id=4, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ADAUSDT")
                )
        self.assertEqual(blocked["status"], "pm_rejected")
        self.assertEqual(blocked["error"], "bnb_spot_max_open_reached")
        self.assertEqual(blocked["receipt"]["status"], "pm_rejected")
        self.assertEqual(probe.calls, [])

    def test_symbol_cooldown_and_exact_boundaries(self) -> None:
        ledger = self._ledger()
        self._seed(ledger, 11, "BTCUSDT", "filled", timedelta(minutes=200))
        probe = _Probe()
        env = _env(self.root)
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                cooled = gw.process_one(
                    _row(decision_id=12, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BTCUSDT")
                )
                other = gw.process_one(
                    _row(decision_id=13, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ETHUSDT")
                )
        self.assertEqual(cooled["error"], "bnb_spot_symbol_cooldown")
        self.assertEqual(other["error"], "probe")
        self.assertEqual(len(probe.calls), 1)

        edge = ExecutionLedger(self.root / "edge.db")
        moment = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            edge.admit_fade_buy_frequency(
                decision_log_id=1,
                symbol="BTCUSDT",
                now=moment - timedelta(minutes=240),
            )
        )
        edge.settle_fade_buy_slot(1, "filled", now=moment - timedelta(minutes=240))
        self.assertEqual(
            edge.admit_fade_buy_frequency(decision_log_id=2, symbol="BTCUSDT", now=moment),
            "bnb_spot_symbol_cooldown",
        )
        self.assertIsNone(
            edge.admit_fade_buy_frequency(
                decision_log_id=3,
                symbol="BTCUSDT",
                now=moment + timedelta(seconds=1),
            )
        )
        window = ExecutionLedger(self.root / "window.db")
        for index, symbol in enumerate(("ETHUSDT", "BNBUSDT", "SOLUSDT"), start=10):
            self.assertIsNone(
                window.admit_fade_buy_frequency(
                    decision_log_id=index,
                    symbol=symbol,
                    now=moment - timedelta(minutes=120),
                )
            )
            window.settle_fade_buy_slot(index, "filled", now=moment - timedelta(minutes=120))
        self.assertEqual(
            window.admit_fade_buy_frequency(decision_log_id=20, symbol="ADAUSDT", now=moment),
            "bnb_spot_max_open_reached",
        )
        self.assertIsNone(
            window.admit_fade_buy_frequency(
                decision_log_id=21,
                symbol="ADAUSDT",
                now=moment + timedelta(minutes=120, seconds=1),
            )
        )

    def test_invalid_config_and_sell_are_not_the_rolling_cap(self) -> None:
        probe = _Probe()
        env = _env(self.root)
        env["BINANCE_SPOT_MAX_OPEN"] = "nope"
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                invalid = gw.process_one(
                    _row(decision_id=9, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="ADAUSDT")
                )
        self.assertEqual(invalid["error"], "bnb_spot_frequency_config_invalid")
        self.assertEqual(probe.calls, [])

        env["BINANCE_SPOT_MAX_OPEN"] = ""
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                sold = gw.process_one(
                    _row(
                        decision_id=10,
                        purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                        action="sell",
                        symbol="BTCUSDT",
                    )
                )
        self.assertEqual(len(probe.calls), 1)
        self.assertNotEqual(sold["error"], "bnb_spot_max_open_reached")

    def test_pending_slots_partial_fill_and_classic_untouched(self) -> None:
        ledger = self._ledger()
        for index in range(1, 4):
            self._seed(ledger, index, f"P{index}USDT", "pending", timedelta(minutes=5))
        probe = _Probe()
        env = _env(self.root)
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                blocked = gw.process_one(
                    _row(decision_id=4, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="XRPUSDT")
                )
        self.assertEqual(blocked["error"], "bnb_spot_max_open_reached")
        ledger.settle_fade_buy_slot(3, "release", now=self.now)
        probe.result = {
            "ok": False,
            "dry_run": False,
            "executed": 1,
            "submitted": False,
            "error": "zero",
            "filled_qty": None,
            "consumer_allowed": False,
        }
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                ignored = gw.process_one(
                    _row(decision_id=5, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="XRPUSDT")
                )
        self.assertEqual(ignored["error"], "zero")
        self.assertEqual(len(probe.calls), 1)
        self.assertEqual(ledger._fade_slot(5)["state"], "released")
        probe.result = {
            "ok": True,
            "dry_run": False,
            "executed": 0,
            "submitted": True,
            "error": None,
            "filled_qty": "0.4",
            "consumer_allowed": False,
            "maker_slot": None,
        }
        with mock.patch.dict(os.environ, env):
            with self._gate(probe, PURPOSE_LIVE_BNB_BOLL_FADE) as gw:
                gw.process_one(
                    _row(decision_id=6, purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="XRPUSDT")
                )
        self.assertEqual(ledger._fade_slot(6)["state"], "filled")
        ledger.settle_fade_buy_slot(6, "filled", now=self.now)
        self.assertEqual(ledger._fade_slot(6)["state"], "filled")
        active = [
            row
            for row in ledger.conn.execute(
                "SELECT decision_log_id, state FROM binance_spot_fade_buy_slots"
            ).fetchall()
            if row["state"] in {"filled", "pending", "reserved"} and row["decision_log_id"] == 6
        ]
        self.assertEqual(len(active), 1)
        self.assertEqual(classify_fade_slot({"executed": 1}), "release")
        self.assertEqual(classify_fade_slot({"filled_qty": "0.4", "executed": 0}), "filled")
        self.assertEqual(classify_fade_slot({"maker_slot": "pending", "filled_qty": "0"}), "pending")

        classic = _Probe()
        with mock.patch.dict(os.environ, env):
            with self._gate(classic, PURPOSE_LIVE_BNB_BOLL_CLASSIC) as gw:
                gw.process_one(
                    _row(decision_id=7, purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC, symbol="BTCUSDT")
                )
        self.assertEqual(len(classic.calls), 1)
        self.assertIsNone(ledger._fade_slot(7))
