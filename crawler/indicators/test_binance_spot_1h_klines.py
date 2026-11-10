"""Hourly kline collector must not touch 5m rows or indicator factors."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from indicators.binance_spot import SpotSymbol, ensure_schema, parse_kline_row, upsert_indicator, upsert_klines
from indicators.binance_spot_1h_klines import (
    FETCH_LIMIT,
    KlineLockBusy,
    acquire_hourly_lock,
    contiguous_closed_1h,
    ingest_closed_1h_klines,
    kline_url,
    run_hourly_collector,
)


def _bar(open_ms: int, quote: float, *, base_volume: float = 1.0) -> list:
    close_ms = open_ms + 3_599_999
    return [
        open_ms,
        "10",
        "11",
        "9",
        "10.5",
        str(base_volume),
        close_ms,
        str(quote),
        3,
        "0.4",
        "4",
        "0",
    ]


def mod_file() -> Path:
    import indicators.binance_spot_1h_klines as hourly

    return Path(hourly.__file__)


def _window(start_open: int, count: int, quote: float = 10.0) -> list[list]:
    return [_bar(start_open + i * 3_600_000, quote) for i in range(count)]


class HourlyKlineCollectorTest(unittest.TestCase):
    def _seed(self, conn: sqlite3.Connection) -> None:
        ensure_schema(conn)
        upsert_klines(
            conn,
            "BTCUSDT",
            "5m",
            [parse_kline_row(_bar(1_000, 7.0))],
        )
        upsert_indicator(
            conn,
            {
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "status": "TRADING",
                "interval": "5m",
                "price_usd": 10.5,
                "change_5m": -0.2,
                "updated_at": "2026-11-10T05:00:00Z",
                "tech_updated_at": "KEEP",
            },
        )
        conn.commit()

    def test_quote_volume_is_kline_index_7_and_unclosed_hour_is_dropped(self) -> None:
        raw = _bar(0, 1234.5, base_volume=9.0)
        parsed = parse_kline_row(raw)
        assert parsed is not None
        self.assertEqual(parsed.quote_volume, 1234.5)
        self.assertEqual(parsed.volume, 9.0)
        self.assertEqual(parsed.close_time_ms - parsed.open_time_ms, 3_599_999)
        closed = _window(0, 21, 4.0)
        forming = _bar(21 * 3_600_000, 99.0)
        now = forming[0] + 60_000
        accepted, reason = contiguous_closed_1h(
            [parse_kline_row(row) for row in closed + [forming]],
            now_ms=now,
        )
        self.assertIsNone(reason)
        self.assertEqual(len(accepted), 21)
        self.assertNotIn(forming[0], [bar.open_time_ms for bar in accepted])
        self.assertTrue(all(bar.close_time_ms <= now for bar in accepted))
        self.assertEqual(accepted[0].open_time_ms, 0)
        self.assertEqual(accepted[0].close_time_ms, 3_599_999)

    def test_5m_rows_and_indicator_factors_stay_put(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            self._seed(conn)
            now = 21 * 3_600_000
            payload = _window(0, 21, 8.0) + [_bar(21 * 3_600_000, 1.0)]

            def fetcher(url: str):
                self.assertIn("interval=1h", url)
                self.assertIn(f"limit={FETCH_LIMIT}", url)
                self.assertNotIn("interval=5m", url)
                return payload

            stats = ingest_closed_1h_klines(
                conn,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=fetcher,
                now_ms=now,
            )
            self.assertEqual(stats["indicators"], 0)
            self.assertEqual(stats["failed"], 0)
            five = conn.execute(
                "SELECT quote_volume, open_time_ms FROM binance_klines WHERE symbol='BTCUSDT' AND interval='5m'"
            ).fetchone()
            factor = conn.execute(
                "SELECT interval, tech_updated_at, change_5m FROM binance_indicators WHERE symbol='BTCUSDT'"
            ).fetchone()
            hour = conn.execute(
                "SELECT open_time_ms, close_time_ms, quote_volume FROM binance_klines WHERE interval='1h' ORDER BY open_time_ms"
            ).fetchall()
            conn.close()
        self.assertEqual(five, (7.0, 1_000))
        self.assertEqual(factor, ("5m", "KEEP", -0.2))
        self.assertEqual(len(hour), 21)
        self.assertEqual(hour[0][0], 0)
        self.assertEqual(hour[0][1], 3_599_999)
        self.assertEqual(hour[0][2], 8.0)

    def test_hourly_refresh_keeps_source_time_and_older_bars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            ensure_schema(conn)
            first_now = 21 * 3_600_000
            calls = {"n": 0}

            def fetcher(url: str):
                del url
                calls["n"] += 1
                if calls["n"] == 1:
                    return _window(0, 21, 10.0)
                return _window(3_600_000, 21, 11.0)

            ingest_closed_1h_klines(
                conn,
                [SpotSymbol("ETHUSDT", "ETH")],
                fetcher=fetcher,
                now_ms=first_now,
            )
            ingest_closed_1h_klines(
                conn,
                [SpotSymbol("ETHUSDT", "ETH")],
                fetcher=fetcher,
                now_ms=first_now + 3_600_000,
            )
            rows = conn.execute(
                """
                SELECT open_time_ms, close_time_ms, quote_volume
                FROM binance_klines WHERE symbol='ETHUSDT' AND interval='1h'
                ORDER BY open_time_ms
                """
            ).fetchall()
            conn.close()
        self.assertEqual(rows[0], (0, 3_599_999, 10.0))
        self.assertEqual(rows[-1][0], 21 * 3_600_000)
        self.assertEqual(rows[-1][1], 21 * 3_600_000 + 3_599_999)
        refreshed = [row for row in rows if row[0] == 3_600_000][0]
        self.assertEqual(refreshed[2], 11.0)
        self.assertEqual(refreshed[1], 3_600_000 + 3_599_999)

    def test_fetch_failure_and_bad_bars_do_not_replace_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            ensure_schema(conn)
            good = parse_kline_row(_bar(0, 5.0))
            assert good is not None
            upsert_klines(conn, "BTCUSDT", "1h", [good])
            conn.commit()
            sleeps: list[float] = []

            def boom(url: str):
                del url
                raise RuntimeError("timeout")

            failed = ingest_closed_1h_klines(
                conn,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=boom,
                now_ms=21 * 3_600_000,
                sleeper=sleeps.append,
            )
            self.assertEqual(failed["failed"], 1)
            self.assertEqual(sleeps, [0.4, 0.8])
            kept = conn.execute(
                "SELECT quote_volume FROM binance_klines WHERE symbol='BTCUSDT' AND interval='1h'"
            ).fetchone()
            self.assertEqual(kept[0], 5.0)

            def bad_duration(url: str):
                del url
                rows = _window(0, 21, 3.0)
                rows[0][6] = rows[0][0] + 3_600_000
                return rows

            rejected = ingest_closed_1h_klines(
                conn,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=bad_duration,
                now_ms=21 * 3_600_000,
            )
            self.assertEqual(rejected["errors"][0]["error"], "need_21_contiguous_closed_1h")
            still = conn.execute(
                "SELECT quote_volume, open_time_ms FROM binance_klines WHERE interval='1h'"
            ).fetchall()
            conn.close()
        self.assertEqual(still, [(5.0, 0)])

    def test_negative_quote_is_rejected(self) -> None:
        rows = _window(0, 21, 1.0)
        rows[-1][7] = "-1"
        parsed = [parse_kline_row(row) for row in rows]
        accepted, reason = contiguous_closed_1h(parsed, now_ms=21 * 3_600_000)
        self.assertEqual(accepted, [])
        self.assertEqual(reason, "invalid_latest_closed_hour:quote")

    def test_loop_reads_the_clock_again_after_sleep(self) -> None:
        import indicators.binance_spot_1h_klines as mod

        state = {"ms": 1_800_000_000_000}
        new_open = 1_800_000_000_000

        def fake_time() -> float:
            return state["ms"] / 1000.0

        def fake_sleep(seconds: float) -> None:
            self.assertGreaterEqual(seconds, 300)
            state["ms"] += 3_600_000

        def fetcher(url: str):
            del url
            now = int(mod.time.time() * 1000)
            expected = (now - 3_599_999) // 3_600_000 * 3_600_000
            start = expected - 20 * 3_600_000
            return _window(start, 21, 6.0)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            sqlite3.connect(db).close()
            original = (mod.time.time, mod.time.sleep, mod.load_primary_universe, mod.fetch_json)
            mod.time.time = fake_time
            mod.time.sleep = fake_sleep
            mod.fetch_json = fetcher
            mod.load_primary_universe = lambda paths=None: (
                [SpotSymbol("BTCUSDT", "BTC")],
                Path("universe"),
            )
            try:
                code = mod.main(
                    ["--db", str(db), "--loop", "--sleep-sec", "300", "--max-rounds", "2"]
                )
            finally:
                mod.time.time, mod.time.sleep, mod.load_primary_universe, mod.fetch_json = original
            conn = sqlite3.connect(db)
            row = conn.execute(
                """
                SELECT close_time_ms, quote_volume FROM binance_klines
                WHERE symbol='BTCUSDT' AND interval='1h' AND open_time_ms=?
                """,
                (new_open,),
            ).fetchone()
            conn.close()
        self.assertEqual(code, 0)
        self.assertEqual(row, (new_open + 3_599_999, 6.0))

    def test_loop_tracks_universe_additions_and_removals(self) -> None:
        import indicators.binance_spot_1h_klines as mod

        now = 30 * 3_600_000
        expected = (now - 3_599_999) // 3_600_000 * 3_600_000
        calls = {"n": 0}
        fetched: list[str] = []

        def load(paths=None):
            del paths
            calls["n"] += 1
            if calls["n"] == 1:
                return (
                    [SpotSymbol("BTCUSDT", "BTC"), SpotSymbol("ETHUSDT", "ETH")],
                    Path("universe-round-1"),
                )
            return (
                [
                    SpotSymbol("BTCUSDT", "BTC"),
                    SpotSymbol("PYTHUSDT", "PYTH"),
                    SpotSymbol("ACTUSDT", "ACT"),
                ],
                Path("universe-round-2"),
            )

        def fetcher(url: str):
            fetched.append(url)
            start = expected - 20 * 3_600_000
            return _window(start, 21, 8.0)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            sqlite3.connect(db).close()
            original = (mod.load_primary_universe, mod.fetch_json, mod.time.sleep)
            mod.load_primary_universe = load
            mod.fetch_json = fetcher
            mod.time.sleep = lambda _seconds: None
            try:
                code = mod.main(
                    [
                        "--db",
                        str(db),
                        "--loop",
                        "--sleep-sec",
                        "300",
                        "--max-rounds",
                        "2",
                        "--now-ms",
                        str(now),
                    ]
                )
            finally:
                mod.load_primary_universe, mod.fetch_json, mod.time.sleep = original
            conn = sqlite3.connect(db)
            counts = dict(
                conn.execute(
                    "SELECT symbol, COUNT(*) FROM binance_klines WHERE interval='1h' GROUP BY symbol"
                ).fetchall()
            )
            pyth_tip = conn.execute(
                "SELECT MAX(open_time_ms), MAX(close_time_ms) FROM binance_klines WHERE symbol='PYTHUSDT'"
            ).fetchone()
            act_n = conn.execute(
                "SELECT COUNT(*) FROM binance_klines WHERE symbol='ACTUSDT' AND interval='1h'"
            ).fetchone()[0]
            eth_n = conn.execute(
                "SELECT COUNT(*) FROM binance_klines WHERE symbol='ETHUSDT' AND interval='1h'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(code, 0)
        self.assertEqual(counts["PYTHUSDT"], 21)
        self.assertEqual(act_n, 21)
        self.assertEqual(eth_n, 21)
        self.assertEqual(pyth_tip, (expected, expected + 3_599_999))
        round2 = fetched[2:]
        self.assertTrue(any("symbol=PYTHUSDT" in url for url in round2))
        self.assertTrue(any("symbol=ACTUSDT" in url for url in round2))
        self.assertFalse(any("symbol=ETHUSDT" in url for url in round2))

    def test_universe_read_error_does_not_reuse_the_previous_symbols(self) -> None:
        import indicators.binance_spot_1h_klines as mod

        now = 30 * 3_600_000
        calls = {"n": 0}
        fetched: list[str] = []

        def load(paths=None):
            del paths
            calls["n"] += 1
            if calls["n"] == 1:
                return [SpotSymbol("BTCUSDT", "BTC")], Path("universe-ok")
            raise OSError("universe file replaced")

        def fetcher(url: str):
            fetched.append(url)
            expected = (now - 3_599_999) // 3_600_000 * 3_600_000
            return _window(expected - 20 * 3_600_000, 21, 4.0)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            sqlite3.connect(db).close()
            original = (mod.load_primary_universe, mod.fetch_json, mod.time.sleep)
            mod.load_primary_universe = load
            mod.fetch_json = fetcher
            mod.time.sleep = lambda _seconds: None
            try:
                code = mod.main(
                    [
                        "--db",
                        str(db),
                        "--loop",
                        "--sleep-sec",
                        "300",
                        "--max-rounds",
                        "2",
                        "--now-ms",
                        str(now),
                    ]
                )
            finally:
                mod.load_primary_universe, mod.fetch_json, mod.time.sleep = original
            conn = sqlite3.connect(db)
            btc = conn.execute(
                "SELECT COUNT(*) FROM binance_klines WHERE symbol='BTCUSDT'"
            ).fetchone()[0]
            conn.close()
        self.assertEqual(code, 2)
        self.assertEqual(len(fetched), 1)
        self.assertIn("symbol=BTCUSDT", fetched[0])
        self.assertEqual(btc, 21)

    def test_explicit_symbol_allowlist_waits_for_the_live_universe(self) -> None:
        import indicators.binance_spot_1h_klines as mod

        now = 30 * 3_600_000
        calls = {"n": 0}
        fetched: list[str] = []

        def load(paths=None):
            del paths
            calls["n"] += 1
            if calls["n"] == 1:
                return [SpotSymbol("BTCUSDT", "BTC")], Path("universe-round-1")
            return (
                [SpotSymbol("BTCUSDT", "BTC"), SpotSymbol("PYTHUSDT", "PYTH")],
                Path("universe-round-2"),
            )

        def fetcher(url: str):
            fetched.append(url)
            expected = (now - 3_599_999) // 3_600_000 * 3_600_000
            return _window(expected - 20 * 3_600_000, 21, 2.0)

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            sqlite3.connect(db).close()
            original = (mod.load_primary_universe, mod.fetch_json, mod.time.sleep)
            mod.load_primary_universe = load
            mod.fetch_json = fetcher
            mod.time.sleep = lambda _seconds: None
            try:
                code = mod.main(
                    [
                        "--db",
                        str(db),
                        "--loop",
                        "--sleep-sec",
                        "300",
                        "--max-rounds",
                        "2",
                        "--now-ms",
                        str(now),
                        "--symbol",
                        "PYTHUSDT",
                    ]
                )
            finally:
                mod.load_primary_universe, mod.fetch_json, mod.time.sleep = original
        self.assertEqual(code, 0)
        self.assertEqual(len(fetched), 1)
        self.assertIn("symbol=PYTHUSDT", fetched[0])
        self.assertNotIn("symbol=BTCUSDT", fetched[0])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            ensure_schema(conn)
            now = 30 * 3_600_000
            latest_open = 29 * 3_600_000
            upsert_klines(conn, "BTCUSDT", "1h", [parse_kline_row(_bar(latest_open, 42.0))])
            conn.commit()
            rows = _window(0, 30, 10.0)
            rows[-1][7] = "-5"
            stats = ingest_closed_1h_klines(
                conn,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=lambda url: rows,
                now_ms=now,
            )
            code = run_hourly_collector(
                db,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=lambda url: rows,
                now_ms=now,
            )
            stored = conn.execute(
                """
                SELECT open_time_ms, quote_volume FROM binance_klines
                WHERE symbol='BTCUSDT' AND interval='1h' ORDER BY open_time_ms
                """
            ).fetchall()
            conn.close()
        self.assertEqual(stats["symbols"], 0)
        self.assertEqual(stats["klines"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["errors"][0]["error"], "invalid_latest_closed_hour:quote")
        self.assertEqual(code, 2)
        self.assertEqual(stored, [(latest_open, 42.0)])

    def test_stale_21_bar_window_is_not_a_fresh_hour(self) -> None:
        now = 30 * 3_600_000
        expected = 29 * 3_600_000
        stale_end = expected - 5 * 3_600_000
        rows = _window(stale_end - 20 * 3_600_000, 21, 3.0)
        accepted, reason = contiguous_closed_1h(
            [parse_kline_row(row) for row in rows],
            now_ms=now,
        )
        self.assertEqual(accepted, [])
        self.assertEqual(reason, "missing_latest_closed_hour")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            ensure_schema(conn)
            stats = ingest_closed_1h_klines(
                conn,
                [SpotSymbol("BTCUSDT", "BTC")],
                fetcher=lambda url: rows,
                now_ms=now,
            )
            count = conn.execute("SELECT COUNT(*) FROM binance_klines").fetchone()[0]
            conn.close()
        self.assertEqual(stats["symbols"], 0)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(count, 0)

    def test_flattened_deploy_root_finds_exports_universe(self) -> None:
        from indicators.binance_spot_1h_klines import hourly_universe_paths, load_hourly_universe

        saved_env = os.environ.pop("BINANCE_SPOT_UNIVERSE_DB", None)
        saved_json = os.environ.pop("BINANCE_SPOT_UNIVERSE_JSON", None)
        flat = Path("/workspace/dexscreener-crawler/indicators/binance_spot_1h_klines.py")
        paths = hourly_universe_paths(flat)
        self.assertEqual(
            paths[0],
            flat.resolve().parents[1] / "exports" / "binance_spot_universe.db",
        )
        try:
            self._flat_universe_file()
        finally:
            if saved_env is not None:
                os.environ["BINANCE_SPOT_UNIVERSE_DB"] = saved_env
            if saved_json is not None:
                os.environ["BINANCE_SPOT_UNIVERSE_JSON"] = saved_json

    def _flat_universe_file(self) -> None:
        from indicators.binance_spot_1h_klines import load_hourly_universe

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "dexscreener-crawler"
            (root / "exports").mkdir(parents=True)
            (root / "indicators").mkdir()
            db = root / "exports" / "binance_spot_universe.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE symbols (symbol TEXT, base_asset TEXT, quote_asset TEXT, status TEXT)"
            )
            conn.execute("INSERT INTO symbols VALUES ('BTCUSDT', 'BTC', 'USDT', 'TRADING')")
            conn.commit()
            conn.close()
            module = root / "indicators" / "binance_spot_1h_klines.py"
            rows, source = load_hourly_universe(module)
        self.assertEqual(source, db)
        self.assertEqual([item.symbol for item in rows], ["BTCUSDT"])

    def test_second_collector_does_not_take_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            held = acquire_hourly_lock(db)
            try:
                with self.assertRaises(KlineLockBusy):
                    acquire_hourly_lock(db)
                code = run_hourly_collector(
                    db,
                    [SpotSymbol("BTCUSDT", "BTC")],
                    fetcher=lambda url: [],
                    now_ms=1,
                    loop=False,
                )
                self.assertEqual(code, 3)
            finally:
                held.close()

    def test_universe_loader_is_the_5m_source_and_skips_stablecoin_bases(self) -> None:
        from indicators.binance_spot import canonical_spot_usdt, symbols_from_sqlite

        self.assertIsNone(canonical_spot_usdt("USDCUSDT", "USDC", "USDT"))
        self.assertIsNone(canonical_spot_usdt("FDUSDUSDT"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binance_spot_universe.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE symbols (symbol TEXT, base_asset TEXT, quote_asset TEXT, status TEXT)"
            )
            conn.executemany(
                "INSERT INTO symbols VALUES (?, ?, 'USDT', 'TRADING')",
                [("BTCUSDT", "BTC"), ("USDCUSDT", "USDC"), ("ETHUSDT", "ETH")],
            )
            conn.commit()
            conn.close()
            names = [item.symbol for item in symbols_from_sqlite(path)]
        self.assertEqual(names, ["BTCUSDT", "ETHUSDT"])
        url = kline_url("BTCUSDT", interval="1h", limit=FETCH_LIMIT)
        self.assertIn("/api/v3/klines?", url)
        self.assertIn("interval=1h", url)
        text = Path(mod_file()).read_text(encoding="utf-8")
        imported = text.split("from indicators.binance_spot import", 1)[1].split(")", 1)[0]
        self.assertNotIn("upsert_indicator", imported)
        self.assertNotIn("persist_klines_and_factors", imported)
        self.assertNotIn("decision_volume_z", text)

    def test_sqlite_write_error_rolls_back_the_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dex.db"
            conn = sqlite3.connect(db)
            ensure_schema(conn)
            upsert_klines(conn, "BTCUSDT", "1h", [parse_kline_row(_bar(0, 4.0))])
            conn.commit()
            real = upsert_klines

            def explode(conn, symbol, interval, klines):
                del conn, symbol, interval, klines
                raise sqlite3.OperationalError("disk full")

            import indicators.binance_spot_1h_klines as mod

            mod.upsert_klines = explode
            try:
                stats = mod.ingest_closed_1h_klines(
                    conn,
                    [SpotSymbol("BTCUSDT", "BTC")],
                    fetcher=lambda url: _window(0, 21, 9.0),
                    now_ms=21 * 3_600_000,
                )
            finally:
                mod.upsert_klines = real
            row = conn.execute(
                "SELECT quote_volume FROM binance_klines WHERE interval='1h'"
            ).fetchone()
            conn.close()
        self.assertEqual(stats["failed"], 1)
        self.assertIn("disk full", stats["errors"][0]["error"])
        self.assertEqual(row[0], 4.0)


if __name__ == "__main__":
    unittest.main()
