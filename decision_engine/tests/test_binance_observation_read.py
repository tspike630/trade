"""Read-only pass-through of Binance observation columns.

The indicator job owns the arithmetic. These tests lock the decision reader:
old and partial schemas still scan, NULL stays None, and the eight fields do
not become a new tech_boll_fade gate.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine.core.binance_indicators import (
    OBSERVATION_FIELDS,
    OBSERVATION_READ_CONTRACT,
    _optional_number,
    list_binance_hot_tokens,
)
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy

_SCHEMA = (
    Path(__file__).resolve().parents[2] / "crawler" / "models" / "binance_indicator_tables.sql"
)
_ENTRY_ZONE = (
    Path(__file__).resolve().parents[1] / "strategies" / "live_bnb_entry_zone.py"
)
_TECH_BOLL = Path(__file__).resolve().parents[1] / "strategies" / "tech_boll_fade.py"

_NEW_GATES = tuple(name for name in OBSERVATION_FIELDS if name != "trade_size_ratio")


def _schema_sql(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def _table_rows(conn: sqlite3.Connection, table: str) -> list:
    return conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()


class ObservationContractTests(unittest.TestCase):
    def test_contract_keeps_upstream_null_rules_and_wick_formula(self) -> None:
        text = OBSERVATION_READ_CONTRACT
        self.assertEqual(len(OBSERVATION_FIELDS), 8)
        for name in OBSERVATION_FIELDS:
            self.assertIn(name, text)
        self.assertIn("genuine", text)
        self.assertIn("complete local 7d closed-bar history", text)
        self.assertIn("12 contiguous closed 5m bars", text)
        self.assertIn("complete taker quote data", text)
        self.assertIn("sum(quote_volume)/sum(trades)", text)
        self.assertIn("not the mean of per-bar averages", text)
        self.assertIn("(close-low)/(high-low)", text)
        self.assertIn("not the anatomical lower wick", text)
        self.assertIn("(min(open, close)-low)/(high-low)", text)
        self.assertIn("does not rewrite", text)
        shipped = _SCHEMA.read_text(encoding="utf-8")
        for name in OBSERVATION_FIELDS:
            self.assertNotIn(name, shipped)

    def test_optional_number_keeps_zero_and_drops_non_finite(self) -> None:
        self.assertIsNone(_optional_number(None))
        self.assertIsNone(_optional_number(True))
        self.assertIsNone(_optional_number(False))
        self.assertIsNone(_optional_number(float("nan")))
        self.assertIsNone(_optional_number(float("inf")))
        self.assertIsNone(_optional_number("nan"))
        self.assertIsNone(_optional_number("  "))
        self.assertIsNone(_optional_number("nope"))
        self.assertEqual(_optional_number(0), 0)
        self.assertEqual(_optional_number(0.0), 0.0)
        self.assertEqual(_optional_number("0"), 0.0)
        self.assertEqual(_optional_number(" -1.5 "), -1.5)
        self.assertEqual(_optional_number(-3), -3)


class ObservationSchemaTests(unittest.TestCase):
    def test_shipped_schema_returns_none_and_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "old.db")
            conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
            conn.execute(
                """
                INSERT INTO binance_indicators (
                    symbol, base_asset, quote_asset, status, interval,
                    price_usd, change_5m, change_24h, volume_strength,
                    boll_lower, quote_volume_24h, tier, updated_at
                ) VALUES (
                    'OLDUSDT', 'OLD', 'USDT', 'TRADING', '5m',
                    1.0, -4.0, 9.0, 0.9,
                    2.0, 80000.0, 'hot', '2026-11-07T00:00:00Z'
                )
                """
            )
            step = 5 * 60 * 1000
            start = 1_700_000_000_000
            conn.executemany(
                """
                INSERT INTO binance_klines (
                    symbol, interval, open_time_ms, open, high, low, close,
                    volume, quote_volume, close_time_ms, trades
                ) VALUES ('OLDUSDT', '5m', ?, 1, 2, 0.5, 1.5, 4, 10, ?, 2)
                """,
                [
                    (start + index * step, start + index * step + step)
                    for index in range(12 * 24 * 7)
                ],
            )
            conn.commit()
            before_changes = conn.total_changes
            before_schema = _schema_sql(conn)
            before_indicators = _table_rows(conn, "binance_indicators")
            before_klines = conn.execute("SELECT COUNT(*) FROM binance_klines").fetchone()
            rows = list_binance_hot_tokens(conn, fresh_sec=0)
            self.assertEqual(conn.total_changes, before_changes)
            self.assertEqual(_schema_sql(conn), before_schema)
            self.assertEqual(_table_rows(conn, "binance_indicators"), before_indicators)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM binance_klines").fetchone(),
                before_klines,
            )
            self.assertEqual([row["token_address"] for row in rows], ["OLDUSDT"])
            for name in OBSERVATION_FIELDS:
                self.assertIn(name, rows[0])
                self.assertIsNone(rows[0][name])
            self.assertEqual(rows[0]["change_24h"], 9.0)
            self.assertNotEqual(rows[0]["change_7d"], 9.0 * 7)
            conn.close()

    def test_partial_schema_does_not_derive_missing_observations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "partial.db")
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT PRIMARY KEY,
                    quote_asset TEXT,
                    status TEXT,
                    interval TEXT,
                    change_5m REAL,
                    change_24h REAL,
                    bar_quote_volume REAL,
                    bar_trades REAL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    taker_buy_quote_volume_5m REAL,
                    buy_ratio_1h REAL,
                    updated_at TEXT
                )
                """
            )
            # Components that would tempt a reader to invent the other fields.
            # close location = (9-6)/(10-6) = 0.75
            # anatomical wick = (min(8,9)-6)/(10-6) = 0.5
            # avg trade = 100/4 = 25
            # buy ratio = 40/100 = 0.4
            conn.execute(
                """
                INSERT INTO binance_indicators (
                    symbol, quote_asset, status, interval, change_5m, change_24h,
                    bar_quote_volume, bar_trades, open, high, low, close,
                    taker_buy_quote_volume_5m, buy_ratio_1h, updated_at
                ) VALUES (
                    'PARTUSDT', 'USDT', 'TRADING', '5m', -3.0, 10.0,
                    100.0, 4.0, 8.0, 10.0, 6.0, 9.0,
                    40.0, NULL, '2026-11-07T00:00:00Z'
                )
                """
            )
            conn.commit()
            before_changes = conn.total_changes
            before_cols = [
                row[1] for row in conn.execute("PRAGMA table_info(binance_indicators)")
            ]
            rows = list_binance_hot_tokens(conn, fresh_sec=0)
            self.assertEqual(conn.total_changes, before_changes)
            self.assertEqual(
                [row[1] for row in conn.execute("PRAGMA table_info(binance_indicators)")],
                before_cols,
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["taker_buy_quote_volume_5m"], 40.0)
            self.assertIsNone(row["buy_ratio_1h"])
            self.assertIsNone(row["buy_ratio_5m"])
            self.assertIsNone(row["change_7d"])
            self.assertIsNone(row["volume_7d_avg"])
            self.assertIsNone(row["avg_trade_usd_5m"])
            self.assertIsNone(row["trade_size_ratio"])
            self.assertIsNone(row["lower_wick_ratio"])
            self.assertNotEqual(row["lower_wick_ratio"], 0.75)
            self.assertNotEqual(row["lower_wick_ratio"], 0.5)
            self.assertNotEqual(row["avg_trade_usd_5m"], 25.0)
            self.assertNotEqual(row["change_7d"], 70.0)
            self.assertEqual(row["bar_quote_volume"], 100.0)
            self.assertEqual(row["bar_trades"], 4.0)
            conn.close()

    def test_present_values_nulls_and_text_map_without_filling_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "full.db")
            conn.execute(
                """
                CREATE TABLE binance_indicators (
                    symbol TEXT PRIMARY KEY,
                    quote_asset TEXT,
                    status TEXT,
                    interval TEXT,
                    change_5m REAL,
                    change_7d,
                    volume_7d_avg,
                    taker_buy_quote_volume_5m,
                    buy_ratio_5m,
                    buy_ratio_1h,
                    avg_trade_usd_5m,
                    trade_size_ratio,
                    lower_wick_ratio,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    updated_at TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO binance_indicators (
                    symbol, quote_asset, status, interval, change_5m,
                    change_7d, volume_7d_avg, taker_buy_quote_volume_5m,
                    buy_ratio_5m, buy_ratio_1h, avg_trade_usd_5m,
                    trade_size_ratio, lower_wick_ratio,
                    open, high, low, close, updated_at
                ) VALUES (
                    ?, 'USDT', 'TRADING', '5m', ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, '2026-11-07T00:00:00Z'
                )
                """,
                [
                    (
                        "NUMUSDT",
                        -5.0,
                        -3.5,
                        0,
                        12.5,
                        0.4,
                        None,
                        80,
                        1.5,
                        0.2,
                        8.0,
                        10.0,
                        6.0,
                        9.0,
                    ),
                    (
                        "NULLUSDT",
                        -1.0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        1.0,
                        4.0,
                        1.0,
                        2.0,
                    ),
                    (
                        "TEXTUSDT",
                        -2.0,
                        "nope",
                        "",
                        "nan",
                        "inf",
                        None,
                        "0",
                        "-1.5",
                        "0.25",
                        1.0,
                        4.0,
                        1.0,
                        3.0,
                    ),
                ],
            )
            conn.commit()
            before_changes = conn.total_changes
            rows = {
                row["token_address"]: row
                for row in list_binance_hot_tokens(conn, limit=10, fresh_sec=0)
            }
            self.assertEqual(conn.total_changes, before_changes)
            self.assertEqual(set(rows), {"NUMUSDT", "NULLUSDT", "TEXTUSDT"})
            num = rows["NUMUSDT"]
            self.assertEqual(num["change_7d"], -3.5)
            self.assertEqual(num["volume_7d_avg"], 0)
            self.assertEqual(num["taker_buy_quote_volume_5m"], 12.5)
            self.assertEqual(num["buy_ratio_5m"], 0.4)
            self.assertIsNone(num["buy_ratio_1h"])
            self.assertEqual(num["avg_trade_usd_5m"], 80)
            self.assertEqual(num["trade_size_ratio"], 1.5)
            # Stored sentinel is not replaced by (close-low)/(high-low) = 0.75.
            self.assertEqual(num["lower_wick_ratio"], 0.2)
            self.assertNotEqual(num["lower_wick_ratio"], 0.75)
            for name in OBSERVATION_FIELDS:
                self.assertIsNone(rows["NULLUSDT"][name])
            text = rows["TEXTUSDT"]
            self.assertIsNone(text["change_7d"])
            self.assertIsNone(text["volume_7d_avg"])
            self.assertIsNone(text["taker_buy_quote_volume_5m"])
            self.assertIsNone(text["buy_ratio_5m"])
            self.assertIsNone(text["buy_ratio_1h"])
            self.assertEqual(text["avg_trade_usd_5m"], 0.0)
            self.assertEqual(text["trade_size_ratio"], -1.5)
            self.assertEqual(text["lower_wick_ratio"], 0.25)
            conn.close()


