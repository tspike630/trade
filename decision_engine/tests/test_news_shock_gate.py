"""News sidechannel, on-chain verifier, historical audit, and read-only router gate."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import decision_engine  # noqa: F401  (puts crawler/ on sys.path before sources)
from decision_engine.core.news_shock_audit import (
    asof_numeric,
    audit_news_shock_history,
    heat_delta,
    kline_forward_return,
    main as audit_main,
)
from decision_engine.core.news_shock_gate import (
    CALIBRATION_STATUS,
    REGIME_BULL,
    REGIME_CRISIS,
    REGIME_DEFENSIVE,
    NEWS_EVENTS_COLUMNS,
    OnchainCalibration,
    OnchainJudgment,
    NewsReadSnapshot,
    NewsRouteEffect,
    NewsShockStatus,
    XWatchPoint,
    classify_news_row,
    evaluate_news_overlay,
    judge_onchain,
    load_news_snapshot,
)
from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)
from sources.news import (
    NEWS_EVENTS_DDL,
    SOURCE_CATALOG,
    TIER_RUMOR,
    TIER_WHITELIST,
    default_db_path,
    parse_rss_xml,
    repo_root,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
AS_OF = "2026-06-01T12:00:00Z"


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes(delta: float) -> str:
    return _iso(NOW + timedelta(minutes=delta))


def _snap(
    shocks: tuple[NewsShockStatus, ...] = (),
    *,
    availability: str = "ok",
    detail: str | None = None,
    context: int = 1,
    x_points: tuple[XWatchPoint, ...] = (),
) -> NewsReadSnapshot:
    return NewsReadSnapshot(
        mode="ro",
        availability=availability,  # type: ignore[arg-type]
        detail=detail,
        db_path=None,
        as_of=AS_OF,
        window_min=90,
        shocks=shocks,
        context_count=context,
        scanned=1,
        suppressed=0,
        x_watch_availability="not_configured",
        x_watch_detail=None,
        x_points=x_points,
        feature_availability="not_requested",
        feature_detail=None,
        obi_points=(),
        taker_points=(),
        whale_points=(),
    )


def _shock(severity: str, keyword: str, source: str, age: float = 12.0) -> NewsShockStatus:
    return NewsShockStatus(severity, keyword, keyword, source, age)  # type: ignore[arg-type]


def _row(source: str, tier: str, title: str, published: str, summary: str = "") -> dict[str, str]:
    return {
        "source": source,
        "source_tier": tier,
        "title": title,
        "summary": summary,
        "published_at": published,
        "fetched_at": published,
        "url": "https://example.test/" + title,
    }


def _state1(symbol: str = "SOLUSDT") -> dict[str, object]:
    return {
        "binance_symbol": symbol,
        "symbol": symbol,
        "funding_z": 0.1,
        "xs_breadth_1h": 0.40,
        "gamma_regime": "positive_gamma_pinned",
    }


def _bull(symbol: str = "SOLUSDT", **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "binance_symbol": symbol,
        "symbol": symbol,
        "nvda_1h_ret_pct": 1.60,
        "xs_breadth_1h": 0.75,
        "xs_rank_pct_1h": 0.88,
        "funding_z": 0.20,
    }
    row.update(extra)
    return row


def _calibration() -> OnchainCalibration:
    return OnchainCalibration(
        min_heat_delta=5,
        min_obi_aligned=0.20,
        min_taker_aligned=0.55,
        min_whale_aligned=1.20,
        max_obi_divergent=-0.05,
        max_taker_divergent=0.60,
        max_whale_divergent=0.70,
        max_feature_age_min=90,
    )


def _heat(**extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "mention_count_prev": 10,
        "mention_count_last": 30,
        "last_searched_at": AS_OF,
    }
    base.update(extra)
    return base


def _init_news(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(NEWS_EVENTS_DDL)
    conn.commit()
    conn.close()


def _insert_news(
    path: Path,
    *,
    source: str,
    tier: str,
    title: str,
    url: str,
    published_at: str,
    summary: str = "",
    onchain_status: str = "pending",
    fetched_at: str | None = None,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        INSERT INTO news_events (
            source, source_tier, title, url, published_at, fetched_at,
            summary, onchain_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source, tier, title, url, published_at, fetched_at or _iso(NOW), summary, onchain_status),
    )
    conn.commit()
    conn.close()


class TestFedSources(unittest.TestCase):
    def test_fed_sources_are_enabled_whitelist_and_both_copies_agree(self) -> None:
        by_id = {item["id"]: item for item in SOURCE_CATALOG}
        self.assertEqual(
            by_id["fed_monetary"]["url"],
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
        )
        self.assertEqual(
            by_id["fed_speeches"]["url"],
            "https://www.federalreserve.gov/feeds/speeches.xml",
        )
        self.assertTrue(by_id["fed_monetary"]["enabled"])
        self.assertTrue(by_id["fed_speeches"]["enabled"])
        self.assertEqual(by_id["fed_monetary"]["tier"], TIER_WHITELIST)
        self.assertEqual(by_id["fed_speeches"]["tier"], TIER_WHITELIST)
        self.assertEqual(by_id["newsbtc"]["tier"], TIER_RUMOR)
        root = repo_root()
        for rel in ("sources/news.py", "crawler/sources/news.py"):
            text = (root / rel).read_text(encoding="utf-8")
            self.assertIn("fed_monetary", text)
            self.assertIn("press_monetary.xml", text)
            self.assertIn("fed_speeches", text)
            self.assertIn("speeches.xml", text)

    def test_default_db_path_is_repo_root_and_env_override(self) -> None:
        self.assertEqual(Path(default_db_path()), repo_root() / "news_events.db")
        previous = os.environ.get("NEWS_EVENTS_DB_PATH")
        os.environ["NEWS_EVENTS_DB_PATH"] = "/tmp/custom-news-events.db"
        try:
            self.assertEqual(default_db_path(), "/tmp/custom-news-events.db")
        finally:
            if previous is None:
                os.environ.pop("NEWS_EVENTS_DB_PATH", None)
            else:
                os.environ["NEWS_EVENTS_DB_PATH"] = previous

    def test_fed_rss_items_are_parsed_without_network(self) -> None:
        payload = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Federal Reserve issues FOMC statement</title>
            <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260601a.htm</link>
            <description>The Committee expects higher for longer.</description>
            <pubDate>Mon, 01 Jun 2026 12:00:00 GMT</pubDate>
          </item>
        </channel></rss>"""
        monetary = parse_rss_xml(payload, source_id="fed_monetary", tier=TIER_WHITELIST)
        speeches = parse_rss_xml(payload, source_id="fed_speeches", tier=TIER_WHITELIST)
        self.assertEqual(monetary[0].source, "fed_monetary")
        self.assertEqual(speeches[0].source, "fed_speeches")
        self.assertIn("higher for longer", monetary[0].summary or "")
        self.assertTrue(monetary[0].published_at)


