"""Unit tests for Step 2B Shrunk Covariance Matrix & BTC/ETH Net-Beta Sizer."""

from __future__ import annotations

import math
import os
import unittest

from decision_engine.core.covariance_beta_sizer import (
    ENV_COV_BETA_SIZER,
    _cholesky_is_spd,
    compute_beta_neutral_portfolio_weights,
    cov_beta_sizer_enabled,
    estimate_shrunk_covariance_and_betas,
)
from decision_engine.core.portfolio_clusters import cluster_open_exposure_block


class CovarianceBetaSizerTests(unittest.TestCase):
    def _synthetic_returns(self) -> dict[str, list[float]]:
        btc = [0.01 * math.sin(i * 0.4) + (0.004 if i % 3 == 0 else -0.002) for i in range(40)]
        eth = [1.15 * b + 0.002 * math.cos(i * 0.7) for i, b in enumerate(btc)]
        fet = [1.80 * b + 0.003 * math.sin(i * 0.9) for i, b in enumerate(btc)]
        tao = [1.78 * b + 0.0031 * math.sin(i * 0.9 + 0.05) for i, b in enumerate(btc)]
        uncorr = [0.008 * math.cos(i * 1.7 + 0.3) for i in range(40)]
        return {
            "BTCUSDT": btc,
            "ETHUSDT": eth,
            "FETUSDT": fet,
            "TAOUSDT": tao,
            "UNCORRUSDT": uncorr,
        }

    def test_default_flag_off_and_spd_guarantee_even_when_t_less_than_n(self) -> None:
        self.assertFalse(cov_beta_sizer_enabled({}))
        short_returns = {
            f"SYM{k}USDT": [0.01 * math.sin((i + 1) * (k + 1) * 0.3) for i in range(12)]
            for k in range(20)
        }
        model = estimate_shrunk_covariance_and_betas(short_returns, min_observations=12)
        assert model is not None
        dense = [[model.covariance[si][sj] for sj in model.symbols] for si in model.symbols]
        self.assertTrue(_cholesky_is_spd(dense))

    def test_high_correlation_crowding_haircut_and_long_short_beta_neutralization(self) -> None:
        rets = self._synthetic_returns()
        model = estimate_shrunk_covariance_and_betas(rets)
        assert model is not None
        self.assertGreater(model.pair_correlation("FETUSDT", "TAOUSDT") or 0.0, 0.90)
        self.assertLess(abs(model.pair_correlation("FETUSDT", "UNCORRUSDT") or 0.0), 0.35)
        self.assertGreater(model.beta_btc["FETUSDT"], 1.5)

        plan = compute_beta_neutral_portfolio_weights(
            [
                {"binance_symbol": "FETUSDT", "action": "buy", "size_pct": 0.25},
                {"binance_symbol": "TAOUSDT", "action": "buy", "size_pct": 0.25},
                {"binance_symbol": "UNCORRUSDT", "action": "buy", "size_pct": 0.20},
                {"binance_symbol": "ETHUSDT", "action": "short", "size_pct": 0.25},
            ],
            model,
            max_portfolio_vol=0.020,
            max_abs_net_beta_btc=0.15,
            max_pair_corr=0.75,
        )
        self.assertLess(plan.correlation_haircuts["FETUSDT"], 0.55)
        self.assertEqual(plan.correlation_haircuts["UNCORRUSDT"], 1.0)
        self.assertLessEqual(abs(plan.net_beta_btc_after), 0.1501)
        self.assertLessEqual(plan.ex_ante_volatility, 0.0201)

    def test_portfolio_clusters_covariance_guard_catches_cross_sector_high_corr_when_enabled(self) -> None:
        rets = self._synthetic_returns()
        rets["PEPEUSDT"] = [x * 1.02 for x in rets["FETUSDT"]]
        open_positions = [
            {"symbol": "FETUSDT", "purpose": "live_bnb_boll_fade", "status": "oco_working"}
        ]

        self.assertIsNone(
            cluster_open_exposure_block(
                open_positions,
                purpose="live_bnb_boll_fade",
                symbol="PEPEUSDT",
                enabled=True,
                returns_by_symbol=rets,
            )
        )

        prev = os.environ.get(ENV_COV_BETA_SIZER)
        os.environ[ENV_COV_BETA_SIZER] = "1"
        try:
            reason = cluster_open_exposure_block(
                open_positions,
                purpose="live_bnb_boll_fade",
                symbol="PEPEUSDT",
                enabled=True,
                returns_by_symbol=rets,
                max_pair_corr=0.75,
            )
            uncorr_reason = cluster_open_exposure_block(
                open_positions,
                purpose="live_bnb_boll_fade",
                symbol="UNCORRUSDT",
                enabled=True,
                returns_by_symbol=rets,
                max_pair_corr=0.75,
            )
        finally:
            if prev is None:
                os.environ.pop(ENV_COV_BETA_SIZER, None)
            else:
                os.environ[ENV_COV_BETA_SIZER] = prev

        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertTrue(reason.startswith("covariance_corr_cap:FETUSDT:"))
        self.assertIsNone(uncorr_reason)


if __name__ == "__main__":
    unittest.main()