def _paper_row(**extra: object) -> dict:
    row = {
        "chain_id": "binance",
        "token_address": "OBSUSDT",
        "primary_pair_address": "OBS/USDT",
        "change_5m": -4.0,
        "volume_strength": 0.9,
        "boll_lower": 1.0,
        "price_usd": 0.8,
    }
    row.update(extra)
    return row


def _zone_row(**extra: object) -> dict:
    low = 100.0
    span = 10.0
    row = {
        "binance_symbol": "OBSUSDT",
        "chain_id": "binance",
        "token_address": "OBSUSDT",
        "kline_low": low,
        "boll_mid": low + span,
        "price_usd": low + 0.08 * span,
        "trade_size_ratio": 1.3,
        "change_5m": -0.5,
        "volume_strength": 0.1,
        "boll_lower": low,
    }
    row.update(extra)
    return row


def _observation_extremes() -> dict:
    return {
        "change_7d": -80.0,
        "volume_7d_avg": 1.0,
        "taker_buy_quote_volume_5m": 0.0,
        "buy_ratio_5m": 0.0,
        "buy_ratio_1h": 1.0,
        "avg_trade_usd_5m": 1_000_000.0,
        "lower_wick_ratio": 0.0,
    }


def _signal_body(signal: dict) -> dict:
    return {key: value for key, value in signal.items() if key != "ts"}


