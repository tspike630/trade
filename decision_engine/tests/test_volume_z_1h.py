"""Synthetic Binance-shaped 1h bars for volume_z. Not the box database."""

from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine import config as de_config
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.core.volume_z_1h import (
    VOLUME_Z_BASELINE_BARS,
    VOLUME_Z_CONTRACT_VERSION,
    VOLUME_Z_MIN_CLOSED_BARS,
    attach_stored_volume_z,
    compute_volume_z,
    inspect_volume_z,
    refresh_volume_z_from_klines,
    write_symbol_volume_z,
)
from decision_engine.strategies import get_strategy

_HOUR_MS = 3_600_000
_CLOSE_SPAN = _HOUR_MS - 1
_START = 1_700_000_000_000
_BASELINE = [100.0] * 19 + [160.0]


def _bar(index: int, quote: float, *, open_ms: int | None = None) -> list:
    opened = _START + index * _HOUR_MS if open_ms is None else open_ms
    return [
        opened,
        "100",
        "101",
        "99",
        "100",
        "5",
        opened + _CLOSE_SPAN,
        quote,
        10,
        "1",
        "1",
        "0",
    ]


def _closed_now(last_index: int) -> int:
    return _START + last_index * _HOUR_MS + _CLOSE_SPAN


def _mean_std(values: list[float]) -> tuple[float, float]:
    mean = sum(values) / len(values)
    var = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return mean, math.sqrt(var)


def _series(current: float, *, extra: list | None = None) -> list:
    rows = [_bar(index, quote) for index, quote in enumerate(_BASELINE)]
    rows.append(_bar(VOLUME_Z_BASELINE_BARS, current))
    if extra:
        rows.extend(extra)
    return rows