class TestLexicon(unittest.TestCase):
    def test_crisis_phrases_and_negation_quotes_future_and_stale(self) -> None:
        crisis = classify_news_row(
            _row("coindesk", "whitelist", "Exchange hacked overnight", _minutes(-10)),
            now=NOW,
        )
        self.assertEqual(crisis.severity, "crisis")
        self.assertEqual(crisis.matched_keyword, "exchange hacked")

        emergency = classify_news_row(
            _row("fed_monetary", "whitelist", "Emergency rate hike announced", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(emergency.severity, "crisis")
        self.assertEqual(emergency.matched_keyword, "emergency rate hike")

        for title in ("Bank halts withdrawals", "Venue suspends withdrawals"):
            status = classify_news_row(
                _row("coindesk", "whitelist", title, _minutes(-5)),
                now=NOW,
            )
            self.assertEqual(status.severity, "crisis", title)

        negated = classify_news_row(
            _row("coindesk", "whitelist", "Officials confirm no emergency rate hike", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(negated.severity, "none")
        self.assertEqual(negated.suppressed_reason, "no_keyword")

        not_hawk = classify_news_row(
            _row("fed_speeches", "whitelist", "The Committee is not hawkish", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(not_hawk.severity, "none")

        quoted = classify_news_row(
            _row("fed_speeches", "whitelist", 'The chair said "higher for longer" was retired', _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(quoted.severity, "none")

        direct = classify_news_row(
            _row("fed_speeches", "whitelist", "Inflation remains elevated", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(direct.severity, "hawkish")
        self.assertEqual(direct.source, "fed_speeches")

        media = classify_news_row(
            _row("coindesk", "whitelist", "Traders call the Fed hawkish", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(media.severity, "none")
        self.assertEqual(media.suppressed_reason, "hawkish_not_fed_source")

        rumor = classify_news_row(
            _row("newsbtc", "rumor", "Exchange hacked", _minutes(-5)),
            now=NOW,
        )
        self.assertEqual(rumor.severity, "none")
        self.assertEqual(rumor.suppressed_reason, "rumor_or_non_whitelist")

        future = classify_news_row(
            _row("coindesk", "whitelist", "Exchange hacked", _minutes(24 * 60)),
            now=NOW,
        )
        self.assertEqual(future.suppressed_reason, "future")
        soon = classify_news_row(
            _row("coindesk", "whitelist", "Exchange hacked", _minutes(10)),
            now=NOW,
        )
        self.assertEqual(soon.suppressed_reason, "future")

        fresh = classify_news_row(
            _row("coindesk", "whitelist", "Chapter 11 filing", _minutes(-90)),
            now=NOW,
        )
        self.assertEqual(fresh.severity, "crisis")
        stale = classify_news_row(
            _row("coindesk", "whitelist", "Chapter 11 filing", _minutes(-91), ),
            now=NOW,
        )
        self.assertEqual(stale.suppressed_reason, "stale")
        recrawl = classify_news_row(
            {
                **_row("coindesk", "whitelist", "SEC emergency action", _minutes(-60 * 24 * 3)),
                "fetched_at": _iso(NOW),
            },
            now=NOW,
        )
        self.assertEqual(recrawl.suppressed_reason, "late_archive")

    def test_real_schema_columns_match_the_ingest_ddl(self) -> None:
        self.assertTrue({"title", "summary", "onchain_status", "published_at", "source_tier"} <= NEWS_EVENTS_COLUMNS)


class TestOnchainVerifier(unittest.TestCase):
    def test_missing_features_stay_pending_and_labels_are_not_trusted(self) -> None:
        snap = _snap(context=1)
        missing = judge_onchain({"binance_symbol": "SOLUSDT", "onchain_status": "aligned"}, snap, _calibration())
        self.assertEqual(missing.status, "pending")
        self.assertIn("obi_top5", missing.reason)
        self.assertIn("taker_buy_ratio", missing.reason)
        self.assertIn("whale_retail_divergence", missing.reason)
        self.assertIn("heat_evolution", missing.reason)

        complete = {
            "binance_symbol": "SOLUSDT",
            "onchain_status": "aligned",
            "obi_top5": 0.40,
            "taker_buy_ratio": 0.70,
            "whale_retail_divergence": 1.50,
            **_heat(),
        }
        uncalibrated = judge_onchain(complete, snap, None)
        self.assertEqual(uncalibrated, OnchainJudgment("pending", "awaiting_calibration"))
        labeled = judge_onchain(complete, snap, _calibration())
        self.assertEqual(labeled.status, "aligned")

        no_context = judge_onchain(complete, _snap(context=0), _calibration())
        self.assertEqual(no_context.reason, "no_news_context")

        depth20 = judge_onchain(
            {
                "binance_symbol": "SOLUSDT",
                "orderbook_imbalance_20": 0.80,
                "perp_taker_buy_sell_ratio": 8.0,
                "whale_retail_divergence": 1.50,
                **_heat(),
            },
            snap,
            _calibration(),
        )
        self.assertEqual(depth20.status, "pending")
        self.assertIn("obi_top5", depth20.reason)
        self.assertIn("taker_buy_ratio", depth20.reason)

    def test_calibration_has_no_defaults_and_rank_weight_defaults_off(self) -> None:
        for param in inspect.signature(OnchainCalibration).parameters.values():
            self.assertIs(param.default, inspect.Parameter.empty)
        router_sig = inspect.signature(apply_five_state_regime_router)
        self.assertIsNone(router_sig.parameters["onchain_calibration"].default)
        self.assertIsNone(router_sig.parameters["aligned_rank_weight"].default)
        self.assertEqual(CALIBRATION_STATUS, "not_calibrated")
        self.assertEqual(REGIME_CRISIS, REGIME_5_CRISIS_LIQUIDATION_HALT)
        self.assertEqual(REGIME_DEFENSIVE, REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(REGIME_BULL, REGIME_3_CROSS_ASSET_BULL_PULLBACK)

    def test_ambiguous_heat_is_not_guessed(self) -> None:
        points = (
            XWatchPoint("SOL", None, 1, 9, AS_OF),
            XWatchPoint("SOLUSDT", None, 4, 8, AS_OF),
        )
        judged = judge_onchain(
            {
                "binance_symbol": "SOLUSDT",
                "obi_top5": 0.4,
                "taker_buy_ratio": 0.7,
                "whale_retail_divergence": 1.5,
            },
            _snap(x_points=points),
            _calibration(),
        )
        self.assertEqual(judged.status, "pending")
        self.assertIn("ambiguous_x_watch", judged.reason)


class TestRouterGate(unittest.TestCase):
    def test_crisis_blocks_buy_and_keeps_sell(self) -> None:
        snap = _snap(shocks=(_shock("crisis", "exchange hacked", "coindesk"),))
        out, counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_bull()},
                {"action": "sell", "binance_symbol": "SOLUSDT"},
            ],
            [],
            news_snapshot=snap,
        )
        self.assertEqual([row["action"] for row in out], ["sell"])
        self.assertFalse(out[0]["news_shock_applied"])
        self.assertEqual(out[0]["news_shock_severity"], "crisis")
        self.assertEqual(counts[REGIME_5_CRISIS_LIQUIDATION_HALT], 1)

    def test_rumor_and_non_fed_hawkish_do_not_change_state(self) -> None:
        rumor = classify_news_row(
            _row("newsbtc", "rumor", "Exchange hacked", _minutes(-5)),
            now=NOW,
        )
        self.assertIsNotNone(rumor.suppressed_reason)
        media = _snap(shocks=())
        # A CoinDesk hawkish line is not a shock, so the snapshot carries none.
        out, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_state1()}],
            [],
            news_snapshot=media,
        )
        self.assertEqual(out[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertNotEqual(out[0]["regime_playbook"], "news_hawkish_defensive_fade")

    def test_fed_hawkish_uses_state4_template_without_pretending_the_hedge_filled(self) -> None:
        snap = _snap(shocks=(_shock("hawkish", "inflation remains elevated", "fed_speeches"),))
        out, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "ETHUSDT", "size_pct": 0.10, **_state1("ETHUSDT")}],
            [],
            news_snapshot=snap,
        )
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertEqual(row["regime_playbook"], "news_hawkish_defensive_fade")
        self.assertAlmostEqual(row["regime_size_multiplier"], 0.55)
        self.assertAlmostEqual(row["regime_tp_multiplier"], 0.80)
        self.assertFalse(row["enable_trailing_ratchet"])
        self.assertTrue(row["require_perp_beta_hedge"])
        self.assertFalse(row["perp_beta_hedge_already_on"])
        self.assertTrue(row["news_hedge_label_only"])
        self.assertEqual(row["recommended_order_type"], "LIMIT_MAKER")
        self.assertAlmostEqual(row["size_pct"], 0.055)
        for forbidden in ("skip_risk", "bypass_entry_ih", "force_fill", "hedge_filled"):
            self.assertNotIn(forbidden, row)

    def test_kelly_budget_stays_tighter_than_the_playbook_multiplier(self) -> None:
        snap = _snap(shocks=(_shock("hawkish", "higher for longer", "fed_monetary"),))
        out, _counts = apply_five_state_regime_router(
            [{
                "action": "buy",
                "binance_symbol": "BTCUSDT",
                "size_pct": 0.20,
                "use_optimal_kelly_matrix": True,
                **_state1("BTCUSDT"),
            }],
            [],
            news_snapshot=snap,
        )
        self.assertAlmostEqual(out[0]["regime_size_multiplier"], 0.55)
        self.assertAlmostEqual(out[0]["size_pct"], 0.05)

    def test_state3_divergence_rejects_buy_without_replacing_tighter_book_checks(self) -> None:
        features = {
            "obi_top5": -0.10,
            "taker_buy_ratio": 0.50,
            "whale_retail_divergence": 0.50,
            **_heat(),
        }
        snap = _snap(context=1)
        out, counts = apply_five_state_regime_router(
            [
                {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_bull(**features)},
                {"action": "sell", "binance_symbol": "SOLUSDT"},
            ],
            [],
            news_snapshot=snap,
            onchain_calibration=_calibration(),
        )
        self.assertEqual([row["action"] for row in out], ["sell"])
        self.assertEqual(counts[REGIME_3_CROSS_ASSET_BULL_PULLBACK], 1)

        thin = evaluate_five_state_regime(_bull(obi_top5=-0.40, taker_buy_ratio=0.30))
        overlay = evaluate_news_overlay(
            regime=thin.regime,
            allow_buy=thin.allow_buy,
            snapshot=_snap(shocks=(_shock("hawkish", "hawkish", "fed_speeches"),)),
            row=_bull(obi_top5=-0.40, taker_buy_ratio=0.30),
            calibration=_calibration(),
        )
        self.assertFalse(thin.allow_buy)
        self.assertEqual(thin.reject_reason, "regime3_thin_bid_book_trap")
        self.assertIs(overlay.effect, NewsRouteEffect.KEEP)

    def test_aligned_reorders_existing_leaders_and_does_not_open_a_trade(self) -> None:
        aligned = {
            "obi_top5": 0.30,
            "taker_buy_ratio": 0.62,
            "whale_retail_divergence": 1.40,
            **_heat(),
        }
        plain = {
            "obi_top5": 0.05,
            "taker_buy_ratio": 0.50,
            "whale_retail_divergence": 1.00,
            **_heat(),
        }
        snap = _snap(context=1)
        strong = {"action": "buy", "binance_symbol": "AAAUSDT", "size_pct": 0.08, **_bull("AAAUSDT", xs_rank_pct_1h=0.95, **plain)}
        weak = {"action": "buy", "binance_symbol": "BBBUSDT", "size_pct": 0.08, **_bull("BBBUSDT", xs_rank_pct_1h=0.50, **aligned)}
        shadow, _counts = apply_five_state_regime_router(
            [strong, weak],
            [],
            news_snapshot=snap,
            onchain_calibration=_calibration(),
        )
        self.assertEqual([row["binance_symbol"] for row in shadow], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(shadow[1]["news_aligned_rank"], "shadow")
        self.assertIsNone(shadow[1]["news_aligned_rank_weight"])
        # State 3's existing 1.15 multiplier still applies. The rank weight does not.
        self.assertAlmostEqual(shadow[0]["size_pct"], round(0.08 * 1.15, 6))
        self.assertAlmostEqual(shadow[1]["size_pct"], shadow[0]["size_pct"])

        ranked, _counts = apply_five_state_regime_router(
            [strong, weak],
            [],
            news_snapshot=snap,
            onchain_calibration=_calibration(),
            aligned_rank_weight=0.60,
        )
        self.assertEqual([row["binance_symbol"] for row in ranked], ["BBBUSDT", "AAAUSDT"])
        self.assertEqual(ranked[0]["news_aligned_rank"], "applied")
        self.assertAlmostEqual(ranked[0]["size_pct"], shadow[0]["size_pct"])
        self.assertAlmostEqual(ranked[1]["size_pct"], shadow[0]["size_pct"])
        self.assertEqual(len(ranked), 2)
        empty, _counts = apply_five_state_regime_router(
            [],
            [],
            news_snapshot=_snap(shocks=(_shock("hawkish", "hawkish", "fed_speeches"),)),
        )
        self.assertEqual(empty, [])

    def test_priority_keeps_existing_crisis_and_does_not_reopen_blocks(self) -> None:
        crisis = _snap(shocks=(_shock("crisis", "insolvency", "coindesk"),))
        halted = evaluate_five_state_regime(_state1(), macro_halt="vix")
        keep_crisis = evaluate_news_overlay(
            regime=halted.regime,
            allow_buy=halted.allow_buy,
            snapshot=crisis,
            row=_state1(),
        )
        self.assertEqual(halted.reject_reason, "regime5_macro_halt:vix")
        self.assertIs(keep_crisis.effect, NewsRouteEffect.KEEP)
        self.assertEqual(keep_crisis.meta["news_priority"], "existing_crisis")

        hawk = _snap(shocks=(_shock("hawkish", "further tightening", "fed_monetary"),))
        keep_halt = evaluate_news_overlay(
            regime=halted.regime,
            allow_buy=halted.allow_buy,
            snapshot=hawk,
            row=_state1(),
        )
        self.assertIs(keep_halt.effect, NewsRouteEffect.KEEP)

        extreme = evaluate_five_state_regime({**_state1(), "funding_z": 3.0})
        keep_extreme = evaluate_news_overlay(
            regime=extreme.regime,
            allow_buy=extreme.allow_buy,
            snapshot=hawk,
            row=_state1(),
        )
        self.assertFalse(extreme.allow_buy)
        self.assertEqual(extreme.reject_reason, "regime4_extreme_crowded_long_trap")
        self.assertIs(keep_extreme.effect, NewsRouteEffect.KEEP)

        crowded = evaluate_five_state_regime({**_state1(), "cot_regime": "cot_leveraged_long_crowded"})
        keep_crowded = evaluate_news_overlay(
            regime=crowded.regime,
            allow_buy=crowded.allow_buy,
            snapshot=hawk,
            row=_state1(),
        )
        self.assertEqual(crowded.regime, REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertIs(keep_crowded.effect, NewsRouteEffect.KEEP)

        squeeze = evaluate_five_state_regime({**_state1(), "funding_z": -2.4})
        self.assertEqual(squeeze.regime, REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        replaced = evaluate_news_overlay(
            regime=squeeze.regime,
            allow_buy=squeeze.allow_buy,
            snapshot=hawk,
            row=_state1(),
        )
        self.assertIs(replaced.effect, NewsRouteEffect.HAWKISH_DEFENSIVE)
        divergent_features = {
            "obi_top5": -0.10,
            "taker_buy_ratio": 0.50,
            "whale_retail_divergence": 0.50,
            **_heat(),
        }
        not_state3 = evaluate_news_overlay(
            regime=squeeze.regime,
            allow_buy=True,
            snapshot=_snap(context=1),
            row={**_state1(), **divergent_features},
            calibration=_calibration(),
        )
        self.assertIs(not_state3.effect, NewsRouteEffect.KEEP)

    def test_one_snapshot_is_reused_for_every_symbol_and_does_not_reread(self) -> None:
        snap = _snap(shocks=(_shock("hawkish", "tariff shock", "fed_speeches", age=7.5),))
        candidates = [
            {"action": "buy", "binance_symbol": "AAAUSDT", "size_pct": 0.10, **_state1("AAAUSDT")},
            {"action": "buy", "binance_symbol": "BBBUSDT", "size_pct": 0.10, **_state1("BBBUSDT")},
        ]
        with patch("decision_engine.core.news_shock_gate.sqlite3.connect") as connect:
            first, _counts = apply_five_state_regime_router(candidates, [], news_snapshot=snap)
            second, _counts = apply_five_state_regime_router(candidates, [], news_snapshot=snap)
            connect.assert_not_called()
        self.assertEqual(first[0]["news_shock_age_min"], first[1]["news_shock_age_min"])
        self.assertEqual(first[0]["news_shock_age_min"], second[1]["news_shock_age_min"])
        self.assertEqual(first[0]["news_shock_keyword"], "tariff shock")


class TestSnapshotFailures(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _buy(self) -> list[dict[str, object]]:
        return [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, **_state1()}]

    def test_missing_locked_timeout_schema_future_and_stale(self) -> None:
        missing = self.root / "absent.db"
        snap = load_news_snapshot(missing, now=NOW, timeout_sec=0.2, symbols=[])
        self.assertEqual(snap.availability, "missing_db")
        self.assertFalse(missing.exists())
        out, _counts = apply_five_state_regime_router(self._buy(), [], news_snapshot=snap)
        self.assertEqual(out[0]["news_gate_availability"], "missing_db")
        self.assertEqual(out[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertTrue(out[0]["news_onchain_reason"].startswith("news_db_"))

        real = self.root / "news.db"
        _init_news(real)
        _insert_news(
            real,
            source="coindesk",
            tier="whitelist",
            title="Exchange hacked",
            url="https://example.test/hack",
            published_at=_minutes(-15),
        )
        _insert_news(
            real,
            source="newsbtc",
            tier="rumor",
            title="Exchange hacked rumor",
            url="https://example.test/rumor",
            published_at=_minutes(-15),
        )
        _insert_news(
            real,
            source="coindesk",
            tier="whitelist",
            title="Bridge exploit tomorrow",
            url="https://example.test/future",
            published_at=_minutes(24 * 60),
            fetched_at=_iso(NOW),
        )
        _insert_news(
            real,
            source="coindesk",
            tier="whitelist",
            title="Old insolvency recrawl",
            url="https://example.test/old",
            published_at=_minutes(-60 * 24 * 5),
            fetched_at=_iso(NOW),
        )
        _insert_news(
            real,
            source="fed_speeches",
            tier="whitelist",
            title='Speaker quoted "higher for longer" only as history',
            url="https://example.test/quote",
            published_at=_minutes(-8),
        )
        loaded = load_news_snapshot(real, now=NOW, timeout_sec=1.0, symbols=[])
        self.assertEqual(loaded.availability, "ok")
        self.assertEqual(len(loaded.shocks), 1)
        self.assertEqual(loaded.shocks[0].matched_keyword, "exchange hacked")
        blocked, _counts = apply_five_state_regime_router(self._buy(), [], news_snapshot=loaded)
        self.assertEqual(blocked, [])

        holder = sqlite3.connect(real)
        holder.isolation_level = None
        holder.execute("PRAGMA journal_mode=DELETE")
        holder.execute("BEGIN EXCLUSIVE")
        try:
            locked = load_news_snapshot(real, now=NOW, timeout_sec=0.2, symbols=[])
        finally:
            holder.execute("ROLLBACK")
            holder.close()
        self.assertEqual(locked.availability, "locked")
        passed, _counts = apply_five_state_regime_router(self._buy(), [], news_snapshot=locked)
        self.assertEqual(len(passed), 1)

        timed = load_news_snapshot(real, now=NOW, timeout_sec=0, symbols=[])
        self.assertEqual(timed.availability, "timeout")
        still, _counts = apply_five_state_regime_router(self._buy(), [], news_snapshot=timed)
        self.assertEqual(len(still), 1)

        broken = self.root / "broken.db"
        broken_conn = sqlite3.connect(broken)
        broken_conn.execute("CREATE TABLE news_events (id INTEGER PRIMARY KEY, title TEXT)")
        broken_conn.commit()
        broken_conn.close()
        schema = load_news_snapshot(broken, now=NOW, timeout_sec=0.5, symbols=[])
        self.assertEqual(schema.availability, "schema_error")
        kept, _counts = apply_five_state_regime_router(self._buy(), [], news_snapshot=schema)
        self.assertEqual(len(kept), 1)

        garbage = self.root / "garbage.db"
        garbage.write_bytes(b"this is not sqlite")
        errored = load_news_snapshot(garbage, now=NOW, timeout_sec=0.5, symbols=[])
        self.assertEqual(errored.availability, "error")

    def test_future_book_row_is_not_the_live_feature(self) -> None:
        path = self.root / "deriv.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE binance_spot_microstructure (
                symbol TEXT, ts_ms INTEGER, ret_5m_pct REAL, ret_1h_pct REAL,
                ret_4h_pct REAL, ret_1d_pct REAL, taker_buy_ratio_5m REAL,
                taker_buy_ratio_1h REAL, spread_bps REAL, bid_depth_20_usd REAL,
                ask_depth_20_usd REAL, orderbook_imbalance_20 REAL,
                impact_10k_bps REAL, whale_net_buy_usd REAL, aggtrade_whale_buy_ratio REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE binance_perp_counterparty (
                symbol TEXT, ts_ms INTEGER, whale_retail_divergence REAL
            )
            """
        )
        now_ms = int(NOW.timestamp() * 1000)
        conn.execute(
            "INSERT INTO binance_spot_microstructure (symbol, ts_ms, orderbook_imbalance_20, taker_buy_ratio_5m) VALUES (?, ?, ?, ?)",
            ("SOLUSDT", now_ms - 60_000, 0.20, 0.61),
        )
        conn.execute(
            "INSERT INTO binance_spot_microstructure (symbol, ts_ms, orderbook_imbalance_20, taker_buy_ratio_5m) VALUES (?, ?, ?, ?)",
            ("SOLUSDT", now_ms + 3_600_000, 0.99, 0.10),
        )
        conn.execute(
            "INSERT INTO binance_perp_counterparty (symbol, ts_ms, whale_retail_divergence) VALUES (?, ?, ?)",
            ("SOLUSDT", now_ms - 60_000, 1.30),
        )
        conn.commit()
        conn.close()
        news = self.root / "news.db"
        _init_news(news)
        _insert_news(
            news,
            source="coindesk",
            tier="whitelist",
            title="Market color",
            url="https://example.test/color",
            published_at=_minutes(-5),
        )
        snap = load_news_snapshot(
            news,
            now=NOW,
            timeout_sec=1.0,
            symbols=["SOLUSDT"],
            derivatives_db=path,
        )
        self.assertEqual(snap.feature_availability, "ok")
        self.assertEqual(list(snap.obi_points), [])
        self.assertEqual([point.value for point in snap.taker_points], [0.61])
        self.assertNotIn(0.10, [point.value for point in snap.taker_points])
        judged = judge_onchain(
            {"binance_symbol": "SOLUSDT", **_heat()},
            snap,
            _calibration(),
        )
        self.assertEqual(judged.status, "pending")
        self.assertIn("obi_top5", judged.reason)


class TestHistoricalAudit(unittest.TestCase):
    def test_point_in_time_helpers_drop_future_and_pre_event_bars(self) -> None:
        event = 1_700_000_000_000
        self.assertIsNone(asof_numeric([(event + 60_000, 99.0)], event, 90 * 60_000))
        self.assertIsNone(asof_numeric([(event - 2 * 24 * 3600 * 1000, 0.2)], event, 90 * 60_000))
        self.assertEqual(asof_numeric([(event - 60_000, 0.2), (event + 60_000, 99.0)], event, 90 * 60_000), (event - 60_000, 0.2))
        self.assertIsNone(heat_delta([(event + 1, 5.0)], event, 90 * 60_000))
        self.assertEqual(
            heat_delta([(event - 40 * 60_000, 10.0), (event - 10 * 60_000, 16.0), (event + 10 * 60_000, 99.0)], event, 90 * 60_000),
            6.0,
        )
        ret = kline_forward_return(
            [
                (event - 3_600_000, 50.0, 40.0),
                (event, 100.0, 99.0),
                (event + 55 * 60_000, 101.0, 110.0),
            ],
            event,
            60,
        )
        self.assertAlmostEqual(ret or 0.0, 0.10)
        self.assertIsNone(
            kline_forward_return([(event - 3_600_000, 50.0, 40.0)], event, 60)
        )

    def test_audit_stays_uncalibrated_and_does_not_backfill(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            absent = root / "no-such.db"
            empty = audit_news_shock_history(news_db=absent, now=NOW)
            self.assertEqual(empty["calibration_status"], "not_calibrated")
            self.assertIsNone(empty["recommended_thresholds"])
            self.assertFalse(empty["thresholds_enabled"])
            self.assertFalse(empty["gates_passed"])
            self.assertFalse(absent.exists())
            self.assertTrue(any(item["series"] == "x_heat_timeseries" for item in empty["untestable"]))

            db = root / "history.db"
            conn = sqlite3.connect(db)
            conn.executescript(NEWS_EVENTS_DDL)
            conn.executescript(
                """
                CREATE TABLE ccxt_ohlcv (
                    symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, close REAL, volume REAL
                );
                CREATE TABLE binance_klines (
                    symbol TEXT, interval TEXT, open_time_ms INTEGER,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    close_time_ms INTEGER
                );
                CREATE TABLE binance_spot_microstructure (
                    symbol TEXT, ts_ms INTEGER, orderbook_imbalance_20 REAL,
                    taker_buy_ratio_5m REAL, taker_buy_ratio_1h REAL
                );
                CREATE TABLE binance_perp_counterparty (
                    symbol TEXT, ts_ms INTEGER, whale_retail_divergence REAL
                );
                CREATE TABLE indicator_history (
                    chain_id TEXT, token_address TEXT, recorded_at TEXT,
                    sentiment_heat_score REAL
                );
                CREATE TABLE x_watch (
                    chain_id TEXT, token_address TEXT, symbol TEXT, query_key TEXT,
                    mode TEXT, mention_count_total INTEGER, mention_count_prev INTEGER,
                    mention_count_last INTEGER, last_searched_at TEXT,
                    created_at TEXT, updated_at TEXT
                );
                """
            )
            base = datetime(2026, 5, 1, tzinfo=timezone.utc)
            as_of = base + timedelta(days=5)
            crisis_at = base + timedelta(days=4)
            crisis_ms = int(crisis_at.timestamp() * 1000)

            def add_news(title: str, url: str, when: datetime, source: str = "coindesk", tier: str = "whitelist") -> None:
                conn.execute(
                    """
                    INSERT INTO news_events (
                        source, source_tier, title, url, published_at, fetched_at, summary, onchain_status
                    ) VALUES (?, ?, ?, ?, ?, ?, '', 'pending')
                    """,
                    (source, tier, title, url, _iso(when), _iso(as_of)),
                )

            add_news("Desk note one", "https://example.test/a", base)
            add_news("Desk note two", "https://example.test/b", base + timedelta(days=1))
            add_news("Desk note three", "https://example.test/c", base + timedelta(days=2))
            add_news("Desk note four", "https://example.test/d", base + timedelta(days=3))
            add_news("Exchange hacked", "https://example.test/e", crisis_at)
            add_news("Exchange hacked", "https://example.test/e2", crisis_at + timedelta(minutes=30))
            add_news("Exchange hacked", "https://example.test/rumor", crisis_at, source="newsbtc", tier="rumor")
            add_news("Exchange hacked", "https://example.test/future", as_of + timedelta(days=1))
            fetch_ms = int(as_of.timestamp() * 1000)
            conn.execute(
                "INSERT INTO binance_klines (symbol, interval, open_time_ms, open, close, volume) VALUES (?, '5m', ?, ?, ?, ?)",
                ("BTCUSDT", crisis_ms, 50.0, 40.0, 1.0),
            )
            conn.execute(
                "INSERT INTO binance_klines (symbol, interval, open_time_ms, open, close, volume) VALUES (?, '5m', ?, ?, ?, ?)",
                ("BTCUSDT", fetch_ms, 100.0, 99.0, 2.0),
            )
            conn.execute(
                "INSERT INTO binance_klines (symbol, interval, open_time_ms, open, close, volume) VALUES (?, '5m', ?, ?, ?, ?)",
                ("BTCUSDT", fetch_ms + 55 * 60_000, 101.0, 110.0, 3.0),
            )
            conn.execute(
                "INSERT INTO ccxt_ohlcv (symbol, timeframe, ts, open, close, volume) VALUES (?, '5m', ?, ?, ?, ?)",
                ("BTC/USDT", fetch_ms, 1.0, 50.0, 9.0),
            )
            conn.execute(
                "INSERT INTO binance_spot_microstructure (symbol, ts_ms, orderbook_imbalance_20, taker_buy_ratio_5m) VALUES (?, ?, 99, 0.99)",
                ("BTCUSDT", fetch_ms + 3_600_000),
            )
            conn.execute(
                """
                INSERT INTO indicator_history (
                    chain_id, token_address, recorded_at, sentiment_heat_score
                ) VALUES ('solana', 'mint', ?, 10), ('solana', 'mint', ?, 16)
                """,
                (_iso(as_of - timedelta(minutes=40)), _iso(as_of - timedelta(minutes=10))),
            )
            conn.execute(
                """
                INSERT INTO x_watch (
                    chain_id, token_address, symbol, query_key, mode,
                    mention_count_total, mention_count_prev, mention_count_last,
                    last_searched_at, created_at, updated_at
                ) VALUES ('solana', 'mint', 'BTC', 'btc', 'hot', 9999, 1, 9999, ?, ?, ?)
                """,
                (_iso(as_of), _iso(as_of), _iso(as_of)),
            )
            conn.commit()
            conn.close()

            report = audit_news_shock_history(
                news_db=db,
                market_db=db,
                derivatives_db=db,
                indicator_db=db,
                benchmark_symbol="BTCUSDT",
                symbol_by_url={
                    "https://example.test/e": {
                        "symbol": "BTCUSDT",
                        "token_address": "mint",
                        "chain_id": "solana",
                    }
                },
                now=as_of,
            )
            self.assertEqual(report["calibration_status"], "not_calibrated")
            self.assertIsNone(report["recommended_thresholds"])
            self.assertFalse(report["thresholds_enabled"])
            self.assertFalse(report["gates_passed"])
            self.assertFalse(report["production_attested"])
            self.assertFalse(report["local_inventory_2026_11_10"]["recomputed_here"])
            self.assertEqual(report["local_inventory_2026_11_10"]["news_rows"], 1869)
            self.assertEqual(report["local_inventory_2026_11_10"]["binance_5m_aligned_news"], 375)
            self.assertEqual(report["local_inventory_2026_11_10"]["crisis_terms_in_binance_window"], 0)
            self.assertEqual(report["local_inventory_2026_11_10"]["fed_or_hawkish_terms_in_binance_window"], 1)
            self.assertIsNone(report["local_inventory_2026_11_10"]["false_positive_rate"])
            self.assertEqual(report["descriptive"]["false_positive_rate"]["status"], "not_computed")
            self.assertIn("production_history_not_attested", report["gates_failed"])
            self.assertEqual(report["sample"]["after_dedup"], 5)
            self.assertEqual(report["sample"]["rumor_ignored"], 1)
            self.assertGreaterEqual(report["sample"]["future_ignored"], 1)
            self.assertGreater(report["sample"]["late_archive_rows"], 0)
            self.assertAlmostEqual(report["descriptive"]["test_return_60m"]["mean"], 0.10)
            self.assertEqual(report["coverage_counts"]["obi_top5_asof"], 0)
            self.assertEqual(report["coverage_counts"]["imbalance20_asof"], 0)
            self.assertTrue(any(item["series"] == "obi_top5" for item in report["untestable"]))
            self.assertTrue(any(item["series"] == "onchain_labels" for item in report["untestable"]))
            self.assertGreaterEqual(report["coverage_counts"]["heat_delta"], 1)
            self.assertFalse(report["descriptive"]["cost_applied"])
            self.assertIn("Bonferroni", report["methodology"]["multiple_testing"])
            self.assertIn("published_at", report["methodology"]["time_split"])
            blob = json.dumps(report)
            self.assertNotIn("99", blob)
            self.assertTrue(any("x_watch" in note for note in report["notes"]))
            for key in (
                "time_split",
                "dedup",
                "sample_size",
                "coverage",
                "cost",
                "benchmark",
                "confidence_interval",
                "multiple_testing",
                "no_lookahead",
                "no_snapshot_backfill",
            ):
                self.assertIn(key, report["methodology"])

            out = root / "report.json"
            code = audit_main(["--news-db", str(absent), "--out", str(out)])
            self.assertEqual(code, 0)
            saved = json.loads(out.read_text(encoding="utf-8"))
            self.assertIsNone(saved["recommended_thresholds"])
            self.assertFalse((root / "news_shock_calibration.json").exists())


if __name__ == "__main__":
    unittest.main()
