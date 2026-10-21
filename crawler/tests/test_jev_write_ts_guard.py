"""jev upsert_signal refuses stance rows without any timestamp (defense-in-depth).

No network, temp DB only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sources.jev import JevStore

CHAIN = "solana"
ADDR = "guardmint111111111111111111111111111111111"


class JevWriteTsGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JevStore(Path(self.tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _row(self, **kw):
        return {"chain_id": CHAIN, "token_address": ADDR, **kw}

    def test_refuses_stance_without_timestamps(self) -> None:
        for fa, aa in ((None, None), ("", ""), ("  ", None)):
            with self.assertLogs("dexscreener.sources.jev", level="WARNING") as cm:
                ok = self.store.upsert_signal(self._row(stance="bullish", fetched_at=fa, analyzed_at=aa))
            self.assertFalse(ok)
            self.assertIn(ADDR, cm.output[0])
            self.assertIn(CHAIN, cm.output[0])
        self.assertEqual(self.store.count_signals(), 0)

    def test_writes_with_fetched_at(self) -> None:
        self.assertTrue(self.store.upsert_signal(self._row(stance="bullish", fetched_at="2026-10-21T00:00:00Z")))
        self.assertEqual(self.store.count_signals(), 1)

    def test_writes_with_analyzed_at_only(self) -> None:
        self.assertTrue(self.store.upsert_signal(self._row(stance="bullish", analyzed_at="2026-10-21T00:00:00Z")))
        self.assertIsNotNone(self.store.get_signal(CHAIN, ADDR)["fetched_at"])

    def test_no_stance_still_written(self) -> None:
        self.assertTrue(self.store.upsert_signal(self._row(error="timeout")))
        self.assertEqual(self.store.count_signals(), 1)


if __name__ == "__main__":
    unittest.main()
