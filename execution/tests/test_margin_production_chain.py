"""Explicit account route through DecisionEngine.tick and process_pending.

The default tick stays on the spot adapter. An explicit offline fixture with
a caller-supplied approval reaches CrossMarginTrack.open_position. Missing
approval, an illegal domain, a failed phase, and a dual submit do not.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import decision_engine  # noqa: F401
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import scan_limit_for_purpose
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.core.token_cooldown import TokenCooldown
from decision_engine.run import build_production_engine
from decision_engine.strategies import get_strategy
from decision_engine.tests.quiet_macro import write_quiet_macro
from execution.executors.binance_spot_gate import REPLAY_FLOOR_DECISION_ID
from execution.gateway import ExecutionGateway
from execution.margin_cross.fixture import short_case
from execution.margin_cross.risk import OFFLINE_FIXTURE_STRATEGY_ID
from execution.tests.test_account_dispatch import _offline_approval


class _SpotAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _sig: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"ok": False, "submitted": False, "dry_run": True, "error": "spot-probe"}


def _borrow_count(result: dict[str, Any]) -> int:
    writes = (result.get("exec_result") or {}).get("writes") or []
    return sum(1 for name in writes if name == "borrow")


class MarginProductionChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "PUBLIC_DERIVATIVES_DB_PATH",
                "NEWS_EVENTS_DB_PATH",
                "LIVE_BNB_FACTOR_SOURCE",
                "CROSS_MARGIN_PARAMETERS_APPROVED",
                "CROSS_MARGIN_LIVE_ENABLED",
                "CROSS_MARGIN_APPROVED_CONFIG_JSON",
                "BINANCE_SPOT_PRODUCTION_BOOK",
            )
        }
        os.environ.pop("BINANCE_SPOT_PRODUCTION_BOOK", None)
        os.environ.pop("CROSS_MARGIN_PARAMETERS_APPROVED", None)
        os.environ.pop("CROSS_MARGIN_LIVE_ENABLED", None)
        os.environ.pop("CROSS_MARGIN_APPROVED_CONFIG_JSON", None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._dir.cleanup()

    def _gateway(self, decision: Path, adapter: _SpotAdapter) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=str(decision),
            ledger_path=str(self.root / f"{decision.stem}-exec.db"),
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=adapter,  # type: ignore[arg-type]
            paper_enabled=False,
        )

    def _insert(
        self,
        decision: Path,
        row_id: int,
        extras: dict[str, Any],
        *,
        mode: str = "dry_run",
    ) -> None:
        raw = {
            "pair_address": "BTCUSDT",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "extras": extras,
        }
        conn = sqlite3.connect(decision)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                risk_passed INTEGER, mode TEXT, purpose TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO decision_log VALUES (
                ?, '2026-11-19T00:00:00Z', ?, 'buy', 'binance', 'BTCUSDT',
                0.1, 'probe', 1, ?, ?, ?
            )
            """,
            (
                row_id,
                str(extras.get("strategy_id") or "probe"),
                mode,
                PURPOSE_LIVE_BNB_BOLL_FADE,
                json.dumps(raw),
            ),
        )
        conn.commit()
        conn.close()

    def test_default_tick_stays_on_the_spot_adapter(self) -> None:
        decision, adapter, _gateway, stats = self._tick_and_consume(account_route=None)
        conn = sqlite3.connect(decision)
        raw = json.loads(conn.execute("SELECT raw_json FROM decision_log").fetchone()[0])
        conn.close()
        self.assertNotIn("account_domain", raw.get("extras") or {})
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(stats.receipts[0]["executor"], "binance_spot")
        self.assertEqual(_borrow_count(stats.receipts[0]), 0)

    def test_explicit_offline_approval_reaches_open_position_and_restart_does_not_borrow_again(
        self,
    ) -> None:
        case = short_case()
        ledger = self.root / "margin.db"
        route = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": False,
            "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
            "execution_mode": "offline_fixture",
            "intent": case["intent"],
            "snapshot": case["snapshot"],
            "contract": case["contract"],
            "gate": case["gate"],
            "interest_on_borrow": "0.0001",
            "offline_approval": _offline_approval(),
            "margin_ledger_path": str(ledger),
        }
        decision, adapter, gateway, stats = self._tick_and_consume(account_route=route)
        first = stats.receipts[0]
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(first["executor"], "cross_margin_track")
        self.assertEqual(first["receipt"]["status"], "cross_margin_opened")
        self.assertEqual(
            first["exec_result"]["entry"],
            "execution.margin_cross.entry.open_offline_routed",
        )
        self.assertTrue(first["exec_result"]["open"]["phase_ok"])
        self.assertEqual(_borrow_count(first), 1)
        row = sqlite3.connect(decision).execute(
            "SELECT strategy_id, raw_json FROM decision_log"
        ).fetchone()
        extras = json.loads(row[1])["extras"]
        extras["strategy_id"] = row[0]
        self._insert(decision, REPLAY_FLOOR_DECISION_ID + 2, extras)
        again = gateway.process_pending()
        self.assertEqual(adapter.calls, 0)
        self.assertEqual(_borrow_count(again.receipts[0]), 0)
        self.assertTrue(again.receipts[0]["exec_result"]["margin_called"])

    def test_missing_approval_illegal_domain_phase_and_dual_submit_do_not_open(self) -> None:
        case = short_case()
        decision = self.root / "rows.db"
        base = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": False,
            "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
            "execution_mode": "offline_fixture",
            "intent": case["intent"],
            "snapshot": case["snapshot"],
            "contract": case["contract"],
            "gate": case["gate"],
            "interest_on_borrow": "0.0001",
            "margin_ledger_path": str(self.root / "margin.db"),
        }
        missing = dict(base)
        illegal = dict(base)
        illegal["account_domain"] = "binance_margin"
        illegal["offline_approval"] = _offline_approval()
        phase = dict(base)
        phase["intent"] = dict(case["intent"])
        phase["intent"]["intent_id"] = ""
        phase["offline_approval"] = _offline_approval()
        dual = dict(base)
        dual["submit_spot"] = True
        dual["offline_approval"] = _offline_approval()
        floor = REPLAY_FLOOR_DECISION_ID
        self._insert(decision, floor + 1, missing)
        self._insert(decision, floor + 2, illegal)
        self._insert(decision, floor + 3, phase)
        self._insert(decision, floor + 4, dual)
        adapter = _SpotAdapter()
        stats = self._gateway(decision, adapter).process_pending()
        self.assertEqual(len(stats.receipts), 4)
        self.assertEqual(adapter.calls, 0)
        reasons = [item["exec_result"]["reason"] for item in stats.receipts]
        self.assertEqual(
            reasons,
            [
                "parameters_not_approved",
                "account_domain_invalid",
                "intent_id_invalid",
                "duplicate_spot_and_margin",
            ],
        )
        self.assertFalse(stats.receipts[2]["exec_result"]["ok"])
        self.assertFalse(stats.receipts[2]["exec_result"]["open"]["phase_ok"])
        self.assertTrue(stats.receipts[2]["exec_result"]["margin_called"])
        for item in stats.receipts:
            self.assertFalse(item["exec_result"]["spot_called"])

    def test_live_stub_without_an_adapter_keeps_the_spot_skip(self) -> None:
        case = short_case()
        decision = self.root / "live-rows.db"
        floor = REPLAY_FLOOR_DECISION_ID
        offline = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": False,
            "strategy_id": OFFLINE_FIXTURE_STRATEGY_ID,
            "execution_mode": "offline_fixture",
            "intent": case["intent"],
            "snapshot": case["snapshot"],
            "contract": case["contract"],
            "gate": case["gate"],
            "interest_on_borrow": "0.0001",
            "offline_approval": _offline_approval(),
            "margin_ledger_path": str(self.root / "live-margin.db"),
        }
        planned = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": True,
            "strategy_id": "regime5_crisis_contagion_short",
            "execution_mode": "approved_dry_run",
            "symbol": "BTCUSDT",
            "base_asset": "BTC",
            "requested_action": "OPEN_SHORT",
            "now_ms": 1_700_000_000_000,
            "offline_approval": _offline_approval(),
            "margin_ledger_path": str(self.root / "plan-margin.db"),
        }
        explicit_spot = {"account_domain": "binance_spot"}
        self._insert(decision, floor + 1, {}, mode="live_stub")
        self._insert(decision, floor + 2, offline, mode="live_stub")
        self._insert(decision, floor + 3, planned, mode="live_stub")
        self._insert(decision, floor + 4, explicit_spot, mode="live_stub")
        gateway = ExecutionGateway(
            decision_db=str(decision),
            ledger_path=str(self.root / "live-exec.db"),
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            paper_enabled=False,
        )
        stats = gateway.process_pending()
        self.assertEqual(len(stats.receipts), 4)
        plain, opened, refused, spot = stats.receipts
        self.assertEqual(plain["executor"], "binance_spot")
        self.assertEqual(plain["receipt"]["status"], "gate_skip")
        self.assertEqual(plain["receipt"]["gate_reason"], "protection_tool_absent")
        self.assertEqual(opened["executor"], "cross_margin_track")
        self.assertEqual(opened["receipt"]["status"], "cross_margin_opened")
        self.assertEqual(_borrow_count(opened), 1)
        self.assertNotEqual(opened["receipt"]["status"], "gate_skip")
        self.assertFalse(refused["exec_result"]["margin_called"])
        self.assertEqual(refused["exec_result"]["reason"], "parameters_not_approved")
        self.assertEqual(
            refused["exec_result"]["entry"],
            "execution.margin_cross.strategy_gate.build_approved_position_plan",
        )
        self.assertEqual(_borrow_count(refused), 0)
        self.assertEqual(spot["executor"], "binance_spot")
        self.assertEqual(spot["receipt"]["status"], "gate_skip")
        self.assertEqual(spot["receipt"]["gate_reason"], "protection_tool_absent")

    def _tick_and_consume(self, *, account_route: dict[str, Any] | None):
        now_ms = int(time.time() * 1000)
        decision = self.root / ("routed.db" if account_route else "plain.db")
        book = self.root / f"{decision.stem}-book.db"
        self._seed(decision, book, now_ms)
        os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(book)
        os.environ["NEWS_EVENTS_DB_PATH"] = str(self.root / "missing-news.db")
        os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        adapter_db = IndicatorDataAdapter(decision)
        bus = SignalBus(decision, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        try:
            assert bus._rw is not None
            bus._rw.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('decision_log', ?)",
                (REPLAY_FLOOR_DECISION_ID,),
            )
            bus._rw.commit()
            payload = build_production_engine(
                adapter_db,
                bus,
                [get_strategy("tech_boll_fade_v1")],
                risk=RiskManager(cooldown_sec=0),
                hot_limit=scan_limit_for_purpose(PURPOSE_LIVE_BNB_BOLL_FADE, None),
                token_cooldown=TokenCooldown(0),
                entry_ih_min_hours=None,
                account_route=account_route,
                macro_db=write_quiet_macro(decision.with_name(f"{decision.stem}-macro.db")),
            ).tick()
        finally:
            bus.close()
            adapter_db.close()
        self.assertEqual(payload["emitted"], 1)
        spot = _SpotAdapter()
        gateway = self._gateway(decision, spot)
        stats = gateway.process_pending()
        self.assertEqual(stats.processed, 1)
        return decision, spot, gateway, stats

    def _seed(self, db: Path, book: Path, now_ms: int) -> None:
        hour = 3_600_000
        close_span = hour - 1
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY, base_asset TEXT, quote_asset TEXT, status TEXT,
                price_usd REAL, change_5m REAL, volume_strength REAL, boll_lower REAL,
                boll_mid REAL, kline_low REAL, trade_size_ratio REAL, quote_volume_24h REAL,
                xs_breadth_1h REAL, xs_rank_pct_1h REAL
            )
            """
        )
        low, span = 100.0, 10.0
        price = low + 0.08 * span
        conn.execute(
            """
            INSERT INTO binance_indicators VALUES (
                'FADEUSDT', 'FADE', 'USDT', 'TRADING', ?, -4.0, 0.9, ?, ?, ?, 1.3, ?, 0.75, 0.85
            )
            """,
            (price, price + 1.0, low + span, low, float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0),
        )
        conn.execute(
            "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
        )
        conn.execute("INSERT INTO symbols VALUES ('FADEUSDT', 'TRADING', 'FADE', 'USDT')")
        conn.execute(
            """
            CREATE TABLE ccxt_ticks (
                exchange TEXT, symbol TEXT, ts INTEGER, quote_volume_24h REAL, volume_24h REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO ccxt_ticks VALUES ('binance', 'FADE/USDT', ?, 3000000, 1)",
            (now_ms,),
        )
        conn.execute(
            """
            CREATE TABLE binance_klines (
                symbol TEXT, interval TEXT, open_time_ms INTEGER,
                close_time_ms INTEGER, quote_volume REAL
            )
            """
        )
        latest_open = (now_ms // hour) * hour - hour
        if latest_open + close_span > now_ms:
            latest_open -= hour
        for index in range(21):
            opened = latest_open - (20 - index) * hour
            quote = 100.0 if index < 20 else 130.0
            if index == 3:
                quote = 160.0
            conn.execute(
                "INSERT INTO binance_klines VALUES ('FADEUSDT', '1h', ?, ?, ?)",
                (opened, opened + close_span, quote),
            )
        conn.commit()
        conn.close()
        deriv = sqlite3.connect(book)
        deriv.execute(
            """
            CREATE TABLE binance_spot_microstructure (
                symbol TEXT, obi_top5 REAL, obi_top5_source_ts_ms INTEGER,
                obi_top5_error TEXT, orderbook_imbalance_20 REAL
            )
            """
        )
        deriv.execute(
            "INSERT INTO binance_spot_microstructure VALUES ('FADEUSDT', -0.10, ?, NULL, -0.55)",
            (now_ms,),
        )
        deriv.commit()
        deriv.close()


if __name__ == "__main__":
    unittest.main()
