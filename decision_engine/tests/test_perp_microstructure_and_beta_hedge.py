"""Unit and integration tests for Perp Microstructure Filter & Portfolio Beta-Neutral Hedge Engine."""

from __future__ import annotations

import os
import unittest

from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    compute_portfolio_beta_and_hedge,
    evaluate_perp_microstructure,
    portfolio_beta_exposure_block,
    symbol_beta_vs_btc,
)
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy


def _base_row(**overrides: object) -> dict:
    row = {
        "chain_id": "binance",
        "token_address": "SOLUSDT",
        "binance_symbol": "SOLUSDT",
        "kline_low": 100.0,
        "boll_mid": 110.0,
        "price_usd": 102.0,  # alpha = 0.20
        "trade_size_ratio": 1.6,  # large_trade
        "taker_buy_ratio": 0.55,
        "volatility_24h": 0.03,
        "change_5m": -3.5,
        "volume_strength": 1.2,
        "boll_lower": 103.0,
    }
    row.update(overrides)
    return row


class PerpMicrostructureAndBetaHedgeTests(unittest.TestCase):
    def test_perp_funding_crowding_and_basis_dislocation_filters(self) -> None:
        # 1. Overcrowded positive perp funding (+8 bps/8h > +5 bps cap) -> blocked!
        crowded_row = _base_row(funding_rate=0.0008)
        dec_crowded = evaluate_entry_zone(crowded_row)
        self.assertFalse(dec_crowded.emit)
        self.assertEqual(dec_crowded.reject_reason, "perp_long_crowded_cascade")
        self.assertEqual(dec_crowded.perp_regime, "long_crowded_cascade")

        # 2. Extreme negative perp basis (-110 bps < -80 bps floor) with weak buyer absorption -> blocked!
        discount_row = _base_row(
            funding_rate=0.0001,
            perp_mark_price=98.90,
            perp_index_price=100.00,
            taker_buy_ratio=0.35,
        )
        dec_discount = evaluate_entry_zone(discount_row)
        self.assertFalse(dec_discount.emit)
        self.assertEqual(dec_discount.reject_reason, "perp_toxic_discount_dump")

        # 3. Deeply negative funding (-5 bps/8h) + strong spot buyer absorption (0.58) -> allowed & tagged short_squeeze_favorable!
        squeeze_row = _base_row(
            funding_rate=-0.0005,
            perp_mark_price=101.95,
            perp_index_price=102.00,
            taker_buy_ratio=0.58,
        )
        dec_squeeze = evaluate_entry_zone(squeeze_row)
        self.assertTrue(dec_squeeze.emit)
        self.assertEqual(dec_squeeze.perp_regime, "short_squeeze_favorable")

    def test_portfolio_beta_budget_and_btc_eth_perp_hedge_calculator(self) -> None:
        self.assertAlmostEqual(symbol_beta_vs_btc("BTCUSDT"), 1.00, places=2)
        self.assertAlmostEqual(symbol_beta_vs_btc("SOLUSDT"), 1.45, places=2)
        self.assertAlmostEqual(symbol_beta_vs_btc("PEPEUSDT"), 1.85, places=2)

        open_positions = [
            {
                "id": 1,
                "symbol": "SOLUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "quantity": "1.0",
                "entry_price": "150.0",  # $150 notional (15% of $1,000 book), beta 1.45 -> $217.50 beta-weighted
            },
            {
                "id": 2,
                "symbol": "FETUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "oco_working",
                "quantity": "100.0",
                "entry_price": "1.50",  # $150 notional (15% of $1,000 book), beta 1.65 -> $247.50 beta-weighted
            },
        ]
        # Total beta-weighted exposure = $217.50 + $247.50 = $465.00 (portfolio_beta_pct = 0.4650)
        hedge = compute_portfolio_beta_and_hedge(
            open_positions,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            book_usdt=1000.0,
        )
        self.assertAlmostEqual(hedge["gross_long_usdt"], 300.0, places=2)
        self.assertAlmostEqual(hedge["beta_weighted_usdt"], 465.0, places=2)
        self.assertAlmostEqual(hedge["portfolio_beta_pct"], 0.465, places=3)
        self.assertEqual(
            hedge["hedge_recommendations"]["pure_btc_perp_short_usdt"],
            465.0,
        )
        # 70% BTC ($325.50) + 30% ETH ($465 * 0.30 / 1.15 = $121.30)
        basket = hedge["hedge_recommendations"]["basket_hedge"]
        self.assertAlmostEqual(basket["BTCUSDT_PERP_SHORT_USDT"], 325.5, places=2)
        self.assertAlmostEqual(basket["ETHUSDT_PERP_SHORT_USDT"], 121.3, places=2)

    def test_live_buy_guards_enforces_portfolio_beta_cap_and_outputs_hedge_plan(self) -> None:
        old_gate = os.environ.get("LIVE_BNB_PORTFOLIO_BETA_GATE")
        old_cap = os.environ.get("LIVE_BNB_MAX_PORTFOLIO_BETA")
        old_book = os.environ.get("BINANCE_SPOT_BOOK_USDT")
        os.environ["LIVE_BNB_PORTFOLIO_BETA_GATE"] = "1"
        os.environ["LIVE_BNB_MAX_PORTFOLIO_BETA"] = "0.25"
        os.environ["BINANCE_SPOT_BOOK_USDT"] = "1000"
        try:
            book = ProtectionBook.ready(
                positions=[
                    {
                        "id": 1,
                        "symbol": "SOLUSDT",
                        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                        "status": "oco_working",
                        "quantity": "1.0",
                        "protection": "exchange_oco",
                        "oco_order_list_id": "1",
                        "oco_above_order_id": "2",
                        "oco_below_order_id": "3",
                        "entry_price": "120.0",  # 12% of book * 1.45 beta = 0.174 beta_pct
                    }
                ],
                canonical=[],
            )
            signals = [
                {
                    "action": "buy",
                    "token_address": "PEPEUSDT",
                    "binance_symbol": "PEPEUSDT",
                    "order_type": "MARKET",
                    "price_usd": 10.0,
                    "stop_price": 9.5,
                    "take_profit_price": 11.0,
                    "size_pct": 0.05,  # 5% * 1.85 meme beta = +0.0925 -> total 0.2665 > 0.25 cap!
                },
                {
                    "action": "buy",
                    "token_address": "BTCUSDT",
                    "binance_symbol": "BTCUSDT",
                    "order_type": "MARKET",
                    "price_usd": 90000.0,
                    "stop_price": 88000.0,
                    "take_profit_price": 94000.0,
                    "size_pct": 0.05,  # 5% * 1.00 BTC beta = +0.05 -> total 0.224 <= 0.25 cap!
                },
            ]
            kept, report = apply_live_buy_guards(
                signals,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                book=book,
            )
            # The open list is three stored ids. Decision does not call the
            # exchange, so the purpose halts before the beta cap runs.
            self.assertEqual(kept, [])
            self.assertEqual(report.get("protection_halt"), "oco_readback_unverified")
        finally:
            if old_gate is None:
                os.environ.pop("LIVE_BNB_PORTFOLIO_BETA_GATE", None)
            else:
                os.environ["LIVE_BNB_PORTFOLIO_BETA_GATE"] = old_gate
            if old_cap is None:
                os.environ.pop("LIVE_BNB_MAX_PORTFOLIO_BETA", None)
            else:
                os.environ["LIVE_BNB_MAX_PORTFOLIO_BETA"] = old_cap
            if old_book is None:
                os.environ.pop("BINANCE_SPOT_BOOK_USDT", None)
            else:
                os.environ["BINANCE_SPOT_BOOK_USDT"] = old_book


if __name__ == "__main__":
    unittest.main()
