"""Observation log, stop-prone OOS shadow, and ccxt listing guard."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine.core.ccxt_listing_guard import (
    SPREAD_BLOCK_ENABLED,
    apply_ccxt_listing_guard,
    count_contiguous_closed_hours,
    load_ccxt_guard,
)
from decision_engine.core.news_feature_observation_log import record_news_poll
from decision_engine.core.shadow_stop_prone_oos import (
    BLOCK_ENABLED,
    annotate_shadow_stop_prone,
    run_oos_report,
    state1_blocks_on_negative_change_1h,
)
from sources.news import NEWS_EVENTS_DDL, NewsStore

NOW = datetime(2026, 11, 10, 6, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
STEP = 5 * 60 * 1000


def _hours(n: int, start: int) -> list[int]:
    return [start + i * STEP for i in range(n * 12)]


class TestObservationLog(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _news(self) -> Path:
        path = self.root / "news.db"
        conn = sqlite3.connect(path)
        conn.executescript(NEWS_EVENTS_DDL)
        conn.execute(
            """
            INSERT INTO news_events (
                source, source_tier, title, url, published_at, fetched_at,
                summary, onchain_status
            ) VALUES ('coindesk', 'whitelist', 'Desk note', 'https://example.test/a',
                      '2026-11-10T05:50:00Z', '2026-11-10T05:55:00Z', '', 'pending')
            """
        )
        conn.commit()
        conn.close()
        return path

    def _deriv(self) -> Path:
        path = self.root / "deriv.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE binance_spot_microstructure (
                symbol TEXT, ts_ms INTEGER, orderbook_imbalance_20 REAL,
                taker_buy_ratio_5m REAL, taker_buy_ratio_1h REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE binance_perp_counterparty (
                symbol TEXT, ts_ms INTEGER, whale_retail_divergence REAL,
                perp_taker_buy_sell_ratio REAL
            )
            """
        )
        fresh = NOW_MS - 60_000
        future = NOW_MS + 60_000
        conn.execute(
            "INSERT INTO binance_spot_microstructure VALUES ('BTCUSDT', ?, 0.4, 0.55, 0.5)",
            (fresh,),
        )
        conn.execute(
            "INSERT INTO binance_spot_microstructure VALUES ('BTCUSDT', ?, 0.99, 0.1, 0.1)",
            (future,),
        )
        conn.execute(
            "INSERT INTO binance_perp_counterparty VALUES ('BTCUSDT', ?, 1.4, 4.2)",
            (fresh,),
        )
        conn.commit()
        conn.close()
        return path

    def test_unmapped_event_is_not_guessed_and_rerun_is_idempotent(self) -> None:
        news = self._news()
        log = self.root / "obs.db"
        with NewsStore(news) as store:
            rows = store.rows_by_url(["https://example.test/a"])
            self.assertEqual(rows[0]["onchain_status"], "pending")
        deriv = self._deriv()
        first = record_news_poll(
            rows,
            poll_id="poll-1",
            observed_at="2026-11-10T06:00:00Z",
            log_path=log,
            derivatives_db=deriv,
        )
        second = record_news_poll(
            rows,
            poll_id="poll-1",
            observed_at="2026-11-10T06:00:00Z",
            log_path=log,
            derivatives_db=deriv,
        )
        self.assertEqual(first["rows_written"], 1)
        self.assertEqual(second["rows_written"], 0)
        self.assertEqual(first["x_news_status"], "unavailable")
        self.assertFalse(first["weights_changed"])
        self.assertFalse(first["source_onchain_status_written"])
        conn = sqlite3.connect(log)
        conn.row_factory = sqlite3.Row
        obs = dict(conn.execute("SELECT * FROM feature_observations").fetchone())
        conn.close()
        self.assertEqual(obs["symbol"], "")
        self.assertEqual(obs["mapping_status"], "unmapped")
        self.assertEqual(obs["x_news_status"], "unavailable")
        self.assertIsNone(obs["obi_top5"])
        self.assertIsNone(obs["taker_buy_ratio"])
        news_conn = sqlite3.connect(news)
        status = news_conn.execute("SELECT onchain_status FROM news_events").fetchone()[0]
        news_conn.close()
        self.assertEqual(status, "pending")

    def test_mapped_symbol_keeps_depth20_and_perp_ratio_in_their_own_columns(self) -> None:
        log = self.root / "obs.db"
        record_news_poll(
            [{
                "id": 7,
                "url": "https://example.test/b",
                "source": "coindesk",
                "source_tier": "whitelist",
                "published_at": "2026-11-10T05:50:00Z",
                "fetched_at": "2026-11-10T05:55:00Z",
                "onchain_status": "pending",
            }],
            poll_id="poll-2",
            observed_at="2026-11-10T06:00:00Z",
            log_path=log,
            derivatives_db=self._deriv(),
            symbol_by_url={"https://example.test/b": {"symbol": "BTCUSDT", "venue": "binance"}},
        )
        conn = sqlite3.connect(log)
        conn.row_factory = sqlite3.Row
        obs = dict(conn.execute("SELECT * FROM feature_observations").fetchone())
        conn.close()
        self.assertIsNone(obs["obi_top5"])
        self.assertEqual(obs["obi_top5_status"], "missing_column")
        self.assertAlmostEqual(obs["orderbook_imbalance_20"], 0.4)
        self.assertEqual(obs["imbalance20_status"], "depth20_not_top5")
        self.assertAlmostEqual(obs["taker_buy_ratio"], 0.55)
        self.assertAlmostEqual(obs["perp_taker_buy_sell_ratio"], 4.2)
        self.assertEqual(obs["perp_taker_status"], "buy_sell_ratio_not_buy_share")
        self.assertAlmostEqual(obs["whale_retail_divergence"], 1.4)
        self.assertNotEqual(obs["taker_buy_ratio"], obs["perp_taker_buy_sell_ratio"])


