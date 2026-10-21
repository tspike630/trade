"""JEV refresh tiers, pacing, error preservation, history. Temp DB; no network."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from sources import jev


def _iso(delta_sec: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_sec)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.db"
        c = sqlite3.connect(self.db)
        c.execute(
            "CREATE TABLE tokens (chain_id TEXT, token_address TEXT, has_pairs INT,"
            " last_seen_at TEXT, raw_json TEXT)"
        )
        c.execute("CREATE TABLE pairs (chain_id TEXT, token_address TEXT, raw_json TEXT, updated_at TEXT)")
        c.commit()
        c.close()
        self.store = jev.JevStore(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_token(self, addr, seen=-60, chain="solana"):
        self.store._conn.execute(
            "INSERT INTO tokens VALUES (?,?,1,?,NULL)", (chain, addr, _iso(seen))
        )
        self.store._conn.commit()

    def add_signal(self, addr, fetched_delta, error=None, chain="solana"):
        self.store.upsert_signal(
            {"chain_id": chain, "token_address": addr, "fetched_at": _iso(fetched_delta),
             "jev_score": 50.0, "stance": "NEUTRAL", "error": error}
        )


class TestTiers(_Base):
    def test_half_refresh_half_new(self):
        for i in range(10):
            self.add_token(f"new{i}")
        for i in range(10):
            self.add_token(f"old{i}")
            self.add_signal(f"old{i}", -86400 + i * 60)
        rows = self.store.list_due_hot(limit=8, interval_sec=900, pins=set())
        tiers = [r["tier"] for r in rows]
        self.assertEqual(tiers.count("refresh"), 4)
        self.assertEqual(tiers.count("new"), 4)
        refresh = [r["token_address"] for r in rows if r["tier"] == "refresh"]
        self.assertEqual(refresh, ["old0", "old1", "old2", "old3"])  # oldest first

    def test_share_configurable_and_spill(self):
        for i in range(10):
            self.add_token(f"new{i}")
        self.add_token("old0")
        self.add_signal("old0", -86400)
        rows = self.store.list_due_hot(limit=6, interval_sec=900, refresh_share=0.5, pins=set())
        self.assertEqual(len(rows), 6)  # only 1 refresh available -> spill to new
        self.assertEqual(sum(r["tier"] == "refresh" for r in rows), 1)
        rows = self.store.list_due_hot(limit=6, interval_sec=900, refresh_share=0.0, pins=set())
        self.assertEqual(sum(r["tier"] == "refresh" for r in rows), 0)

    def test_fresh_not_refreshed(self):
        self.add_token("old0")
        self.add_signal("old0", -60)
        self.assertEqual(self.store.list_due_hot(limit=5, interval_sec=900, pins=set()), [])

    def test_pins_first_and_deduped(self):
        for i in range(4):
            self.add_token(f"new{i}")
        self.add_token("held")
        self.add_signal("held", -7200)
        rows = self.store.list_due_hot(
            limit=4, interval_sec=900, pins={("solana", "HELD")}, pin_max_share=0.5
        )
        self.assertEqual(rows[0]["tier"], "pin")
        self.assertEqual(rows[0]["token_address"], "held")
        self.assertEqual(sum(r["token_address"] == "held" for r in rows), 1)
        self.assertEqual(len(rows), 4)


    def test_default_config_all_pins_first(self):
        """Defaults (limit 40, pin share 1.0): every open position is picked before hot tokens."""
        from config import JEV_HOT_LIMIT, JEV_PIN_MAX_SHARE
        for i in range(60):
            self.add_token(f"new{i}")
        pins = {("solana", f"HELD{i}") for i in range(15)}
        rows = self.store.list_due_hot(
            limit=JEV_HOT_LIMIT, interval_sec=900, pins=pins, pin_max_share=JEV_PIN_MAX_SHARE
        )
        self.assertEqual(len(rows), 40)
        self.assertEqual([r["tier"] for r in rows[:15]], ["pin"] * 15)
        self.assertEqual({r["token_address"] for r in rows[:15]}, {f"held{i}" for i in range(15)})

    def test_pins_can_fill_whole_pass(self):
        for i in range(10):
            self.add_token(f"new{i}")
        pins = {("solana", f"HELD{i}") for i in range(50)}
        rows = self.store.list_due_hot(limit=40, interval_sec=900, pins=pins, pin_max_share=1.0)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(r["tier"] == "pin" for r in rows))


class TestErrorsAndHistory(_Base):
    def _client(self, result):
        c = mock.Mock()
        c.fetch_score.return_value = result
        return c

    def test_error_preserves_success(self):
        self.add_token("tok")
        self.add_signal("tok", -3600)
        before = self.store.get_signal("solana", "tok")
        for code in ("status:403", "status:429", "status:503", "http_error:ConnectError"):
            row = jev.fetch_and_store(self.store, self._client({"error": code, "fetched_at": _iso(0)}), "solana", "tok")
            self.assertTrue(row.get("preserved"))
            after = self.store.get_signal("solana", "tok")
            self.assertEqual(after["jev_score"], before["jev_score"])
            self.assertEqual(after["fetched_at"], before["fetched_at"])
            self.assertIsNone(after["error"])
            self.assertEqual(after["last_error"], code)
            self.assertIsNotNone(after["last_error_at"])
        self.assertEqual(self.store.count_history(), 0)

    def test_error_on_new_token_still_written(self):
        jev.fetch_and_store(self.store, self._client({"error": "status:403", "fetched_at": _iso(0)}), "solana", "x")
        r = self.store.get_signal("solana", "x")
        self.assertEqual(r["error"], "status:403")

    def test_success_appends_history(self):
        ok = jev.parse_score_response({"jev_score": 77, "investment_verdict": {"stance": "NEUTRAL"}})
        ok["fetched_at"] = _iso(0)
        for _ in range(3):
            jev.fetch_and_store(self.store, self._client(dict(ok)), "solana", "tok", tier="refresh")
        self.assertEqual(self.store.count_signals(), 1)
        h = self.store._conn.execute("SELECT * FROM jev_signals_history").fetchall()
        self.assertEqual(len(h), 3)
        self.assertEqual(h[0]["stance"], "NEUTRAL")
        self.assertEqual(h[0]["jev_score"], 77.0)
        self.assertEqual(h[0]["tier"], "refresh")
        self.assertTrue(h[0]["fetched_at"])

    def test_existing_history_table_reused(self):
        self.store.close()
        c = sqlite3.connect(self.db)
        c.execute("DROP TABLE jev_signals_history")
        c.execute("CREATE TABLE jev_signals_history (id INTEGER PRIMARY KEY, chain_id TEXT, token_address TEXT, fetched_at TEXT)")
        c.commit(); c.close()
        self.store = jev.JevStore(self.db)
        cols = {r[1] for r in self.store._conn.execute("PRAGMA table_info(jev_signals_history)")}
        self.assertTrue({"stance", "jev_score", "tier"} <= cols)

    def test_consecutive_429_backoff(self):
        self.add_token("a"); self.add_token("b"); self.add_token("c")
        client = self._client({"error": "status:429", "fetched_at": _iso(0)})
        sleeps = []
        with mock.patch.object(jev, "_load_pinned_tokens", return_value=set()):
            stats = jev.run_pass(self.store, client, limit=3, sleep_fn=sleeps.append)
        self.assertEqual(stats["s429"], 3)
        self.assertEqual(sleeps, [60.0, 120.0, 240.0])


class TestPacing(unittest.TestCase):
    def test_hard_caps(self):
        b = jev.MinuteBudget(7, min_gap_sec=1)
        self.assertEqual(b.max_per_min, 6)
        self.assertEqual(b.min_gap_sec, 10.0)

    def test_min_gap_enforced(self):
        clock = [1000.0]
        slept = []

        def sleep(s):
            slept.append(s)
            clock[0] += s

        with mock.patch.object(jev.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(jev.time, "sleep", side_effect=sleep):
            b = jev.MinuteBudget(6)
            stamps = []
            for _ in range(13):
                b.wait_if_needed()
                b.record()
                stamps.append(clock[0])
        gaps = [y - x for x, y in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 10.0 - 1e-9 for g in gaps))
        for i in range(len(stamps)):
            self.assertLessEqual(sum(1 for t in stamps if stamps[i] <= t < stamps[i] + 60), 6)


if __name__ == "__main__":
    unittest.main()
