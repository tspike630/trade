#!/usr/bin/env python3
"""Tiny-fixture tests for same-window JEV signal crowding."""

from __future__ import annotations

import csv
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import signal_crowding as crowd  # noqa: E402
import weekly_side_evidence as wse  # noqa: E402

AS_OF = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
STRATEGY = "jev_strength_v1"


def _buy(
    ts: str,
    *,
    purpose: str | None = "live_candidate",
    chain: str = "solana",
    token: str = "mintA",
) -> crowd.CrowdBuy:
    return crowd.CrowdBuy(
        ts=ts,
        purpose=purpose,
        chain_id=chain,
        token_address=token,
    )


class MetricTests(unittest.TestCase):
    def test_hkt_day_and_hour_cross_utc_midnight(self) -> None:
        # 15:30Z is 23:30 HKT on the 14th. 16:30Z is 00:30 HKT on the 15th.
        self.assertEqual(crowd.hkt_day("2026-10-14T15:30:00Z"), "2026-10-14")
        self.assertEqual(crowd.hkt_hour("2026-10-14T15:30:00Z"), "2026-10-14 23")
        self.assertEqual(crowd.hkt_day("2026-10-14T16:30:00Z"), "2026-10-15")
        self.assertEqual(crowd.hkt_hour("2026-10-14T16:30:00Z"), "2026-10-15 00")

    def test_buys_per_hkt_day_uses_window_length(self) -> None:
        rows = [
            _buy("2026-10-14T02:00:00Z", token="a"),
            _buy("2026-10-14T03:00:00Z", token="a"),
            _buy("2026-10-14T04:00:00Z", token="b"),
            _buy("2026-10-14T05:00:00Z", token="b"),
        ]
        report = crowd.observe(rows, as_of=AS_OF, window_days=7, strategy_id=STRATEGY)
        live = report.cohort("live_candidate")
        assert live is not None
        self.assertEqual(live.n_buys, 4)
        self.assertAlmostEqual(live.buys_per_hkt_day or 0, 4 / 7)
        self.assertEqual(live.day_counts, (("2026-10-14", 4),))
        self.assertEqual(live.peak_day, "2026-10-14")
        self.assertEqual(live.peak_day_buys, 4)

    def test_unique_per_buy_and_peak_hour_density(self) -> None:
        # Three buys in 10:00 HKT, one in 11:00 HKT. Two distinct tokens.
        # 02:00Z = 10:00 HKT, 03:00Z = 11:00 HKT.
        rows = [
            _buy("2026-10-14T02:00:00Z", token="a"),
            _buy("2026-10-14T02:10:00Z", token="a"),
            _buy("2026-10-14T02:20:00Z", token="b"),
            _buy("2026-10-14T03:00:00Z", token="b"),
        ]
        row = crowd.cohort_metrics("live_candidate", rows, window_days=7)
        self.assertEqual(row.unique_tokens, 2)
        self.assertAlmostEqual(row.unique_per_buy or 0, 0.5)
        self.assertEqual(row.peak_hour, "2026-10-14 10")
        self.assertEqual(row.peak_hour_buys, 3)
        self.assertAlmostEqual(row.peak_hour_density or 0, 0.75)

    def test_chain_splits_the_same_address(self) -> None:
        rows = [
            _buy("2026-10-14T02:00:00Z", chain="solana", token="mint"),
            _buy("2026-10-14T02:05:00Z", chain="base", token="mint"),
        ]
        row = crowd.cohort_metrics("live_candidate", rows, window_days=7)
        self.assertEqual(row.unique_tokens, 2)
        self.assertAlmostEqual(row.unique_per_buy or 0, 1.0)

    def test_peak_hour_tie_takes_the_later_hour(self) -> None:
        rows = [
            _buy("2026-10-14T02:00:00Z", token="a"),
            _buy("2026-10-14T02:30:00Z", token="b"),
            _buy("2026-10-14T03:00:00Z", token="a"),
            _buy("2026-10-14T03:30:00Z", token="b"),
        ]
        row = crowd.cohort_metrics("live_candidate", rows, window_days=7)
        self.assertEqual(row.peak_hour_buys, 2)
        self.assertEqual(row.peak_hour, "2026-10-14 11")
        self.assertAlmostEqual(row.peak_hour_density or 0, 0.5)

    def test_empty_cohort_rates_are_null_and_daily_rate_is_zero(self) -> None:
        row = crowd.cohort_metrics("sample", (), window_days=7)
        self.assertEqual(row.n_buys, 0)
        self.assertEqual(row.buys_per_hkt_day, 0.0)
        self.assertIsNone(row.unique_per_buy)
        self.assertIsNone(row.peak_hour_density)
        self.assertEqual(row.peak_hour_buys, 0)
        self.assertEqual(row.peak_day_buys, 0)
        self.assertEqual(row.day_counts, ())

    def test_purposes_are_not_pooled(self) -> None:
        rows = [
            _buy("2026-10-14T02:00:00Z", purpose="live_candidate", token="a"),
            _buy("2026-10-14T02:05:00Z", purpose="live_candidate", token="a"),
            _buy("2026-10-14T02:10:00Z", purpose="sample", token="b"),
            _buy("2026-10-14T08:00:00Z", purpose=" sample ", token="c"),
        ]
        report = crowd.observe(rows, as_of=AS_OF, window_days=7, strategy_id=STRATEGY)
        live = report.cohort("live_candidate")
        sample = report.cohort("sample")
        assert live is not None and sample is not None
        self.assertEqual(live.n_buys, 2)
        self.assertEqual(live.unique_tokens, 1)
        self.assertAlmostEqual(live.unique_per_buy or 0, 0.5)
        self.assertEqual(sample.n_buys, 2)
        self.assertEqual(sample.unique_tokens, 2)
        self.assertAlmostEqual(sample.unique_per_buy or 0, 1.0)
        labels = [row.label for row in report.cohorts]
        self.assertEqual(labels, ["live_candidate", "sample"])

    def test_other_purpose_is_its_own_row(self) -> None:
        rows = [
            _buy("2026-10-14T02:00:00Z", purpose="SAMPLE", token="a"),
            _buy("2026-10-14T02:05:00Z", purpose=None, token="b"),
            _buy("2026-10-14T02:10:00Z", purpose="  ", token="c"),
        ]
        report = crowd.observe(rows, as_of=AS_OF, window_days=7, strategy_id=STRATEGY)
        live = report.cohort("live_candidate")
        sample = report.cohort("sample")
        upper = report.cohort("SAMPLE")
        missing = report.cohort("purpose_missing")
        assert live is not None and sample is not None
        assert upper is not None and missing is not None
        self.assertEqual(live.n_buys, 0)
        self.assertEqual(sample.n_buys, 0)
        self.assertEqual(upper.n_buys, 1)
        self.assertEqual(missing.n_buys, 2)
        self.assertEqual(
            [row.label for row in report.cohorts],
            ["live_candidate", "sample", "SAMPLE", "purpose_missing"],
        )

    def test_window_is_inclusive_and_matches_hard_side_evidence(self) -> None:
        hard = wse.hard_side_from_positions([], as_of=AS_OF, window_days=7)
        inside_start = _buy(hard.window_start, token="edge")
        inside_end = _buy(hard.window_end, token="end")
        before = _buy("2026-10-01T00:00:00Z", token="old")
        report = crowd.observe(
            [inside_start, inside_end, before, _buy("not-a-timestamp")],
            as_of=AS_OF,
            window_days=7,
            strategy_id=STRATEGY,
        )
        self.assertEqual(report.window_start, hard.window_start)
        self.assertEqual(report.window_end, hard.window_end)
        live = report.cohort("live_candidate")
        assert live is not None
        self.assertEqual(live.n_buys, 2)
        self.assertEqual(report.n_bad_ts, 1)
        self.assertEqual(wse.STOP_LOSS_SHARE_MIN, 0.50)
        self.assertEqual(wse.DAILY_OPENS_MIN, 20)

    def test_naive_timestamp_is_utc(self) -> None:
        # 16:00 naive is 16:00Z = 00:00 next HKT day, not 16:00 HKT.
        row = crowd.cohort_metrics(
            "live_candidate",
            [_buy("2026-10-14T16:00:00")],
            window_days=7,
        )
        self.assertEqual(row.peak_hour, "2026-10-15 00")
        self.assertEqual(row.peak_day, "2026-10-15")


