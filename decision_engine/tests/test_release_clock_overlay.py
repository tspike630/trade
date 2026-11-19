"""Official clocks added on top of the workbook database. Not the CityU snapshot."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_CLASSIC, SignalBus
from decision_engine.tests.test_macro_circuit_breaker import _FixedStrategy
from scripts.apply_release_clock_overlay import apply_overlay, load_adopted

_OVERLAY = Path(__file__).resolve().parents[1] / "fixtures" / "release_clock_overlay"


def _ms(iso: str) -> int:
    text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)


class OverlayParseTests(unittest.TestCase):
    def test_nine_clocks_match_the_quoted_pages(self) -> None:
        rows = {row["event_key"]: row for row in load_adopted(_OVERLAY)}
        self.assertEqual(len(rows), 9)
        gac = rows["Euro Zone|EU General Affairs Council meeting|2026-11-19"]
        self.assertEqual(gac["utc_ts"], "2026-11-19T09:00:00Z")
        self.assertEqual(gac["local_hkt"], "2026-11-19 17:00:00")
        ecofin = rows["Euro Zone|EU Economic and Financial Affairs Council (Budget) Meeting|2026-11-10"]
        self.assertEqual(ecofin["utc_ts"], "2026-11-10T08:00:00Z")
        boj = rows["JPINTN=ECI|JP BOJ Rate Decision|23 Jan|Japan|2026-01-23"]
        self.assertEqual(boj["utc_ts"], "2026-01-23T03:07:00Z")
        self.assertEqual(boj["release_tz"], "Asia/Tokyo")


class WorkbookOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        raw = os.environ.get("LSEG_RELEASE_WORKBOOK_DB", "").strip()
        source = Path(raw) if raw else None
        if source is None or not source.is_file():
            self.skipTest("workbook acceptance db is not in this process")
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "overlay.db"
        self.counts = apply_overlay(source, _OVERLAY, self.db)

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_adopted_clocks_block_and_unresolved_days_stay_closed(self) -> None:
        self.assertEqual(self.counts["adopted"], 9)
        conn = sqlite3.connect(self.db)
        absent = conn.execute(
            "SELECT COUNT(*) FROM release_event WHERE clock_status = 'absent'"
        ).fetchone()[0]
        conflicts = conn.execute(
            "SELECT COUNT(*) FROM release_event WHERE clock_status = 'conflict'"
        ).fetchone()[0]
        china = conn.execute(
            """
            SELECT COUNT(*) FROM release_event
            WHERE clock_status = 'absent' AND event_key LIKE 'CNEXP%'
            """
        ).fetchone()[0]
        conn.close()
        self.assertEqual(absent, 2252)
        self.assertEqual(conflicts, 30)
        self.assertGreater(china, 0)

        cpi = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-10T13:30:00Z"))
        self.assertEqual(cpi.halt_reason, "macro_rel3_blackout")
        after = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-10T14:05:00Z"))
        self.assertEqual(after.detail, "release_clock_unresolved:absent:2026-11-10:n=7")
        fomc = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-18T19:00:00Z"))
        self.assertEqual(fomc.halt_reason, "central_bank_blackout")
        after_fomc = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-18T19:50:00Z"))
        self.assertEqual(after_fomc.detail, "release_coverage_unproven:Belgium:rel3")
        boj = evaluate_macro_gate(self.db, now_ms=_ms("2026-01-23T03:07:00Z"))
        self.assertEqual(boj.halt_reason, "macro_rel3_blackout")
        self.assertIn("JP BOJ Rate Decision", boj.detail or "")

        buy = {
            "action": "buy",
            "strategy_id": "tech_boll_fade_v1",
            "chain_id": "binance",
            "token_address": "BNBUSDT",
            "binance_symbol": "BNBUSDT",
            "size_pct": 0.05,
            "reason": "fade",
            "liquidity_usd": 90_000.0,
            "order_type": "MARKET",
            "price_usd": 100.0,
            "stop_price": 95.0,
            "take_profit_price": 110.0,
            "volume_strength": 1.0,
        }
        decision = Path(self._tmp.name) / "decision.db"
        sqlite3.connect(decision).close()
        hot = [{
            "venue": "binance", "chain_id": "binance", "token_address": "BNBUSDT",
            "binance_symbol": "BNBUSDT", "ccxt_symbol": "BNB/USDT",
            "liquidity_usd": 90_000.0, "universe_member": True,
            "quote_turnover_24h_usdt": 3_000_000.0, "quote_turnover_24h_source": "test",
            "quote_turnover_24h_asof": _ms("2026-11-10T13:30:00Z"), "volume_strength": 1.0,
        }]
        adapter = IndicatorDataAdapter(decision)
        bus = SignalBus(decision, purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC)
        engine = DecisionEngine(
            adapter, bus, [_FixedStrategy([buy])],
            risk=RiskManager(cooldown_sec=0, min_liq_usd=50_000),
            protection_book=ProtectionBook.ready([], []),
            macro_db=self.db,
        )

        def run_at(iso: str) -> dict:
            with patch("decision_engine.core.engine.time.time", return_value=_ms(iso) / 1000.0), patch.object(
                DecisionEngine, "_load_live_bnb_hot", lambda self, dex: (hot, "binance_indicators"),
            ), patch(
                "decision_engine.core.engine.filter_live_bnb_hot",
                lambda conn, db_path, rows, now_ms=None: (list(rows), {"scanned": 1, "passed": 1}),
            ):
                return engine.tick()

        try:
            inside = run_at("2026-11-10T13:30:00Z")
            outside = run_at("2026-11-10T14:05:00Z")
        finally:
            bus.close()
            adapter.close()
        self.assertEqual(inside["macro_halt"], "macro_rel3_blackout")
        self.assertEqual(inside["emitted"], 0)
        self.assertEqual(outside["macro_halt"], "macro_lseg_stale_or_missing")
        self.assertEqual(outside["emitted"], 0)