class SourceShapeTests(unittest.TestCase):
    """Synthetic fixtures. Each case is a Binance kline array, not a stored z row."""

    def test_gap_duplicate_conflict_and_out_of_order(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        intact = compute_volume_z(_series(1000.0), now_ms=now_ms)
        self.assertIsNotNone(intact)
        gapped = [_bar(index, 100.0) for index in range(22) if index != 10]
        self.assertIsNone(compute_volume_z(gapped, now_ms=_closed_now(21)))
        self.assertEqual(
            inspect_volume_z(gapped, now_ms=_closed_now(21))["status"],
            "latest_21_not_consecutive",
        )
        shuffled = list(_series(1000.0))
        shuffled.reverse()
        self.assertEqual(compute_volume_z(shuffled, now_ms=now_ms), intact)
        conflicted = _series(1000.0) + [_bar(VOLUME_Z_BASELINE_BARS, 5.0)]
        self.assertIsNone(compute_volume_z(conflicted, now_ms=now_ms))
        identical = _series(1000.0) + [_bar(3, _BASELINE[3])]
        self.assertEqual(compute_volume_z(identical, now_ms=now_ms), intact)

    def test_newest_invalid_bar_does_not_keep_an_older_window(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        rows = _series(1000.0)
        rows[-1][7] = -1.0
        self.assertIsNone(compute_volume_z(rows, now_ms=now_ms))
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "z.db")
            _insert_klines(conn, "FADEUSDT", _series(1000.0))
            self.assertIsNotNone(
                refresh_volume_z_from_klines(conn, now_ms=now_ms, symbols=["FADEUSDT"])
            )
            conn.execute(
                "UPDATE binance_klines SET quote_volume = -1 WHERE open_time_ms = ?",
                (_START + VOLUME_Z_BASELINE_BARS * _HOUR_MS,),
            )
            conn.commit()
            again = refresh_volume_z_from_klines(conn, now_ms=now_ms, symbols=["FADEUSDT"])
            self.assertEqual(again, [])
            left = conn.execute("SELECT COUNT(*) FROM decision_volume_z").fetchone()
            self.assertEqual(int(left[0]), 0)
            stamped = attach_stored_volume_z(
                conn, [{"binance_symbol": "FADEUSDT"}], now_ms=now_ms
            )
            self.assertNotIn("volume_z", stamped[0])
            conn.close()

    def test_next_hour_boundary_uses_the_newly_closed_bar(self) -> None:
        previous_close = _closed_now(VOLUME_Z_BASELINE_BARS)
        newer = _bar(VOLUME_Z_MIN_CLOSED_BARS, 50.0)
        rows = _series(1000.0) + [newer]
        still_open = compute_volume_z(rows, now_ms=newer[6] - 1)
        just_closed = compute_volume_z(rows, now_ms=newer[6])
        assert still_open is not None and just_closed is not None
        self.assertEqual(still_open[2], previous_close)
        self.assertEqual(just_closed[2], newer[6])
        self.assertNotEqual(still_open[0], just_closed[0])

    def test_reader_rejects_a_stored_row_that_does_not_match_source(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "z.db")
            _insert_klines(conn, "FADEUSDT", _series(1000.0))
            record = write_symbol_volume_z(conn, "FADEUSDT", _series(1000.0), now_ms=now_ms)
            self.assertIsNotNone(record)
            conn.execute(
                "UPDATE decision_volume_z SET contract_version = 'other', volume_z = 99"
            )
            conn.commit()
            stamped = attach_stored_volume_z(
                conn, [{"binance_symbol": "FADEUSDT"}], now_ms=now_ms
            )
            self.assertNotIn("volume_z", stamped[0])
            conn.close()


class ComputeContractTests(unittest.TestCase):
    def test_current_bar_is_outside_the_baseline_and_uses_sample_std(self) -> None:
        mean, std = _mean_std(_BASELINE)
        current = 1000.0
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        score = compute_volume_z(_series(current), now_ms=now_ms)
        self.assertIsNotNone(score)
        assert score is not None
        self.assertAlmostEqual(score[0], (current - mean) / std, places=9)
        self.assertAlmostEqual(score[4], mean, places=9)
        self.assertNotAlmostEqual(score[4], (sum(_BASELINE) + current) / 21, places=6)
        self.assertEqual(score[2], now_ms)

    def test_twenty_closed_bars_do_not_output_and_clear_a_prior_value(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "z.db")
            written = write_symbol_volume_z(conn, "fetusdt", _series(1000.0), now_ms=now_ms)
            self.assertIsNotNone(written)
            short = _series(1000.0)[:-1]
            self.assertEqual(len(short), 20)
            cleared = write_symbol_volume_z(conn, "FETUSDT", short, now_ms=now_ms)
            self.assertIsNone(cleared)
            left = conn.execute("SELECT COUNT(*) FROM decision_volume_z").fetchone()
            self.assertEqual(int(left[0]), 0)
            conn.close()

    def test_open_candle_is_excluded(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        open_bar = _bar(
            VOLUME_Z_MIN_CLOSED_BARS,
            1_000_000_000.0,
            open_ms=_START + VOLUME_Z_MIN_CLOSED_BARS * _HOUR_MS,
        )
        self.assertGreater(open_bar[6], now_ms)
        without = compute_volume_z(_series(1000.0), now_ms=now_ms)
        with_open = compute_volume_z(_series(1000.0, extra=[open_bar]), now_ms=now_ms)
        self.assertEqual(without, with_open)
        only_open = [_bar(index, 100.0) for index in range(20)] + [open_bar]
        self.assertIsNone(compute_volume_z(only_open, now_ms=now_ms))

    def test_zero_std_and_nonfinite_quote_do_not_output(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        flat = [_bar(index, 100.0) for index in range(VOLUME_Z_MIN_CLOSED_BARS)]
        self.assertIsNone(compute_volume_z(flat, now_ms=now_ms))
        broken = _series(1000.0)
        broken[3][7] = float("nan")
        self.assertIsNone(compute_volume_z(broken, now_ms=now_ms))
        infinite = _series(1000.0)
        infinite[-1][7] = float("inf")
        self.assertIsNone(compute_volume_z(infinite, now_ms=now_ms))

    def test_base_volume_and_five_minute_bars_are_not_the_series(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        rows = _series(1000.0)
        for row in rows:
            row[5] = 9_999_999.0
        quote_only = compute_volume_z(rows, now_ms=now_ms)
        self.assertIsNotNone(quote_only)
        five_minute = []
        for index, quote in enumerate(_BASELINE + [1000.0]):
            opened = _START + index * 300_000
            five_minute.append(
                [opened, "1", "1", "1", "1", "1", opened + 299_999, quote, 1, "0", "0", "0"]
            )
        self.assertIsNone(compute_volume_z(five_minute, now_ms=now_ms))


class StorageAndTickTests(unittest.TestCase):
    def _seed_fade(self, db: Path) -> None:
        row = (
            "FADEUSDT",
            "FADE",
            "USDT",
            "TRADING",
            100.8,
            -4.0,
            0.9,
            101.8,
            110.0,
            100.0,
            1.3,
            float(de_config.DECISION_MIN_LIQ_USD) + 10_000.0,
            0.75,
            0.85,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY,
                base_asset TEXT,
                quote_asset TEXT,
                status TEXT,
                price_usd REAL,
                change_5m REAL,
                volume_strength REAL,
                boll_lower REAL,
                boll_mid REAL,
                kline_low REAL,
                trade_size_ratio REAL,
                quote_volume_24h REAL,
                xs_breadth_1h REAL,
                xs_rank_pct_1h REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO binance_indicators VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        now_ms = int(time.time() * 1000)
        conn.execute(
            "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
        )
        conn.execute("INSERT INTO symbols VALUES ('FADEUSDT', 'TRADING', 'FADE', 'USDT')")
        conn.execute(
            """
            CREATE TABLE ccxt_ticks (
                exchange TEXT, symbol TEXT, ts INTEGER,
                quote_volume_24h REAL, volume_24h REAL
            )
            """
        )
        conn.execute(
            "INSERT INTO ccxt_ticks VALUES ('binance', 'FADE/USDT', ?, 3000000, 1)",
            (now_ms,),
        )
        conn.commit()
        conn.close()

    def _tick(self, db: Path) -> dict:
        adapter = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        old = os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
        try:
            engine = DecisionEngine(
                adapter,
                bus,
                [get_strategy("tech_boll_fade_v1")],
                risk=RiskManager(cooldown_sec=0),
            )
            return engine.tick()
        finally:
            if old is None:
                os.environ.pop("LIVE_BNB_FACTOR_SOURCE", None)
            else:
                os.environ["LIVE_BNB_FACTOR_SOURCE"] = old
            bus.close()
            adapter.close()

    def test_threshold_and_adjacent_value_reach_the_tick(self) -> None:
        mean, std = _mean_std(_BASELINE)
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        reject_quote = mean + 3.8 * std
        reject_rows = _series(reject_quote)
        reject_score = compute_volume_z(reject_rows, now_ms=now_ms)
        while reject_score is None or reject_score[0] < 3.8:
            reject_quote = math.nextafter(reject_quote, math.inf)
            reject_rows = _series(reject_quote)
            reject_score = compute_volume_z(reject_rows, now_ms=now_ms)
        allow_rows = _series(mean + 3.79 * std)
        allow_score = compute_volume_z(allow_rows, now_ms=now_ms)
        self.assertIsNotNone(allow_score)
        assert allow_score is not None
        self.assertLess(allow_score[0], 3.8)
        self.assertGreater(allow_score[0], 3.7)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reject_db = root / "reject.db"
            allow_db = root / "allow.db"
            self._seed_fade(reject_db)
            self._seed_fade(allow_db)
            reject_conn = sqlite3.connect(reject_db)
            allow_conn = sqlite3.connect(allow_db)
            # The tick clock is wall time. Place the closed bar at that clock.
            wall = int(time.time() * 1000)
            shifted_reject = _shift(reject_rows, wall)
            shifted_allow = _shift(allow_rows, wall)
            _insert_klines(reject_conn, "FADEUSDT", shifted_reject)
            _insert_klines(allow_conn, "FADEUSDT", shifted_allow)
            reject_conn.close()
            allow_conn.close()
            rejected = self._tick(reject_db)
            allowed = self._tick(allow_db)
            self.assertEqual(rejected["emitted"], 0)
            self.assertEqual(allowed["emitted"], 1)
            saved_conn = sqlite3.connect(allow_db)
            saved = saved_conn.execute(
                """
                SELECT volume_z, contract_version, source_close_time_ms, baseline_count
                FROM decision_volume_z
                """
            ).fetchone()
            saved_conn.close()
        self.assertEqual(saved[1], VOLUME_Z_CONTRACT_VERSION)
        self.assertEqual(int(saved[2]), wall)
        self.assertEqual(int(saved[3]), VOLUME_Z_BASELINE_BARS)
        self.assertLess(float(saved[0]), 3.8)

    def test_stale_storage_is_not_stamped_and_table_refresh_ignores_other_intervals(self) -> None:
        now_ms = _closed_now(VOLUME_Z_BASELINE_BARS)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "z.db")
            _ensure_kline_table(conn)
            _insert_klines(conn, "FADEUSDT", _series(1000.0))
            record = write_symbol_volume_z(conn, "FADEUSDT", _series(1000.0), now_ms=now_ms)
            self.assertIsNotNone(record)
            rows = [{"binance_symbol": "FADEUSDT", "volume_strength": 9.0}]
            fresh = attach_stored_volume_z(conn, rows, now_ms=now_ms)
            self.assertIn("volume_z", fresh[0])
            self.assertNotEqual(fresh[0]["volume_z"], fresh[0]["volume_strength"])
            stale_clock = now_ms + 3_600_000 + 1
            stale_rows = [{"binance_symbol": "FADEUSDT", "volume_strength": 9.0}]
            stale = attach_stored_volume_z(conn, stale_rows, now_ms=stale_clock)
            self.assertNotIn("volume_z", stale[0])
            boundary = attach_stored_volume_z(
                conn, [{"binance_symbol": "FADEUSDT"}], now_ms=now_ms + 3_600_000
            )
            self.assertIn("volume_z", boundary[0])
            for index, quote in enumerate(_BASELINE + [1000.0]):
                opened = _START + index * _HOUR_MS
                conn.execute(
                    "INSERT INTO binance_klines VALUES ('SOLUSDT', '1h', ?, ?, ?)",
                    (opened, quote, opened + _CLOSE_SPAN),
                )
                conn.execute(
                    "INSERT INTO binance_klines VALUES ('SOLUSDT', '5m', ?, ?, ?)",
                    (opened, quote * 50, opened + 299_999),
                )
            conn.commit()
            written = refresh_volume_z_from_klines(conn, now_ms=now_ms, symbols=["SOLUSDT"])
            self.assertEqual(len(written), 1)
            direct = compute_volume_z(_series(1000.0), now_ms=now_ms)
            assert direct is not None
            self.assertAlmostEqual(written[0].volume_z, direct[0], places=9)
            self.assertAlmostEqual(written[0].baseline_mean, direct[4], places=9)
            conn.close()


def _ensure_kline_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS binance_klines (
            symbol TEXT, interval TEXT, open_time_ms INTEGER,
            quote_volume REAL, close_time_ms INTEGER
        )
        """
    )


def _insert_klines(conn: sqlite3.Connection, symbol: str, rows: list) -> None:
    """Store synthetic source rows the way binance_klines keeps 1h bars."""
    _ensure_kline_table(conn)
    for row in rows:
        conn.execute(
            "INSERT INTO binance_klines VALUES (?, '1h', ?, ?, ?)",
            (symbol, row[0], row[7], row[6]),
        )
    conn.commit()


def _shift(rows: list, close_ms: int) -> list:
    """Move a closed series so the latest close timestamp is ``close_ms``."""
    last_open = rows[-1][0]
    delta = close_ms - (last_open + _CLOSE_SPAN)
    moved = []
    for row in rows:
        copy = list(row)
        copy[0] = int(copy[0]) + delta
        copy[6] = int(copy[6]) + delta
        moved.append(copy)
    return moved


if __name__ == "__main__":
    unittest.main()
