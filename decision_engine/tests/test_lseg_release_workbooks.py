"""Lesg crypto workbooks: release clocks, not the CityU period-date snapshot."""

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
from scripts.import_lseg_release_workbooks import (
    SNAPSHOT_SHA256,
    _merge,
    build_acceptance_db,
)

_DEFAULT_SOURCES = Path("/tmp/lseg-release-sources-t351/xlsx")


def _ms(iso: str) -> int:
    text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)


def _sources() -> Path | None:
    raw = os.environ.get("LSEG_RELEASE_SOURCE_DIR", "").strip()
    path = Path(raw) if raw else _DEFAULT_SOURCES
    return path if path.is_dir() else None


class ReleaseMergeTests(unittest.TestCase):
    def test_disagreement_is_a_conflict_and_an_empty_clock_is_not_invented(self) -> None:
        identity = ("USCPF=ECI", "Core CPI MM, SA", "Oct", "United States", "2026-11-10")
        conflict = _merge(
            {
                identity: [
                    ("a.xlsx", {"Local Time": "2026-11-10T21:30:00", "Relevance": "3", "Period": "Oct"}),
                    ("b.xlsx", {"Local Time": "2026-11-10T20:30:00", "Relevance": "3", "Period": "Oct"}),
                ]
            },
            "Local Time",
        )
        self.assertEqual(conflict[0]["status"], "conflict")
        empty = _merge(
            {identity: [("a.xlsx", {"Local Time": "", "Relevance": "3", "Period": "Oct"})]},
            "Local Time",
        )
        self.assertEqual(empty[0]["status"], "absent")
        filled = _merge(
            {
                identity: [
                    ("a.xlsx", {"Local Time": "", "Relevance": "3", "Period": "Oct"}),
                    ("b.xlsx", {"Local Time": "2026-11-10T21:30:00", "Relevance": "3", "Period": "Oct"}),
                ]
            },
            "Local Time",
        )
        self.assertEqual(filled[0]["status"], "release")
        self.assertEqual(filled[0]["local_time"], "2026-11-10T21:30:00")


class WorkbookAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        source = _sources()
        if source is None:
            self.skipTest(
                "lesg crypto workbooks are not in this process; "
                "set LSEG_RELEASE_SOURCE_DIR to require them"
            )
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "lseg_release_workbooks.db"
        self.counts = build_acceptance_db(source, self.db)

    def tearDown(self) -> None:
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_cpi_and_fomc_clocks_drive_the_reader_and_leave_gaps_uncovered(self) -> None:
        conn = sqlite3.connect(self.db)
        cpi = conn.execute(
            """
            SELECT clock_status, observation_period, utc_ts, release_local_hkt, sources
            FROM release_event
            WHERE event_key = 'USCPF=ECI|Core CPI MM, SA|Oct|United States|2026-11-10'
            """
        ).fetchone()
        fomc = conn.execute(
            """
            SELECT clock_status, utc_ts, release_local_hkt
            FROM release_event
            WHERE event_key LIKE '%minutes from its meeting of October 27%'
            """
        ).fetchone()
        excluded = conn.execute(
            "SELECT role FROM source_file WHERE name LIKE '%ECOCAL%'"
        ).fetchone()
        absent = conn.execute(
            "SELECT kind, COUNT(*) FROM release_event WHERE clock_status = 'absent' GROUP BY kind"
        ).fetchall()
        conflicts = conn.execute(
            "SELECT COUNT(*) FROM release_event WHERE clock_status = 'conflict'"
        ).fetchone()
        days = {
            row[0][:10]
            for row in conn.execute(
                """
                SELECT utc_ts FROM macro_events
                UNION SELECT utc_ts FROM central_bank_events
                """
            )
        }
        conn.close()
        self.assertEqual(cpi[0], "release")
        self.assertEqual(cpi[1], "Oct")
        self.assertEqual(cpi[2], "2026-11-10T13:30:00Z")
        self.assertEqual(cpi[3], "2026-11-10 21:30:00")
        self.assertIn("lesg_crypto__Economic Events.xlsx", cpi[4])
        self.assertIn("lesg_crypto__Economic Events (1).xlsx", cpi[4])
        self.assertNotIn("Economic Events (2)", cpi[4])
        self.assertEqual(fomc[0], "release")
        self.assertEqual(fomc[1], "2026-11-18T19:00:00Z")
        self.assertEqual(fomc[2], "2026-11-19 03:00:00")
        self.assertEqual(excluded[0], "excluded_date_only")
        absent_map = dict(absent)
        self.assertGreaterEqual(absent_map["central_bank"], 2000)
        self.assertGreaterEqual(absent_map["economic"], 100)
        self.assertGreater(conflicts[0], 0)
        self.assertGreater(len(days), 100)
        first = min(days)
        last = max(days)
        span = (
            datetime.fromisoformat(last) - datetime.fromisoformat(first)
        ).days + 1
        self.assertGreater(span - len(days), 0)

        cpi_ms = _ms("2026-11-10T13:30:00Z")
        at_cpi = evaluate_macro_gate(self.db, now_ms=cpi_ms)
        self.assertEqual(at_cpi.halt_reason, "macro_rel3_blackout")
        self.assertIn("Core CPI", at_cpi.detail or "")
        after_cpi = evaluate_macro_gate(self.db, now_ms=cpi_ms + 35 * 60_000)
        self.assertIsNone(after_cpi.halt_reason)
        morning = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-10T09:00:00Z"))
        self.assertIsNone(morning.halt_reason)

        fomc_ms = _ms("2026-11-18T19:00:00Z")
        at_fomc = evaluate_macro_gate(self.db, now_ms=fomc_ms)
        self.assertEqual(at_fomc.halt_reason, "central_bank_blackout")
        after_fomc = evaluate_macro_gate(self.db, now_ms=fomc_ms + 50 * 60_000)
        self.assertIsNone(after_fomc.halt_reason)

        gap = evaluate_macro_gate(self.db, now_ms=_ms("2023-11-12T16:00:00Z"))
        self.assertEqual(gap.halt_reason, "macro_lseg_stale_or_missing")
        self.assertEqual(gap.detail, "release_calendar_not_covering_query_day")
        built = __import__("hashlib").sha256(self.db.read_bytes()).hexdigest()
        self.assertNotEqual(built, SNAPSHOT_SHA256)

    def test_tick_drops_the_buy_inside_the_cpi_window_and_not_after_it(self) -> None:
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
            "venue": "binance",
            "chain_id": "binance",
            "token_address": "BNBUSDT",
            "binance_symbol": "BNBUSDT",
            "ccxt_symbol": "BNB/USDT",
            "liquidity_usd": 90_000.0,
            "universe_member": True,
            "quote_turnover_24h_usdt": 3_000_000.0,
            "quote_turnover_24h_source": "test",
            "quote_turnover_24h_asof": _ms("2026-11-10T13:30:00Z"),
            "volume_strength": 1.0,
        }]
        adapter = IndicatorDataAdapter(decision)
        bus = SignalBus(decision, purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC)
        engine = DecisionEngine(
            adapter,
            bus,
            [_FixedStrategy([buy])],
            risk=RiskManager(cooldown_sec=0, min_liq_usd=50_000),
            protection_book=ProtectionBook.ready([], []),
            macro_db=self.db,
        )

        def run_at(iso: str) -> dict:
            stamp = _ms(iso) / 1000.0
            with patch("decision_engine.core.engine.time.time", return_value=stamp), patch.object(
                DecisionEngine,
                "_load_live_bnb_hot",
                lambda self, dex: (hot, "binance_indicators"),
            ), patch(
                "decision_engine.core.engine.filter_live_bnb_hot",
                lambda conn, db_path, rows, now_ms=None: (list(rows), {"scanned": len(rows), "passed": len(rows)}),
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
        self.assertNotIn("macro_halt", outside)
        self.assertEqual(outside["emitted"], 1)
