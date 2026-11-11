"""Both live arms through a temp-db tick, then the same router the engine calls.

AGENTS.md is the design note for State 3 thresholds. It is not a runner.
Missing obi_top5 / volume_z does not reject. Kelly metadata is not the
quote. Execution sizes ``size_pct * BINANCE_SPOT_BOOK_USDT``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies import get_strategy
from execution.executors.binance_spot_protect import resolve_entry_size

_BOOK = {"BINANCE_SPOT_BOOK_USDT": "400"}


def _zone_row(symbol: str = "FADEUSDT") -> tuple:
    low, span = 100.0, 10.0
    price = low + 0.08 * span
    return (
        symbol,
        symbol[:-4],
        "USDT",
        "TRADING",
        price,
        -4.0,
        0.9,
        price + 1.0,
        low + span,
        low,
        1.3,
        float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0,
    )


def _classic_row() -> tuple:
    return (
        "KEEPUSDT",
        "KEEP",
        "USDT",
        "TRADING",
        0.8,
        -3.0,
        0.9,
        1.0,
        1.2,
        None,
        None,
        float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0,
    )


def _seed(db: Path, row: tuple) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE binance_indicators (
            symbol TEXT PRIMARY KEY,
            base_asset TEXT,
            quote_asset TEXT,
            status TEXT,
            price_usd REAL,
            change_5m REAL,
            volume_strength REAL,
            boll_lower REAL,
            boll_mid REAL,
            kline_low REAL,
            trade_size_ratio REAL,
            quote_volume_24h REAL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO binance_indicators (
            symbol, base_asset, quote_asset, status,
            price_usd, change_5m, volume_strength, boll_lower,
            boll_mid, kline_low, trade_size_ratio, quote_volume_24h
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )
    now_ms = int(time.time() * 1000)
    conn.execute(
        "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
    )
    conn.execute(
        "INSERT INTO symbols VALUES (?, 'TRADING', ?, 'USDT')",
        (row[0], row[1]),
    )
    conn.execute(
        """
        CREATE TABLE ccxt_ticks (
            exchange TEXT, symbol TEXT, ts INTEGER,
            quote_volume_24h REAL, volume_24h REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO ccxt_ticks VALUES ('binance', ?, ?, 3000000, 1)",
        (f"{row[1]}/USDT", now_ms),
    )
    conn.commit()
    conn.close()


def _tick(db: Path, purpose: str) -> dict:
    adapter = IndicatorDataAdapter(db)
    bus = SignalBus(db, purpose=purpose)
    old = os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
    try:
        engine = DecisionEngine(
            adapter,
            bus,
            [get_strategy("tech_boll_fade_v1")],
            risk=RiskManager(cooldown_sec=0),
        )
        return engine.tick()
    finally:
        if old is None:
            os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        else:
            os.environ["LIVE_BNB_FACTOR_SOURCE"] = old
        bus.close()
        adapter.close()