class ReportTests(unittest.TestCase):
    def test_markdown_labels_both_and_does_not_veto(self) -> None:
        report = crowd.observe(
            [
                _buy("2026-10-14T02:00:00Z", purpose="live_candidate"),
                _buy("2026-10-14T06:00:00Z", purpose="sample", token="s"),
            ],
            as_of=AS_OF,
            window_days=7,
            strategy_id=STRATEGY,
        )
        text = "\n".join(crowd.render_markdown(report))
        self.assertIn("live_candidate", text)
        self.assertIn("sample", text)
        self.assertIn("不否决 Step2", text)
        self.assertIn("只观察", text)
        self.assertIn("2026-10-14", text)
        self.assertNotIn("不可启动 Step2", text)
        stdout = crowd.format_stdout(report)[0]
        self.assertIn("live_candidate n=1", stdout)
        self.assertIn("sample n=1", stdout)
        self.assertIn("不否决 Step2", stdout)

    def test_unavailable_is_blank_not_zero(self) -> None:
        report = crowd.unavailable_report(
            as_of=AS_OF,
            window_days=7,
            strategy_id=STRATEGY,
            note="decision_log read failed (OperationalError)",
        )
        self.assertFalse(report.available)
        fields = crowd.trend_fields(report)
        self.assertTrue(all(value == "" for value in fields.values()))
        self.assertNotIn("step2_blocked", fields)
        text = "\n".join(crowd.render_markdown(report))
        self.assertIn("未形成", text)
        self.assertIn("不把缺数写成 0", text)

    def test_trend_cells_for_both_labels(self) -> None:
        report = crowd.observe(
            [
                _buy("2026-10-14T02:00:00Z", purpose="live_candidate", token="a"),
                _buy("2026-10-14T02:10:00Z", purpose="live_candidate", token="a"),
                _buy("2026-10-14T08:00:00Z", purpose="sample", token="b"),
            ],
            as_of=AS_OF,
            window_days=7,
            strategy_id=STRATEGY,
        )
        fields = crowd.trend_fields(report)
        self.assertEqual(set(fields), set(crowd.TREND_KEYS))
        self.assertEqual(fields["crowd_lc_n"], "2")
        self.assertEqual(fields["crowd_sample_n"], "1")
        self.assertEqual(fields["crowd_lc_unique_per_buy"], "0.500000")
        self.assertEqual(fields["crowd_lc_peak_hour_density"], "1.000000")
        self.assertEqual(fields["crowd_sample_unique_per_buy"], "1.000000")
        self.assertEqual(fields["crowd_lc_peak_day_buys"], "2")
        self.assertEqual(crowd.trend_fields(None)["crowd_lc_n"], "")


