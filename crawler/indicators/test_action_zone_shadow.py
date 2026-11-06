"""Closed-bar action zone and the α ruler. Live entry is not wired here."""

from __future__ import annotations

import sqlite3
import statistics
import tempfile
import unittest
from pathlib import Path

from config import INDICATORS_WEIGHT_TECHNICAL
from decision_engine.core.binance_indicators import list_binance_hot_tokens
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from indicators.action_zone_shadow import (
    ACTION_ZONE_COLUMN_NAMES,
    ACTION_ZONE_WEIGHT,
    REASON_BAREFOOT,
    REASON_CHASED,
    REASON_RETAIL,
    REASON_RR,
    STOP_BUFFER_BPS,
    action_zone_row_from_klines,
    alpha_recipe,
    bar_is_pin,
    indicator_write_columns,
    lower_shadow_rebound_pct,
    _z_at,
)
from indicators.binance_spot import (
    SpotKline,
    SpotSymbol,
    ensure_schema,
    factors_from_klines,
    persist_klines_and_factors,
)
from indicators.technical import change_pct, volume_strength
from indicators.technical_lib import boll_band_series, compute_pandas_ta
from indicators.base import INTERVAL_SEC


_MISSING_DB = Path("/tmp/trade-no-such-crowding-futures.db")
_L = 0.940
_M = 1.000
_S = 0.060


def _bar(
    index: int,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    trades: int | None = 10,
    quote: float = 1000.0,
    taker: float | None = None,
    start: int = 1_700_000_000_000,
) -> SpotKline:
    step = 300_000
    open_ms = start + index * step
    item = SpotKline(
        open_time_ms=open_ms,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        quote_volume=quote,
        close_time_ms=open_ms + 299_999,
        trades=trades,
    )
    if taker is not None:
        object.__setattr__(item, "taker_buy_quote_volume", taker)
    return item


def _flat(
    index: int,
    price: float = 100.0,
    *,
    trades: int | None = 10,
    quote: float = 1000.0,
) -> SpotKline:
    return _bar(
        index,
        open_=price,
        high=price,
        low=price,
        close=price,
        trades=trades,
        quote=quote,
    )


