"""Unit tests for Dual-Mode Perp Short Alpha Engine & Gated USDT-M Perp Short Adapter."""

from __future__ import annotations

from dataclasses import asdict
import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from decision_engine.core.perp_short_alpha_engine import (
    build_perp_beta_hedge_orders,
    evaluate_directional_short_candidates,
    is_short_squeeze_veto,
    map_spot_to_usdm_perp,
    run_perp_short_tick,
)
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from execution.executors.binance_perp_short import BinancePerpShortAdapter


class TestPerpShortAlphaEngineAndExecutor(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_1000x_contract_mapping(self) -> None:
        perp, scale = map_spot_to_usdm_perp("PEPEUSDT")
        self.assertEqual(perp, "1000PEPEUSDT")
        self.assertEqual(scale, 1000.0)

        perp_btc, scale_btc = map_spot_to_usdm_perp("BTCUSDT")
        self.assertEqual(perp_btc, "BTCUSDT")
        self.assertEqual(scale_btc, 1.0)

    def test_mode_a_state4_50pct_and_state5_100pct_beta_hedge_shield(self) -> None:
        open_positions = [
            {
                "symbol": "SOLUSDT",
                "status": "open",
                "quantity": 1.0,
                "entry_price": 150.0,
            }
        ]
        hot = [
            {"binance_symbol": "BTCUSDT", "price_usd": 90000.0},
            {"binance_symbol": "ETHUSDT", "price_usd": 3000.0},
            {"binance_symbol": "SOLUSDT", "price_usd": 150.0},
        ]
        state4_buys = [
            {
                "action": "buy",
                "binance_symbol": "SOLUSDT",
                "size_pct": 0.10,
                "market_regime": "crowded_long_defensive",
                "require_perp_beta_hedge": True,
            }
        ]
        # SOL beta = 1.45; open 150 + candidate 32 = 182 gross -> 263.9 beta_weighted
        # State 4 -> 50% hedge ratio -> ~131.95 beta USDT split across BTCUSDT & ETHUSDT
        orders_s4 = build_perp_beta_hedge_orders(
            open_positions,
            state4_buys,
            hot,
            book_usdt=320.0,
        )
        self.assertEqual(len(orders_s4), 2)
        self.assertTrue(all(o.mode == "beta_hedge" for o in orders_s4))
        self.assertTrue(all(o.playbook == "state4_defensive_beta_hedge_50pct" for o in orders_s4))
        self.assertTrue(all(o.order_type == "LIMIT_MAKER" for o in orders_s4))
        self.assertTrue(all(o.leverage == 1 and o.margin_type == "ISOLATED" for o in orders_s4))

        # State 5 (macro_halt active, zero new spot buys) -> 100% full Beta lock via MARKET
        orders_s5 = build_perp_beta_hedge_orders(
            open_positions,
            [],
            hot,
            macro_halt="fomc_blackout",
            book_usdt=320.0,
        )
        self.assertEqual(len(orders_s5), 2)
        self.assertTrue(all(o.playbook == "state5_full_beta_lock_100pct" for o in orders_s5))
        self.assertTrue(all(o.order_type == "MARKET" for o in orders_s5))
        s4_total = sum(o.notional_usdt or 0.0 for o in orders_s4)
        s5_total = sum(o.notional_usdt or 0.0 for o in orders_s5)
        self.assertGreater(s5_total, s4_total * 1.4)

    def test_mode_b_all_three_directional_short_spears_and_wall_mid_pricing(self) -> None:
        hot = [
            # Spear 1: Speculative altcoin in VC cliff unlock window
            {
                "binance_symbol": "ALTXYZUSDT",
                "price_usd": 2.00,
                "in_unlock_blackout": 1,
                "obi_top5": -0.35,
                "taker_buy_ratio": 0.39,
                "funding_rate": 0.0001,
            },
            # Spear 2: PEPE crowded retail long trap + positive funding carry + L2 WallMid
            {
                "binance_symbol": "PEPEUSDT",
                "price_usd": 0.00001000,
                "global_account_ls_ratio": 2.65,
                "whale_retail_divergence": 0.35,
                "funding_rate": 0.00045,
                "funding_z": 2.6,
                "taker_buy_ratio": 0.42,
                "tick_size": 0.00000001,
                "bids": [
                    [0.00000998, 100000000],
                    [0.00000995, 120000000],
                    [0.00000990, 5000000000],
                ],
                "asks": [
                    [0.00001002, 100000000],
                    [0.00001005, 120000000],
                    [0.00001010, 8000000000],
                ],
            },
            # Spear 3: Cross-asset NVDA -2.6% dump + Leave-One-Out put-wing spike
            {
                "binance_symbol": "FETUSDT",
                "price_usd": 1.50,
                "nvda_1h_ret_pct": -2.6,
                "put_wing_residual_provenance": "leave_one_out",
                "put_wing_iv_residual_pct": 5.2,
                "taker_buy_ratio": 0.40,
            },
        ]
        signals = evaluate_directional_short_candidates(hot, book_usdt=320.0, max_shorts=3)
        by_playbook = {s.playbook: s for s in signals}
        self.assertEqual(
            set(by_playbook.keys()),
            {
                "unlock_cliff_short",
                "crowded_long_funding_carry_short",
                "cross_asset_crash_contagion_short",
            },
        )

        pepe_sig = by_playbook["crowded_long_funding_carry_short"]
        self.assertEqual(pepe_sig.perp_symbol, "1000PEPEUSDT")
        self.assertEqual(pepe_sig.order_type, "LIMIT_MAKER")
        # 0.00001010 ask wall - 1 tick (0.00000001) = 0.00001009 * 1000 = 0.01009
        self.assertAlmostEqual(pepe_sig.entry_price or 0.0, 0.01009, places=6)
        self.assertGreater(pepe_sig.stop_price or 0.0, pepe_sig.entry_price or 0.0)
        self.assertLess(pepe_sig.take_profit_price or 0.0, pepe_sig.entry_price or 0.0)
        self.assertAlmostEqual(pepe_sig.notional_usdt or 0.0, 64.0, places=2)  # 20% of 320

        unlock_sig = by_playbook["unlock_cliff_short"]
        self.assertAlmostEqual(unlock_sig.notional_usdt or 0.0, 48.0, places=2)  # 15% of 320

    def test_missing_book_does_not_open_unlock_short_and_call_only_smile_does_not_abort(self) -> None:
        missing_book = evaluate_directional_short_candidates(
            [
                {
                    "binance_symbol": "ALTXYZUSDT",
                    "price_usd": 2.00,
                    "in_unlock_blackout": 1,
                    "funding_rate": 0.0001,
                }
            ],
            book_usdt=320.0,
        )
        self.assertEqual(missing_book, [])

        spot = 100.0
        tte = 7.0 / 365.0
        chain = []
        for strike in (100, 103, 106, 110, 114):
            moneyness = math.log(strike / spot) / math.sqrt(tte)
            chain.append({"strike": strike, "iv_pct": 50.0 + 40.0 * moneyness * moneyness, "tte_years": tte})
        mixed = evaluate_directional_short_candidates(
            [
                {
                    "binance_symbol": "ALTXYZUSDT",
                    "price_usd": 2.00,
                    "in_unlock_blackout": 1,
                    "obi_top5": -0.35,
                    "taker_buy_ratio": 0.39,
                    "funding_rate": 0.0001,
                },
                {
                    "binance_symbol": "FETUSDT",
                    "price_usd": spot,
                    "option_chain": chain,
                    "taker_buy_ratio": 0.20,
                },
            ],
            book_usdt=320.0,
            max_shorts=3,
        )
        self.assertEqual([row.playbook for row in mixed], ["unlock_cliff_short"])

    def test_hard_anti_squeeze_veto_blocks_open_and_emits_emergency_close(self) -> None:
        veto, reason = is_short_squeeze_veto({"funding_z": -1.4, "funding_rate": -0.0003})
        self.assertTrue(veto)
        self.assertEqual(reason, "veto_negative_funding_z_squeeze_risk")

        hot = [
            {
                "binance_symbol": "SOLUSDT",
                "price_usd": 150.0,
                "in_unlock_blackout": 1,
                "global_account_ls_ratio": 2.8,
                "whale_retail_divergence": 0.30,
                "funding_z": -1.5,
                "has_open_perp_short": True,
            }
        ]
        signals = evaluate_directional_short_candidates(hot, book_usdt=320.0)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].action, "close_short")
        self.assertEqual(signals[0].mode, "squeeze_emergency_exit")
        self.assertEqual(signals[0].order_type, "MARKET")

    def test_gated_perp_short_adapter_safety_gates_and_pnl_lifecycle(self) -> None:
        ledger_db = self.root / "perp_ledger.db"
        adapter = BinancePerpShortAdapter(ledger_db, dry_run=True, book_usdt=320.0)

        # 1. Reject Cross Margin
        r_cross = adapter.execute_signal(
            {
                "action": "open_short",
                "perp_symbol": "SOLUSDT",
                "margin_type": "CROSS",
                "entry_price": 150.0,
                "stop_price": 153.0,
                "take_profit_price": 142.0,
                "notional_usdt": 60.0,
            }
        )
        self.assertFalse(r_cross.accepted)
        self.assertEqual(r_cross.reject_reason, "cross_margin_forbidden")

        # 2. Reject invalid stop (stop below entry on a short!)
        r_bad_stop = adapter.execute_signal(
            {
                "action": "open_short",
                "perp_symbol": "SOLUSDT",
                "margin_type": "ISOLATED",
                "entry_price": 150.0,
                "stop_price": 148.0,
                "take_profit_price": 142.0,
                "notional_usdt": 60.0,
            }
        )
        self.assertFalse(r_bad_stop.accepted)
        self.assertEqual(r_bad_stop.reject_reason, "short_stop_must_be_above_entry")

        # 3. Reject < 20 USDT notional
        r_dust = adapter.execute_signal(
            {
                "action": "open_short",
                "perp_symbol": "SOLUSDT",
                "margin_type": "ISOLATED",
                "entry_price": 150.0,
                "stop_price": 153.0,
                "take_profit_price": 142.0,
                "notional_usdt": 15.0,
            }
        )
        self.assertFalse(r_dust.accepted)
        self.assertEqual(r_dust.reject_reason, "notional_below_20_usdt_floor")

        # 4. Valid open_short clamps 10x request down to safe 1x default & opens cleanly
        r_open = adapter.execute_signal(
            {
                "action": "open_short",
                "perp_symbol": "SOLUSDT",
                "spot_symbol": "SOLUSDT",
                "playbook": "crowded_long_funding_carry_short",
                "margin_type": "ISOLATED",
                "leverage": 10,
                "entry_price": 150.0,
                "stop_price": 153.0,
                "take_profit_price": 142.5,
                "notional_usdt": 60.0,
            }
        )
        self.assertTrue(r_open.accepted)
        self.assertEqual(r_open.leverage, 1)
        self.assertAlmostEqual(r_open.quantity or 0.0, 0.4, places=6)

        # 5. Duplicate open on same symbol rejected
        r_dup = adapter.execute_signal(
            {
                "action": "open_short",
                "perp_symbol": "SOLUSDT",
                "margin_type": "ISOLATED",
                "entry_price": 150.0,
                "stop_price": 153.0,
                "take_profit_price": 142.5,
                "notional_usdt": 60.0,
            }
        )
        self.assertFalse(r_dup.accepted)
        self.assertEqual(r_dup.reject_reason, "already_holding_open_short")

        # 6. Take-profit mark trigger closes short with positive PnL: (150 - 140) * 0.4 = +4.0 USDT
        exits = adapter.evaluate_protective_exits({"SOLUSDT": 140.0})
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0].accepted)
        self.assertAlmostEqual(exits[0].closed_pnl_usdt or 0.0, 4.0, places=4)
        self.assertEqual(adapter.open_positions(), [])

    def test_engine_tick_fires_perp_short_even_when_spot_candidates_empty(self) -> None:
        from decision_engine.core.engine import DecisionEngine

        bus_db = self.root / "signals.db"
        adapter = MagicMock()
        adapter.db_path = bus_db
        adapter.conn = sqlite3.connect(str(bus_db))
        adapter.count_indicators.return_value = 10
        crash_hot = [
            {
                "binance_symbol": "BTCUSDT",
                "symbol": "BTCUSDT",
                "price_usd": 90000.0,
                "liq_cascade_regime": "long_liquidation_waterfall",
                "taker_buy_ratio": 0.35,
            }
        ]
        adapter.list_hot_tokens.return_value = crash_hot

        book = ProtectionBook.ready(
            positions=[
                {
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "symbol": "SOLUSDT",
                    "status": "open",
                    "entry_order_id": "101",
                    "protection": "exchange_oco",
                    "oco_order_list_id": "201",
                    "oco_above_order_id": "202",
                    "oco_below_order_id": "203",
                    "quantity": 1.0,
                    "entry_price": 150.0,
                }
            ],
            canonical=[],
        )

        with SignalBus(bus_db, dry_run=True, purpose=PURPOSE_LIVE_BNB_BOLL_FADE) as bus:
            engine = DecisionEngine(
                adapter,
                bus,
                strategies=[],  # Zero spot candidates!
                protection_book=book,
            )
            with (
                patch.object(engine, "_load_live_bnb_hot", return_value=(crash_hot, "binance_indicators")),
                patch("decision_engine.core.engine.filter_live_bnb_hot", return_value=(crash_hot, {"kept": 1})),
                patch("decision_engine.core.engine.evaluate_macro_gate") as mock_macro,
                patch.dict(os.environ, {"ENABLE_PERP_SHORT_ENGINE": "1", "CITYU_INSTITUTIONAL_DB_PATH": str(self.root / "none.db")}, clear=False),
            ):
                mock_macro.return_value = MagicMock(halt_reason=None)
                out_on = engine.tick()

            self.assertEqual(out_on["candidates"], 0)
            self.assertIn("perp_short", out_on)
            self.assertGreaterEqual(out_on["perp_short"]["hedge_orders"], 1)
            self.assertGreaterEqual(out_on["perp_short"]["directional_orders"], 1)
            # Spot decision_log remains completely clean (0 rows) while perp_short_signals is populated
            dlog_n = adapter.conn.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0]
            pshort_n = adapter.conn.execute("SELECT COUNT(*) FROM perp_short_signals").fetchone()[0]
            self.assertEqual(dlog_n, 0)
            self.assertGreaterEqual(pshort_n, 2)

            # When ENABLE_PERP_SHORT_ENGINE is unset, tick payload has no perp_short key
            with (
                patch.object(engine, "_load_live_bnb_hot", return_value=(crash_hot, "binance_indicators")),
                patch("decision_engine.core.engine.filter_live_bnb_hot", return_value=(crash_hot, {"kept": 1})),
                patch("decision_engine.core.engine.evaluate_macro_gate") as mock_macro2,
                patch.dict(os.environ, {"ENABLE_PERP_SHORT_ENGINE": "0"}, clear=False),
            ):
                mock_macro2.return_value = MagicMock(halt_reason=None)
                out_off = engine.tick()
            self.assertNotIn("perp_short", out_off)
        adapter.conn.close()


if __name__ == "__main__":
    unittest.main()