def _written(db: Path, purpose: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT token_address, size_pct, raw_json FROM decision_log WHERE purpose = ?",
            (purpose,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _bull(**extra: object) -> dict:
    row = {
        "symbol": "SOLUSDT",
        "binance_symbol": "SOLUSDT",
        "nvda_1h_ret_pct": 1.60,
        "xs_breadth_1h": 0.75,
        "xs_rank_pct_1h": 0.85,
    }
    row.update(extra)
    return row


class BothArmTickTests(unittest.TestCase):
    def test_classic_and_fade_ticks_emit_strategy_size_not_kelly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            classic_db = Path(tmp) / "classic.db"
            fade_db = Path(tmp) / "fade.db"
            _seed(classic_db, _classic_row())
            _seed(fade_db, _zone_row())
            classic = _tick(classic_db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            fade = _tick(fade_db, PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(classic["bnb_factor_source"], "binance_indicators")
            self.assertEqual(fade["bnb_factor_source"], "binance_indicators")
            self.assertEqual(classic["emitted"], 1)
            self.assertEqual(fade["emitted"], 1)
            for db, purpose in (
                (classic_db, PURPOSE_LIVE_BNB_BOLL_CLASSIC),
                (fade_db, PURPOSE_LIVE_BNB_BOLL_FADE),
            ):
                rows = _written(db, purpose)
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(float(rows[0]["size_pct"]), 0.05)
                raw = json.loads(rows[0]["raw_json"])
                extras = raw.get("extras") or {}
                self.assertNotEqual(extras.get("optimal_base_position_pct"), 0.05)
                self.assertAlmostEqual(extras.get("optimal_base_position_pct"), 0.185)
                self.assertNotIn("use_optimal_kelly_matrix", extras)
                self.assertNotIn("obi_top5", extras)
                self.assertNotIn("volume_z", extras)

    def test_execution_quote_follows_emitted_size_pct_not_kelly_stamp(self) -> None:
        extras = {
            "order_type": "MARKET",
            "optimal_base_position_pct": 0.185,
            "optimal_target_cumulative_pct": 0.185,
            "market_regime": REGIME_1_POSITIVE_GAMMA_PINNED,
        }
        sized, err, plan = resolve_entry_size(
            {"action": "buy", "size_pct": 0.05},
            extras,
            environ=_BOOK,
        )
        self.assertIsNone(err)
        assert plan is not None
        self.assertEqual(plan["source"], "size_pct")
        self.assertEqual(plan["size_pct"], "0.05")
        self.assertEqual(sized["quote_order_qty"], "20")
        routed, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "ETHUSDT", "funding_z": -2.45}],
        )
        self.assertAlmostEqual(routed[0]["size_pct"], 0.06)
        self.assertAlmostEqual(routed[0]["optimal_base_position_pct"], 0.35)
        squeezed, squeeze_err, squeeze_plan = resolve_entry_size(
            {"action": "buy", "size_pct": routed[0]["size_pct"]},
            {
                "order_type": "MARKET",
                "optimal_base_position_pct": routed[0]["optimal_base_position_pct"],
                "optimal_target_cumulative_pct": routed[0]["optimal_target_cumulative_pct"],
            },
            environ=_BOOK,
        )
        self.assertIsNone(squeeze_err)
        assert squeeze_plan is not None
        self.assertEqual(squeeze_plan["size_pct"], "0.06")
        self.assertEqual(squeezed["quote_order_qty"], "24")


class CollectorDoesNotSupplyState3FieldsTests(unittest.TestCase):
    def test_spot_microstructure_imbalance_20_does_not_reject_the_tick(self) -> None:
        """The collector stores a 20-level imbalance, not ``obi_top5`` or ``volume_z``.

        A full fade tick must keep the buy. Renaming that column would invent
        a top-5 book the table does not contain.
        """
        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book = root / "public_derivatives.db"
            conn = sqlite3.connect(book)
            conn.execute(
                """
                CREATE TABLE binance_spot_microstructure (
                    symbol TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    ret_5m_pct REAL,
                    ret_1h_pct REAL,
                    ret_4h_pct REAL,
                    ret_1d_pct REAL,
                    taker_buy_ratio_5m REAL,
                    taker_buy_ratio_1h REAL,
                    spread_bps REAL,
                    bid_depth_20_usd REAL,
                    ask_depth_20_usd REAL,
                    orderbook_imbalance_20 REAL,
                    impact_10k_bps REAL,
                    whale_net_buy_usd REAL,
                    aggtrade_whale_buy_ratio REAL,
                    PRIMARY KEY (symbol, ts_ms)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO binance_spot_microstructure (
                    symbol, ts_ms, orderbook_imbalance_20, taker_buy_ratio_5m
                ) VALUES ('FADEUSDT', ?, -0.90, 0.10)
                """,
                (now_ms,),
            )
            conn.commit()
            conn.close()
            fade_db = root / "fade.db"
            _seed(fade_db, _zone_row())
            old_db = os.environ.get("PUBLIC_DERIVATIVES_DB_PATH")
            os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(book)
            try:
                payload = _tick(fade_db, PURPOSE_LIVE_BNB_BOLL_FADE)
            finally:
                if old_db is None:
                    os.environ.pop("PUBLIC_DERIVATIVES_DB_PATH", None)
                else:
                    os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = old_db
            self.assertEqual(payload["emitted"], 1)
            raw = json.loads(_written(fade_db, PURPOSE_LIVE_BNB_BOLL_FADE)[0]["raw_json"])
            extras = raw.get("extras") or {}
            self.assertNotIn("obi_top5", extras)
            self.assertNotIn("spot_obi_top5", extras)
            self.assertNotIn("volume_z", extras)
            self.assertNotIn("vol_z_1h", extras)
            self.assertNotIn("orderbook_imbalance_20", extras)
            self.assertNotEqual(extras.get("market_regime"), "cross_asset_momentum_pullback")
            reason = str(raw.get("reason") or "")
            self.assertNotIn("regime3_thin_bid_book_trap", reason)
            self.assertNotIn("regime3_climax_volume_distribution_trap", reason)


class StateRoutingTests(unittest.TestCase):
    def test_state3_doc_thresholds_and_missing_fields(self) -> None:
        thin = evaluate_five_state_regime(_bull(obi_top5=-0.25, taker_buy_ratio=0.40))
        self.assertFalse(thin.allow_buy)
        self.assertEqual(thin.reject_reason, "regime3_thin_bid_book_trap")
        strong_taker = evaluate_five_state_regime(_bull(obi_top5=-0.25, taker_buy_ratio=0.80))
        self.assertTrue(strong_taker.allow_buy)
        climax = evaluate_five_state_regime(
            _bull(volume_z=3.8, whale_retail_divergence=0.55)
        )
        self.assertFalse(climax.allow_buy)
        self.assertEqual(climax.reject_reason, "regime3_climax_volume_distribution_trap")
        not_distributing = evaluate_five_state_regime(
            _bull(volume_z=3.8, whale_retail_divergence=1.5)
        )
        self.assertTrue(not_distributing.allow_buy)
        absent = evaluate_five_state_regime(_bull())
        self.assertTrue(absent.allow_buy)
        self.assertIsNone(absent.reject_reason)

    def test_states_1_2_4_5_and_sell_do_not_change(self) -> None:
        state1, _ = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "SOLUSDT"}],
        )
        self.assertEqual(state1[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertAlmostEqual(state1[0]["size_pct"], 0.05)

        state2, _ = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "ETHUSDT", "funding_z": -2.45}],
        )
        self.assertEqual(state2[0]["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        self.assertAlmostEqual(state2[0]["size_pct"], 0.06)

        state4, _ = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.05}],
            [{"binance_symbol": "SOLUSDT", "funding_z": 2.0}],
        )
        self.assertEqual(state4[0]["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertAlmostEqual(state4[0]["size_pct"], 0.0275)

        mixed, counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.05},
                {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 1.0},
            ],
            [{"binance_symbol": "SOLUSDT", "liq_cascade_regime": "long_liquidation_waterfall"}],
        )
        self.assertEqual(counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)
        self.assertEqual([row["action"] for row in mixed], ["sell"])
        self.assertEqual(mixed[0]["size_pct"], 1.0)


if __name__ == "__main__":
    unittest.main()