class AlphaRecipeTests(unittest.TestCase):
    def test_yang_example_stop_is_point_one_s_not_the_937_print(self) -> None:
        row = alpha_recipe(low=_L, mid=_M, close=_L + 0.20 * _S, trade_size_ratio=1.45)
        self.assertAlmostEqual(row["space_s"], _S)
        self.assertAlmostEqual(row["stop_price_zone"], _L - 0.1 * _S)
        self.assertAlmostEqual(row["stop_price_zone"], 0.934)
        self.assertNotAlmostEqual(row["stop_price_zone"], 0.937, places=5)
        self.assertAlmostEqual(row["tp_price"], _M)
        self.assertEqual(row["zone_tier"], 1)
        self.assertAlmostEqual(row["entry_zone_low"], 0.9448, places=4)
        self.assertAlmostEqual(row["entry_zone_high"], 0.9640, places=4)
        self.assertAlmostEqual(row["entry_zone_low"], _L + 0.08 * _S)
        self.assertAlmostEqual(row["entry_zone_high"], _L + 0.40 * _S)

    def test_ratio_065_is_tier3_skip_retail_crowded(self) -> None:
        row = alpha_recipe(low=_L, mid=_M, close=_L + 0.20 * _S, trade_size_ratio=0.65)
        self.assertEqual(row["zone_tier"], 3)
        self.assertEqual(row["entry_signal"], "skip")
        self.assertEqual(row["entry_skip_reason"], REASON_RETAIL)
        self.assertIsNone(row["entry_zone_low"])
        self.assertIsNone(row["entry_zone_high"])
        self.assertIsNone(row["limit_buy_price"])
        self.assertAlmostEqual(row["hunter_limit_price"], _L - 0.50 * _S)
        self.assertAlmostEqual(row["hunter_limit_price"], 0.910)

    def test_buffered_rr_and_raw_ablation(self) -> None:
        # rr_to_mid = (1-α)/(α+0.1). rr_raw = (1-α)/α. Tier bars read the first.
        cases = (
            (0.12, 4.0, 0.88 / 0.12),
            (0.28, 0.72 / 0.38, 0.72 / 0.28),
            (0.40, 1.2, 1.5),
        )
        for alpha, buffered, raw in cases:
            row = alpha_recipe(
                low=_L,
                mid=_M,
                close=_L + alpha * _S,
                trade_size_ratio=1.0,
            )
            self.assertAlmostEqual(row["alpha_zone"], alpha, places=9)
            self.assertAlmostEqual(row["rr_to_mid"], buffered, places=9)
            self.assertAlmostEqual(row["rr_raw"], raw, places=9)
            self.assertAlmostEqual(
                row["rr_to_mid"],
                (_M - row["space_l"] - alpha * _S) / ((_L + alpha * _S) - row["stop_price_zone"]),
                places=9,
            )

    def test_tier_boundaries_use_buffered_rr(self) -> None:
        market = alpha_recipe(low=_L, mid=_M, close=_L + 0.20 * _S, trade_size_ratio=1.45)
        self.assertEqual(market["entry_signal"], "market")
        self.assertGreaterEqual(market["rr_to_mid"], 1.5)
        self.assertIsNone(market["limit_buy_price"])

        at_bar = alpha_recipe(low=_L, mid=_M, close=_L + 0.34 * _S, trade_size_ratio=1.45)
        self.assertAlmostEqual(at_bar["rr_to_mid"], 1.5, places=9)
        self.assertEqual(at_bar["entry_signal"], "market")

        top = alpha_recipe(low=_L, mid=_M, close=_L + 0.40 * _S, trade_size_ratio=1.45)
        self.assertAlmostEqual(top["entry_zone_low"], 0.9448, places=4)
        self.assertAlmostEqual(top["entry_zone_high"], 0.9640, places=4)
        self.assertAlmostEqual(top["rr_to_mid"], 1.2, places=9)
        self.assertAlmostEqual(top["rr_raw"], 1.5, places=9)
        self.assertEqual(top["entry_signal"], "skip")
        self.assertEqual(top["entry_skip_reason"], REASON_RR)

        bare = alpha_recipe(low=_L, mid=_M, close=_L + 0.07 * _S, trade_size_ratio=1.45)
        self.assertEqual(bare["entry_signal"], "skip")
        self.assertEqual(bare["entry_skip_reason"], REASON_BAREFOOT)

        edge = alpha_recipe(low=_L, mid=_M, close=_L + 0.08 * _S, trade_size_ratio=1.45)
        self.assertEqual(edge["entry_signal"], "market")
        self.assertAlmostEqual(edge["alpha_zone"], 0.08, places=9)

        chased = alpha_recipe(low=_L, mid=_M, close=_L + 0.41 * _S, trade_size_ratio=1.45)
        self.assertEqual(chased["entry_signal"], "skip")
        self.assertEqual(chased["entry_skip_reason"], REASON_CHASED)

        tier2_market = alpha_recipe(low=_L, mid=_M, close=_L + 0.12 * _S, trade_size_ratio=1.0)
        self.assertEqual(tier2_market["zone_tier"], 2)
        self.assertAlmostEqual(tier2_market["rr_to_mid"], 4.0, places=9)
        self.assertAlmostEqual(tier2_market["rr_raw"], 0.88 / 0.12, places=9)
        self.assertEqual(tier2_market["entry_signal"], "market")

        tier2_inside_short = alpha_recipe(
            low=_L, mid=_M, close=_L + 0.28 * _S, trade_size_ratio=1.0
        )
        self.assertAlmostEqual(tier2_inside_short["rr_to_mid"], 0.72 / 0.38, places=9)
        self.assertAlmostEqual(tier2_inside_short["rr_raw"], 0.72 / 0.28, places=9)
        self.assertEqual(tier2_inside_short["entry_signal"], "skip")
        self.assertEqual(tier2_inside_short["entry_skip_reason"], REASON_RR)

        above = alpha_recipe(low=_L, mid=_M, close=_L + 0.30 * _S, trade_size_ratio=0.85)
        self.assertEqual(above["zone_tier"], 2)
        self.assertEqual(above["entry_signal"], "limit")
        self.assertAlmostEqual(above["limit_buy_price"], _L + 0.20 * _S)
        self.assertAlmostEqual(above["limit_buy_price"], 0.952)
        self.assertIsNone(above["entry_skip_reason"])

        self.assertEqual(
            alpha_recipe(low=_L, mid=_M, close=0.95, trade_size_ratio=1.3)["zone_tier"],
            1,
        )
        self.assertEqual(
            alpha_recipe(low=_L, mid=_M, close=0.95, trade_size_ratio=0.85)["zone_tier"],
            2,
        )
        self.assertEqual(
            alpha_recipe(low=_L, mid=_M, close=0.95, trade_size_ratio=0.849)["zone_tier"],
            3,
        )

    def test_non_positive_span_nulls_alpha_group(self) -> None:
        for mid in (_L, _L - 0.01, None):
            row = alpha_recipe(low=_L, mid=mid, close=0.95, trade_size_ratio=2.0)
            for key in (
                "space_l",
                "space_m",
                "space_s",
                "alpha_zone",
                "rr_to_mid",
                "rr_raw",
                "trade_size_ratio",
                "zone_tier",
                "entry_signal",
                "stop_price_zone",
                "tp_price",
                "entry_zone_low",
                "hunter_limit_price",
            ):
                self.assertIsNone(row[key], key)

    def test_rr_raw_null_when_close_does_not_clear_the_low(self) -> None:
        row = alpha_recipe(low=_L, mid=_M, close=_L, trade_size_ratio=1.45)
        self.assertAlmostEqual(row["alpha_zone"], 0.0)
        self.assertIsNone(row["rr_raw"])
        self.assertAlmostEqual(row["rr_to_mid"], (_M - _L) / (_L - (_L - 0.1 * _S)))
        self.assertEqual(row["entry_signal"], "skip")
        self.assertEqual(row["entry_skip_reason"], REASON_BAREFOOT)

    def test_pin_trigger_legs(self) -> None:
        self.assertTrue(bar_is_pin(pierced=True, change_5m=-1.0, change_5m_z=0.0))
        self.assertTrue(bar_is_pin(pierced=False, change_5m=-2.0, change_5m_z=None))
        self.assertTrue(bar_is_pin(pierced=False, change_5m=-0.5, change_5m_z=-2.0))
        self.assertFalse(bar_is_pin(pierced=False, change_5m=-1.9, change_5m_z=-1.9))

    def test_change_5m_z_excludes_the_current_bar_and_flat_sigma(self) -> None:
        prior = [1.0, -1.0] * 10
        returns: list[float | None] = [None, *prior, -4.0]
        sigma = statistics.stdev(prior)
        self.assertAlmostEqual(_z_at(returns, 21), -4.0 / sigma)
        self.assertIsNone(_z_at(returns, 20))
        flat: list[float | None] = [None, *([0.0] * 20), -3.0]
        self.assertIsNone(_z_at(flat, 21))


