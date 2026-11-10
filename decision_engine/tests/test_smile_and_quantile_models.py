import math
import unittest

from decision_engine.core.regime_strategy_router import (
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    evaluate_five_state_regime,
)
from decision_engine.core.smile_and_quantile_models import (
    fit_volatility_smile_residuals,
    resolve_rolling_thresholds,
)


class SmileAndQuantileModelsTests(unittest.TestCase):
    def test_rolling_quantile_adapts_to_low_vol_and_high_vol_regimes(self) -> None:
        # Cold-start (<30 samples) -> exact static fallback (1.20%, 3.80)
        cold = resolve_rolling_thresholds({"nvda_1h_ret_history": [0.1, 0.2]})
        self.assertFalse(cold.used_empirical_nvda)
        self.assertAlmostEqual(cold.nvda_breakout_1h_pct, 1.20, places=3)

        # Low-vol regime: 60 bars distributed in [-0.40%, +0.75%] -> P95 ~ +0.69%, so +0.85% triggers State 3!
        low_vol_hist = [-0.40 + (1.15 * i / 59.0) for i in range(60)]
        dec = evaluate_five_state_regime(
            {
                "symbol": "SOLUSDT",
                "nvda_1h_ret_pct": 0.85,
                "nvda_1h_ret_history": low_vol_hist,
                "xs_rank_pct_1h": 0.85,
            }
        )
        self.assertEqual(dec.regime, REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertTrue(dec.allow_buy)

    def test_volatility_smile_parabola_detects_otm_put_crash_insurance_spike(self) -> None:
        spot = 100_000.0
        tte = 7.0 / 365.0
        # Baseline clean parabola: IV(m) = 50 + 2.0*m + 12.0*m^2 across 7 strikes,
        # EXCEPT the 90,000 OTM put is bid up +7.5% above the curve by smart-money crash hedgers!
        strikes = [90_000, 94_000, 97_000, 100_000, 103_000, 106_000, 110_000]
        chain = []
        for k in strikes:
            m = math.log(k / spot) / math.sqrt(tte)
            iv = 50.0 + 2.0 * m + 12.0 * m * m
            if k == 90_000:
                iv += 7.5  # Whale OTM put crash-insurance spike!
            chain.append({"strike": k, "iv_pct": iv, "tte_years": tte})

        snap = fit_volatility_smile_residuals(chain, spot)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertTrue(snap.put_crash_insurance_spike)
        self.assertGreaterEqual(snap.max_put_wing_residual_pct, 6.5)

        # Router immediately shifts to State 4 defensive + Perp Beta Hedge!
        dec = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "spot_price": spot, "option_chain": chain}
        )
        self.assertEqual(dec.regime, REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(dec.playbook, "smile_put_wing_crash_insurance_defensive")
        self.assertTrue(dec.require_perp_beta_hedge)


if __name__ == "__main__":
    unittest.main()