class TestShadowOos(unittest.TestCase):
    def test_state1_negative_change_does_not_block_and_missing_sample_stays_open(self) -> None:
        self.assertFalse(BLOCK_ENABLED)
        self.assertFalse(state1_blocks_on_negative_change_1h("positive_gamma_mean_reversion", -0.02))
        stamped, report = annotate_shadow_stop_prone(
            [{
                "action": "buy",
                "market_regime": "positive_gamma_mean_reversion",
                "change_1h": -0.4,
                "binance_symbol": "SOLUSDT",
            }]
        )
        self.assertEqual(len(stamped), 1)
        self.assertFalse(stamped[0]["shadow_stop_prone_block"])
        self.assertTrue(stamped[0]["shadow_change_1h_negative_ignored"])
        self.assertFalse(report["block_enabled"])
        missing = run_oos_report(Path("/tmp/does-not-exist-shadow.csv"))
        self.assertFalse(missing["enable_block"])
        self.assertFalse(missing["dex_liquidity_transferable_to_binance"])
        self.assertIn("missing", missing["availability"])

    def test_large_sample_still_does_not_enable_a_block(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shadow.csv"
            start = datetime(2026, 10, 1, tzinfo=timezone.utc)
            lines = ["opened_at,exit_reason,volatility_24h,change_1h,liq_usd"]
            for i in range(120):
                stamp = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                vol = 4.0 if i % 2 == 0 else 0.2
                reason = "stop" if i % 2 == 0 else "tp"
                lines.append(f"{stamp},{reason},{vol},-0.01,10000")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = run_oos_report(path)
        self.assertEqual(report["availability"], "ok")
        self.assertFalse(report["enable_block"])
        self.assertFalse(report["production_attested"])
        self.assertGreater(report["split"]["test"], 0)
        self.assertIn("change_1h<0 alone", report["excluded_from_any_block"])


class TestCcxtGuard(unittest.TestCase):
    def _db(self, path: Path, *, official: int | None, opens: list[int], stale: bool = False) -> None:
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE ccxt_ohlcv (
                exchange TEXT, symbol TEXT, timeframe TEXT, ts INTEGER,
                open REAL, high REAL, low REAL, close REAL, volume REAL
            );
            CREATE TABLE ccxt_listings (
                exchange TEXT, symbol TEXT, first_seen_ts INTEGER,
                first_price REAL, note TEXT, official_listed_at_ms INTEGER
            );
            CREATE TABLE ccxt_ticks (
                exchange TEXT, symbol TEXT, ts INTEGER, last_price REAL, bid REAL, ask REAL
            );
            """
        )
        listed = official if official is not None else NOW_MS - 10 * STEP
        conn.execute(
            "INSERT INTO ccxt_listings VALUES ('binance', 'NEW/USDT', ?, 1, 'seen', ?)",
            (NOW_MS - 86400000, official),
        )
        for stamp in opens:
            conn.execute(
                "INSERT INTO ccxt_ohlcv VALUES ('binance', 'NEW/USDT', '5m', ?, 1, 1, 1, 1, 1)",
                (stamp,),
            )
        tick_ts = NOW_MS - (40 * 60 * 1000 if stale else 1000)
        conn.execute(
            "INSERT INTO ccxt_ticks VALUES ('binance', 'NEW/USDT', ?, 10, 10, 10.1)",
            (tick_ts,),
        )
        conn.execute(
            "INSERT INTO ccxt_ticks VALUES ('okx', 'NEW/USDT', ?, 12, 12, 12.2)",
            (tick_ts,),
        )
        conn.commit()
        conn.close()

    def test_gaps_and_unclosed_bars_do_not_make_an_hour(self) -> None:
        start = NOW_MS - 12 * STEP
        full = _hours(1, start)
        self.assertEqual(
            count_contiguous_closed_hours(full, now_ms=NOW_MS, listed_at_ms=start),
            (1, "contiguous"),
        )
        self.assertEqual(
            count_contiguous_closed_hours(full[:-1], now_ms=NOW_MS, listed_at_ms=start)[0],
            0,
        )
        gapped = full[:6] + [full[7] + STEP]
        self.assertEqual(
            count_contiguous_closed_hours(gapped, now_ms=NOW_MS, listed_at_ms=start)[1],
            "gapped",
        )
        unclosed = full + [NOW_MS]
        hours, status = count_contiguous_closed_hours(unclosed, now_ms=NOW_MS, listed_at_ms=start)
        self.assertEqual((hours, status), (1, "contiguous"))

    def test_unknown_listing_and_state1_do_not_block(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ccxt.db"
            start = NOW_MS - 12 * STEP
            self._db(path, official=None, opens=_hours(1, start))
            snap = load_ccxt_guard(path, symbols=["NEW/USDT"], now_ms=NOW_MS)
            buy = {
                "action": "buy",
                "ccxt_symbol": "NEW/USDT",
                "market_regime": "short_squeeze_breakout",
            }
            kept, report = apply_ccxt_listing_guard([buy], snap, now_ms=NOW_MS)
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["ccxt_listing_class"], "unknown")
            self.assertFalse(kept[0]["ccxt_chase_block"])
            self.assertFalse(SPREAD_BLOCK_ENABLED)
            self.assertFalse(kept[0]["ccxt_spread_block"])

            path2 = Path(tmp) / "ccxt2.db"
            self._db(path2, official=start, opens=_hours(1, start))
            snap2 = load_ccxt_guard(path2, symbols=["NEW/USDT"], now_ms=NOW_MS)
            state1 = {
                "action": "buy",
                "ccxt_symbol": "NEW/USDT",
                "market_regime": "positive_gamma_mean_reversion",
            }
            kept1, _report = apply_ccxt_listing_guard([state1], snap2, now_ms=NOW_MS)
            self.assertEqual(len(kept1), 1)
            self.assertTrue(kept1[0]["ccxt_state1_preserved"])
            chase = {
                "action": "buy",
                "ccxt_symbol": "NEW/USDT",
                "market_regime": "cross_asset_momentum_pullback",
            }
            kept_chase, report_chase = apply_ccxt_listing_guard([chase], snap2, now_ms=NOW_MS)
            self.assertEqual(kept_chase, [])
            self.assertEqual(report_chase["dropped_chase_buys"], 1)
            sell = {"action": "sell", "ccxt_symbol": "NEW/USDT", "market_regime": "cross_asset_momentum_pullback"}
            kept_sell, _report = apply_ccxt_listing_guard([sell], snap2, now_ms=NOW_MS)
            self.assertEqual(kept_sell[0]["action"], "sell")

    def test_stale_or_missing_db_does_not_block(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            missing = load_ccxt_guard(Path(tmp) / "absent.db", symbols=["NEW/USDT"], now_ms=NOW_MS)
            self.assertEqual(missing.availability, "missing_db")
            kept, _report = apply_ccxt_listing_guard(
                [{"action": "buy", "ccxt_symbol": "NEW/USDT", "market_regime": "short_squeeze_breakout"}],
                missing,
                now_ms=NOW_MS,
            )
            self.assertEqual(len(kept), 1)
            path = Path(tmp) / "stale.db"
            start = NOW_MS - 12 * STEP
            self._db(path, official=start, opens=_hours(1, start), stale=True)
            # Bars are fresh relative to NOW_MS; force staleness by moving the clock forward.
            snap = load_ccxt_guard(path, symbols=["NEW/USDT"], now_ms=NOW_MS + 2 * 60 * 60 * 1000)
            self.assertTrue(snap.stale)
            kept_stale, _report = apply_ccxt_listing_guard(
                [{"action": "buy", "ccxt_symbol": "NEW/USDT", "market_regime": "short_squeeze_breakout"}],
                snap,
                now_ms=NOW_MS + 2 * 60 * 60 * 1000,
            )
            self.assertEqual(len(kept_stale), 1)
            self.assertFalse(kept_stale[0]["ccxt_chase_block"])


if __name__ == "__main__":
    unittest.main()
