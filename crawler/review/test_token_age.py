"""Joint fine buckets share the coarse raw-then-pairs order. No invented stamps."""

from __future__ import annotations

import unittest

from review.attribution import _token_age_bucket_computed, resolve_token_age_hours
from review.data import SignalChainRow, parse_ts
from review.joint import token_age_fine_bucket, token_age_hours

SIGNAL = "2026-10-19T00:00:00Z"
SIGNAL_MS = int(parse_ts(SIGNAL).timestamp() * 1000)
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def _row(
    *,
    raw: dict | None = None,
    pair_created_at: int | None = None,
    token_age_hours_value: float | None = None,
) -> SignalChainRow:
    return SignalChainRow(
        decision_id=1,
        ts=SIGNAL,
        strategy_id="jev_strength_v1",
        action="buy",
        chain_id="solana",
        token_address="token-1",
        pair_address="pair-ok",
        size_pct=None,
        reason=None,
        risk_passed=1,
        mode="dry_run",
        raw={} if raw is None else raw,
        jev_strength=None,
        exec_id=None,
        exec_ts=None,
        validation_ok=None,
        validation_errors=None,
        executed=None,
        executor=None,
        receipt={},
        position_id=None,
        fill_side=None,
        fill_price=None,
        pnl_usd=None,
        pair_created_at=pair_created_at,
        token_age_hours=token_age_hours_value,
    )


class JointTokenAgeOrderTest(unittest.TestCase):
    def test_raw_missing_pairs_present_is_not_unknown(self) -> None:
        row = _row(raw={}, pair_created_at=SIGNAL_MS - 3 * DAY_MS, token_age_hours_value=1.0)
        self.assertAlmostEqual(token_age_hours(row) or -1, 72.0, places=3)
        self.assertEqual(resolve_token_age_hours(row), token_age_hours(row))
        self.assertEqual(_token_age_bucket_computed(row), "1-7d")
        self.assertEqual(token_age_fine_bucket(row), "1-7d")

    def test_raw_extras_beats_pairs(self) -> None:
        row = _row(
            raw={"extras": {"pairCreatedAt": SIGNAL_MS - 2 * HOUR_MS}},
            pair_created_at=SIGNAL_MS - 10 * DAY_MS,
            token_age_hours_value=24 * 10,
        )
        self.assertAlmostEqual(token_age_hours(row) or -1, 2.0, places=3)
        self.assertEqual(_token_age_bucket_computed(row), "<1d")
        self.assertEqual(token_age_fine_bucket(row), "1-24h")

    def test_both_missing_is_unknown(self) -> None:
        row = _row(raw={}, pair_created_at=None, token_age_hours_value=None)
        self.assertIsNone(token_age_hours(row))
        self.assertEqual(_token_age_bucket_computed(row), "unknown")
        self.assertEqual(token_age_fine_bucket(row), "unknown")

    def test_precomputed_hours_used_only_when_no_stamp(self) -> None:
        row = _row(raw={"extras": {"pairCreatedAt": ""}}, token_age_hours_value=3.0)
        self.assertEqual(token_age_hours(row), 3.0)
        self.assertEqual(_token_age_bucket_computed(row), "<1d")
        self.assertEqual(token_age_fine_bucket(row), "1-24h")

    def test_future_raw_does_not_invent_from_pairs(self) -> None:
        row = _row(
            raw={"extras": {"pairCreatedAt": SIGNAL_MS + HOUR_MS}},
            pair_created_at=SIGNAL_MS - 3 * DAY_MS,
            token_age_hours_value=72.0,
        )
        self.assertIsNone(token_age_hours(row))
        self.assertEqual(_token_age_bucket_computed(row), "unknown")
        self.assertEqual(token_age_fine_bucket(row), "unknown")


if __name__ == "__main__":
    unittest.main()
