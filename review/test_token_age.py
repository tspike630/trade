"""token_age: raw_json first, pairs.pair_created_at fallback. No invented stamps."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from review.attribution import (
    TOKEN_AGE_EDGES,
    _token_age_bucket,
    resolve_token_age_hours,
    signal_attribution,
)
from review.data import (
    DbPaths,
    SignalChainRow,
    annotate_pair_created_at,
    load_signal_chain,
    parse_ts,
)
from review.evolution import build_evolution_suggestions
from review.report import _render_attribution_section

SIGNAL = "2026-10-19T00:00:00Z"
SIGNAL_MS = int(parse_ts(SIGNAL).timestamp() * 1000)
HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def _row(
    *,
    decision_id: int = 1,
    ts: str = SIGNAL,
    chain_id: str = "solana",
    pair_address: str | None = "pair-ok",
    raw: dict | None = None,
    pair_created_at_ms: int | None = None,
) -> SignalChainRow:
    return SignalChainRow(
        decision_id=decision_id,
        ts=ts,
        strategy_id="jev_strength_v1",
        action="buy",
        chain_id=chain_id,
        token_address="token-1",
        pair_address=pair_address,
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
        pair_created_at_ms=pair_created_at_ms,
    )


class TokenAgeBucketTest(unittest.TestCase):
    def test_edges_unchanged(self) -> None:
        self.assertEqual(TOKEN_AGE_EDGES, "<1d / 1–7d / >7d / unknown")
        self.assertEqual(
            _token_age_bucket(_row(pair_created_at_ms=SIGNAL_MS - 23 * HOUR_MS)),
            "<1d",
        )
        self.assertEqual(
            _token_age_bucket(_row(pair_created_at_ms=SIGNAL_MS - 24 * HOUR_MS)),
            "1-7d",
        )
        self.assertEqual(
            _token_age_bucket(_row(pair_created_at_ms=SIGNAL_MS - 7 * DAY_MS)),
            "1-7d",
        )
        self.assertEqual(
            _token_age_bucket(_row(pair_created_at_ms=SIGNAL_MS - 7 * DAY_MS - 1000)),
            ">7d",
        )

    def test_raw_missing_pairs_present_is_not_unknown(self) -> None:
        row = _row(raw={}, pair_created_at_ms=SIGNAL_MS - 3 * DAY_MS)
        self.assertEqual(_token_age_bucket(row), "1-7d")
        self.assertAlmostEqual(resolve_token_age_hours(row) or -1, 72.0, places=3)
        buckets = signal_attribution([row])["dimensions"]["token_age"]["buckets"]
        self.assertEqual(buckets["1-7d"]["n"], 1)
        self.assertEqual(buckets["unknown"]["n"], 0)
        note = signal_attribution([row])["dimensions"]["token_age"]["note"]
        self.assertIn("raw 优先，pairs 回退", note)

    def test_raw_extras_beats_pairs_stamp(self) -> None:
        row = _row(
            raw={"extras": {"pairCreatedAt": SIGNAL_MS - 2 * HOUR_MS}},
            pair_created_at_ms=SIGNAL_MS - 10 * DAY_MS,
        )
        self.assertEqual(_token_age_bucket(row), "<1d")
        self.assertAlmostEqual(resolve_token_age_hours(row) or -1, 2.0, places=3)

    def test_raw_string_extras_pair_created_at(self) -> None:
        row = _row(
            raw={"extras": {"pairCreatedAt": str(SIGNAL_MS - 2 * HOUR_MS)}},
        )
        self.assertEqual(_token_age_bucket(row), "<1d")

    def test_both_missing_is_unknown(self) -> None:
        row = _row(raw={}, pair_created_at_ms=None, pair_address=None)
        self.assertIsNone(resolve_token_age_hours(row))
        self.assertEqual(_token_age_bucket(row), "unknown")
        buckets = signal_attribution([row])["dimensions"]["token_age"]["buckets"]
        self.assertEqual(buckets["unknown"]["n"], 1)

    def test_empty_and_non_timestamp_do_not_invent(self) -> None:
        self.assertEqual(
            _token_age_bucket(_row(raw={"extras": {"pairCreatedAt": ""}}, pair_created_at_ms=None)),
            "unknown",
        )
        self.assertEqual(
            _token_age_bucket(_row(raw={"pairCreatedAt": 0}, pair_created_at_ms=None)),
            "unknown",
        )
        self.assertEqual(
            _token_age_bucket(_row(raw={}, pair_created_at_ms=0)),
            "unknown",
        )

    def test_future_raw_stays_unknown_without_pairs_fallback(self) -> None:
        row = _row(
            raw={"extras": {"pairCreatedAt": SIGNAL_MS + HOUR_MS}},
            pair_created_at_ms=SIGNAL_MS - 3 * DAY_MS,
        )
        self.assertIsNone(resolve_token_age_hours(row))
        self.assertEqual(_token_age_bucket(row), "unknown")


class AnnotatePairCreatedAtTest(unittest.TestCase):
    def _paths(self, root: Path) -> DbPaths:
        return DbPaths(
            main=str(root / "dexscreener.db"),
            execution=str(root / "execution_ledger.db"),
            paper=str(root / "missing_paper.db"),
            backtest=str(root / "missing_backtest.db"),
        )

    def _snapshot(self, db: Path) -> list[tuple]:
        conn = sqlite3.connect(db)
        try:
            return conn.execute(
                """
                SELECT chain_id, pair_address, pair_created_at, raw_json
                FROM pairs
                ORDER BY chain_id, pair_address
                """
            ).fetchall()
        finally:
            conn.close()

    def test_join_is_read_only_and_does_not_invent_null_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "dexscreener.db"
            conn = sqlite3.connect(db)
            created_ms = SIGNAL_MS - 3 * DAY_MS
            conn.execute(
                """
                CREATE TABLE pairs (
                    chain_id TEXT,
                    pair_address TEXT,
                    pair_created_at INTEGER,
                    raw_json TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO pairs VALUES (?, ?, ?, ?)",
                [
                    ("solana", "pair-ok", created_ms, "{}"),
                    (
                        "solana",
                        "pair-null",
                        None,
                        '{"pairCreatedAt": 1700000000000}',
                    ),
                    ("ethereum", "pair-ok", SIGNAL_MS - 10 * DAY_MS, "{}"),
                ],
            )
            conn.commit()
            conn.close()
            before = self._snapshot(db)
            db.chmod(0o444)

            rows = annotate_pair_created_at(
                [
                    _row(decision_id=1, pair_address="pair-ok", chain_id="solana"),
                    _row(decision_id=2, pair_address="pair-null", chain_id="solana"),
                    _row(decision_id=3, pair_address="pair-ok", chain_id="ethereum"),
                    _row(decision_id=4, pair_address=None),
                    _row(decision_id=5, pair_address="missing-pair"),
                ],
                self._paths(root),
            )
            db.chmod(0o644)
            after = self._snapshot(db)

        self.assertEqual(before, after)
        by_id = {r.decision_id: r for r in rows}
        self.assertEqual(by_id[1].pair_created_at_ms, created_ms)
        self.assertEqual(_token_age_bucket(by_id[1]), "1-7d")
        self.assertIsNone(by_id[2].pair_created_at_ms)
        self.assertEqual(_token_age_bucket(by_id[2]), "unknown")
        self.assertEqual(by_id[3].pair_created_at_ms, SIGNAL_MS - 10 * DAY_MS)
        self.assertIsNone(by_id[4].pair_created_at_ms)
        self.assertIsNone(by_id[5].pair_created_at_ms)

    def test_missing_column_leaves_stamp_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = sqlite3.connect(root / "dexscreener.db")
            conn.execute(
                "CREATE TABLE pairs (chain_id TEXT, pair_address TEXT, raw_json TEXT)"
            )
            conn.execute(
                "INSERT INTO pairs VALUES ('solana', 'pair-ok', '{\"pairCreatedAt\": 1}')"
            )
            conn.commit()
            conn.close()
            rows = annotate_pair_created_at(
                [_row(pair_address="pair-ok")],
                self._paths(root),
            )
        self.assertIsNone(rows[0].pair_created_at_ms)
        self.assertEqual(_token_age_bucket(rows[0]), "unknown")

    def test_load_signal_chain_attaches_pairs_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created_ms = SIGNAL_MS - 3 * DAY_MS
            raw_ms = SIGNAL_MS - 2 * HOUR_MS
            main = sqlite3.connect(root / "dexscreener.db")
            main.executescript(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                    chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                    risk_passed INTEGER, mode TEXT, raw_json TEXT
                );
                CREATE TABLE pairs (
                    chain_id TEXT, pair_address TEXT, pair_created_at INTEGER,
                    raw_json TEXT
                );
                """
            )
            main.execute(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address,
                     risk_passed, mode, raw_json)
                VALUES (1, ?, 'jev', 'buy', 'solana', 'tok', 1, 'dry_run', ?)
                """,
                (
                    SIGNAL,
                    '{"pair_address":"pair-raw","extras":{"pairCreatedAt": %d}}' % raw_ms,
                ),
            )
            main.execute(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address,
                     risk_passed, mode, raw_json)
                VALUES (2, ?, 'jev', 'buy', 'solana', 'tok', 1, 'dry_run',
                        '{"pair_address":"pair-ok"}')
                """,
                (SIGNAL,),
            )
            main.execute(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address,
                     risk_passed, mode, raw_json)
                VALUES (3, ?, 'jev', 'buy', 'solana', 'tok', 1, 'dry_run',
                        '{"pair_address":"pair-null"}')
                """,
                (SIGNAL,),
            )
            main.executemany(
                "INSERT INTO pairs VALUES (?, ?, ?, ?)",
                [
                    ("solana", "pair-raw", SIGNAL_MS - 10 * DAY_MS, "{}"),
                    ("solana", "pair-ok", created_ms, "{}"),
                    (
                        "solana",
                        "pair-null",
                        None,
                        '{"pairCreatedAt": 1700000000000}',
                    ),
                ],
            )
            main.commit()
            before = main.execute(
                "SELECT pair_address, pair_created_at FROM pairs ORDER BY pair_address"
            ).fetchall()
            main.close()

            exec_db = sqlite3.connect(root / "execution_ledger.db")
            exec_db.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY, ts TEXT, decision_log_id INTEGER,
                    strategy_id TEXT, action TEXT, chain_id TEXT, token_address TEXT,
                    pair_address TEXT, size_pct REAL, reason TEXT, mode TEXT,
                    validation_ok INTEGER, validation_errors TEXT, executed INTEGER,
                    executor TEXT, receipt_json TEXT
                )
                """
            )
            exec_db.commit()
            exec_db.close()

            chain = load_signal_chain(
                self._paths(root),
                period_start="2026-10-01T00:00:00Z",
                period_end="2026-10-20T00:00:00Z",
            )
            main = sqlite3.connect(root / "dexscreener.db")
            after = main.execute(
                "SELECT pair_address, pair_created_at FROM pairs ORDER BY pair_address"
            ).fetchall()
            main.close()

        self.assertEqual(before, after)
        by_id = {r.decision_id: r for r in chain}
        self.assertEqual(by_id[1].pair_created_at_ms, SIGNAL_MS - 10 * DAY_MS)
        self.assertEqual(_token_age_bucket(by_id[1]), "<1d")
        self.assertEqual(by_id[2].pair_created_at_ms, created_ms)
        self.assertEqual(_token_age_bucket(by_id[2]), "1-7d")
        self.assertIsNone(by_id[3].pair_created_at_ms)
        self.assertEqual(_token_age_bucket(by_id[3]), "unknown")

    def test_missing_pairs_table_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = sqlite3.connect(root / "dexscreener.db")
            main.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                    chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                    risk_passed INTEGER, mode TEXT, raw_json TEXT
                )
                """
            )
            main.execute(
                """
                INSERT INTO decision_log
                    (id, ts, strategy_id, action, chain_id, token_address,
                     risk_passed, mode, raw_json)
                VALUES (1, ?, 'jev', 'buy', 'solana', 'tok', 1, 'dry_run',
                        '{"pair_address":"pair-ok"}')
                """,
                (SIGNAL,),
            )
            main.commit()
            main.close()
            exec_db = sqlite3.connect(root / "execution_ledger.db")
            exec_db.execute(
                """
                CREATE TABLE execution_log (
                    id INTEGER PRIMARY KEY, ts TEXT, decision_log_id INTEGER,
                    strategy_id TEXT, action TEXT, chain_id TEXT, token_address TEXT,
                    pair_address TEXT, size_pct REAL, reason TEXT, mode TEXT,
                    validation_ok INTEGER, validation_errors TEXT, executed INTEGER,
                    executor TEXT, receipt_json TEXT
                )
                """
            )
            exec_db.commit()
            exec_db.close()
            chain = load_signal_chain(
                self._paths(root),
                period_start="2026-10-01T00:00:00Z",
                period_end="2026-10-20T00:00:00Z",
            )
        self.assertEqual(len(chain), 1)
        self.assertIsNone(chain[0].pair_created_at_ms)
        self.assertEqual(_token_age_bucket(chain[0]), "unknown")


class TokenAgeCopyTest(unittest.TestCase):
    def test_report_and_evolution_describe_fallback(self) -> None:
        row = _row(raw={}, pair_created_at_ms=SIGNAL_MS - 3 * DAY_MS)
        attr = signal_attribution([row])
        text = "\n".join(_render_attribution_section(attr, None, None))
        self.assertIn("token_age（raw 优先，pairs 回退）", text)
        self.assertNotIn("raw_json 创建时间", text)

        with tempfile.TemporaryDirectory() as tmp:
            suggestions = build_evolution_suggestions(
                chain=[row],
                attribution=attr,
                db_root=tmp,
            )
        age = next(s for s in suggestions if "token_age" in s["item"])
        self.assertEqual(age["status"], "观察中")
        self.assertIn("unknown=0/1", age["evidence"])
        self.assertIn("raw 优先，pairs", age["evidence"])
        self.assertIn("观察 unknown 占比下降", age["action_hint"])
        self.assertIn("不改桶", age["action_hint"])
        self.assertNotIn("仅 raw_json", age["action_hint"])


if __name__ == "__main__":
    unittest.main()
