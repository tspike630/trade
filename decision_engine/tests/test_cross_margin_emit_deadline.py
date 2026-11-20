"""Deadline and filtered symbols must not hold the next 5m bar."""

from __future__ import annotations

import sqlite3
import unittest

from decision_engine.core.cross_margin_emit_align import (
    CrossMarginEmitState,
    filter_hot_to_pending_ready,
    next_cross_margin_scan_batch,
    trail_deadline_ms,
)


class _Stop(Exception):
    pass


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE binance_klines (symbol TEXT, interval TEXT, open_time_ms INTEGER, close_time_ms INTEGER)"
    )
    conn.execute(
        "CREATE TABLE binance_indicators (symbol TEXT, kline_close_time_ms INTEGER)"
    )
    target = 1_700_000_000_000
    prev = target - 5 * 60 * 1000
    for symbol in ("BTCUSDT", "牛来USDT"):
        conn.execute(
            "INSERT INTO binance_klines VALUES (?, '5m', ?, ?)",
            (symbol, prev - 5 * 60 * 1000, prev),
        )
        conn.execute(
            "INSERT INTO binance_klines VALUES (?, '5m', ?, ?)",
            (symbol, prev, target),
        )
        conn.execute(
            "INSERT INTO binance_indicators VALUES (?, ?)",
            (symbol, target),
        )
    return conn


class EmitDeadlineTests(unittest.TestCase):
    def test_filtered_symbol_is_consumed_and_does_not_stay_pending(self) -> None:
        target = 1_700_000_000_000
        state = CrossMarginEmitState()
        state.armed = True
        state.target_close_ms = target
        state.scan_symbols = {"BTCUSDT", "牛来USDT"}
        kept = filter_hot_to_pending_ready(
            [{"binance_symbol": "BTCUSDT", "kline_close_time_ms": target}],
            state,
        )
        self.assertEqual([row["binance_symbol"] for row in kept], ["BTCUSDT"])
        self.assertIn(("BTCUSDT", target), state.evaluated_close)
        self.assertIn(("牛来USDT", target), state.evaluated_close)
        self.assertIsNone(state.scan_symbols)

    def test_pending_after_deadline_is_not_returned(self) -> None:
        target = 1_700_000_000_000
        conn = _db()
        state = CrossMarginEmitState()
        state.target_close_ms = target
        state.last_seen_close_ms = target
        state.first_appear_ms = target
        state.first_shot_done = True
        deadline = trail_deadline_ms(target)

        def clock() -> int:
            return deadline

        def sleep(_sec: float) -> None:
            if state.bar_cycle_done:
                raise _Stop()
            raise AssertionError("pending past the deadline must not wait for another poll")

        with self.assertRaises(_Stop):
            next_cross_margin_scan_batch(
                conn,
                state,
                now_ms_fn=clock,
                sleep_fn=sleep,
                poll_sec=0.01,
                trail_poll_sec=0.01,
            )
        self.assertTrue(state.bar_cycle_done)
        self.assertIn(("牛来USDT", target), state.evaluated_close)
        self.assertIn(("BTCUSDT", target), state.evaluated_close)


if __name__ == "__main__":
    unittest.main()
