"""News isolation stays off the five-state, CityU, counterparty, and Kelly paths."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import decision_engine  # noqa: F401
from decision_engine.core.news_shock_gate import apply_news_isolation
from decision_engine.core.perp_microstructure_and_beta_hedge import attach_counterparty_context
from decision_engine.core.regime_strategy_router import (
    apply_five_state_regime_router,
    evaluate_five_state_regime,
)
from scripts.build_news_isolation_engine import ENGINE_BEFORE, patch_engine

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
PROD_PERP = "ea3d43afcb4e5aa6388c69e698742ac04b6ff3d10c27fa7a3f5dc8072c619a34"
PROD_CLUSTERS = "5dbd84373298ecd7df2480e4766c79d0b037663afa60e29fa21768303aec26d1"
PROD_GUARDS = "4ff94eb982a5afc1c60d919d9ba9557fcb10e42d157d18a20891ab61b4b8d9cc"


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _news_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY,
            source TEXT, source_tier TEXT, title TEXT, url TEXT,
            published_at TEXT, fetched_at TEXT, summary TEXT,
            raw_json TEXT, onchain_status TEXT, notes TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO news_events (
            id, source, source_tier, title, url, published_at, fetched_at,
            summary, raw_json, onchain_status, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


def _row(row_id: int, source: str, title: str) -> tuple:
    published = _iso(NOW - timedelta(minutes=10))
    return (
        row_id, source, "whitelist", title, f"https://example.test/{row_id}",
        published, published, "", "{}", "pending", "",
    )


def _extreme_candidate() -> dict:
    return {
        "action": "buy",
        "binance_symbol": "SOLUSDT",
        "symbol": "SOLUSDT",
        "size_pct": 0.10,
        "funding_z": -2.0,
        "funding_rate": -0.001,
        "nvda_1h_ret_pct": -3.0,
        "smh_1h_ret_pct": -3.0,
        "cot_regime": "cot_institutional_squeeze_favorable",
        "obi_top5": -0.9,
        "taker_buy_ratio": 0.1,
        "whale_retail_divergence": 0.2,
        "use_optimal_kelly_matrix": True,
    }


def _write_extreme_dbs(root: Path) -> dict[str, Path]:
    counterparty = root / "data" / "counterparty" / "public_derivatives.db"
    cityu = root / "data" / "cityu" / "cityu_institutional.db"
    lseg = root / "data" / "lseg" / "lseg_macro.db"
    for path in (counterparty, cityu, lseg):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(counterparty)
    conn.execute(
        """
        CREATE TABLE binance_perp_counterparty (
            symbol TEXT, ts_ms INTEGER, funding_z REAL, funding_rate REAL,
            perp_basis_bps REAL, top_position_ls_ratio REAL,
            global_account_ls_ratio REAL, whale_retail_divergence REAL,
            liq_cascade_regime TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO binance_perp_counterparty VALUES "
        "('SOLUSDT', 1, -3.0, -0.001, 80, 0.4, 2.4, 0.3, 'long_liquidation_waterfall')"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(cityu)
    conn.execute(
        """
        CREATE TABLE lseg_cftc_cot_positioning (
            date TEXT, symbol TEXT, open_interest TEXT,
            lev_money_net TEXT, asset_mgr_net TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO lseg_cftc_cot_positioning VALUES ('2026-11-03', 'BTC', '100', '-40', '5')"
    )
    conn.commit()
    conn.close()
    conn = sqlite3.connect(lseg)
    conn.execute(
        """
        CREATE TABLE macro_events (
            country TEXT, indicator_name TEXT, utc_ts TEXT, utc_ms INTEGER,
            relevance INTEGER, local_time_hkt TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO macro_events VALUES ('US', 'CPI', ?, ?, 3, '15:00')",
        (_iso(NOW), int(NOW.timestamp() * 1000)),
    )
    conn.execute(
        """
        CREATE TABLE central_bank_events (
            country TEXT, event_name TEXT, utc_ts TEXT, utc_ms INTEGER,
            is_high_impact INTEGER, local_time_hkt TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO central_bank_events VALUES ('US', 'FOMC', ?, ?, 1, '02:00')",
        (_iso(NOW), int(NOW.timestamp() * 1000)),
    )
    conn.commit()
    conn.close()
    return {"counterparty": counterparty, "cityu": cityu, "lseg": lseg}


class TestNewsIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self._env = tempfile.TemporaryDirectory()
        self.root = Path(self._env.name)
        self.dbs = _write_extreme_dbs(self.root)
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "PUBLIC_DERIVATIVES_DB_PATH",
                "CITYU_INSTITUTIONAL_DB_PATH",
                "LSEG_MACRO_DB_PATH",
                "DECISION_MACRO_DB",
                "X_WATCH_DB_PATH",
                "NEWS_EVENTS_DB_PATH",
            )
        }
        os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(self.dbs["counterparty"])
        os.environ["CITYU_INSTITUTIONAL_DB_PATH"] = str(self.dbs["cityu"])
        os.environ["LSEG_MACRO_DB_PATH"] = str(self.dbs["lseg"])
        os.environ["DECISION_MACRO_DB"] = str(self.dbs["lseg"])
        os.environ["X_WATCH_DB_PATH"] = str(self.root / "x_watch.db")

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._env.cleanup()

    def _opened(self, news: Path | None, rows: list[dict]):
        opened: list[str] = []
        real = sqlite3.connect

        def spy(*args, **kwargs):
            if args:
                opened.append(str(args[0]))
            return real(*args, **kwargs)

        with patch("decision_engine.core.news_shock_gate.sqlite3.connect", spy):
            out, report = apply_news_isolation(rows, db_path=news, now=NOW, timeout_sec=1.0)
        return out, report, opened

    def test_extreme_books_do_not_change_candidates_without_news(self) -> None:
        news = self.root / "news_events.db"
        _news_db(news, [_row(1, "coindesk", "bitcoin holds a narrow range")])
        buy = _extreme_candidate()
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 0.10, "funding_z": -2.0}
        out, report, opened = self._opened(news, [buy, sell])
        self.assertIs(out[0], buy)
        self.assertIs(out[1], sell)
        self.assertEqual(buy["size_pct"], 0.10)
        self.assertNotIn("market_regime", buy)
        self.assertFalse(report["candidates_changed"])
        self.assertFalse(report["router_called"])
        self.assertFalse(report["kelly_applied"])
        self.assertFalse(report["market_features_read"])
        self.assertIsNone(report["aligned_rank_weight"])
        joined = " ".join(opened)
        for forbidden in ("public_derivatives.db", "cityu_institutional.db", "lseg_macro.db", "x_watch.db"):
            self.assertNotIn(forbidden, joined)
        self.assertTrue(any(str(news) in item for item in opened))

    def test_full_router_still_changes_size_from_the_same_extreme_book(self) -> None:
        hot = attach_counterparty_context(
            [{"binance_symbol": "SOLUSDT"}],
            db_path=self.dbs["counterparty"],
            now_ms=1,
        )
        self.assertEqual(hot[0]["funding_z"], -3.0)
        self.assertEqual(hot[0]["liq_cascade_regime"], "long_liquidation_waterfall")
        decision = evaluate_five_state_regime(hot[0])
        self.assertFalse(decision.allow_buy)
        self.assertEqual(decision.regime, "crisis_liquidation_halt")
        self.assertEqual(decision.size_multiplier, 0.0)
        routed, _counts = apply_five_state_regime_router(
            [{"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10}],
            hot,
        )
        self.assertEqual(routed, [])

    def test_crisis_drops_buy_and_keeps_sell_size(self) -> None:
        news = self.root / "news_events.db"
        _news_db(news, [_row(1, "coindesk", "exchange hacked and withdrawals halted")])
        buy = _extreme_candidate()
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 0.10}
        out, report, _opened = self._opened(news, [buy, sell])
        self.assertEqual([row["action"] for row in out], ["sell"])
        self.assertEqual(out[0]["size_pct"], 0.10)
        self.assertEqual(out[0]["news_shock_severity"], "crisis")
        self.assertFalse(out[0]["news_shock_applied"])
        self.assertEqual(report["effect"], "crisis_halt")
        self.assertEqual(report["market_regime"], "crisis_liquidation_halt")
        self.assertEqual(report["regime_playbook"], "cash_halt")
        self.assertEqual(report["dropped_buys"], 1)
        self.assertFalse(report["hedge_filled"])
        self.assertFalse(report["kelly_applied"])

    def test_fed_hawkish_uses_state4_template_without_kelly_replacement(self) -> None:
        news = self.root / "news_events.db"
        _news_db(news, [_row(1, "fed_speeches", "inflation remains elevated")])
        buy = _extreme_candidate()
        buy["size_pct"] = 0.20
        sell = {"action": "sell", "binance_symbol": "SOLUSDT", "size_pct": 0.20}
        out, report, _opened = self._opened(news, [buy, sell])
        self.assertEqual([row["action"] for row in out], ["buy", "sell"])
        self.assertEqual(out[0]["market_regime"], "crowded_long_defensive")
        self.assertEqual(out[0]["regime_playbook"], "news_hawkish_defensive_fade")
        self.assertAlmostEqual(out[0]["regime_size_multiplier"], 0.55)
        self.assertAlmostEqual(out[0]["regime_tp_multiplier"], 0.80)
        self.assertFalse(out[0]["enable_trailing_ratchet"])
        self.assertTrue(out[0]["require_perp_beta_hedge"])
        self.assertTrue(out[0]["news_hedge_label_only"])
        self.assertFalse(out[0]["perp_beta_hedge_already_on"])
        self.assertEqual(out[0]["recommended_order_type"], "LIMIT_MAKER")
        self.assertAlmostEqual(out[0]["size_pct"], 0.11)
        self.assertNotIn("optimal_base_position_pct", out[0])
        self.assertEqual(out[1]["size_pct"], 0.20)
        self.assertEqual(report["effect"], "hawkish_defensive")
        self.assertFalse(report["kelly_applied"])

    def test_non_fed_hawkish_and_divergent_features_do_not_change_weight(self) -> None:
        news = self.root / "news_events.db"
        _news_db(news, [_row(1, "coindesk", "inflation remains elevated")])
        buy = _extreme_candidate()
        out, report, _opened = self._opened(news, [buy])
        self.assertIs(out[0], buy)
        self.assertEqual(buy["size_pct"], 0.10)
        self.assertFalse(report["candidates_changed"])
        self.assertIsNone(report["aligned_rank_weight"])

    def test_missing_locked_schema_and_timeout_pass_candidates_through(self) -> None:
        buy = _extreme_candidate()
        missing, missing_report, _opened = self._opened(self.root / "absent.db", [buy])
        self.assertIs(missing[0], buy)
        self.assertEqual(missing_report["availability"], "missing_db")
        self.assertTrue(missing_report["fail_open"])
        self.assertFalse(missing_report["candidates_changed"])

        empty = self.root / "empty.db"
        sqlite3.connect(empty).close()
        schema, schema_report, _opened = self._opened(empty, [buy])
        self.assertIs(schema[0], buy)
        self.assertEqual(schema_report["availability"], "schema_error")
        self.assertTrue(schema_report["fail_open"])

        locked_path = self.root / "locked.db"
        _news_db(locked_path, [_row(1, "coindesk", "exchange hacked")])
        holder = sqlite3.connect(locked_path)
        holder.execute("BEGIN EXCLUSIVE")
        try:
            locked, locked_report = apply_news_isolation(
                [buy], db_path=locked_path, now=NOW, timeout_sec=0.05
            )
        finally:
            holder.close()
        self.assertIs(locked[0], buy)
        self.assertIn(locked_report["availability"], {"locked", "timeout"})
        self.assertTrue(locked_report["fail_open"])
        self.assertEqual(buy["size_pct"], 0.10)

        readable = self.root / "readable.db"
        _news_db(readable, [_row(1, "coindesk", "exchange hacked")])
        timed, timed_report = apply_news_isolation(
            [buy], db_path=readable, now=NOW, timeout_sec=0.0
        )
        self.assertIs(timed[0], buy)
        self.assertEqual(timed_report["availability"], "timeout")
        self.assertTrue(timed_report["fail_open"])
        self.assertFalse(timed_report["candidates_changed"])


class TestIsolatedEngineBlob(unittest.TestCase):
    def test_patch_matches_the_shipped_file_and_leaves_main_engine_on_the_router(self) -> None:
        base = subprocess.check_output(
            ["git", "cat-file", "blob", "bc392fea:decision_engine/core/engine.py"],
            cwd=REPO,
        )
        self.assertEqual(hashlib.sha256(base).hexdigest(), ENGINE_BEFORE)
        patched = patch_engine(base.decode("utf-8")).encode("utf-8")
        shipped = (REPO / "deploy" / "news_isolation" / "engine.py").read_bytes()
        self.assertEqual(patched, shipped)
        text = shipped.decode("utf-8")
        self.assertIn("apply_news_isolation", text)
        for forbidden in (
            "apply_five_state_regime_router",
            "evaluate_macro_gate",
            "attach_cityu_institutional_context",
            "attach_counterparty_context",
            "drawdown_and_kelly_budget",
        ):
            self.assertNotIn(forbidden, text)
        main_engine = (REPO / "decision_engine" / "core" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("apply_five_state_regime_router", main_engine)
        self.assertNotIn("apply_news_isolation", main_engine)

    def test_mixed_tree_imports_isolated_engine_beside_production_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = subprocess.check_output(["git", "archive", "bc392fea"], cwd=REPO)
            subprocess.run(["tar", "-x", "-C", str(root)], input=archive, check=True)
            (root / "decision_engine" / "core" / "engine.py").write_bytes(
                (REPO / "deploy" / "news_isolation" / "engine.py").read_bytes()
            )
            gate = REPO / "decision_engine" / "core" / "news_shock_gate.py"
            (root / "decision_engine" / "core" / "news_shock_gate.py").write_bytes(gate.read_bytes())
            perp = subprocess.check_output(
                ["git", "show", "origin/main:decision_engine/core/perp_microstructure_and_beta_hedge.py"],
                cwd=REPO,
            )
            clusters = subprocess.check_output(
                ["git", "show", "origin/main:decision_engine/core/portfolio_clusters.py"],
                cwd=REPO,
            )
            (root / "decision_engine" / "core" / "perp_microstructure_and_beta_hedge.py").write_bytes(perp)
            (root / "decision_engine" / "core" / "portfolio_clusters.py").write_bytes(clusters)
            self.assertEqual(hashlib.sha256(perp).hexdigest(), PROD_PERP)
            self.assertEqual(hashlib.sha256(clusters).hexdigest(), PROD_CLUSTERS)
            guards = (root / "decision_engine" / "core" / "live_buy_guards.py").read_bytes()
            self.assertEqual(hashlib.sha256(guards).hexdigest(), PROD_GUARDS)
            _write_extreme_dbs(root)
            probe = r"""
import hashlib, os, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(root / "data/counterparty/public_derivatives.db")
os.environ["LSEG_MACRO_DB_PATH"] = str(root / "data/lseg/lseg_macro.db")
os.environ["CITYU_INSTITUTIONAL_DB_PATH"] = str(root / "data/cityu/cityu_institutional.db")
import decision_engine.core.engine as engine
from decision_engine.core.news_shock_gate import apply_news_isolation
names = engine.DecisionEngine.tick.__code__.co_names
assert "apply_news_isolation" in names, names
for forbidden in ("apply_five_state_regime_router", "evaluate_macro_gate", "attach_cityu_institutional_context", "attach_counterparty_context"):
    assert forbidden not in names, forbidden
buy = {"action": "buy", "binance_symbol": "SOLUSDT", "size_pct": 0.10, "funding_z": -3.0, "use_optimal_kelly_matrix": True}
out, report = apply_news_isolation([buy], db_path=root / "missing-news.db")
assert out[0] is buy and buy["size_pct"] == 0.10 and report["availability"] == "missing_db"
assert report["fail_open"] and not report["candidates_changed"]
print("MIX_OK")
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root)
            env.pop("PYTHONSAFEPATH", None)
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(root)],
                cwd="/tmp",
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertIn("MIX_OK", completed.stdout)


if __name__ == "__main__":
    unittest.main()
