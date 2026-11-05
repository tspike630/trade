"""Binance spot USDT side book: purpose identity, not a second boll-fade formula."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine import config as de_config
from decision_engine.core.bnb_spot_map import (
    BINANCE_QUOTE,
    BINANCE_VENUE,
    CEX_UNIVERSE,
    classify_listed_symbol,
    load_binance_spot_usdt_symbols,
    map_hot_tokens_to_binance_spot,
    normalize_spot_base,
    stamp_bnb_spot_signals,
)
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies import get_strategy, resolve_strategy_id
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy


def _hit_row(**overrides: object) -> dict:
    row = {
        "chain_id": "solana",
        "token_address": "MintZZQBOLL",
        "symbol": "ZZQBOLL",
        "change_5m": -3.0,
        "volume_strength": 0.9,
        "price_usd": 0.9,
        "boll_lower": 1.0,
        "liquidity_usd": 90_000,
        "primary_pair_address": "DexPairA",
    }
    row.update(overrides)
    return row


class SpotSymbolTests(unittest.TestCase):
    def test_normalize_spot_usdt_only(self) -> None:
        self.assertEqual(normalize_spot_base(" sol "), "SOL")
        self.assertEqual(normalize_spot_base("SOL/USDT"), "SOL")
        self.assertEqual(normalize_spot_base("SOLUSDT"), "SOL")
        self.assertIsNone(normalize_spot_base("SOL/USDC"))
        self.assertIsNone(normalize_spot_base("SOL/USDT:USDT"))
        self.assertIsNone(normalize_spot_base("USDT"))
        self.assertIsNone(normalize_spot_base(""))
        self.assertIsNone(normalize_spot_base("SO L"))

    def test_futures_symbol_is_not_spot(self) -> None:
        self.assertEqual(classify_listed_symbol("ZZQBOLL/USDT", "ZZQBOLL", "USDT"), "ZZQBOLLUSDT")
        self.assertIsNone(classify_listed_symbol("BTC/USDT:USDT", "BTC", "USDT"))
        self.assertIsNone(classify_listed_symbol("ETH/USDC", "ETH", "USDC"))

    def test_map_keeps_factors_and_replaces_dex_identity(self) -> None:
        listed = {"ZZQBOLL/USDT", "ZZQFLATUSDT"}
        rows = [
            _hit_row(),
            _hit_row(token_address="MintNope", symbol="ZZQNOPE"),
            _hit_row(token_address="MintDup", symbol="zzqboll"),
            _hit_row(token_address="MintBare", symbol=None),
        ]
        mapped, stats = map_hot_tokens_to_binance_spot(rows, listed_symbols=listed)
        self.assertEqual(stats["mapped"], 1)
        self.assertEqual(stats["skipped_unlisted"], 1)
        self.assertEqual(stats["skipped_duplicate"], 1)
        self.assertEqual(stats["skipped_no_symbol"], 1)
        self.assertEqual(len(mapped), 1)
        got = mapped[0]
        self.assertEqual(got["chain_id"], BINANCE_VENUE)
        self.assertEqual(got["token_address"], "ZZQBOLLUSDT")
        self.assertEqual(got["binance_symbol"], "ZZQBOLLUSDT")
        self.assertEqual(got["ccxt_symbol"], "ZZQBOLL/USDT")
        self.assertEqual(got["market"], "spot")
        self.assertEqual(got["quote_asset"], BINANCE_QUOTE)
        self.assertEqual(got["universe"], CEX_UNIVERSE)
        self.assertEqual(got["source_chain_id"], "solana")
        self.assertEqual(got["source_token_address"], "MintZZQBOLL")
        self.assertEqual(got["change_5m"], -3.0)
        self.assertEqual(got["volume_strength"], 0.9)
        self.assertEqual(got["boll_lower"], 1.0)
        self.assertEqual(got["price_usd"], 0.9)
        self.assertNotEqual(got["token_address"], "MintZZQBOLL")

    def test_same_strategy_formula_on_mapped_row(self) -> None:
        mapped, _stats = map_hot_tokens_to_binance_spot(
            [_hit_row(), _hit_row(token_address="flat", symbol="ZZQFLAT", change_5m=-0.1)],
            listed_symbols={"ZZQBOLLUSDT", "ZZQFLATUSDT"},
        )
        signals = TechBollFadeStrategy().on_tick({"hot_tokens": mapped})
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["strategy_id"], "tech_boll_fade_v1")
        self.assertEqual(signals[0]["token_address"], "ZZQBOLLUSDT")
        self.assertEqual(signals[0]["chain_id"], BINANCE_VENUE)
        self.assertIn("tech_fade&boll_lower", signals[0]["and_mode"])
        stamped = stamp_bnb_spot_signals(signals, mapped)
        self.assertEqual(stamped[0]["source_token_address"], "MintZZQBOLL")
        self.assertEqual(stamped[0]["binance_symbol"], "ZZQBOLLUSDT")

    def test_stamp_drops_dex_mint(self) -> None:
        mapped, _stats = map_hot_tokens_to_binance_spot(
            [_hit_row()],
            listed_symbols={"ZZQBOLLUSDT"},
        )
        leaked = {
            "chain_id": "solana",
            "token_address": "MintZZQBOLL",
            "action": "buy",
        }
        self.assertEqual(stamp_bnb_spot_signals([leaked], mapped), [])

    def test_listing_reader_skips_futures_and_reads_side_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            primary = Path(tmp) / "ind.db"
            side = Path(tmp) / "ccxt.db"
            conn = sqlite3.connect(primary)
            conn.execute(
                """
                CREATE TABLE cex_listing_events (
                    exchange TEXT, symbol TEXT, base TEXT, quote TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO cex_listing_events VALUES (?, ?, ?, ?)",
                ("binance", "BTC/USDT:USDT", "BTC", "USDT"),
            )
            conn.execute(
                "INSERT INTO cex_listing_events VALUES (?, ?, ?, ?)",
                ("okx", "ZZQBOLL/USDT", "ZZQBOLL", "USDT"),
            )
            conn.commit()
            side_conn = sqlite3.connect(side)
            side_conn.execute(
                """
                CREATE TABLE ccxt_ohlcv (
                    exchange TEXT, symbol TEXT, timeframe TEXT, ts INTEGER
                )
                """
            )
            side_conn.execute(
                "INSERT INTO ccxt_ohlcv VALUES ('binance', 'ZZQBOLL/USDT', '5m', 1)"
            )
            side_conn.commit()
            side_conn.close()
            found = load_binance_spot_usdt_symbols(conn, extra_paths=[side])
            conn.close()
            self.assertEqual(found, {"ZZQBOLLUSDT"})

    def test_alias_is_the_same_strategy_class(self) -> None:
        self.assertEqual(resolve_strategy_id("live_bnb_boll_fade"), "tech_boll_fade_v1")
        self.assertEqual(resolve_strategy_id("race_tech_boll_fade"), "tech_boll_fade_v1")
        live = get_strategy("live_bnb_boll_fade")
        race = get_strategy("race_tech_boll_fade")
        self.assertIsInstance(live, TechBollFadeStrategy)
        self.assertIsInstance(race, TechBollFadeStrategy)
        self.assertEqual(live.strategy_id, race.strategy_id)
        self.assertEqual(live.min_change_5m, race.min_change_5m)
        self.assertEqual(live.min_volume_strength, race.min_volume_strength)


