"""Unit and integration tests for LSEG macro & cross-asset circuit breaker.

    python -m unittest decision_engine.tests.test_macro_circuit_breaker
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import decision_engine  # noqa: F401
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.macro_circuit_breaker import (
    evaluate_macro_gate,
    resolve_macro_db_path,
)
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies.base_strategy import BaseStrategy


def _utc_ms(iso_str: str) -> int:
    text = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)


def _create_macro_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE macro_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ric TEXT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            relevance INTEGER,
            indicator_name TEXT,
            period TEXT,
            reuters_poll REAL,
            actual REAL,
            surprise REAL,
            prior REAL,
            revised REAL,
            smart_economics REAL,
            predicted_surprise REAL
        );
        CREATE TABLE central_bank_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_date_hkt TEXT,
            local_time_hkt TEXT,
            utc_ts TEXT,
            utc_ms INTEGER,
            country TEXT,
            event_name TEXT,
            is_high_impact INTEGER
        );
        CREATE TABLE cross_asset_bars (
            ric TEXT NOT NULL,
            interval TEXT NOT NULL,
            utc_ts TEXT NOT NULL,
            utc_ms INTEGER NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL NOT NULL,
            volume REAL,
            yield_val REAL,
            desc TEXT,
            PRIMARY KEY (ric, interval, utc_ms)
        );
        """
    )
    conn.commit()
    conn.close()


class _FixedStrategy(BaseStrategy):
    strategy_id = "tech_boll_fade_v1"

    def __init__(self, signals: list[dict]) -> None:
        super().__init__()
        self._signals = signals

    def on_tick(self, data: dict) -> list[dict]:
        return [dict(s) for s in self._signals]