class ClosedBarStateTests(unittest.TestCase):
    def _row(
        self,
        bars: list[SpotKline],
        *,
        now_ms: int | None = None,
    ) -> dict:
        return action_zone_row_from_klines(
            bars,
            futures_db=_MISSING_DB,
            now_ms=now_ms,
        )

    def test_blank_row_keeps_shadow_lock_and_every_column(self) -> None:
        row = self._row([])
        for name in ACTION_ZONE_COLUMN_NAMES:
            self.assertIn(name, row)
        self.assertEqual(row["action_zone_state"], "shadow")
        self.assertEqual(row["action_zone_weight"], 0.0)
        self.assertEqual(row["stop_buffer_bps"], STOP_BUFFER_BPS)
        self.assertIsNone(row["entry_signal"])
        self.assertEqual(INDICATORS_WEIGHT_TECHNICAL, 0.0)
        self.assertEqual(ACTION_ZONE_WEIGHT, 0.0)

    def test_next_bar_missing_is_null_and_zero_range_is_zero(self) -> None:
        self.assertEqual(lower_shadow_rebound_pct(10.0, 10.0, 10.0), 0.0)
        self.assertIsNone(lower_shadow_rebound_pct(9.0, 10.0, 10.0))
        bars = [
            _flat(0, 100.0),
            _bar(1, open_=100.0, high=90.0, low=90.0, close=90.0),
        ]
        row = self._row(bars)
        self.assertEqual(row["pin_low"], 90.0)
        self.assertEqual(row["sell_climax_low"], 90.0)
        self.assertEqual(row["hammer_close"], 90.0)
        self.assertEqual(row["lower_shadow_rebound_pct"], 0.0)
        self.assertIsNone(row["no_new_low"])
        self.assertIsNone(row["action_complete"])
        self.assertIsNone(row["rebound_from_low_pct"])
        self.assertIsNone(row["breakout_fired"])
        self.assertIsNone(row["natural_entry_high"])
        self.assertIsNone(row["setup_invalidated"])
        self.assertAlmostEqual(row["natural_stop_price"], 90.0 * (1.0 - 10 / 10_000))
        self.assertAlmostEqual(row["natural_entry_low"], 90.0)

    def test_confirmation_breakout_and_invalidation_are_ablation_only(self) -> None:
        pin = _bar(1, open_=100.0, high=100.0, low=90.0, close=95.0)
        hold = _bar(2, open_=95.0, high=97.0, low=91.0, close=96.0)
        quiet = self._row([_flat(0), pin])
        self.assertIsNone(quiet["action_complete"])
        confirmed = self._row([_flat(0), pin, hold])
        self.assertEqual(confirmed["no_new_low"], 1)
        self.assertEqual(confirmed["action_complete"], 1)
        self.assertEqual(confirmed["setup_invalidated"], 0)
        self.assertIsNone(confirmed["breakout_fired"])
        self.assertEqual(confirmed["natural_entry_high"], 97.0)
        self.assertEqual(confirmed["breakout_entry_price"], 97.0)
        self.assertAlmostEqual(confirmed["rebound_from_low_pct"], (96.0 - 90.0) / 90.0 * 100.0)

        broke = _bar(3, open_=96.0, high=99.0, low=92.0, close=98.0)
        fired = self._row([_flat(0), pin, hold, broke])
        self.assertEqual(fired["breakout_fired"], 1)
        self.assertEqual(fired["setup_invalidated"], 0)
        self.assertEqual(fired["pin_low"], 90.0)

        wicked = _bar(3, open_=96.0, high=110.0, low=89.0, close=100.0)
        dead = self._row([_flat(0), pin, hold, wicked])
        self.assertEqual(dead["setup_invalidated"], 1)
        self.assertEqual(dead["breakout_fired"], 0)

        later = _bar(4, open_=100.0, high=120.0, low=95.0, close=110.0)
        still_dead = self._row([_flat(0), pin, hold, wicked, later])
        self.assertEqual(still_dead["setup_invalidated"], 1)
        self.assertEqual(still_dead["breakout_fired"], 0)

        # Close sits on the pin low, so the reclaim fails, but the bar is not
        # itself a fresh -2% pin (the needle already closed at 90.5).
        needle = _bar(1, open_=100.0, high=100.0, low=90.0, close=90.5)
        close_only = _bar(2, open_=90.5, high=91.0, low=90.0, close=90.0)
        incomplete = self._row([_flat(0), needle, close_only])
        self.assertEqual(incomplete["pin_low"], 90.0)
        self.assertEqual(incomplete["no_new_low"], 1)
        self.assertEqual(incomplete["action_complete"], 0)
        self.assertEqual(incomplete["breakout_fired"], 0)

    def test_forming_bar_wick_is_ignored(self) -> None:
        pin = _bar(1, open_=100.0, high=100.0, low=90.0, close=95.0)
        closed = [_flat(0), pin]
        forming = _bar(2, open_=95.0, high=120.0, low=50.0, close=60.0)
        now = pin.close_time_ms
        without = self._row(closed, now_ms=now)
        with_forming = self._row([*closed, forming], now_ms=now)
        self.assertEqual(without["pin_low"], with_forming["pin_low"])
        self.assertEqual(without["action_complete"], with_forming["action_complete"])
        self.assertIsNone(with_forming["action_complete"])
        self.assertIsNone(with_forming["no_new_low"])
        self.assertIsNone(with_forming["breakout_fired"])

    def test_later_bars_do_not_move_this_bars_alpha(self) -> None:
        bars: list[SpotKline] = []
        for index in range(300):
            bars.append(_flat(index, 1.0, quote=1000.0, trades=10))
        pin = _bar(
            300,
            open_=1.0,
            high=1.0,
            low=0.94,
            close=0.97,
            quote=1450.0,
            trades=10,
        )
        bars.append(pin)
        first = self._row(bars)
        self.assertEqual(first["pin_low"], 0.94)
        self.assertEqual(first["space_l"], 0.94)
        self.assertIsNone(first["action_complete"])
        self.assertAlmostEqual(first["trade_size_ratio"], 1.45)
        self.assertEqual(first["zone_tier"], 1)
        prefix_mid = boll_band_series([bar.close for bar in bars])[1][-1]
        self.assertIsNotNone(prefix_mid)
        self.assertAlmostEqual(first["space_m"], prefix_mid)
        self.assertAlmostEqual(first["alpha_zone"], (0.97 - 0.94) / (prefix_mid - 0.94))

        confirm = _bar(301, open_=0.97, high=0.99, low=0.95, close=0.98, quote=50_000.0, trades=10)
        second = self._row([*bars, confirm])
        self.assertEqual(second["action_complete"], 1)
        self.assertAlmostEqual(second["space_m"], first["space_m"])
        self.assertAlmostEqual(second["alpha_zone"], first["alpha_zone"])
        self.assertAlmostEqual(second["trade_size_ratio"], first["trade_size_ratio"])
        self.assertEqual(second["zone_tier"], first["zone_tier"])
        self.assertEqual(second["pin_low"], 0.94)
        # A hole in the 288-bar window nulls the ratio but not the coordinates.
        hole = list(bars)
        hole[20] = _bar(20, open_=1.0, high=1.0, low=1.0, close=1.0, trades=None, quote=1000.0)
        holed = self._row(hole)
        self.assertIsNone(holed["trade_size_ratio"])
        self.assertIsNone(holed["zone_tier"])
        self.assertIsNone(holed["entry_signal"])
        self.assertAlmostEqual(holed["space_l"], 0.94)
        self.assertAlmostEqual(holed["space_m"], first["space_m"])

    def test_missing_crowding_columns_stay_null_not_zero(self) -> None:
        pin = _bar(13, open_=100.0, high=100.0, low=90.0, close=95.0, trades=40, quote=8000.0)
        hold = _bar(14, open_=95.0, high=97.0, low=91.0, close=96.0)
        bars = [_flat(i) for i in range(13)] + [pin, hold]
        row = self._row(bars)
        self.assertEqual(row["action_complete"], 1)
        self.assertIsNone(row["trades_climax"])
        self.assertIsNone(row["taker_buy_absorb"])
        self.assertIsNone(row["avg_trade_size_spike"])
        self.assertIsNone(row["ls_flush"])
        self.assertIsNone(row["crowding_cleared"])
        self.assertIsNone(row["action_zone_ready"])

    def test_crowding_flags_and_futures_clear(self) -> None:
        pin = _bar(
            13,
            open_=100.0,
            high=100.0,
            low=90.0,
            close=95.0,
            trades=40,
            quote=8000.0,
            taker=4400.0,
        )
        hold = _bar(14, open_=95.0, high=97.0, low=91.0, close=96.0)
        bars = [_flat(i, trades=10, quote=1000.0) for i in range(13)] + [pin, hold]
        plain = action_zone_row_from_klines(bars, symbol="ZZZUSDT", futures_db=_MISSING_DB)
        self.assertEqual(plain["trades_climax"], 1)
        self.assertEqual(plain["taker_buy_absorb"], 1)
        self.assertEqual(plain["avg_trade_size_spike"], 1)
        self.assertIsNone(plain["action_zone_ready"])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "crowding_futures.db"
            self._seed_futures(db, bars, ls_latest=1.8, oi_latest=80.0)
            cleared = action_zone_row_from_klines(bars, symbol="ZZZUSDT", futures_db=db)
            self.assertEqual(cleared["ls_flush"], 1)
            self.assertEqual(cleared["oi_flush"], 1)
            self.assertEqual(cleared["crowding_cleared"], 1)
            self.assertEqual(cleared["crowding_crowded_long"], 0)
            self.assertEqual(cleared["action_zone_ready"], 1)

            self._seed_futures(db, bars, ls_latest=2.2, oi_latest=120.0)
            piled = action_zone_row_from_klines(bars, symbol="ZZZUSDT", futures_db=db)
            self.assertEqual(piled["ls_flush"], 0)
            self.assertEqual(piled["oi_flush"], 0)
            self.assertEqual(piled["crowding_cleared"], 0)
            self.assertEqual(piled["crowding_crowded_long"], 1)
            self.assertEqual(piled["action_zone_ready"], 0)

            odd = Path(tmp) / "odd.db"
            conn = sqlite3.connect(odd)
            conn.execute("CREATE TABLE ls_ratio (note TEXT)")
            conn.execute("CREATE TABLE oi_hist (note TEXT)")
            conn.commit()
            conn.close()
            unknown = action_zone_row_from_klines(bars, symbol="ZZZUSDT", futures_db=odd)
            self.assertIsNone(unknown["ls_flush"])
            self.assertIsNone(unknown["oi_flush"])
            self.assertIsNone(unknown["action_zone_ready"])

    def _seed_futures(
        self,
        path: Path,
        bars: list[SpotKline],
        *,
        ls_latest: float,
        oi_latest: float,
    ) -> None:
        conn = sqlite3.connect(path)
        conn.execute("DROP TABLE IF EXISTS ls_ratio")
        conn.execute("DROP TABLE IF EXISTS oi_hist")
        conn.execute(
            "CREATE TABLE ls_ratio (symbol TEXT, timestamp INTEGER, global_long_short_account_ratio REAL)"
        )
        conn.execute(
            "CREATE TABLE oi_hist (symbol TEXT, time_ms INTEGER, sum_open_interest REAL)"
        )
        # Four samples ending on the last closed bar. Prior three sit on earlier bars
        # whose close is 100, so the latest spot close (96) is a price drop.
        stamps = [bars[i].close_time_ms for i in (8, 10, 12, 14)]
        ls_values = (2.0, 2.0, 2.0, ls_latest)
        oi_values = (100.0, 100.0, 100.0, oi_latest)
        conn.executemany(
            "INSERT INTO ls_ratio VALUES ('ZZZUSDT', ?, ?)",
            list(zip(stamps, ls_values)),
        )
        conn.executemany(
            "INSERT INTO oi_hist VALUES ('ZZZ/USDT', ?, ?)",
            list(zip(stamps, oi_values)),
        )
        conn.commit()
        conn.close()


