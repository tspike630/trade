"""Tests for Milestone 18: Purged Walk-Forward OOS Validation & Deflated Sharpe Ratio (PSR/DSR)."""

from __future__ import annotations

import unittest

from decision_engine.backtest.metrics import compute_metrics, compute_psr_and_dsr
from decision_engine.backtest.walk_forward import (
    build_walk_forward_splits,
    evaluate_walk_forward_folds,
    partition_trades_by_split,
)


class TestWalkForwardAndDsr(unittest.TestCase):
    def test_compute_psr_and_dsr_penalizes_multiple_testing_trials(self) -> None:
        # Consistent positive trade returns
        rets = [0.025, 0.018, -0.010, 0.031, 0.022, -0.008, 0.019, 0.027, 0.015, 0.021]
        single = compute_psr_and_dsr(rets, num_trials=1)
        multi_50 = compute_psr_and_dsr(rets, num_trials=50)

        assert single["sharpe_ratio"] is not None
        assert single["psr"] is not None
        assert single["dsr"] is not None
        assert multi_50["dsr"] is not None
        assert multi_50["dsr_hurdle_sr"] is not None

        self.assertGreater(single["sharpe_ratio"], 1.0)
        self.assertGreater(single["psr"], 0.95)
        self.assertAlmostEqual(single["psr"], single["dsr"], places=6)
        self.assertGreater(multi_50["dsr_hurdle_sr"], 0.0)
        self.assertLess(multi_50["dsr"], single["dsr"])

    def test_compute_metrics_includes_sharpe_sortino_calmar_psr_dsr(self) -> None:
        closed = [
            {"pnl_usd": 45.0, "size_usd": 1000.0},
            {"pnl_usd": -15.0, "size_usd": 1000.0},
            {"pnl_usd": 38.0, "size_usd": 1000.0},
            {"pnl_usd": 52.0, "size_usd": 1000.0},
            {"pnl_usd": -12.0, "size_usd": 1000.0},
            {"pnl_usd": 40.0, "size_usd": 1000.0},
        ]
        curve = [
            {"equity_usd": 10000.0},
            {"equity_usd": 10045.0},
            {"equity_usd": 10030.0},
            {"equity_usd": 10068.0},
            {"equity_usd": 10120.0},
            {"equity_usd": 10108.0},
            {"equity_usd": 10148.0},
        ]
        m = compute_metrics(closed, curve, start_cash=10000.0, num_trials=5)
        self.assertIsNotNone(m["sharpe_ratio"])
        self.assertIsNotNone(m["sortino_ratio"])
        self.assertIsNotNone(m["calmar_ratio"])
        self.assertIsNotNone(m["probabilistic_sharpe_ratio"])
        self.assertIsNotNone(m["deflated_sharpe_ratio"])
        self.assertGreater(m["sharpe_ratio"], 0.5)
        self.assertGreater(m["calmar_ratio"], 1.0)

    def test_purged_and_embargoed_walk_forward_splits_prevent_leakage(self) -> None:
        start_ts = 1_700_000_000.0
        end_ts = start_ts + 100_000.0
        splits = build_walk_forward_splits(
            start_ts,
            end_ts,
            n_splits=3,
            train_fraction=0.60,
            embargo_sec=3600.0,
        )
        self.assertEqual(len(splits), 3)
        s0 = splits[0]
        self.assertEqual(s0.embargo_end_ts - s0.train_end_ts, 3600.0)
        self.assertEqual(s0.test_start_ts, s0.embargo_end_ts)

        trades = [
            # 1) Clean IS trade (opens and closes before train_end_ts)
            {"id": 1, "entry_ts": s0.train_start_ts + 100, "exit_ts": s0.train_end_ts - 200},
            # 2) Leaking trade (opens in IS, closes after train_end_ts -> MUST be purged!)
            {"id": 2, "entry_ts": s0.train_end_ts - 500, "exit_ts": s0.train_end_ts + 400},
            # 3) Embargo-window trade (opens inside embargo -> excluded from both IS and OOS!)
            {"id": 3, "entry_ts": s0.train_end_ts + 1800, "exit_ts": s0.train_end_ts + 2400},
            # 4) Clean OOS trade (opens after test_start_ts)
            {"id": 4, "entry_ts": s0.test_start_ts + 100, "exit_ts": s0.test_start_ts + 900},
        ]
        is_set, oos_set = partition_trades_by_split(trades, s0)
        self.assertEqual([t["id"] for t in is_set], [1])
        self.assertEqual([t["id"] for t in oos_set], [4])

    def test_evaluate_walk_forward_folds_distinguishes_robust_vs_overfitted(self) -> None:
        robust_folds = [
            {
                "is_returns": [0.03, 0.02, -0.01, 0.025, 0.018, 0.022],
                "oos_returns": [0.024, 0.019, -0.008, 0.021, 0.017],
                "selected_params": {"alpha_cap": 0.40, "min_odds": 1.5},
            },
            {
                "is_returns": [0.028, 0.019, -0.009, 0.024, 0.021, 0.017],
                "oos_returns": [0.022, 0.016, -0.007, 0.020, 0.018],
                "selected_params": {"alpha_cap": 0.40, "min_odds": 1.5},
            },
        ]
        summary_ok = evaluate_walk_forward_folds(robust_folds, num_trials=3)
        self.assertFalse(summary_ok.overfitting_detected)
        self.assertEqual(summary_ok.parameter_stability_score, 1.0)
        assert summary_ok.wfe_ratio is not None
        self.assertGreater(summary_ok.wfe_ratio, 0.70)

        overfitted_folds = [
            {
                "is_returns": [0.05, 0.04, 0.06, 0.03, 0.05],
                "oos_returns": [-0.03, -0.02, 0.005, -0.04, -0.01],
                "selected_params": {"alpha_cap": 0.31, "min_odds": 2.1},
            },
            {
                "is_returns": [0.04, 0.05, 0.03, 0.06, 0.04],
                "oos_returns": [-0.02, -0.03, -0.01, 0.002, -0.025],
                "selected_params": {"alpha_cap": 0.19, "min_odds": 1.3},
            },
        ]
        summary_bad = evaluate_walk_forward_folds(overfitted_folds, num_trials=40)
        self.assertTrue(summary_bad.overfitting_detected)
        self.assertIn("oos_sharpe_non_positive", summary_bad.overfitting_reasons)
        self.assertIn("wfe_below_min_threshold", summary_bad.overfitting_reasons)
        self.assertIn("deflated_sharpe_below_confidence", summary_bad.overfitting_reasons)


if __name__ == "__main__":
    unittest.main()