class SqliteTests(unittest.TestCase):
    def test_loader_labels_column_purpose_and_skips_other_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decision.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    strategy_id TEXT,
                    action TEXT,
                    chain_id TEXT,
                    token_address TEXT,
                    purpose TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, chain_id, token_address, purpose, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "2026-10-14T02:00:00Z",
                        STRATEGY,
                        "buy",
                        "solana",
                        "mintLive",
                        "live_candidate",
                        '{"purpose":"sample"}',
                    ),
                    (
                        "2026-10-14T03:00:00Z",
                        STRATEGY,
                        "buy",
                        "solana",
                        "mintSample",
                        None,
                        '{"purpose":"sample","chain_id":"ignored"}',
                    ),
                    (
                        "2026-10-14T04:00:00Z",
                        STRATEGY,
                        "sell",
                        "solana",
                        "mintSell",
                        "live_candidate",
                        "{}",
                    ),
                    (
                        "2026-10-14T05:00:00Z",
                        "other_strategy",
                        "buy",
                        "solana",
                        "mintOther",
                        "live_candidate",
                        "{}",
                    ),
                    (
                        "2026-10-01T00:00:00Z",
                        STRATEGY,
                        "buy",
                        "solana",
                        "mintOld",
                        "live_candidate",
                        "{}",
                    ),
                ],
            )
            conn.commit()
            report = crowd.compute_for_weekly(
                decision_conn=conn,
                strategy_id=STRATEGY,
                window_days=7,
                as_of=AS_OF,
            )
            conn.close()
        self.assertTrue(report.available)
        live = report.cohort("live_candidate")
        sample = report.cohort("sample")
        assert live is not None and sample is not None
        self.assertEqual(live.n_buys, 1)
        self.assertEqual(live.unique_tokens, 1)
        self.assertEqual(sample.n_buys, 1)
        self.assertEqual(sample.day_counts, (("2026-10-14", 1),))

    def test_missing_columns_fall_back_to_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    strategy_id TEXT,
                    action TEXT,
                    raw_json TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO decision_log (ts, strategy_id, action, raw_json)
                VALUES (?, ?, 'buy', ?)
                """,
                (
                    "2026-10-14T02:00:00Z",
                    STRATEGY,
                    '{"purpose":"live_candidate","chain_id":"solana","token_address":"fromRaw"}',
                ),
            )
            conn.commit()
            buys = crowd.load_strategy_buys(conn, STRATEGY)
            conn.close()
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].purpose, "live_candidate")
        self.assertEqual(buys[0].chain_id, "solana")
        self.assertEqual(buys[0].token_address, "fromRaw")


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = _SCRIPTS / "jev_validity.py"
        spec = importlib.util.spec_from_file_location("jev_validity_crowd_test", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        cls.jev = mod

    def test_trend_row_keeps_step2_veto_independent_of_crowding(self) -> None:
        report = crowd.observe(
            [_buy("2026-10-14T02:00:00Z", token="a")] * 5,
            as_of=AS_OF,
            window_days=7,
            strategy_id=STRATEGY,
        )
        stats = self.jev.HorizonStats()
        result = {
            "n_raw": 0,
            "n_filt": 0,
            "unique_token": 0,
            "st1": stats,
            "st4": stats,
            "strata": {"0.19+": {"n_filt": 0, "1h": stats}},
        }
        row = self.jev.build_trend_row(result, side=None, crowd=report)
        self.assertEqual(row["step2_blocked"], "0")
        self.assertEqual(row["crowd_lc_n"], "5")
        self.assertEqual(row["crowd_sample_n"], "0")
        self.assertIn("crowd_lc_peak_hour_density", self.jev.TREND_FIELDS)
        text = (_SCRIPTS / "signal_crowding.py").read_text(encoding="utf-8")
        self.assertNotIn("risk_manager", text)
        self.assertNotIn("DECISION_COOLDOWN_SEC", text)
        self.assertNotIn("JEV_MIN_STRENGTH", text)

    def test_main_prints_crowding_without_touching_step2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "dex.db"
            out = root / "report.md"
            trend = root / "trend.csv"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE decision_log (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    strategy_id TEXT,
                    action TEXT,
                    chain_id TEXT,
                    token_address TEXT,
                    reason TEXT,
                    mode TEXT,
                    purpose TEXT,
                    raw_json TEXT
                )
                """
            )
            fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                INSERT INTO decision_log
                    (ts, strategy_id, action, chain_id, token_address, reason, mode, purpose, raw_json)
                VALUES (?, ?, 'buy', 'solana', 'mintA', 'jev_strength=0.01', 'dry_run', 'live_candidate', '{}')
                """,
                (fresh, STRATEGY),
            )
            conn.commit()
            conn.close()
            code = self.jev.main(
                [
                    "--db",
                    str(db),
                    "--out",
                    str(out),
                    "--trend-csv",
                    str(trend),
                    "--no-contrast",
                    "--no-side-evidence",
                ]
            )
            self.assertEqual(code, 0)
            text = out.read_text(encoding="utf-8")
            self.assertIn("## 信号拥挤度（只观察）", text)
            self.assertIn("`live_candidate`", text)
            self.assertIn("`sample`", text)
            self.assertIn("不否决 Step2", text)
            self.assertIn("暂缓据此开 Step2", text)
            self.assertNotIn("硬旁证否决", text)
            with trend.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[-1]["crowd_lc_n"], "1")
            self.assertEqual(rows[-1]["step2_blocked"], "0")
            self.assertEqual(rows[-1]["crowd_sample_n"], "0")


if __name__ == "__main__":
    unittest.main()