class SchemaAndLiveUntouchedTests(unittest.TestCase):
    def test_alter_keeps_old_rows_and_dedupes_avg_trade_size(self) -> None:
        self.assertEqual(
            indicator_write_columns(("symbol", "avg_trade_size", "updated_at")).count(
                "avg_trade_size"
            ),
            1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            script = (
                Path(__file__).resolve().parents[1] / "models" / "binance_indicator_tables.sql"
            ).read_text(encoding="utf-8")
            conn.executescript(script)
            # Crowding PR landed first: the shared column already exists.
            conn.execute("ALTER TABLE binance_indicators ADD COLUMN avg_trade_size REAL")
            conn.execute(
                """
                INSERT INTO binance_indicators (
                    symbol, interval, volume_strength, avg_trade_size, updated_at
                ) VALUES ('KEEPUSDT', '5m', 0.42, 7.5, '2026-11-05T00:00:00Z')
                """
            )
            conn.execute(
                """
                CREATE TABLE token_indicators (
                    chain_id TEXT, token_address TEXT, change_5m REAL
                )
                """
            )
            conn.execute("INSERT INTO token_indicators VALUES ('solana', 'MintPaper', 1.25)")
            conn.commit()
            before = conn.execute("SELECT * FROM token_indicators").fetchall()
            ensure_schema(conn)
            ensure_schema(conn)
            kept = conn.execute(
                "SELECT volume_strength, avg_trade_size FROM binance_indicators WHERE symbol = 'KEEPUSDT'"
            ).fetchone()
            self.assertEqual(kept, (0.42, 7.5))
            cols = [row[1] for row in conn.execute("PRAGMA table_info(binance_indicators)")]
            self.assertEqual(cols.count("avg_trade_size"), 1)
            self.assertIn("stop_price_zone", cols)
            self.assertIn("rr_to_mid", cols)
            self.assertIn("rr_raw", cols)
            self.assertIn("entry_signal", cols)
            self.assertIn("natural_stop_price", cols)
            self.assertEqual(conn.execute("SELECT * FROM token_indicators").fetchall(), before)
            conn.close()

    def test_factors_leave_volume_strength_and_hot_scan_alone(self) -> None:
        start = 1_700_000_000_000
        step = 300_000
        raw = []
        for i in range(30):
            close = 10.0 if i == 29 else 100.0
            open_ms = start + i * step
            raw.append(
                [
                    open_ms,
                    "100",
                    "101",
                    "99",
                    str(close),
                    "10",
                    open_ms + 299_999,
                    "2000",
                    3,
                ]
            )
        from indicators.binance_spot import parse_kline_row, price_series_from_klines

        bars = [parse_kline_row(row) for row in raw]
        assert all(bar is not None for bar in bars)
        factors = factors_from_klines(bars, futures_db=_MISSING_DB)
        assert factors is not None
        series = price_series_from_klines(bars)
        self.assertAlmostEqual(
            factors["volume_strength"],
            volume_strength(series),
            places=6,
        )
        self.assertAlmostEqual(
            factors["change_5m"],
            change_pct(series, INTERVAL_SEC["change_5m"]),
            places=6,
        )
        closes = [float(row["price_usd"]) for row in series]
        self.assertEqual(factors["boll_lower"], compute_pandas_ta(closes)["boll_lower"])
        self.assertEqual(factors["action_zone_state"], "shadow")
        self.assertEqual(factors["action_zone_weight"], 0.0)

        source = (
            Path(__file__).resolve().parents[2]
            / "decision_engine"
            / "strategies"
            / "tech_boll_fade.py"
        )
        body = source.read_text(encoding="utf-8")
        self.assertNotIn("entry_signal", body)
        self.assertNotIn("stop_price_zone", body)
        self.assertNotIn("action_zone_ready", body)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ind.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE token_indicators (chain_id TEXT, token_address TEXT, change_5m REAL)"
            )
            conn.execute("INSERT INTO token_indicators VALUES ('solana', 'MintPaper', 1.25)")
            conn.commit()
            before = conn.execute("SELECT * FROM token_indicators").fetchall()
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("ZZQBOLLUSDT", "ZZQBOLL"),
                bars,
            )
            self.assertIsNotNone(stored)
            self.assertEqual(conn.execute("SELECT * FROM token_indicators").fetchall(), before)
            hot = list_binance_hot_tokens(conn, limit=5, fresh_sec=3600)
            self.assertEqual([row["token_address"] for row in hot], ["ZZQBOLLUSDT"])
            for banned in (
                "entry_signal",
                "stop_price_zone",
                "pin_low",
                "rr_to_mid",
                "action_zone_ready",
                "breakout_fired",
                "alpha_zone",
            ):
                self.assertNotIn(banned, hot[0])
            signals = TechBollFadeStrategy().on_tick({"hot_tokens": hot})
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["strategy_id"], "tech_boll_fade_v1")
            self.assertNotIn("entry_signal", signals[0])
            forced = conn.execute(
                "SELECT action_zone_weight, action_zone_state, stop_buffer_bps FROM binance_indicators"
            ).fetchone()
            self.assertEqual(forced, (0.0, "shadow", 10))
            conn.close()


if __name__ == "__main__":
    unittest.main()
