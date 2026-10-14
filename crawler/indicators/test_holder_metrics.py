"""Fixture tests for read-only I-1 holder metrics. NULL stays NULL."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from config import (
    INDICATORS_WEIGHT_FUNDING,
    INDICATORS_WEIGHT_SENTIMENT,
    INDICATORS_WEIGHT_TECHNICAL,
)
from indicators.holder_metrics import (
    dev_balance_delta,
    read_dev_balance_delta,
    read_top10_share,
    read_top10_share_history,
    top10_share,
    top10_share_history,
)

_MINT = "mint111"
_DEV = "devwallet111"
_OTHER = "retail222"


def _holder(
    rank: int,
    amount: float | None,
    *,
    pct: float | None = None,
    supply: float | None = 100.0,
    snapshot_id: str = "snap-a",
    recorded_at: str = "2026-10-14T00:00:00Z",
    holder: str = "holder",
    token: str = _MINT,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "recorded_at": recorded_at,
        "rank": rank,
        "holder_address": holder,
        "amount_ui": amount,
        "pct_of_supply": pct,
        "total_supply_ui": supply,
        "token_address": token,
    }


class Top10ShareTest(unittest.TestCase):
    def test_sums_amounts_over_supply(self) -> None:
        rows = [
            _holder(1, 25.0, pct=0.99, holder="a"),
            _holder(2, 15.0, pct=0.99, holder="b"),
        ]
        self.assertAlmostEqual(top10_share(rows) or 0.0, 0.40)

    def test_fewer_than_ten_rows_still_sum(self) -> None:
        self.assertAlmostEqual(top10_share([_holder(1, 10.0)]) or 0.0, 0.10)

    def test_rank_above_ten_is_ignored(self) -> None:
        rows = [
            _holder(1, 10.0),
            _holder(11, 50.0, holder="tail"),
        ]
        self.assertAlmostEqual(top10_share(rows) or 0.0, 0.10)

    def test_string_amount_is_accepted(self) -> None:
        row = _holder(1, None, supply=8.0)
        row["amount_ui"] = "2.5"
        self.assertAlmostEqual(top10_share([row]) or 0.0, 2.5 / 8.0)

    def test_empty_is_null(self) -> None:
        self.assertIsNone(top10_share([]))

    def test_missing_amount_and_pct_is_null(self) -> None:
        rows = [
            _holder(1, 10.0, pct=0.10),
            _holder(2, None, pct=None, holder="b"),
        ]
        self.assertIsNone(top10_share(rows))

    def test_null_amount_uses_complete_pct(self) -> None:
        rows = [
            _holder(1, 10.0, pct=0.10),
            _holder(2, None, pct=0.20, holder="b"),
        ]
        self.assertAlmostEqual(top10_share(rows) or 0.0, 0.30)

    def test_pct_sum_when_supply_missing(self) -> None:
        rows = [
            _holder(1, None, pct=0.20, supply=None),
            _holder(2, None, pct=0.05, supply=None, holder="b"),
        ]
        self.assertAlmostEqual(top10_share(rows) or 0.0, 0.25)

    def test_zero_supply_without_pct_is_null(self) -> None:
        self.assertIsNone(top10_share([_holder(1, 10.0, pct=None, supply=0.0)]))

    def test_negative_amount_without_pct_is_null(self) -> None:
        self.assertIsNone(top10_share([_holder(1, -5.0, pct=None)]))

    def test_bool_and_nan_are_null(self) -> None:
        self.assertIsNone(top10_share([_holder(1, None, pct=None)]))
        row = _holder(1, None, pct=None)
        row["amount_ui"] = True
        self.assertIsNone(top10_share([row]))
        row = _holder(1, None, pct=None)
        row["amount_ui"] = float("nan")
        self.assertIsNone(top10_share([row]))

    def test_conflicting_supply_is_null(self) -> None:
        rows = [
            _holder(1, 10.0, pct=0.10, supply=100.0),
            _holder(2, 10.0, pct=0.10, supply=200.0, holder="b"),
        ]
        self.assertIsNone(top10_share(rows))

    def test_duplicate_rank_is_null(self) -> None:
        rows = [_holder(1, 10.0), _holder(1, 5.0, holder="b")]
        self.assertIsNone(top10_share(rows))

    def test_mixed_snapshots_are_null(self) -> None:
        rows = [
            _holder(1, 10.0, snapshot_id="a"),
            _holder(1, 20.0, snapshot_id="b", holder="b"),
        ]
        self.assertIsNone(top10_share(rows))

    def test_share_is_not_clamped(self) -> None:
        self.assertAlmostEqual(
            top10_share([_holder(1, 8.0, supply=5.0)]) or 0.0,
            1.6,
        )


class Top10ShareHistoryTest(unittest.TestCase):
    def test_orders_points_and_keeps_null_gaps(self) -> None:
        rows = [
            _holder(
                1,
                30.0,
                snapshot_id="late",
                recorded_at="2026-10-15T00:00:00Z",
            ),
            _holder(
                1,
                None,
                pct=None,
                snapshot_id="early",
                recorded_at="2026-10-14T00:00:00Z",
            ),
            _holder(1, 99.0, snapshot_id=""),
        ]
        points = top10_share_history(rows)
        self.assertEqual([point["snapshot_id"] for point in points], ["early", "late"])
        self.assertIsNone(points[0]["top10_share"])
        self.assertAlmostEqual(points[1]["top10_share"] or 0.0, 0.30)

    def test_empty_history(self) -> None:
        self.assertEqual(top10_share_history([]), [])


class DevBalanceDeltaTest(unittest.TestCase):
    def _label(
        self,
        address: str = _DEV,
        *,
        label_type: str = "dev",
        token_mint: str | None = _MINT,
    ) -> dict[str, object]:
        return {
            "address": address,
            "label_type": label_type,
            "token_mint": token_mint,
        }

    def _edge(
        self,
        src: str,
        dst: str,
        amount: float | None,
        *,
        token: str = _MINT,
    ) -> dict[str, object]:
        return {
            "token_address": token,
            "from_address": src,
            "to_address": dst,
            "amount_ui": amount,
        }

    def test_net_inflow_is_positive(self) -> None:
        delta = dev_balance_delta(
            [self._label()],
            [self._edge(_OTHER, _DEV, 12.0), self._edge(_DEV, _OTHER, 5.0)],
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, 7.0)

    def test_net_outflow_is_negative(self) -> None:
        delta = dev_balance_delta(
            [self._label()],
            [self._edge(_DEV, _OTHER, 4.0)],
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, -4.0)

    def test_dev_to_dev_cancels(self) -> None:
        other_dev = "devwallet222"
        delta = dev_balance_delta(
            [self._label(), self._label(other_dev)],
            [
                self._edge(_DEV, other_dev, 9.0),
                self._edge(_OTHER, _DEV, 2.0),
            ],
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, 2.0)

    def test_null_amount_is_skipped(self) -> None:
        delta = dev_balance_delta(
            [self._label()],
            [self._edge(_OTHER, _DEV, None), self._edge(_OTHER, _DEV, 3.0)],
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, 3.0)

    def test_all_null_edges_without_balances_is_null(self) -> None:
        delta = dev_balance_delta(
            [self._label()],
            [self._edge(_OTHER, _DEV, None)],
            token_address=_MINT,
        )
        self.assertIsNone(delta)

    def test_no_labels_is_null(self) -> None:
        self.assertIsNone(
            dev_balance_delta([], [self._edge(_OTHER, _DEV, 5.0)], token_address=_MINT)
        )

    def test_whale_label_is_not_dev(self) -> None:
        delta = dev_balance_delta(
            [self._label(label_type="whale_top10")],
            [self._edge(_OTHER, _DEV, 5.0)],
            token_address=_MINT,
        )
        self.assertIsNone(delta)

    def test_other_mint_label_is_ignored(self) -> None:
        delta = dev_balance_delta(
            [self._label(token_mint="othermint")],
            [self._edge(_OTHER, _DEV, 5.0)],
            token_address=_MINT,
        )
        self.assertIsNone(delta)

    def test_unscoped_label_counts_case_insensitive(self) -> None:
        delta = dev_balance_delta(
            [self._label(address="DevWallet111", token_mint=None)],
            [self._edge(_OTHER, "devwallet111", 1.5)],
            token_address=_MINT,
        )
        self.assertIsNotNone(delta)
        assert delta is not None
        self.assertAlmostEqual(delta, 1.5)

    def test_edges_win_over_balances(self) -> None:
        balances = [
            _holder(1, 100.0, holder=_DEV, snapshot_id="s1", recorded_at="2026-10-14T00:00:00Z"),
            _holder(1, 10.0, holder=_DEV, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
        ]
        delta = dev_balance_delta(
            [self._label()],
            [self._edge(_OTHER, _DEV, 0.0)],
            balances,
            token_address=_MINT,
        )
        self.assertEqual(delta, 0.0)

    def test_balance_fallback_is_last_minus_first(self) -> None:
        balances = [
            _holder(1, 100.0, holder=_DEV, snapshot_id="s1", recorded_at="2026-10-14T00:00:00Z"),
            _holder(1, 70.0, holder=_DEV, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
            _holder(2, 5.0, holder=_OTHER, snapshot_id="s1"),
            _holder(2, 50.0, holder=_OTHER, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
        ]
        delta = dev_balance_delta(
            [self._label()],
            [],
            balances,
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, -30.0)

    def test_single_balance_observation_is_null(self) -> None:
        balances = [
            _holder(1, 100.0, holder=_DEV, snapshot_id="s1"),
        ]
        self.assertIsNone(
            dev_balance_delta([self._label()], [], balances, token_address=_MINT)
        )

    def test_dev_missing_from_top10_is_not_zero(self) -> None:
        balances = [
            _holder(1, 40.0, holder=_OTHER, snapshot_id="s1"),
            _holder(1, 40.0, holder=_OTHER, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
        ]
        self.assertIsNone(
            dev_balance_delta([self._label()], [], balances, token_address=_MINT)
        )

    def test_null_balance_does_not_zero_fill(self) -> None:
        balances = [
            _holder(1, 100.0, holder=_DEV, snapshot_id="s1", recorded_at="2026-10-14T00:00:00Z"),
            _holder(1, None, holder=_DEV, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
        ]
        self.assertIsNone(
            dev_balance_delta([self._label()], [], balances, token_address=_MINT)
        )

    def test_two_token_accounts_sum_before_delta(self) -> None:
        balances = [
            _holder(1, 10.0, holder=_DEV, snapshot_id="s1", recorded_at="2026-10-14T00:00:00Z"),
            _holder(2, 5.0, holder=_DEV, snapshot_id="s1", recorded_at="2026-10-14T00:00:00Z"),
            _holder(1, 4.0, holder=_DEV, snapshot_id="s2", recorded_at="2026-10-15T00:00:00Z"),
        ]
        delta = dev_balance_delta(
            [self._label()],
            [],
            balances,
            token_address=_MINT,
        )
        self.assertAlmostEqual(delta or 0.0, -11.0)

    def test_unrelated_edges_do_not_invent_a_zero(self) -> None:
        self.assertIsNone(
            dev_balance_delta(
                [self._label()],
                [self._edge(_OTHER, "someoneelse", 8.0)],
                token_address=_MINT,
            )
        )


class ReaderTest(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE address_labels (
                chain_id TEXT,
                address TEXT,
                label_type TEXT,
                token_mint TEXT
            );
            CREATE TABLE token_holder_snapshots (
                chain_id TEXT,
                token_address TEXT,
                snapshot_id TEXT,
                recorded_at TEXT,
                rank INTEGER,
                holder_address TEXT,
                amount_ui REAL,
                pct_of_supply REAL,
                total_supply_ui REAL
            );
            CREATE TABLE token_transfer_edges (
                chain_id TEXT,
                token_address TEXT,
                from_address TEXT,
                to_address TEXT,
                amount_ui REAL,
                recorded_at TEXT
            );
            """
        )
        return conn

    def test_reads_latest_share_and_history(self) -> None:
        conn = self._conn()
        conn.executemany(
            """
            INSERT INTO token_holder_snapshots (
                chain_id, token_address, snapshot_id, recorded_at, rank,
                holder_address, amount_ui, pct_of_supply, total_supply_ui
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("solana", _MINT, "early", "2026-10-14T00:00:00Z", 1, "a", None, None, 100.0),
                ("solana", _MINT, "late", "2026-10-15T01:00:00Z", 1, "a", 20.0, 0.20, 100.0),
                ("solana", _MINT, "late", "2026-10-15T01:00:00Z", 2, "b", 5.0, 0.05, 100.0),
            ],
        )
        self.assertAlmostEqual(read_top10_share(conn, "solana", _MINT) or 0.0, 0.25)
        history = read_top10_share_history(conn, "Solana", _MINT)
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[0]["top10_share"])
        self.assertAlmostEqual(history[1]["top10_share"] or 0.0, 0.25)
        self.assertIsNone(read_top10_share(conn, "solana", _MINT, snapshot_id="early"))

    def test_reads_dev_flow_and_does_not_write(self) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO address_labels VALUES (?, ?, ?, ?)",
            ("solana", _DEV, "dev", _MINT),
        )
        conn.execute(
            """
            INSERT INTO token_transfer_edges (
                chain_id, token_address, from_address, to_address, amount_ui, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("solana", _MINT, _DEV, _OTHER, 6.0, "2026-10-14T00:00:00Z"),
        )
        before = conn.execute("SELECT COUNT(*) FROM token_transfer_edges").fetchone()
        delta = read_dev_balance_delta(conn, "solana", _MINT)
        after = conn.execute("SELECT COUNT(*) FROM token_transfer_edges").fetchone()
        self.assertAlmostEqual(delta or 0.0, -6.0)
        self.assertEqual(before, after)

    def test_missing_tables_are_null(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.assertIsNone(read_top10_share(conn, "solana", _MINT))
        self.assertEqual(read_top10_share_history(conn, "solana", _MINT), [])
        self.assertIsNone(read_dev_balance_delta(conn, "solana", _MINT))
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertEqual(names, set())

    def test_weights_stay_zero_and_helpers_are_unwired(self) -> None:
        self.assertEqual(INDICATORS_WEIGHT_TECHNICAL, 0.0)
        self.assertEqual(INDICATORS_WEIGHT_FUNDING, 0.0)
        self.assertEqual(INDICATORS_WEIGHT_SENTIMENT, 0.0)
        root = Path(__file__).resolve().parents[1]
        watched = [
            root / "indicators" / "composite.py",
            root / "indicators" / "run.py",
            root / "decision_engine" / "core" / "engine.py",
            root / "review" / "factor_loop.py",
            root / "review" / "gates.py",
        ]
        for path in watched:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("holder_metrics", text, path.name)
            self.assertNotIn("top10_share", text, path.name)
            self.assertNotIn("dev_balance_delta", text, path.name)


if __name__ == "__main__":
    unittest.main()
