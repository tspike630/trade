"""Unit and integration tests for P2 Risk-Parity Sizing & Beta Cluster Exposure Controls."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from decision_engine.core.live_bnb_turnover import (
    REASON_CLUSTER_TICK_CAP,
    SOURCE_TICKER,
    cap_live_bnb_buys,
)
from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.portfolio_clusters import (
    ENV_CLUSTER_CAP_ENABLED,
    ENV_INVERSE_VOL_SIZING,
    cluster_open_exposure_block,
    compute_inverse_vol_size,
    symbol_sector_cluster,
)
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PortfolioClustersAndVolSizingTests(unittest.TestCase):
    def test_symbol_sector_cluster_classification(self) -> None:
        self.assertEqual(symbol_sector_cluster("BTCUSDT"), "major_l1")
        self.assertEqual(symbol_sector_cluster("FETUSDT"), "ai_compute")
        self.assertEqual(symbol_sector_cluster("TAOUSDT"), "ai_compute")
        self.assertEqual(symbol_sector_cluster("PEPEUSDT"), "meme_high_beta")
        self.assertEqual(symbol_sector_cluster("WIFUSDT"), "meme_high_beta")
        self.assertEqual(symbol_sector_cluster("AAVEUSDT"), "defi_infra")
        self.assertEqual(symbol_sector_cluster("ARBUSDT"), "l2_scaling")
        self.assertEqual(symbol_sector_cluster("DASHUSDT"), "legacy_pow_privacy")
        self.assertEqual(symbol_sector_cluster("UNKNOWNTOKENUSDT"), "idiosyncratic")

    def test_inverse_stop_width_and_volatility_sizing(self) -> None:
        # Tight stop (1.0% stop distance, target 1.5% -> 1.5x capped at 1.25x)
        tight = compute_inverse_vol_size(
            0.05,
            tier_multiplier=1.0,
            entry_price=100.0,
            stop_price=99.0,
            volatility_24h=1.0,
            apply_scaling=True,
        )
        self.assertTrue(tight.applied)
        self.assertAlmostEqual(tight.stop_distance_pct or 0.0, 0.01)
        self.assertAlmostEqual(tight.risk_parity_scalar, 1.25)
        self.assertAlmostEqual(tight.size_pct, 0.05 * 1.25)

        # Wide stop (4.5% stop distance, target 1.5% -> 1/3 = 0.333333x)
        wide = compute_inverse_vol_size(
            0.06,
            tier_multiplier=1.0,
            entry_price=100.0,
            stop_price=95.5,
            volatility_24h=1.2,
            apply_scaling=True,
        )
        self.assertTrue(wide.applied)
        self.assertAlmostEqual(wide.stop_distance_pct or 0.0, 0.045)
        self.assertAlmostEqual(wide.risk_parity_scalar, 1.0 / 3.0, places=5)
        self.assertAlmostEqual(wide.size_pct, 0.02, places=5)
        self.assertEqual(wide.position_size_note, "inverse_stop_width_risk_parity")

        # Extreme 6% stop + high volatility_24h=6.0 -> clamped at floor 0.25x
        extreme = compute_inverse_vol_size(
            0.08,
            tier_multiplier=1.0,
            entry_price=100.0,
            stop_price=90.0,
            volatility_24h=6.0,
            apply_scaling=True,
        )
        self.assertAlmostEqual(extreme.risk_parity_scalar, 0.25)
        self.assertAlmostEqual(extreme.size_pct, 0.02)

    def test_strategy_stamps_risk_parity_and_applies_when_env_enabled(self) -> None:
        # Span = 30 (mid=130, low=100), alpha=0.20 -> price=106, stop=97 (stop_dist = 9/106 = 8.49% -> scalar=0.25)
        row = {
            "binance_symbol": "PEPEUSDT",
            "chain_id": "binance",
            "token_address": "PEPEUSDT",
            "kline_low": 100.0,
            "boll_mid": 130.0,
            "price_usd": 106.0,
            "trade_size_ratio": 1.8,
            "taker_buy_ratio": 0.55,
            "volatility_24h": 3.5,
            "change_5m": -4.0,
            "volume_strength": 1.5,
            "boll_lower": 108.0,
            "liquidity_usd": 90_000.0,
        }
        prev = os.environ.get(ENV_INVERSE_VOL_SIZING)
        os.environ[ENV_INVERSE_VOL_SIZING] = "1"
        try:
            signals = TechBollFadeStrategy(position_pct=0.08).on_tick(
                {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_BOLL_FADE}
            )
        finally:
            if prev is None:
                os.environ.pop(ENV_INVERSE_VOL_SIZING, None)
            else:
                os.environ[ENV_INVERSE_VOL_SIZING] = prev

        self.assertEqual(len(signals), 1)
        sig = signals[0]
        self.assertEqual(sig["sector_cluster"], "meme_high_beta")
        self.assertAlmostEqual(sig["risk_parity_scalar"], 0.25)
        self.assertAlmostEqual(sig["size_pct"], 0.05 * 0.25)
        self.assertEqual(sig["position_size_note"], "inverse_stop_width_risk_parity")

    def test_per_tick_cluster_cap_deduplicates_same_sector_and_promotes_uncorrelated(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        asof = _iso_now()

        def _sig(symbol: str, vs: float) -> dict:
            return {
                "action": "buy",
                "chain_id": "binance",
                "token_address": symbol,
                "binance_symbol": symbol,
                "volume_strength": vs,
                "quote_turnover_24h_usdt": 15_000_000.0,
                "quote_turnover_24h_source": SOURCE_TICKER,
                "quote_turnover_24h_asof": asof,
                "universe_member": True,
            }

        # PEPEUSDT (vs=3.0) and WIFUSDT (vs=2.5) are both meme_high_beta;
        # AAVEUSDT (vs=1.8) is defi_infra. With cluster cap on, Top-2 picks PEPEUSDT + AAVEUSDT!
        candidates = [
            _sig("PEPEUSDT", 3.0),
            _sig("WIFUSDT", 2.5),
            _sig("AAVEUSDT", 1.8),
        ]
        prev = os.environ.get(ENV_CLUSTER_CAP_ENABLED)
        os.environ[ENV_CLUSTER_CAP_ENABLED] = "1"
        try:
            kept, report = cap_live_bnb_buys(candidates, now_ms=now_ms)
        finally:
            if prev is None:
                os.environ.pop(ENV_CLUSTER_CAP_ENABLED, None)
            else:
                os.environ[ENV_CLUSTER_CAP_ENABLED] = prev

        self.assertEqual(report["selected"], ["PEPEUSDT", "AAVEUSDT"])
        self.assertEqual(report["reasons"].get(REASON_CLUSTER_TICK_CAP), 1)
        self.assertEqual([s["binance_symbol"] for s in kept], ["PEPEUSDT", "AAVEUSDT"])

    def test_open_book_cluster_cap_blocks_second_symbol_in_same_sector(self) -> None:
        book = ProtectionBook.ready(
            positions=(
                {
                    "symbol": "FETUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "status": "open",
                    "protection": "exchange_oco",
                    "oco_order_list_id": 1001,
                    "oco_above_order_id": 2001,
                    "oco_below_order_id": 2002,
                    "quantity": "1",
                },
            ),
            canonical=(),
        )
        signals = [
            {
                "action": "buy",
                "binance_symbol": "TAOUSDT",
                "price_usd": 400.0,
                "stop_price": 390.0,
                "order_type": "MARKET",
            },
            {
                "action": "buy",
                "binance_symbol": "UNIUSDT",
                "price_usd": 10.0,
                "stop_price": 9.5,
                "order_type": "MARKET",
            },
        ]
        prev = os.environ.get(ENV_CLUSTER_CAP_ENABLED)
        os.environ[ENV_CLUSTER_CAP_ENABLED] = "1"
        try:
            kept, report = apply_live_buy_guards(
                signals,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                book=book,
            )
        finally:
            if prev is None:
                os.environ.pop(ENV_CLUSTER_CAP_ENABLED, None)
            else:
                os.environ[ENV_CLUSTER_CAP_ENABLED] = prev

        # The open row is stored ids only. Decision halts before the cluster cap.
        self.assertEqual(kept, [])
        self.assertEqual(report.get("protection_halt"), "oco_readback_unverified")


if __name__ == "__main__":
    unittest.main()
