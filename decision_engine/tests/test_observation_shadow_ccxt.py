"""Observation log, stop-prone OOS shadow, and ccxt listing guard."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

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
from decision_engine.core.regime_strategy_router import apply_five_state_regime_router
from sources.news import NEWS_EVENTS_DDL, NewsItem, NewsStore, run_once

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


class TestObservationDoesNotBlockPoll(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _item(self) -> NewsItem:
        return NewsItem(
            source="coindesk",
            source_tier="whitelist",
            title="Desk note",
            url="https://example.test/old-copy",
            published_at="2026-01-16T00:49:34Z",
            summary="",
            raw={"title": "Desk note"},
        )

    def _pull(self, *, poll_id: str, log: Path) -> dict:
        def _fetch(_client, _src):
            return [self._item()], None

        with patch("sources.news.fetch_feed", _fetch):
            return run_once(
                db_path=str(self.root / "news.db"),
                source_ids=["coindesk"],
                poll_id=poll_id,
                observation_log=str(log),
                timeout_sec=1.0,
            )

    def test_old_copy_dedup_still_logs_each_new_poll(self) -> None:
        log = self.root / "obs.db"
        first = self._pull(poll_id="round-a", log=log)
        second = self._pull(poll_id="round-b", log=log)
        third = self._pull(poll_id="round-b", log=log)
        self.assertEqual(first["items_written"], 1)
        self.assertEqual(second["items_dup"], 1)
        self.assertEqual(second["observation_log"]["rows_written"], 1)
        self.assertEqual(third["observation_log"]["rows_written"], 0)
        self.assertEqual(second["observation_log"]["x_news_status"], "unavailable")
        conn = sqlite3.connect(log)
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "SELECT poll_id, x_news_status, published_at FROM feature_observations ORDER BY poll_id"
        )]
        news = sqlite3.connect(self.root / "news.db")
        status = news.execute("SELECT onchain_status, fetched_at FROM news_events").fetchone()
        news.close()
        conn.close()
        self.assertEqual([row["poll_id"] for row in rows], ["round-a", "round-b"])
        self.assertTrue(all(row["x_news_status"] == "unavailable" for row in rows))
        self.assertEqual(status[0], "pending")
        self.assertEqual(status[1], first["fetched_at"])

    def test_disk_full_and_locked_log_do_not_fail_the_rss_pull(self) -> None:
        def _fetch(_client, _src):
            return [self._item()], None

        with patch("sources.news.fetch_feed", _fetch), patch(
            "sources.news.record_news_poll",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ):
            stats = run_once(
                db_path=str(self.root / "news.db"),
                source_ids=["coindesk"],
                poll_id="disk-full",
                observation_log=str(self.root / "obs.db"),
                timeout_sec=1.0,
            )
        self.assertEqual(stats["items_written"], 1)
        self.assertEqual(stats["observation_log"]["status"], "error")
        self.assertIn("disk is full", stats["observation_log"]["detail"])
        self.assertEqual(stats["observation_log"]["x_news_status"], "unavailable")

        locked = self.root / "locked.db"
        holder = sqlite3.connect(locked)
        holder.isolation_level = None
        holder.execute("BEGIN EXCLUSIVE")
        try:
            with patch("sources.news.fetch_feed", _fetch):
                again = run_once(
                    db_path=str(self.root / "news.db"),
                    source_ids=["coindesk"],
                    poll_id="locked-log",
                    observation_log=str(locked),
                    timeout_sec=1.0,
                )
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(again["items_dup"], 1)
        self.assertEqual(again["observation_log"]["status"], "error")
        self.assertIn("locked", again["observation_log"]["detail"].lower())
        self.assertEqual(again["observation_log"]["x_news_status"], "unavailable")

    def test_router_does_not_call_the_observation_writer(self) -> None:
        router = Path("decision_engine/core/regime_strategy_router.py").read_text(encoding="utf-8")
        engine = Path("decision_engine/core/engine.py").read_text(encoding="utf-8")
        self.assertNotIn("record_news_poll", router)
        self.assertNotIn("record_news_poll", engine)
        with patch(
            "sources.news.record_news_poll",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            out, _counts = apply_five_state_regime_router(
                [{
                    "action": "buy",
                    "binance_symbol": "SOLUSDT",
                    "size_pct": 0.1,
                    "funding_z": 0.1,
                    "xs_breadth_1h": 0.4,
                }],
                [],
            )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["action"], "buy")


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
            lines = [
                "opened_at,exit_reason,volatility_24h,change_1h,liq_usd,pnl_usd,features_usable_at_entry"
            ]
            for i in range(120):
                stamp = (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
                vol = 4.0 if i % 2 == 0 else 0.2
                reason = "stop_loss" if i % 2 == 0 else "take_profit"
                lines.append(f"{stamp},{reason},{vol},-0.01,10000,-1,1")
            lines.append(
                f"{(start + timedelta(hours=121)).strftime('%Y-%m-%dT%H:%M:%SZ')},max_hold,3,-0.2,10000,-50,1"
            )
            lines.append(
                f"{(start + timedelta(hours=122)).strftime('%Y-%m-%dT%H:%M:%SZ')},stop_loss,4,-0.2,10000,-1,0"
            )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            report = run_oos_report(path)
        self.assertEqual(report["availability"], "ok")
        self.assertFalse(report["enable_block"])
        self.assertFalse(report["production_attested"])
        self.assertFalse(report["local_audit_2026_11_10"]["recomputed_here"])
        self.assertEqual(report["local_audit_2026_11_10"]["same_book_temporal_oos"]["stop_loss"], 0)
        self.assertEqual(report["local_audit_2026_11_10"]["multibook_oos_ex_discovery"]["n"], 656)
        self.assertEqual(report["local_audit_2026_11_10"]["r3_on_multibook_oos"]["take_profit_skipped"], 28)
        self.assertEqual(report["local_audit_2026_11_10"]["binance_max_closed_1h"], 7)
        self.assertEqual(report["local_audit_2026_11_10"]["pairs_with_at_least_20_matches_within_5s"], 0)
        self.assertEqual(report["rows_excluded_not_knowable_at_entry"], 1)
        self.assertEqual(report["label"], "stop_loss")
        self.assertFalse(report["negative_pnl_counted_as_stop"])
        self.assertTrue(report["stored_skip_equals_r3_not_r3_or_liq"])
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
            self.assertEqual(len(kept_chase), 1)
            self.assertFalse(kept_chase[0]["ccxt_chase_block"])
            self.assertEqual(kept_chase[0]["ccxt_listing_class"], "unknown")
            self.assertEqual(kept_chase[0]["ccxt_listing_reason"], "coverage_insufficient_not_a_new_listing")
            self.assertEqual(report_chase["dropped_chase_buys"], 0)
            self.assertFalse(report_chase["chase_block_enabled"])
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
            self.assertNotEqual(kept_stale[0]["ccxt_listing_class"], "new")


if __name__ == "__main__":
    unittest.main()