def _seed(db: Path) -> None:
    floor = float(de_config.DECISION_MIN_LIQ_USD)
    raw = json.dumps({"liquidity": {"usd": floor}, "priceUsd": "0.9"})
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE token_indicators (
            chain_id TEXT,
            token_address TEXT,
            change_5m REAL,
            volume_strength REAL,
            boll_lower REAL,
            jev_strength REAL,
            tier TEXT,
            primary_pair_address TEXT,
            updated_at TEXT
        )
        """
    )
    rows = [
        ("solana", "MintA", -3.0, 0.9, 1.0, 0.9, "hot", "PairA", "2026-11-05T00:00:00Z"),
        ("solana", "MintNope", -3.0, 0.9, 1.0, 0.8, "hot", "PairNope", "2026-11-05T00:00:00Z"),
        ("solana", "MintFlat", -0.1, 0.9, 1.0, 0.7, "hot", "PairFlat", "2026-11-05T00:00:00Z"),
        ("solana", "MintDup", -3.0, 0.9, 1.0, 0.2, "hot", "PairDup", "2026-11-05T00:00:00Z"),
    ]
    conn.executemany("INSERT INTO token_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute(
        """
        CREATE TABLE pairs (
            chain_id TEXT, pair_address TEXT, token_address TEXT, raw_json TEXT, updated_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO pairs VALUES (?, ?, ?, ?, ?)",
        [
            ("solana", "PairA", "MintA", raw, "2026-11-05T00:00:00Z"),
            ("solana", "PairNope", "MintNope", raw, "2026-11-05T00:00:00Z"),
            ("solana", "PairFlat", "MintFlat", raw, "2026-11-05T00:00:00Z"),
            ("solana", "PairDup", "MintDup", raw, "2026-11-05T00:00:00Z"),
        ],
    )
    conn.execute(
        """
        CREATE TABLE tokens (
            chain_id TEXT, token_address TEXT, raw_json TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO tokens VALUES (?, ?, ?)",
        [
            ("solana", "MintA", json.dumps({"baseToken": {"symbol": "ZZQBOLL"}})),
            ("solana", "MintNope", json.dumps({"symbol": "ZZQNOPE"})),
            ("solana", "MintFlat", json.dumps({"symbol": "ZZQFLAT"})),
            ("solana", "MintDup", json.dumps({"symbol": "ZZQBOLL"})),
        ],
    )
    conn.execute(
        """
        CREATE TABLE cex_listing_events (
            exchange TEXT, symbol TEXT, base TEXT, quote TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO cex_listing_events VALUES (?, ?, ?, ?)",
        [
            ("binance", "ZZQBOLL/USDT", "ZZQBOLL", "USDT"),
            ("binance", "ZZQFLAT/USDT", "ZZQFLAT", "USDT"),
            ("binance", "BTC/USDT:USDT", "BTC", "USDT"),
        ],
    )
    conn.commit()
    conn.close()


def _decision_rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM decision_log ORDER BY id").fetchall())
    finally:
        conn.close()


class EnginePurposeUniverseTests(unittest.TestCase):
    def _tick(self, db: Path, purpose: str) -> list[sqlite3.Row]:
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
            engine.tick()
        finally:
            bus.close()
            adapter.close()
        return _decision_rows(db)

    def test_live_purpose_emits_binance_symbol_only(self) -> None:
        """auto, no indicator table: the DEX∩listing bypass still stamps the symbol.

        Closed klines supply the entry-zone coordinates. Without them the
        live purpose fails closed and does not fall back to the paper legs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            conn = sqlite3.connect(db)
            # Coordinates live on the mapped DEX row. A binance_klines table
            # would switch auto mode off dex_map and onto the kline factor path.
            conn.execute("ALTER TABLE token_indicators ADD COLUMN boll_mid REAL")
            conn.execute("ALTER TABLE token_indicators ADD COLUMN kline_low REAL")
            conn.execute("ALTER TABLE token_indicators ADD COLUMN trade_size_ratio REAL")
            conn.execute(
                """
                UPDATE token_indicators
                SET boll_mid = 1.2, kline_low = 0.825, trade_size_ratio = 2.0
                WHERE token_address = 'MintA'
                """
            )
            conn.commit()
            conn.close()
            rows = self._tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["purpose"], PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(row["strategy_id"], "tech_boll_fade_v1")
            self.assertEqual(row["chain_id"], BINANCE_VENUE)
            self.assertEqual(row["token_address"], "ZZQBOLLUSDT")
            self.assertEqual(row["mode"], "dry_run")
            raw = json.loads(row["raw_json"])
            extras = raw["extras"]
            self.assertEqual(raw["pair_address"], "ZZQBOLL/USDT")
            self.assertEqual(extras["binance_symbol"], "ZZQBOLLUSDT")
            self.assertEqual(extras["market"], "spot")
            self.assertEqual(extras["quote_asset"], "USDT")
            self.assertEqual(extras["universe"], CEX_UNIVERSE)
            self.assertEqual(extras["source_chain_id"], "solana")
            self.assertEqual(extras["source_token_address"], "MintA")
            self.assertEqual(extras["and_mode"], "entry_zone")
            self.assertEqual(extras["entry_reason"], "large_trade_market")
            self.assertEqual(extras["order_type"], "MARKET")
            self.assertIn("stop_price", extras)
            self.assertIn("take_profit_price", extras)
            self.assertIn("entry_zone_min", extras)
            self.assertIn("alpha", extras)
            self.assertIn("trade_size_ratio", extras)
            self.assertNotIn("local_score", extras)
            self.assertNotIn("MintA", row["token_address"])
            self.assertNotIn("MintNope", row["token_address"])

    def test_race_purpose_still_emits_dex_mints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            rows = self._tick(db, PURPOSE_RACE_TECH_BOLL_FADE)
            self.assertGreaterEqual(len(rows), 1)
            tokens = {row["token_address"] for row in rows}
            self.assertIn("MintA", tokens)
            self.assertIn("MintNope", tokens)
            self.assertNotIn("ZZQBOLLUSDT", tokens)
            for row in rows:
                self.assertEqual(row["purpose"], PURPOSE_RACE_TECH_BOLL_FADE)
                self.assertEqual(row["chain_id"], "solana")
                self.assertEqual(row["strategy_id"], "tech_boll_fade_v1")
                extras = json.loads(row["raw_json"])["extras"]
                self.assertNotIn("binance_symbol", extras)


if __name__ == "__main__":
    unittest.main()