class MacroCircuitBreakerTests(unittest.TestCase):
    def test_unwired_by_default_and_env_resolution(self) -> None:
        self.assertIsNone(resolve_macro_db_path({}))
        self.assertIsNone(
            resolve_macro_db_path(
                {"DECISION_MACRO_DB": "/tmp/x.db", "DECISION_MACRO_GATE_ENABLED": "0"}
            )
        )
        self.assertEqual(
            resolve_macro_db_path({"DECISION_MACRO_DB": "/tmp/lseg.db"}),
            Path("/tmp/lseg.db"),
        )
        default_path = resolve_macro_db_path({"DECISION_MACRO_GATE_ENABLED": "1"})
        self.assertIsNotNone(default_path)
        assert default_path is not None
        self.assertTrue(str(default_path).endswith("data/lseg/lseg_macro.db"))

        status = evaluate_macro_gate(None, now_ms=1_700_000_000_000)
        self.assertFalse(status.configured)
        self.assertIsNone(status.halt_reason)

    def test_calendar_blackout_windows_cb_rel3_and_rel2_auction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro.db"
            _create_macro_db(db)
            fomc_ms = _utc_ms("2026-11-18T19:00:00Z")
            cpi_ms = _utc_ms("2026-11-10T13:30:00Z")
            auc_ms = _utc_ms("2026-11-09T18:00:00Z")
            minor_r2_ms = _utc_ms("2026-11-09T15:00:00Z")

            conn = sqlite3.connect(db)
            conn.execute(
                """
                INSERT INTO central_bank_events (
                    local_date_hkt, local_time_hkt, utc_ts, utc_ms, country, event_name, is_high_impact
                ) VALUES ('2026-11-19', '2026-11-19 03:00:00', '2026-11-18T19:00:00Z', ?, 'United States', 'FOMC Minutes', 1)
                """,
                (fomc_ms,),
            )
            conn.executemany(
                """
                INSERT INTO macro_events (
                    local_date_hkt, local_time_hkt, utc_ts, utc_ms, country, relevance, indicator_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("2026-11-10", "2026-11-10 21:30:00", "2026-11-10T13:30:00Z", cpi_ms, "United States", 3, "Core CPI MM, SA"),
                    ("2026-11-10", "2026-11-10 02:00:00", "2026-11-09T18:00:00Z", auc_ms, "United States", 2, "3Y Note Auc - HY"),
                    ("2026-11-09", "2026-11-09 23:00:00", "2026-11-09T15:00:00Z", minor_r2_ms, "United States", 2, "Wholesale Invt"),
                ],
            )
            conn.commit()
            conn.close()

            # 10 min before FOMC -> blocked
            st_fomc = evaluate_macro_gate(db, now_ms=fomc_ms - 10 * 60_000)
            self.assertEqual(st_fomc.halt_reason, "central_bank_blackout")
            # 40 min after FOMC -> still blocked (within +45m)
            self.assertEqual(
                evaluate_macro_gate(db, now_ms=fomc_ms + 40 * 60_000).halt_reason,
                "central_bank_blackout",
            )
            # 50 min after FOMC -> clear
            self.assertIsNone(
                evaluate_macro_gate(db, now_ms=fomc_ms + 50 * 60_000).halt_reason
            )

            # 5 min after US CPI (Rel=3) -> blocked
            st_cpi = evaluate_macro_gate(db, now_ms=cpi_ms + 5 * 60_000)
            self.assertEqual(st_cpi.halt_reason, "macro_rel3_blackout")
            # Paper purpose -> never blocked by default
            self.assertIsNone(
                evaluate_macro_gate(
                    db,
                    now_ms=cpi_ms + 5 * 60_000,
                    purpose=PURPOSE_RACE_TECH_BOLL_FADE,
                ).halt_reason
            )

            # US 3Y Note Auction (Rel=2 with 'Auc') -> blocked within [-10m, +20m]
            st_auc = evaluate_macro_gate(db, now_ms=auc_ms + 10 * 60_000)
            self.assertEqual(st_auc.halt_reason, "macro_rel2_auction_blackout")

            # Minor Rel=2 ('Wholesale Invt') -> clear by default, blocked if DECISION_MACRO_BLOCK_ALL_REL2=1
            self.assertIsNone(
                evaluate_macro_gate(db, now_ms=minor_r2_ms).halt_reason
            )
            self.assertEqual(
                evaluate_macro_gate(
                    db,
                    now_ms=minor_r2_ms,
                    environ={"DECISION_MACRO_BLOCK_ALL_REL2": "1"},
                ).halt_reason,
                "macro_rel2_auction_blackout",
            )

    def test_cross_asset_contagion_triggers_and_staleness_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "macro_ca.db"
            _create_macro_db(db)
            t0 = _utc_ms("2026-11-08T12:00:00Z")
            conn = sqlite3.connect(db)
            # Insert 4 bars of NQc1 dropping -0.30% over 15m (3 intervals of 5m)
            bars = [
                ("NQc1", "5min", "2026-11-08T11:45:00Z", t0 - 15 * 60_000, 20000.0),
                ("NQc1", "5min", "2026-11-08T11:50:00Z", t0 - 10 * 60_000, 19980.0),
                ("NQc1", "5min", "2026-11-08T11:55:00Z", t0 - 5 * 60_000, 19960.0),
                ("NQc1", "5min", "2026-11-08T12:00:00Z", t0, 19940.0),  # -0.30%
            ]
            for ric, iv, ts, ms, cl in bars:
                conn.execute(
                    "INSERT INTO cross_asset_bars (ric, interval, utc_ts, utc_ms, close) VALUES (?, ?, ?, ?, ?)",
                    (ric, iv, ts, ms, cl),
                )
            conn.commit()
            conn.close()

            # Fresh at t0 + 2 min -> blocks with cross_asset_nq_dump
            st = evaluate_macro_gate(db, now_ms=t0 + 2 * 60_000)
            self.assertEqual(st.halt_reason, "cross_asset_nq_dump")

            # Stale at t0 + 45 min (> 30m) -> does not block on old bars
            st_stale = evaluate_macro_gate(db, now_ms=t0 + 45 * 60_000)
            self.assertIsNone(st_stale.halt_reason)

    def test_engine_and_guards_drop_buys_keep_sells_and_fail_closed_on_missing_db(self) -> None:
        buy_sig = {
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
        sell_sig = {
            "action": "sell",
            "strategy_id": "tech_boll_fade_v1",
            "chain_id": "binance",
            "token_address": "BNBUSDT",
            "binance_symbol": "BNBUSDT",
            "size_pct": 1.0,
            "reason": "tp",
        }
        kept, report = apply_live_buy_guards(
            [buy_sig, sell_sig],
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            book=ProtectionBook.unwired(),
            macro_halt="central_bank_blackout",
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["action"], "sell")
        self.assertEqual(report["macro_halt"], "central_bank_blackout")
        self.assertEqual(report["macro_dropped"], 1)

        # Engine fail-closed when macro_db is configured to a non-existent path
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "decision.db"
            sqlite3.connect(db).close()
            now_ms = int(time.time() * 1000)
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
                "quote_turnover_24h_asof": now_ms,
                "volume_strength": 1.0,
            }]
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            engine = DecisionEngine(
                adapter,
                bus,
                [_FixedStrategy([buy_sig])],
                risk=RiskManager(cooldown_sec=0, min_liq_usd=50_000),
                protection_book=ProtectionBook.ready([], []),
                macro_db=Path(tmp) / "missing_lseg_macro.db",
            )
            try:
                with patch.object(
                    DecisionEngine,
                    "_load_live_bnb_hot",
                    lambda self, dex: (hot, "binance_indicators"),
                ), patch(
                    "decision_engine.core.engine.filter_live_bnb_hot",
                    lambda conn, db_path, rows, now_ms=None: (list(rows), {"scanned": len(rows), "passed": len(rows)}),
                ):
                    payload = engine.tick()
                self.assertEqual(payload["emitted"], 0)
                self.assertEqual(payload["macro_halt"], "macro_status_unknown")
            finally:
                bus.close()
                adapter.close()

    def test_real_lseg_macro_db_upcoming_events_if_present(self) -> None:
        real_db = Path(__file__).resolve().parents[2] / "data" / "lseg" / "lseg_macro.db"
        if not real_db.is_file():
            self.skipTest("data/lseg/lseg_macro.db not built in this environment")
        # 2026-11-10T13:30:00Z = US Oct Core CPI (Relevance=3)
        cpi_ms = _utc_ms("2026-11-10T13:30:00Z")
        st_cpi = evaluate_macro_gate(real_db, now_ms=cpi_ms)
        self.assertEqual(st_cpi.halt_reason, "macro_rel3_blackout")

        # 2026-11-18T19:00:00Z = FOMC Minutes (High-impact CB)
        fomc_ms = _utc_ms("2026-11-18T19:00:00Z")
        st_fomc = evaluate_macro_gate(real_db, now_ms=fomc_ms)
        self.assertEqual(st_fomc.halt_reason, "central_bank_blackout")


if __name__ == "__main__":
    unittest.main()
