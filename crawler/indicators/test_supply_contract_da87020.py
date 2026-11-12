"""Current main supply contract. These tests lock what da87020 already does.

They do not change quote-versus-base orderflow, closed-bar filtering, or
funding fill-if-empty. A failure here means that contract moved.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from decision_engine.core.binance_indicators import list_binance_hot_tokens
from decision_engine.core.perp_microstructure_and_beta_hedge import (
    attach_counterparty_context,
)
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from execution.binance_ws_stream import BinanceMarketStreamCache
from indicators.binance_spot import (
    SpotKline,
    SpotSymbol,
    _INDICATOR_COLS,
    ensure_schema,
    factors_from_klines,
    parse_kline_row,
    persist_klines_and_factors,
    taker_buy_ratio_from_kline,
    upsert_indicator,
)


def _bar(
    *,
    quote: float | None,
    taker_quote: float | None,
    base: float | None,
    volume: float = 10.0,
) -> SpotKline:
    return SpotKline(
        open_time_ms=1_700_000_000_000,
        open=1.0,
        high=1.1,
        low=0.9,
        close=1.0,
        volume=volume,
        quote_volume=quote if quote is not None else 0.0,
        close_time_ms=1_700_000_299_999,
        trades=4,
        taker_buy_base_volume=base,
        taker_buy_quote_volume=taker_quote,
    )


class CurrentSupplyContractTests(unittest.TestCase):
    def test_quote_ratio_wins_when_it_is_inside_the_unit_interval(self) -> None:
        bar = _bar(quote=5_000.0, taker_quote=2_600.0, base=1.0)
        self.assertAlmostEqual(taker_buy_ratio_from_kline(bar) or 0.0, 0.52)

    def test_out_of_range_quote_ratio_uses_base_share(self) -> None:
        """Landed orderflow rule: quote share above 1 falls through to base."""
        bar = _bar(quote=100.0, taker_quote=150.0, base=4.0, volume=10.0)
        self.assertAlmostEqual(taker_buy_ratio_from_kline(bar) or 0.0, 0.4)

    def test_zero_quote_uses_base_share(self) -> None:
        bar = _bar(quote=0.0, taker_quote=5.0, base=8.0, volume=10.0)
        self.assertAlmostEqual(taker_buy_ratio_from_kline(bar) or 0.0, 0.8)

    def test_producer_does_not_write_trade_size_ratio(self) -> None:
        self.assertNotIn("trade_size_ratio", _INDICATOR_COLS)
        start = 1_700_000_000_000
        step = 300_000
        bars = []
        for index in range(25):
            open_ms = start + index * step
            raw = [
                open_ms,
                "100",
                "101",
                "99",
                "100",
                "10",
                open_ms + step - 1,
                "2000",
                10,
                "4",
                "800",
                "0",
            ]
            parsed = parse_kline_row(raw)
            assert parsed is not None
            bars.append(parsed)
        with tempfile.TemporaryDirectory() as tmp:
            conn = sqlite3.connect(Path(tmp) / "ind.db")
            stored = persist_klines_and_factors(
                conn,
                SpotSymbol("SOLUSDT", "SOL"),
                bars,
                now_ms=bars[-1].close_time_ms,
            )
            assert stored is not None
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(binance_indicators)")
            }
            self.assertNotIn("trade_size_ratio", cols)
            self.assertAlmostEqual(stored["taker_buy_ratio"], 800.0 / 2000.0)
            hot = list_binance_hot_tokens(conn, fresh_sec=0)
            self.assertEqual(len(hot), 1)
            self.assertNotIn("trade_size_ratio", hot[0])
            conn.close()

    def test_a_stored_trade_size_ratio_still_reaches_entry_and_a_negative_is_rejected(
        self,
    ) -> None:
        """The producer does not write this column. The current reader does copy it."""
        plain = {
            "binance_symbol": "SOLUSDT",
            "price_usd": 0.8,
            "boll_mid": 1.0,
            "kline_low": 0.75,
            "trade_size_ratio": 1.5,
            "change_5m": -4.0,
            "volume_strength": 0.9,
            "boll_lower": 0.9,
        }
        emitted = evaluate_entry_zone(plain)
        self.assertTrue(emitted.emit)
        self.assertEqual(emitted.trade_size_ratio, 1.5)
        self.assertEqual(emitted.entry_reason, "large_trade_market")
        invalid = evaluate_entry_zone({**plain, "trade_size_ratio": -1.0})
        self.assertFalse(invalid.emit)
        self.assertEqual(invalid.reject_reason, "trade_size_ratio_invalid")
        poisoned_taker = evaluate_entry_zone({**plain, "taker_buy_ratio": 1.5})
        self.assertEqual(poisoned_taker.reject_reason, "taker_buy_ratio_invalid")

    def test_closed_filter_is_opt_in_and_the_socket_buffer_drops_the_open_bar(self) -> None:
        closed = SpotKline(
            1_700_000_000_000, 10, 11, 9, 10, 1, 80, 1_700_000_299_999,
            trades=4, taker_buy_quote_volume=20, taker_buy_base_volume=1,
        )
        future_close = int(time.time() * 1000) + 3_600_000
        forming = SpotKline(
            closed.open_time_ms + 300_000,
            50, 51, 49, 50, 1, 9_000, future_close,
            trades=1, taker_buy_quote_volume=100, taker_buy_base_volume=1,
        )
        filtered = factors_from_klines(
            [closed, forming], now_ms=closed.close_time_ms
        )
        assert filtered is not None
        self.assertEqual(filtered["price_usd"], 10.0)
        unfiltered = factors_from_klines([closed, forming])
        assert unfiltered is not None
        self.assertEqual(unfiltered["price_usd"], 50.0)

        cache = BinanceMarketStreamCache(kline_max_bars=10)
        seen: list[list] = []
        cache.register_kline_close_callback(
            lambda _sym, _tf, bars: seen.append(list(bars))
        )

        def _msg(open_ms: int, *, closed_flag: bool, close_px: str) -> dict:
            return {
                "stream": "solusdt@kline_5m",
                "data": {
                    "e": "kline",
                    "E": open_ms + 299_999,
                    "s": "SOLUSDT",
                    "k": {
                        "t": open_ms,
                        "T": open_ms + 299_999,
                        "s": "SOLUSDT",
                        "i": "5m",
                        "o": "10",
                        "c": close_px,
                        "h": "11",
                        "l": "9",
                        "v": "1",
                        "n": 4,
                        "x": closed_flag,
                        "q": "80",
                        "V": "0.4",
                        "Q": "20",
                    },
                },
            }

        cache.ingest_message(_msg(1_700_000_000_000, closed_flag=False, close_px="40"))
        self.assertEqual(seen, [])
        cache.ingest_message(_msg(1_700_000_000_000, closed_flag=True, close_px="10"))
        cache.ingest_message(_msg(1_700_000_300_000, closed_flag=False, close_px="50"))
        self.assertEqual(len(seen), 1)
        self.assertEqual([bar.close for bar in seen[0]], [10.0])
        closed_only = cache.get_closed_klines("SOLUSDT", "5m", min_bars=1)
        assert closed_only is not None
        self.assertEqual([bar.close for bar in closed_only], [10.0])

    def test_indicator_refresh_clears_funding_z_and_the_perp_table_fills_a_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ind = sqlite3.connect(Path(tmp) / "ind.db")
            ensure_schema(ind)
            payload = {
                "symbol": "SOLUSDT",
                "base_asset": "SOL",
                "quote_asset": "USDT",
                "status": "TRADING",
                "interval": "5m",
                "price_usd": 150.0,
                "tech_updated_at": "2026-11-10T00:00:00Z",
                "updated_at": "2026-11-10T00:00:00Z",
            }
            upsert_indicator(ind, payload)
            ind.execute(
                "UPDATE binance_indicators SET funding_z = -2.85 WHERE symbol = 'SOLUSDT'"
            )
            ind.commit()
            upsert_indicator(ind, payload)
            self.assertIsNone(
                ind.execute("SELECT funding_z FROM binance_indicators").fetchone()[0]
            )
            hot = list_binance_hot_tokens(ind, fresh_sec=0)
            self.assertIsNone(hot[0].get("funding_z"))
            ind.close()

            deriv_path = Path(tmp) / "deriv.db"
            deriv = sqlite3.connect(deriv_path)
            deriv.execute(
                """
                CREATE TABLE binance_perp_counterparty (
                    symbol TEXT, ts_ms INTEGER, funding_rate REAL, funding_z REAL,
                    mark_price REAL, index_price REAL
                )
                """
            )
            deriv.execute(
                "INSERT INTO binance_perp_counterparty VALUES ('SOLUSDT', 1, -0.001, -2.1, 100, 100)"
            )
            deriv.commit()
            deriv.close()
            attach_counterparty_context(hot, db_path=deriv_path)
            self.assertAlmostEqual(hot[0]["funding_z"], -2.1)


if __name__ == "__main__":
    unittest.main()
