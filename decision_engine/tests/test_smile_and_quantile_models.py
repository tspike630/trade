import inspect
import math
import unittest

from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    evaluate_five_state_regime,
)
from decision_engine.core.smile_and_quantile_models import (
    empirical_quantile,
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
        self.assertEqual(snap.residual_provenance, "leave_one_out")
        self.assertTrue(snap.put_crash_insurance_spike)
        self.assertIsNotNone(snap.max_put_wing_residual_pct)
        assert snap.max_put_wing_residual_pct is not None
        self.assertGreaterEqual(snap.max_put_wing_residual_pct, 6.5)

        # Router immediately shifts to State 4 defensive + Perp Beta Hedge!
        dec = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "spot_price": spot, "option_chain": chain}
        )
        self.assertEqual(dec.regime, REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(dec.playbook, "smile_put_wing_crash_insurance_defensive")
        self.assertTrue(dec.require_perp_beta_hedge)

    def test_history_has_no_clock_window_or_current_bar_exclusion(self) -> None:
        self.assertNotIn("now_ms", inspect.signature(empirical_quantile).parameters)
        self.assertNotIn("max_window", inspect.signature(empirical_quantile).parameters)
        calm, used = empirical_quantile([0.2] * 30, 0.95, min_samples=30, fallback=1.20)
        self.assertTrue(used)
        # The extra points stand in for a current bar and a future print.
        # They move the quantile because this helper does not drop them.
        contaminated, used_hot = empirical_quantile(
            [0.2] * 30 + [80.0, 90.0],
            0.95,
            min_samples=30,
            fallback=1.20,
        )
        self.assertTrue(used_hot)
        self.assertGreater(contaminated, calm)
        fallback, used_short = empirical_quantile(
            [0.2] * 29 + [float("nan"), None, "x"],  # type: ignore[list-item]
            0.95,
            min_samples=30,
            fallback=1.20,
        )
        self.assertFalse(used_short)
        self.assertEqual(fallback, 1.20)
        thresholds = resolve_rolling_thresholds({})
        self.assertFalse(thresholds.used_empirical_nvda)
        self.assertFalse(thresholds.used_empirical_vol_z)
        self.assertAlmostEqual(thresholds.nvda_breakout_1h_pct, 1.20, places=3)
        self.assertAlmostEqual(thresholds.climax_vol_z, 3.80, places=3)

    def test_negative_or_zero_tte_is_dropped_and_missing_tte_keeps_the_seven_day_default(self) -> None:
        spot = 100.0
        invalid = [
            {"strike": strike, "iv_pct": 50.0, "tte_years": tenor}
            for strike, tenor in ((90, -1.0), (95, 0.0), (100, 1e-8), (110, float("nan")))
        ]
        self.assertIsNone(fit_volatility_smile_residuals(invalid, spot))
        missing_tenor = [
            {"strike": strike, "iv_pct": 50.0}
            for strike in (90, 95, 100, 110)
        ]
        fitted = fit_volatility_smile_residuals(missing_tenor, spot)
        self.assertIsNotNone(fitted)
        assert fitted is not None
        self.assertEqual(fitted.n_strikes, 4)
        self.assertEqual(fitted.residual_provenance, "in_sample")
        self.assertIsNone(fitted.max_put_wing_residual_pct)
        self.assertIsNone(fitted.max_call_wing_residual_pct)
        self.assertFalse(fitted.put_crash_insurance_spike)
        self.assertFalse(fitted.call_squeeze_demand_spike)

    def test_explicit_iv_unit_and_same_contract_labels(self) -> None:
        spot = 100.0
        strikes = (90, 95, 100, 110)
        percent = [
            {"strike": strike, "iv": 50.0, "tte_years": 7.0 / 365.0, "iv_unit": "percent",
             "underlying": "BTC", "expiry": "2026-12-25"}
            for strike in strikes
        ]
        decimal = [
            {**row, "iv": 0.50, "iv_unit": "decimal"}
            for row in percent
        ]
        percent_fit = fit_volatility_smile_residuals(percent, spot)
        decimal_fit = fit_volatility_smile_residuals(decimal, spot)
        self.assertIsNotNone(percent_fit)
        self.assertIsNotNone(decimal_fit)
        assert percent_fit is not None and decimal_fit is not None
        self.assertAlmostEqual(percent_fit.atm_fitted_iv_pct, decimal_fit.atm_fitted_iv_pct, places=3)
        unlabeled = [
            {"strike": strike, "iv": 2.5, "tte_years": 7.0 / 365.0}
            for strike in strikes
        ]
        scaled = fit_volatility_smile_residuals(unlabeled, spot)
        self.assertIsNotNone(scaled)
        assert scaled is not None
        self.assertGreater(scaled.atm_fitted_iv_pct, 100.0)
        unknown = [{**row, "iv_unit": "pct"} for row in percent]
        self.assertIsNone(fit_volatility_smile_residuals(unknown, spot))
        mixed_expiry = [dict(row) for row in percent]
        mixed_expiry[-1]["expiry"] = "2027-01-30"
        self.assertIsNone(fit_volatility_smile_residuals(mixed_expiry, spot))
        mixed_underlying = [dict(row) for row in percent]
        mixed_underlying[-1]["underlying"] = "ETH"
        self.assertIsNone(fit_volatility_smile_residuals(mixed_underlying, spot))
        partial = [dict(row) for row in percent]
        del partial[0]["expiry"]
        self.assertIsNone(fit_volatility_smile_residuals(partial, spot))

    def test_price_usd_does_not_select_the_smile(self) -> None:
        spot = 100_000.0
        tte = 7.0 / 365.0
        chain = []
        for strike in (90_000, 94_000, 97_000, 100_000, 103_000, 106_000, 110_000):
            moneyness = math.log(strike / spot) / math.sqrt(tte)
            iv = 50.0 + 2.0 * moneyness + 12.0 * moneyness * moneyness
            if strike == 90_000:
                iv += 7.5
            chain.append({"strike": strike, "iv_pct": iv, "tte_years": tte})
        ignored = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "price_usd": spot, "option_chain": chain}
        )
        self.assertEqual(ignored.regime, REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertNotEqual(ignored.playbook, "smile_put_wing_crash_insurance_defensive")
        selected = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "close": spot, "option_chain": chain}
        )
        self.assertEqual(selected.playbook, "smile_put_wing_crash_insurance_defensive")
        plain = evaluate_five_state_regime({"symbol": "BTCUSDT", "spot_price": spot})
        self.assertEqual(plain.regime, REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertNotEqual(plain.playbook, "smile_put_wing_crash_insurance_defensive")

    def test_four_point_in_sample_residual_cannot_raise_the_leave_one_out_alert(self) -> None:
        spot = 100_000.0
        tte = 7.0 / 365.0
        clean = [94_000, 97_000, 100_000, 103_000, 106_000]
        spiked_strike = 90_000

        def _chain(strikes: list[int]) -> list[dict[str, float]]:
            rows = []
            for strike in strikes:
                moneyness = math.log(strike / spot) / math.sqrt(tte)
                iv = 50.0 + 2.0 * moneyness + 12.0 * moneyness * moneyness
                if strike == spiked_strike:
                    # Large enough that the shrunk 4-point residual still clears 4.
                    # The old route treated that shrunk number as the leave-one-out alert.
                    iv += 400.0
                rows.append({"strike": float(strike), "iv_pct": iv, "tte_years": tte})
            return rows

        four_strikes = [spiked_strike, 97_000, 100_000, 106_000]
        five_strikes = [spiked_strike, *clean[:4]]
        four = fit_volatility_smile_residuals(_chain(four_strikes), spot)
        five = fit_volatility_smile_residuals(_chain(five_strikes), spot)
        self.assertIsNotNone(four)
        self.assertIsNotNone(five)
        assert four is not None and five is not None
        self.assertEqual(four.n_strikes, 4)
        self.assertEqual(four.residual_provenance, "in_sample")
        self.assertIsNone(four.max_put_wing_residual_pct)
        self.assertFalse(four.put_crash_insurance_spike)
        self.assertIsNotNone(four.in_sample_max_put_wing_residual_pct)
        assert four.in_sample_max_put_wing_residual_pct is not None
        self.assertGreaterEqual(four.in_sample_max_put_wing_residual_pct, 4.0)

        self.assertEqual(five.residual_provenance, "leave_one_out")
        self.assertGreaterEqual(five.n_strikes, 5)
        self.assertIsNotNone(five.max_put_wing_residual_pct)
        self.assertIsNotNone(five.in_sample_max_put_wing_residual_pct)
        assert five.max_put_wing_residual_pct is not None
        assert five.in_sample_max_put_wing_residual_pct is not None
        self.assertGreater(five.max_put_wing_residual_pct, five.in_sample_max_put_wing_residual_pct)
        self.assertGreaterEqual(five.max_put_wing_residual_pct, 4.0)
        self.assertTrue(five.put_crash_insurance_spike)

        held = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "spot_price": spot, "option_chain": _chain(four_strikes),
             "put_wing_iv_residual_pct": 20.0, "put_wing_residual_provenance": "leave_one_out"}
        )
        self.assertNotEqual(held.playbook, "smile_put_wing_crash_insurance_defensive")
        routed = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "spot_price": spot, "option_chain": _chain(five_strikes)}
        )
        self.assertEqual(routed.playbook, "smile_put_wing_crash_insurance_defensive")

        unlabeled = evaluate_five_state_regime(
            {"symbol": "BTCUSDT", "put_wing_iv_residual_pct": 9.0}
        )
        in_sample_stamp = evaluate_five_state_regime(
            {
                "symbol": "BTCUSDT",
                "put_wing_iv_residual_pct": 9.0,
                "put_wing_residual_provenance": "in_sample",
            }
        )
        loo_stamp = evaluate_five_state_regime(
            {
                "symbol": "BTCUSDT",
                "put_wing_iv_residual_pct": 9.0,
                "put_wing_residual_provenance": "leave_one_out",
            }
        )
        self.assertNotEqual(unlabeled.playbook, "smile_put_wing_crash_insurance_defensive")
        self.assertNotEqual(in_sample_stamp.playbook, "smile_put_wing_crash_insurance_defensive")
        self.assertEqual(loo_stamp.playbook, "smile_put_wing_crash_insurance_defensive")


if __name__ == "__main__":
    unittest.main()
