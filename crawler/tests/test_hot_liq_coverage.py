"""Hot-tier liquidity coverage monitor. No live keys, network, or production DBs.

R1 observation only: missing liquidity stays NULL and does not count toward
strategy validity (10-19 旁证).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daily_check import (
    HOT_LIQ_CHECK_NAME,
    CheckReport,
    check_hot_liquidity_coverage,
    run_checks,
    write_json,
)
from history import (
    HOT_LIQ_FAIL_MIN_N,
    extract_metrics,
    format_hot_liq_operator_line,
    hot_liq_coverage_level,
    liquidity_coverage,
    utc_hours_ago_iso,
)
from store import Store
from tools.grok_context import build_context, to_markdown


def _iso(age_sec: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(
    store: Store,
    *,
    n: int,
    liquidity: float | None,
    tier: str = "hot",
    age_sec: float = 60,
    price: float | None = 1.25,
    tag: str = "p",
) -> None:
    for i in range(n):
        store.insert_pair_history(
            "solana",
            f"{tag}{tier}{liquidity}{age_sec}{i}",
            token_address=f"tok{tag}{i}",
            price_usd=price,
            liquidity_usd=liquidity,
            tier=tier,
            recorded_at=_iso(age_sec + i),
        )


class ExtractMetricsTests(unittest.TestCase):
    def test_omitted_liquidity_stays_none(self) -> None:
        missing = extract_metrics({"priceUsd": "1.5", "volume": {"h24": 9}})
        self.assertIsNone(missing["liquidity_usd"])
        self.assertEqual(missing["price_usd"], 1.5)
        self.assertIsNone(extract_metrics({"liquidity": None})["liquidity_usd"])
        self.assertIsNone(extract_metrics({"liquidity": {}})["liquidity_usd"])
        self.assertIsNone(extract_metrics({"liquidity": "nope"})["liquidity_usd"])
        self.assertEqual(
            extract_metrics({"liquidity": {"usd": 12000}})["liquidity_usd"],
            12000.0,
        )


class CoverageLevelTests(unittest.TestCase):
    def test_thresholds_match_calibration(self) -> None:
        self.assertEqual(hot_liq_coverage_level(0, None), "WARN")
        self.assertEqual(hot_liq_coverage_level(100, 0.25), "PASS")
        self.assertEqual(hot_liq_coverage_level(100, 0.34), "WARN")
        self.assertEqual(hot_liq_coverage_level(100, 0.60), "WARN")
        self.assertEqual(hot_liq_coverage_level(HOT_LIQ_FAIL_MIN_N, 0.65), "FAIL")
        self.assertEqual(hot_liq_coverage_level(HOT_LIQ_FAIL_MIN_N - 1, 1.0), "WARN")


class LiquidityCoverageTests(unittest.TestCase):
    def _check(self, store: Store) -> dict:
        report = CheckReport()
        check_hot_liquidity_coverage(store, report)
        self.assertEqual(len(report.checks), 1)
        return report.checks[0]

    def test_empty_db_is_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            row = self._check(store)
            store.close()
            self.assertEqual(row["level"], "WARN")
            self.assertEqual(row["name"], HOT_LIQ_CHECK_NAME)
            self.assertFalse(row["metrics"]["counts_toward_strategy_validity"])
            self.assertEqual(row["metrics"]["evidence_role"], "10-19旁证")
            self.assertEqual(row["metrics"]["hot_1h"]["n"], 0)
            self.assertIsNone(row["metrics"]["hot_1h"]["missing_rate"])
            self.assertIn("不 FAIL", row["detail"])

    def test_buckets_and_live_34pct_is_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=30, liquidity=None, tag="null")
            _insert(store, n=4, liquidity=0, tag="zero")
            _insert(store, n=40, liquidity=1_000, tag="low")
            _insert(store, n=26, liquidity=80_000, tag="high")
            store.add_pending_enrich("solana", "mint")
            row = self._check(store)
            hot = row["metrics"]["hot_1h"]
            store.close()
            self.assertEqual(hot["n"], 100)
            self.assertEqual(hot["missing_n"], 34)
            self.assertEqual(hot["below_min_n"], 40)
            self.assertEqual(hot["ge_min_n"], 26)
            self.assertEqual(
                hot["missing_n"] + hot["below_min_n"] + hot["ge_min_n"],
                hot["n"],
            )
            self.assertEqual(hot["missing_rate"], 0.34)
            self.assertEqual(hot["below_min_rate"], 0.4)
            self.assertEqual(hot["ge_min_rate"], 0.26)
            self.assertEqual(hot["price_present_n"], 100)
            self.assertEqual(row["level"], "WARN")
            self.assertEqual(row["metrics"]["pending_enrich"], 1)
            self.assertFalse(hot["counts_toward_strategy_validity"])

    def test_negative_liquidity_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=1, liquidity=-5, tag="neg")
            cov = liquidity_coverage(
                store._conn,
                since_iso=utc_hours_ago_iso(1),
                tier="hot",
            )
            store.close()
            self.assertEqual(cov["missing_n"], 1)
            self.assertEqual(cov["below_min_n"], 0)

    def test_fail_pass_and_thin_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=20, liquidity=None, tag="gone")
            row = self._check(store)
            self.assertEqual(row["level"], "FAIL")
            self.assertEqual(row["metrics"]["hot_1h"]["missing_rate"], 1.0)
            store.close()

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=12, liquidity=None, tag="miss")
            _insert(store, n=8, liquidity=50_000, tag="ok")
            row = self._check(store)
            store.close()
            self.assertEqual(row["metrics"]["hot_1h"]["missing_rate"], 0.6)
            self.assertEqual(row["level"], "WARN")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=5, liquidity=None, tag="edge")
            _insert(store, n=15, liquidity=60_000, tag="ok")
            row = self._check(store)
            store.close()
            self.assertEqual(row["metrics"]["hot_1h"]["missing_rate"], 0.25)
            self.assertEqual(row["level"], "PASS")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=19, liquidity=None, tag="thin")
            row = self._check(store)
            store.close()
            self.assertEqual(row["level"], "WARN")
            self.assertIn("样本薄", row["detail"])

    def test_tier_and_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=10, liquidity=80_000, tier="hot", age_sec=30, tag="now")
            _insert(store, n=10, liquidity=None, tier="HOT", age_sec=30, tag="case")
            _insert(store, n=30, liquidity=None, tier="warm", age_sec=30, tag="warm")
            _insert(store, n=10, liquidity=None, tier="hot", age_sec=2 * 3600, tag="old")
            _insert(store, n=5, liquidity=None, tier="hot", age_sec=30 * 3600, tag="stale")
            row = self._check(store)
            hot = row["metrics"]["hot_1h"]
            day = row["metrics"]["hot_24h"]
            store.close()
            self.assertEqual(hot["n"], 20)
            self.assertEqual(hot["missing_n"], 10)
            self.assertEqual(hot["missing_rate"], 0.5)
            self.assertEqual(row["level"], "WARN")
            self.assertEqual(day["n"], 30)
            self.assertEqual(day["missing_n"], 20)

    def test_stored_null_from_extract_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            metrics = extract_metrics({"priceUsd": "0.4", "volume": {"h1": 3}})
            store.insert_pair_history(
                "solana",
                "frompayload",
                price_usd=metrics["price_usd"],
                liquidity_usd=metrics["liquidity_usd"],
                tier="hot",
                recorded_at=_iso(10),
            )
            cov = liquidity_coverage(
                store._conn,
                since_iso=utc_hours_ago_iso(1),
                tier="hot",
            )
            store.close()
            self.assertIsNone(metrics["liquidity_usd"])
            self.assertEqual(cov["n"], 1)
            self.assertEqual(cov["missing_n"], 1)
            self.assertEqual(cov["price_present_n"], 1)

    def test_json_output_includes_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            report = CheckReport()
            check_hot_liquidity_coverage(store, report)
            store.close()
            path = write_json(report, Path(tmp))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["checks"][0]["name"], HOT_LIQ_CHECK_NAME)
            self.assertFalse(
                payload["checks"][0]["metrics"]["counts_toward_strategy_validity"]
            )
            self.assertIn("hot_1h", payload["checks"][0]["metrics"])
            self.assertIn("hot_24h", payload["checks"][0]["metrics"])

    def test_run_checks_wires_empty_safe_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            Store(path).close()
            report = run_checks(db=str(path), stale_sec=60)
            hot = next(c for c in report.checks if c["name"] == HOT_LIQ_CHECK_NAME)
            self.assertEqual(hot["level"], "WARN")
            self.assertEqual(hot["metrics"]["hot_1h"]["n"], 0)
            self.assertFalse(hot["metrics"]["counts_toward_strategy_validity"])


class HourlyLineTests(unittest.TestCase):
    def test_line_sits_next_to_health(self) -> None:
        ctx = {
            "generated_at": "2026-10-14T00:00:00Z",
            "enabled_chains": ["solana"],
            "health": {"ok": True, "message": "OK"},
            "stats": {
                "pending_enrich": 13750,
                "tokens": 1,
                "hot_liq_1h_n": 100,
                "hot_liq_1h_missing_rate": 0.34,
            },
            "hot_liquidity_1h": {
                "n": 100,
                "missing_rate": 0.34,
                "below_min_rate": 0.4,
                "ge_min_rate": 0.26,
                "counts_toward_strategy_validity": False,
            },
            "top_scores": [],
            "low_scores": [],
            "history_samples": [],
            "recent_alerts": [],
        }
        md = to_markdown(ctx)
        health_at = md.index("Health:")
        line_at = md.index("Hot liq 1h")
        stats_at = md.index("## Stats")
        self.assertLess(health_at, line_at)
        self.assertLess(line_at, stats_at)
        self.assertIn("missing=34.0%", md)
        self.assertIn("pending_enrich=13750", md)
        self.assertIn("不计有效性", md)
        self.assertIn('"hot_liq_1h_missing_rate"', md)

    def test_build_context_exports_hot_liq_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            _insert(store, n=2, liquidity=None, tag="miss")
            _insert(store, n=2, liquidity=80_000, tag="ok")
            store.add_pending_enrich("solana", "mint")
            ctx = build_context(store, top_n=1, alerts_n=1, history_pairs=1)
            store.close()
            self.assertEqual(ctx["stats"]["pending_enrich"], 1)
            self.assertEqual(ctx["stats"]["hot_liq_1h_n"], 4)
            self.assertEqual(ctx["stats"]["hot_liq_1h_missing_rate"], 0.5)
            self.assertFalse(ctx["hot_liquidity_1h"]["counts_toward_strategy_validity"])
            md = to_markdown(ctx)
            self.assertLess(md.index("Health:"), md.index("Hot liq 1h"))
            self.assertIn("missing=50.0%", md)
            self.assertIn("pending_enrich=1", md)

    def test_missing_coverage_is_unavailable(self) -> None:
        line = format_hot_liq_operator_line(None, 3)
        self.assertIn("unavailable", line)
        self.assertIn("pending_enrich=3", line)


if __name__ == "__main__":
    unittest.main()
