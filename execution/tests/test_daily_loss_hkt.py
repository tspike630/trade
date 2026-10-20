"""HKT day bucket for the paper daily-loss circuit.

Run from the repo root:

    python -m unittest execution.tests.test_daily_loss_hkt

The $200 comparison is unchanged: reject only when summed pnl < -limit.
Only the calendar date of closed_at moves, from UTC to Asia/Hong_Kong.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from execution.risk.limits import _closed_at_hkt_day, check_daily_loss  # noqa: E402

# 2026-10-20 00:30 HKT == 2026-10-19 16:30Z, inside 00:00–08:00 HKT.
NOW_HKT_MORNING = datetime(2026, 10, 19, 16, 30, tzinfo=timezone.utc)
# 2026-10-20 10:00 HKT == 2026-10-20 02:00Z, after the UTC date has rolled.
NOW_HKT_AFTER_UTC_ROLL = datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)


class _Closed:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_closed(self, limit: int = 20) -> list[dict]:
        return list(self._rows)[: int(limit)] if limit else []


def _check(rows: list[dict], *, now: datetime, limit: float | None = 200.0) -> dict:
    with patch("execution.risk.limits._utc_now", return_value=now):
        return check_daily_loss(
            _Closed(rows),
            {"action": "buy"},
            daily_loss_limit_usd=limit,
        )


class DailyLossHktDayTests(unittest.TestCase):
    def test_utc_evening_is_next_hkt_date(self) -> None:
        # 18:00Z is 02:00 the next calendar day in HKT. 15:00Z is still 23:00 HKT.
        self.assertEqual(_closed_at_hkt_day("2026-10-19T18:00:00Z"), "2026-10-20")
        self.assertEqual(_closed_at_hkt_day("2026-10-19T15:00:00Z"), "2026-10-19")
        self.assertEqual(_closed_at_hkt_day("2026-10-19T16:00:00Z"), "2026-10-20")
        self.assertEqual(_closed_at_hkt_day("2026-10-19T15:59:59Z"), "2026-10-19")

    def test_hkt_morning_ignores_previous_hkt_day(self) -> None:
        # UTC date is still 2026-10-19, but HKT today is 2026-10-20.
        # Yesterday's HKT close must not keep the breaker shut.
        within = _check(
            [{"closed_at": "2026-10-19T15:00:00Z", "pnl_usd": -999.0}],
            now=NOW_HKT_MORNING,
        )
        self.assertTrue(within["ok"])
        self.assertNotEqual(within.get("reason"), "daily_loss")

    def test_hkt_morning_counts_utc_evening_close(self) -> None:
        denied = _check(
            [{"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -201.0}],
            now=NOW_HKT_MORNING,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "daily_loss")
        self.assertNotIn(":", denied["reason"])
        self.assertEqual(
            denied["detail"],
            {"today": "2026-10-20", "pnl": -201.0, "limit": 200.0},
        )

    def test_same_bucket_after_utc_rollover(self) -> None:
        # 18:00Z is already 10-20 HKT, so it still counts after UTC becomes 10-20.
        denied = _check(
            [{"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -201.0}],
            now=NOW_HKT_AFTER_UTC_ROLL,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["detail"]["today"], "2026-10-20")
        self.assertEqual(denied["detail"]["pnl"], -201.0)
        skipped = _check(
            [{"closed_at": "2026-10-19T15:00:00Z", "pnl_usd": -999.0}],
            now=NOW_HKT_AFTER_UTC_ROLL,
        )
        self.assertTrue(skipped["ok"])

    def test_hkt_midnight_is_inclusive(self) -> None:
        on_midnight = _check(
            [{"closed_at": "2026-10-19T16:00:00Z", "pnl_usd": -201.0}],
            now=NOW_HKT_MORNING,
        )
        self.assertFalse(on_midnight["ok"])
        self.assertEqual(on_midnight["detail"]["today"], "2026-10-20")
        before = _check(
            [{"closed_at": "2026-10-19T15:59:59Z", "pnl_usd": -999.0}],
            now=NOW_HKT_MORNING,
        )
        self.assertTrue(before["ok"])

    def test_only_hkt_today_sums_and_limit_stays_strict(self) -> None:
        rows = [
            {"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -100.0},
            {"closed_at": "2026-10-19T15:00:00Z", "pnl_usd": -500.0},
            {"closed_at": "2026-10-20T02:00:00+08:00", "pnl_usd": -100.0},
            {"closed_at": "not-a-timestamp", "pnl_usd": -500.0},
        ]
        at_limit = _check(rows, now=NOW_HKT_MORNING)
        self.assertTrue(at_limit["ok"])
        over = _check(
            [
                {"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -100.0},
                {"closed_at": "2026-10-19T15:00:00Z", "pnl_usd": -500.0},
                {"closed_at": "2026-10-20T02:00:00+08:00", "pnl_usd": -101.0},
            ],
            now=NOW_HKT_MORNING,
        )
        self.assertFalse(over["ok"])
        self.assertEqual(over["detail"]["pnl"], -201.0)
        self.assertEqual(over["detail"]["limit"], 200.0)
        self.assertEqual(over["detail"]["today"], "2026-10-20")

    def test_unset_limit_stays_off(self) -> None:
        skipped = _check(
            [{"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -999.0}],
            now=NOW_HKT_MORNING,
            limit=None,
        )
        self.assertEqual(skipped, {"ok": True})

    def test_sell_uses_the_same_hkt_bucket(self) -> None:
        with patch("execution.risk.limits._utc_now", return_value=NOW_HKT_MORNING):
            denied = check_daily_loss(
                _Closed([{"closed_at": "2026-10-19T18:00:00Z", "pnl_usd": -201.0}]),
                {"action": "sell"},
                daily_loss_limit_usd=200.0,
            )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["reason"], "daily_loss")
        self.assertEqual(denied["detail"]["limit"], 200.0)


if __name__ == "__main__":
    unittest.main()
