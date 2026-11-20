"""Cross-margin emitters put real source stamps on the signal clocks."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from decision_engine.core.regime_v2_policy import (
    cross_margin_emit_clock_gap,
    drop_cross_margin_signals_missing_clocks,
)
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_CROSS_MARGIN
from decision_engine.strategies.regime_cross_margin import Regime2ShortSqueezeBreakoutStrategy
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy


class _Zone:
    def __init__(self, **values: object) -> None:
        self._values = values

    def __getattr__(self, name: str) -> object:
        return self._values.get(name)


def _decision() -> _Zone:
    return _Zone(
        allowed=True,
        strategy_id="regime2_short_squeeze_breakout",
        action="OPEN_LONG",
        regime="short_squeeze_breakout",
        size_multiplier=Decimal("1.20"),
        tp_multiplier=Decimal("1.35"),
        reasons=(),
    )


def _complete_clocks() -> dict[str, object]:
    return {
        "indicators_ts_ms": 1,
        "obi_top5_source_ts_ms": 1,
        "perp_counterparty_ts_ms": 1,
        "deribit_gamma_ts_ms": 1,
        "volume_z_source_close_ms": 1,
        "news_fetched_at_ms": 1,
        "cot_asof_date": "2026-11-10",
        "lseg_market_open": False,
    }


class CrossMarginClockWiringTests(unittest.TestCase):
    def test_missing_volume_or_obi_clock_withholds_the_signal(self) -> None:
        complete = _complete_clocks()
        self.assertIsNone(cross_margin_emit_clock_gap(complete))
        missing_volume = dict(complete)
        missing_volume.pop("volume_z_source_close_ms")
        missing_obi = dict(complete)
        missing_obi.pop("obi_top5_source_ts_ms")
        self.assertEqual(
            cross_margin_emit_clock_gap(missing_volume), "volume_z_source_close_ms"
        )
        self.assertEqual(cross_margin_emit_clock_gap(missing_obi), "obi_top5_source_ts_ms")
        kept = drop_cross_margin_signals_missing_clocks(
            [
                {"binance_symbol": "MMTUSDT", "clocks": missing_volume},
                {"binance_symbol": "RAREUSDT", "clocks": missing_obi},
                {"binance_symbol": "BMTUSDT", "clocks": complete},
            ]
        )
        self.assertEqual([row["binance_symbol"] for row in kept], ["BMTUSDT"])
        open_book = dict(complete)
        open_book["lseg_market_open"] = True
        self.assertEqual(
            cross_margin_emit_clock_gap(open_book), "lseg_cross_asset_ts_ms"
        )

    def test_cash_long_formula_does_not_treat_kelly_ceiling_as_approved_size(self) -> None:
        net = Decimal("287.73135835")
        raw = net * Decimal("1.00") * Decimal("1") * Decimal("1.20")
        capped = min(raw, net / (Decimal(1) + Decimal("0.001")))
        self.assertGreater(raw, net)
        self.assertEqual(capped, net / Decimal("1.001"))
        self.assertNotEqual(capped, net * Decimal("0.35"))


class CrossMarginEmitterClockTests(unittest.TestCase):
    def test_regime_long_and_short_emit_the_row_stamps(self) -> None:
        iso = "2026-11-20T07:45:50Z"
        row = {
            "binance_symbol": "YBUSDT",
            "chain_id": "binance",
            "token_address": "YBUSDT",
            "pair_address": "YB/USDT",
            "tech_updated_at": iso,
            "obi_top5_source_ts_ms": 1_763_620_123_480,
            "lseg_market_open": False,
            "_cot_max_date": "2026-11-03",
            "_deribit_gamma_ts_ms": 1_763_614_800_000,
            "deribit_collected_at_ms": 1_763_620_000_000,
        }
        strategy = Regime2ShortSqueezeBreakoutStrategy()
        with (
            patch(
                "decision_engine.strategies.regime_cross_margin.evaluate_approved_regime_intent",
                return_value=_decision(),
            ),
            patch(
                "decision_engine.strategies.regime_cross_margin.order_for_strategy",
                return_value=(
                    {
                        "limit_price": "0.0525",
                        "take_profit_price": "0.06",
                        "stop_price": "0.05",
                        "stop_limit_price": "0.049",
                    },
                    None,
                ),
            ),
        ):
            signals = strategy.on_tick({"hot_tokens": [row], "environ": {}})
        self.assertEqual(len(signals), 1)
        clocks = signals[0]["clocks"]
        expected_ms = int(datetime(2026, 11, 20, 7, 45, 50, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(clocks["indicators_ts_ms"], expected_ms)
        self.assertEqual(clocks["obi_top5_source_ts_ms"], 1_763_620_123_480)
        self.assertIs(clocks["lseg_market_open"], False)
        self.assertEqual(clocks["cot_asof_date"], "2026-11-03")
        self.assertEqual(clocks["deribit_gamma_ts_ms"], 1_763_614_800_000)
        self.assertNotEqual(clocks["deribit_gamma_ts_ms"], 1_763_620_000_000)

    def test_tech_cross_margin_emit_uses_the_same_stamps(self) -> None:
        iso = "2026-11-20T07:51:54Z"
        row = {
            "binance_symbol": "MMTUSDT",
            "chain_id": "binance",
            "token_address": "MMTUSDT",
            "primary_pair_address": "MMT/USDT",
            "tech_updated_at": iso,
            "lseg_market_open": False,
        }
        zone = _Zone(
            emit=True,
            position_size_multiplier=1.0,
            order_type="LIMIT",
            limit_price=0.2444,
            price=0.2444,
            stop_price=0.23,
            entry_reason="zone",
            alpha=0.1,
            trade_size_ratio=1.0,
        )
        strategy = TechBollFadeStrategy()
        with (
            patch(
                "decision_engine.strategies.tech_boll_fade.evaluate_entry_zone",
                return_value=zone,
            ),
            patch(
                "decision_engine.strategies.tech_boll_fade.live_buy_notional_skip",
                return_value=(False, None),
            ),
        ):
            signals = strategy.on_tick(
                {"hot_tokens": [row], "purpose": PURPOSE_LIVE_BNB_CROSS_MARGIN}
            )
        self.assertEqual(len(signals), 1, strategy.rejected)
        self.assertIs(signals[0]["clocks"]["lseg_market_open"], False)
        self.assertIn("indicators_ts_ms", signals[0]["clocks"])
        self.assertNotIn("lseg_cross_asset_ts_ms", signals[0]["clocks"])


if __name__ == "__main__":
    unittest.main()
