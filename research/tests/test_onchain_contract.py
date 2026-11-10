"""Decision-side checks. No network and no live calibration."""

from __future__ import annotations

import unittest

from research.hist_calibration.onchain_contract import (
    review_inputs_to_decision,
    validate_evidence_point,
)


class OnchainContractTests(unittest.TestCase):
    def test_book_depth_and_x_watch_and_future_features_are_rejected(self) -> None:
        depth = validate_evidence_point(
            {
                "series": "obi_top5",
                "instrument": "book_depth_percentage_band",
                "source_id": "binance_vision_bookDepth",
                "ts_ms": 1000,
                "event_ms": 2000,
                "value": -0.2,
            }
        )
        self.assertFalse(depth["ok"])
        self.assertIn("book_depth_is_not_obi_top5", depth["errors"])
        self.assertFalse(depth["x_watch_enabled"])
        watch = validate_evidence_point(
            {
                "series": "heat_history",
                "source_id": "x_watch",
                "ts_ms": 1000,
                "event_ms": 2000,
                "value": 3,
            }
        )
        self.assertIn("x_watch_not_historical", watch["errors"])
        future = validate_evidence_point(
            {
                "series": "taker_buy_ratio",
                "source_id": "binance_vision_klines",
                "ts_ms": 5000,
                "event_ms": 2000,
                "value": 0.4,
            }
        )
        self.assertIn("feature_after_event", future["errors"])
        stale = validate_evidence_point(
            {
                "series": "whale_retail_divergence",
                "source_id": "binance_vision_metrics",
                "ts_ms": 0,
                "event_ms": 90 * 60 * 1000 + 1,
                "value": 1.1,
            }
        )
        self.assertIn("feature_outside_window", stale["errors"])

    def test_a_valid_point_still_does_not_construct_a_calibration(self) -> None:
        point = validate_evidence_point(
            {
                "series": "obi_top5",
                "instrument": "obi_top5",
                "source_id": "live_top5_not_book_depth",
                "ts_ms": 1000,
                "event_ms": 2000,
                "value": 0.1,
            }
        )
        self.assertTrue(point["ok"])
        self.assertFalse(point["calibration_constructed"])
        self.assertFalse(point["x_watch_enabled"])

    def test_review_payload_without_real_top5_stays_not_ready(self) -> None:
        body = review_inputs_to_decision(
            {
                "news_availability": "ok",
                "raw_rows": 50,
                "late_archive_rows": 4,
                "obi_instrument": "book_depth_percentage_band",
                "x_watch_used_as_history": False,
                "events": [
                    {
                        "event_ms": index,
                        "forward_return": 0.0,
                        "obi_top5_asof": -0.1,
                        "taker_asof": 0.5,
                        "whale_asof": 1.0,
                        "heat_delta": 2,
                        "symbol": "BTCUSDT",
                        "title": "story",
                    }
                    for index in range(40)
                ],
            }
        )
        self.assertFalse(body["gates_passed"])
        self.assertIsNone(body["onchain_calibration"])
        self.assertTrue(body["evidence_pending"])
        self.assertFalse(body["x_watch_enabled"])
        self.assertFalse(body["live_router_changed"])


if __name__ == "__main__":
    unittest.main()