class ObservationNotAnEntryGateTests(unittest.TestCase):
    def test_strategy_sources_do_not_read_the_new_fields(self) -> None:
        for path in (_TECH_BOLL, _ENTRY_ZONE):
            text = path.read_text(encoding="utf-8")
            for name in _NEW_GATES:
                self.assertNotIn(name, text)

    def test_paper_legs_ignore_observation_values(self) -> None:
        strategy = TechBollFadeStrategy()
        self.assertEqual(strategy.min_change_5m, 2.0)
        self.assertEqual(strategy.min_volume_strength, 0.5)
        plain = TechBollFadeStrategy().on_tick({"hot_tokens": [_paper_row()]})
        observed = TechBollFadeStrategy().on_tick(
            {"hot_tokens": [_paper_row(**_observation_extremes(), trade_size_ratio=99.0)]}
        )
        self.assertEqual(len(plain), 1)
        self.assertEqual(_signal_body(plain[0]), _signal_body(observed[0]))
        for name in OBSERVATION_FIELDS:
            self.assertNotIn(name, observed[0])
        missed = TechBollFadeStrategy().on_tick(
            {
                "hot_tokens": [
                    _paper_row(change_5m=-1.0, **_observation_extremes())
                ]
            }
        )
        self.assertEqual(missed, [])

    def test_entry_zone_ignores_new_fields_and_keeps_null_ratio_fallthrough(self) -> None:
        plain = evaluate_entry_zone(_zone_row())
        observed = evaluate_entry_zone(_zone_row(**_observation_extremes()))
        self.assertTrue(plain.emit, plain.reject_reason)
        self.assertEqual(plain.emit, observed.emit)
        self.assertEqual(plain.reject_reason, observed.reject_reason)
        self.assertEqual(plain.tier, observed.tier)
        self.assertEqual(plain.entry_reason, observed.entry_reason)
        self.assertEqual(plain.order_type, observed.order_type)
        self.assertEqual(plain.position_size_multiplier, observed.position_size_multiplier)
        self.assertEqual(plain.trade_size_ratio, observed.trade_size_ratio)
        old_book = os.environ.get("BINANCE_SPOT_BOOK_USDT")
        os.environ["BINANCE_SPOT_BOOK_USDT"] = "1000"
        try:
            signals = TechBollFadeStrategy().on_tick(
                {
                    "hot_tokens": [_zone_row(**_observation_extremes())],
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                }
            )
        finally:
            if old_book is None:
                os.environ.pop("BINANCE_SPOT_BOOK_USDT", None)
            else:
                os.environ["BINANCE_SPOT_BOOK_USDT"] = old_book
        self.assertEqual(len(signals), 1)
        for name in _NEW_GATES:
            self.assertNotIn(name, signals[0])

        fallthrough = evaluate_entry_zone(
            _zone_row(
                trade_size_ratio=None,
                avg_trade_size=20.0,
                avg_trade_size_24h=10.0,
            )
        )
        self.assertTrue(fallthrough.emit, fallthrough.reject_reason)
        self.assertEqual(fallthrough.trade_size_ratio_source, "avg_trade_size")
        self.assertAlmostEqual(fallthrough.trade_size_ratio or 0.0, 2.0)

        invalid = evaluate_entry_zone(_zone_row(trade_size_ratio=-1.5))
        self.assertFalse(invalid.emit)
        self.assertEqual(invalid.reject_reason, "trade_size_ratio_invalid")


if __name__ == "__main__":
    unittest.main()
