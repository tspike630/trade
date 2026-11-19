"""Acceptance calendar built from saved BLS and Fed pages.

These rows are not the CityU snapshot and not a hand-written blackout.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from scripts.import_public_release_calendar import (
    SNAPSHOT_SHA256,
    build_acceptance_db,
    refuse_snapshot_overwrite,
)

_SOURCES = Path(__file__).resolve().parents[1] / "fixtures" / "public_release_calendar"


def _ms(iso: str) -> int:
    from datetime import datetime, timezone

    text = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp() * 1000)


class PublicReleaseCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "public_release_calendar.db"
        self.counts = build_acceptance_db(_SOURCES, self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counts_and_observation_period_are_not_the_release_clock(self) -> None:
        self.assertEqual(self.counts["macro_events"], 42)
        self.assertEqual(self.counts["central_bank_events"], 2)
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            """
            SELECT p.observation_period, p.release_local, p.release_tz, p.utc_ts, m.local_time_hkt
            FROM release_provenance p
            JOIN macro_events m ON m.event_id = p.event_key
            WHERE m.indicator_name = 'Consumer Price Index'
              AND p.observation_period = 'October 2026'
            """
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row[0], "October 2026")
        self.assertEqual(row[1], "2026-11-10 08:30")
        self.assertEqual(row[2], "America/New_York")
        self.assertEqual(row[3], "2026-11-10T13:30:00Z")
        self.assertEqual(row[4], "2026-11-10 21:30:00")
        self.assertNotIn("2026-10-31", row[3])

    def test_official_clocks_open_and_close_the_existing_windows(self) -> None:
        cpi = _ms("2026-11-10T13:30:00Z")
        at_cpi = evaluate_macro_gate(self.db, now_ms=cpi)
        self.assertEqual(at_cpi.halt_reason, "macro_rel3_blackout")
        after_cpi = evaluate_macro_gate(self.db, now_ms=cpi + 35 * 60_000)
        self.assertIsNone(after_cpi.halt_reason)
        self.assertTrue(after_cpi.configured)

        minutes = _ms("2026-11-18T19:00:00Z")
        at_minutes = evaluate_macro_gate(self.db, now_ms=minutes)
        self.assertEqual(at_minutes.halt_reason, "central_bank_blackout")
        after_minutes = evaluate_macro_gate(self.db, now_ms=minutes + 50 * 60_000)
        self.assertIsNone(after_minutes.halt_reason)

        statement = evaluate_macro_gate(self.db, now_ms=_ms("2026-10-28T18:00:00Z"))
        self.assertEqual(statement.halt_reason, "central_bank_blackout")

    def test_after_the_last_sourced_release_day_still_locks(self) -> None:
        status = evaluate_macro_gate(self.db, now_ms=_ms("2027-12-16T16:00:00Z"))
        self.assertEqual(status.halt_reason, "macro_lseg_stale_or_missing")
        self.assertEqual(status.detail, "release_calendar_not_covering_query_day")

    def test_a_later_release_does_not_cover_an_empty_day(self) -> None:
        status = evaluate_macro_gate(self.db, now_ms=_ms("2026-11-12T16:00:00Z"))
        self.assertEqual(status.halt_reason, "macro_lseg_stale_or_missing")
        self.assertEqual(status.detail, "release_calendar_not_covering_query_day")

    def test_builder_refuses_the_cityu_snapshot_when_present(self) -> None:
        import hashlib

        built = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertNotEqual(built, SNAPSHOT_SHA256)
        snapshot = Path("/tmp/lseg-real-t348/lseg_macro.db")
        if not snapshot.is_file():
            return
        digest = hashlib.sha256()
        with snapshot.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        self.assertEqual(digest.hexdigest(), SNAPSHOT_SHA256)
        with self.assertRaises(RuntimeError):
            refuse_snapshot_overwrite(snapshot)
        with self.assertRaises(RuntimeError):
            build_acceptance_db(_SOURCES, snapshot)
