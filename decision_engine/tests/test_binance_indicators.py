"""live_bnb reads binance_indicators; race_tech_boll_fade still reads DEX rows."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from decision_engine.core.bnb_spot_map import CEX_UNIVERSE
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies import get_strategy
from decision_engine.tests.test_bnb_spot_map import BINANCE_VENUE, _decision_rows, _seed

_SCHEMA = (
    Path(__file__).resolve().parents[2] / "crawler" / "models" / "binance_indicator_tables.sql"
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump_indicators(db: Path) -> list:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT chain_id, token_address, change_5m FROM token_indicators ORDER BY token_address"
        ).fetchall()
    finally:
        conn.close()


def _insert_cex(db: Path) -> None:
    now = _iso(datetime.now(timezone.utc))
    stale = _iso(datetime.now(timezone.utc) - timedelta(hours=6))
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.execute("ALTER TABLE binance_indicators ADD COLUMN kline_low REAL")
    conn.execute("ALTER TABLE binance_indicators ADD COLUMN trade_size_ratio REAL")
    conn.executemany(
        """
        INSERT INTO binance_indicators (
            symbol, base_asset, quote_asset, status, interval,
            price_usd, change_5m, volume_strength, boll_lower, boll_mid,
            kline_low, trade_size_ratio,
            quote_volume_24h, tier, tech_updated_at, updated_at
        ) VALUES (?, ?, 'USDT', 'TRADING', '5m', ?, ?, ?, ?, ?, ?, ?, ?, 'hot', ?, ?)
        """,
        [
            # alpha=(0.8-0.75)/(1-0.75)=0.20, ratio 1.5 → large_trade market
            ("ONLYCEXUSDT", "ONLYCEX", 0.8, -4.0, 0.9, 1.0, 1.0, 0.75, 1.5, 90_000.0, now, now),
            ("THINUSDT", "THIN", 0.4, -6.0, 0.9, 1.0, None, None, None, 1_000.0, now, now),
            ("STALEUSDT", "STALE", 0.4, -6.0, 0.9, 1.0, None, None, None, 90_000.0, stale, stale),
        ],
    )
    conn.commit()
    conn.close()


def _tick(db: Path, purpose: str) -> tuple[list[sqlite3.Row], dict]:
    adapter = IndicatorDataAdapter(db)
    bus = SignalBus(db, purpose=purpose)
    try:
        engine = DecisionEngine(
            adapter,
            bus,
            [get_strategy("tech_boll_fade_v1")],
            risk=RiskManager(cooldown_sec=0),
            hot_limit=20,
        )
        payload = engine.tick()
    finally:
        bus.close()
        adapter.close()
    return _decision_rows(db), payload


class LiveBnbCexFactorTests(unittest.TestCase):
    def test_live_reads_cex_factors_and_leaves_dex_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            before = _dump_indicators(db)
            _insert_cex(db)
            self.assertEqual(_dump_indicators(db), before)
            rows, payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertEqual(_dump_indicators(db), before)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["purpose"], PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(row["strategy_id"], "tech_boll_fade_v1")
            self.assertEqual(row["chain_id"], BINANCE_VENUE)
            self.assertEqual(row["token_address"], "ONLYCEXUSDT")
            extras = json.loads(row["raw_json"])["extras"]
            self.assertEqual(extras["binance_symbol"], "ONLYCEXUSDT")
            self.assertEqual(extras["universe"], CEX_UNIVERSE)
            self.assertIsNone(extras.get("source_token_address"))
            self.assertNotIn("local_score", extras)
            self.assertNotIn("ZZQBOLLUSDT", {item["token_address"] for item in rows})
            self.assertNotIn("THINUSDT", {item["token_address"] for item in rows})
            self.assertNotIn("STALEUSDT", {item["token_address"] for item in rows})

            race, race_payload = _tick(db, PURPOSE_RACE_TECH_BOLL_FADE)
            self.assertNotIn("bnb_factor_source", race_payload)
            race_rows = [item for item in race if item["purpose"] == PURPOSE_RACE_TECH_BOLL_FADE]
            tokens = {item["token_address"] for item in race_rows}
            self.assertIn("MintA", tokens)
            self.assertNotIn("ONLYCEXUSDT", tokens)
            for item in race_rows:
                self.assertEqual(item["chain_id"], "solana")
                self.assertNotIn(
                    "binance_symbol",
                    json.loads(item["raw_json"]).get("extras", {}),
                )
            self.assertEqual(_dump_indicators(db), before)

    def test_dex_map_bypass_still_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            _insert_cex(db)
            old = os.environ.get("LIVE_BNB_FACTOR_SOURCE")
            os.environ["LIVE_BNB_FACTOR_SOURCE"] = "dex_map"
            try:
                rows, payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
            finally:
                if old is None:
                    os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
                else:
                    os.environ["LIVE_BNB_FACTOR_SOURCE"] = old
            self.assertEqual(payload["bnb_factor_source"], "dex_map")
            tokens = {row["token_address"] for row in rows}
            # Mapped DEX names have no closed-bar coordinates here, so the
            # entry zone fails closed instead of using the old -2% / lower-band legs.
            self.assertEqual(tokens, set())
            self.assertNotIn("ONLYCEXUSDT", tokens)

    def test_binance_source_does_not_fill_from_dex_when_table_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            old = os.environ.get("LIVE_BNB_FACTOR_SOURCE")
            os.environ["LIVE_BNB_FACTOR_SOURCE"] = "binance"
            try:
                rows, payload = _tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
            finally:
                if old is None:
                    os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
                else:
                    os.environ["LIVE_BNB_FACTOR_SOURCE"] = old
            self.assertEqual(payload["bnb_factor_source"], "binance_indicators")
            self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
