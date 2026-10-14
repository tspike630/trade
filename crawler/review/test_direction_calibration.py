"""M-2 direction calibration: both signs, fail closed, weekly gate untouched."""

from __future__ import annotations

import math
import unittest

from review.factor_loop import gate_candidate
from review.gates import (
    DIRECTION_CALIBRATION_TODO,
    MIN_RAW_N,
    calibrate_direction,
)


class CalibrateDirectionTest(unittest.TestCase):
    def test_positive_ic_keeps_written_sign(self) -> None:
        row = calibrate_direction(0.19)
        self.assertTrue(row["passed"])
        self.assertFalse(row["fail_closed"])
        self.assertFalse(row["blocks_a_class"])
        self.assertEqual(row["ic_as_written"], 0.19)
        self.assertEqual(row["ic_flipped"], -0.19)
        self.assertEqual(row["kept_sign"], 1)
        self.assertEqual(row["cleaner_ic"], 0.19)
        self.assertEqual(row["reason"], "both_signs_recorded")

    def test_negative_ic_keeps_flipped_sign(self) -> None:
        row = calibrate_direction(-0.0843)
        self.assertTrue(row["passed"])
        self.assertEqual(row["ic_as_written"], -0.0843)
        self.assertEqual(row["ic_flipped"], 0.0843)
        self.assertEqual(row["kept_sign"], -1)
        self.assertEqual(row["cleaner_ic"], 0.0843)

    def test_zero_ic_records_both_signs_and_keeps_plus(self) -> None:
        row = calibrate_direction(0)
        self.assertTrue(row["passed"])
        self.assertEqual(row["ic_as_written"], 0.0)
        self.assertEqual(row["ic_flipped"], 0.0)
        self.assertEqual(row["kept_sign"], 1)
        self.assertEqual(row["cleaner_ic"], 0.0)

    def test_missing_ic_fails_closed(self) -> None:
        for ic in (None, "", "nan", True, False, math.nan, math.inf, -math.inf):
            row = calibrate_direction(ic)
            self.assertFalse(row["passed"], ic)
            self.assertTrue(row["fail_closed"], ic)
            self.assertTrue(row["blocks_a_class"], ic)
            self.assertIsNone(row["kept_sign"], ic)
            self.assertIsNone(row["ic_as_written"], ic)
            self.assertIsNone(row["ic_flipped"], ic)
            self.assertEqual(row["reason"], "direction_calibration_missing")

    def test_sample_floor_unchanged(self) -> None:
        self.assertEqual(MIN_RAW_N, 15)
        self.assertIn("gate_candidate", DIRECTION_CALIBRATION_TODO)
        self.assertIn("Weights stay 0", DIRECTION_CALIBRATION_TODO)


class WeeklyGateUntouchedTest(unittest.TestCase):
    def test_accept_path_ignores_ic_sign(self) -> None:
        decision = gate_candidate(
            {
                "n_raw": 32,
                "unique_token": 12,
                "up_1h": 0.56,
                "ic_mean": -0.2,
            },
            week="2026-W41",
        )
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["gate_result"], "pass")
        self.assertEqual(
            set(decision),
            {"gate_result", "gate_reason", "status"},
        )
        self.assertNotIn("kept_sign", decision["gate_reason"])


if __name__ == "__main__":
    unittest.main()
