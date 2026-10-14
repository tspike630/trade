#!/usr/bin/env python3
"""Tiny-fixture tests for weekly hard side-evidence and F-1 streaks."""

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

import weekly_side_evidence as wse  # noqa: E402


def _pos(
    *,
    pnl: float | None = None,
    reason: str | None = None,
    opened: str = "2026-10-14T02:00:00Z",
    closed: str | None = "2026-10-14T03:00:00Z",
    status: str = "closed",
    decision_id: int | None = None,
    strategy_id: str | None = None,
) -> wse.PaperPosition:
    return wse.PaperPosition(
        opened_at=opened,
        closed_at=closed,
        status=status,
        exit_reason=reason,
        pnl_usd=pnl,
        decision_log_id=decision_id,
        strategy_id=strategy_id,
    )


def _out(
    i: int,
    *,
    ret: float | None = None,
    pnl: float | None = None,
    executed: bool = False,
    purpose: str | None = "live_candidate",
    strategy: str = "jev_strength_v1",
    source: str | None = None,
    ts: str | None = None,
) -> wse.SignalOutcome:
    return wse.SignalOutcome(
        decision_id=i,
        ts=ts or f"2026-10-14T00:00:{i:02d}Z",
        strategy_id=strategy,
        purpose=purpose,
        signal_source_id=source,
        ret_1h=ret,
        realized_pnl=pnl,
        paper_executed=executed,
    )


AS_OF = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


class StopLossShareTests(unittest.TestCase):
    def test_share_matches_dashboard_net_book(self) -> None:
        # stop_loss -96 and another loss -4: 96% of a net -100 book.
        rows = [
            _pos(pnl=-96, reason="stop_loss"),
            _pos(pnl=-4, reason="max_hold"),
        ]
        share, stop_net, total, n = wse.stop_loss_pnl_share(rows)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(stop_net or 0, -96.0)
        self.assertAlmostEqual(total, -100.0)
        self.assertAlmostEqual(share or 0, 0.96)

    def test_winner_can_cap_share_at_one(self) -> None:
        # Net book -60, stop_loss bucket -96 → dashboard caps the ratio at 1.
        rows = [
            _pos(pnl=-96, reason="stop_loss"),
            _pos(pnl=-4, reason="max_hold"),
            _pos(pnl=40, reason="take_profit"),
        ]
        share, stop_net, total, _n = wse.stop_loss_pnl_share(rows)
        self.assertAlmostEqual(stop_net or 0, -96.0)
        self.assertAlmostEqual(total, -60.0)
        self.assertAlmostEqual(share or 0, 1.0)

    def test_positive_stop_loss_nets_into_the_bucket(self) -> None:
        rows = [
            _pos(pnl=10, reason="stop_loss"),
            _pos(pnl=-50, reason="stop_loss"),
            _pos(pnl=-50, reason="max_hold"),
        ]
        share, stop_net, total, _n = wse.stop_loss_pnl_share(rows)
        self.assertAlmostEqual(stop_net or 0, -40.0)
        self.assertAlmostEqual(total, -90.0)
        self.assertAlmostEqual(share or 0, 40.0 / 90.0)

    def test_empty_loss_book_is_null(self) -> None:
        share, _stop, _loss, n = wse.stop_loss_pnl_share(
            [_pos(pnl=3, reason="take_profit"), _pos(pnl=None, reason="stop_loss")]
        )
        self.assertIsNone(share)
        self.assertEqual(n, 1)

    def test_reason_is_exact_stop_loss(self) -> None:
        share, stop_net, total, _n = wse.stop_loss_pnl_share(
            [_pos(pnl=-10, reason="Stop_Loss")]
        )
        self.assertIsNone(stop_net)
        self.assertAlmostEqual(total, -10.0)
        self.assertIsNone(share)

    def test_flag_threshold_is_inclusive(self) -> None:
        half = [
            _pos(pnl=-1, reason="stop_loss"),
            _pos(pnl=-1, reason="max_hold"),
        ]
        under = [
            _pos(pnl=-49, reason="stop_loss"),
            _pos(pnl=-51, reason="max_hold"),
        ]
        at = wse.hard_side_from_positions(half, as_of=AS_OF, window_days=7)
        below = wse.hard_side_from_positions(under, as_of=AS_OF, window_days=7)
        self.assertTrue(at.flag_stop_loss_share)
        self.assertAlmostEqual(at.stop_loss_pnl_share or 0, 0.50)
        self.assertFalse(below.flag_stop_loss_share)
        self.assertTrue(at.flag_map()["hard_旁证_stop_loss_share"])
        self.assertFalse(below.flag_map()["hard_旁证_stop_loss_share"])


class DailyOpenTests(unittest.TestCase):
    def test_hkt_calendar_not_utc_date(self) -> None:
        # 15:30Z is still 14th in HKT; 16:30Z is the 15th.
        opened = ["2026-10-14T15:30:00Z"] * 10 + ["2026-10-14T16:30:00Z"] * 20
        peak, day = wse.max_daily_opens(opened)
        self.assertEqual(peak, 20)
        self.assertEqual(day, "2026-10-15")

    def test_twenty_opens_flags_nineteen_does_not(self) -> None:
        def rows(n: int) -> list[wse.PaperPosition]:
            return [_pos(pnl=1, reason="take_profit", status="open", closed=None) for _ in range(n)]

        at = wse.hard_side_from_positions(rows(20), as_of=AS_OF, window_days=7)
        under = wse.hard_side_from_positions(rows(19), as_of=AS_OF, window_days=7)
        self.assertEqual(at.max_daily_opens, 20)
        self.assertTrue(at.flag_daily_opens)
        self.assertTrue(at.flag_map()["hard_旁证_daily_opens"])
        self.assertFalse(under.flag_daily_opens)
        self.assertTrue(at.step2_blocked)
        self.assertFalse(under.step2_blocked)

    def test_window_drops_older_closes_and_opens(self) -> None:
        rows = [
            _pos(
                pnl=-80,
                reason="stop_loss",
                opened="2026-10-10T00:00:00Z",
                closed="2026-10-10T00:00:00Z",
            ),
            _pos(
                pnl=-20,
                reason="max_hold",
                opened="2026-10-11T00:00:00Z",
                closed="2026-10-11T00:00:00Z",
            ),
            _pos(
                pnl=-1000,
                reason="stop_loss",
                opened="2026-10-01T00:00:00Z",
                closed="2026-10-01T01:00:00Z",
            ),
        ]
        rows.extend(
            _pos(pnl=None, reason=None, status="open", closed=None, opened="2026-10-01T00:00:00Z")
            for _ in range(5)
        )
        rows.extend(
            _pos(pnl=None, reason=None, status="open", closed=None)
            for _ in range(19)
        )
        hard = wse.hard_side_from_positions(rows, as_of=AS_OF, window_days=7)
        self.assertAlmostEqual(hard.stop_loss_pnl_share or 0, 0.80)
        self.assertEqual(hard.max_daily_opens, 19)
        self.assertTrue(hard.flag_stop_loss_share)
        self.assertFalse(hard.flag_daily_opens)
        self.assertTrue(hard.step2_blocked)

    def test_either_flag_blocks(self) -> None:
        share_only = wse.hard_side_from_positions(
            [_pos(pnl=-8, reason="stop_loss"), _pos(pnl=-2, reason="max_hold")],
            as_of=AS_OF,
        )
        opens_only = wse.hard_side_from_positions(
            [_pos(pnl=1, reason="take_profit", status="open", closed=None) for _ in range(20)],
            as_of=AS_OF,
        )
        self.assertTrue(share_only.step2_blocked)
        self.assertFalse(share_only.flag_daily_opens)
        self.assertTrue(opens_only.step2_blocked)
        self.assertFalse(opens_only.flag_stop_loss_share)

    def test_missing_ledger_does_not_veto(self) -> None:
        hard = wse.unavailable_hard(as_of=AS_OF, window_days=7, note="paper db missing")
        self.assertFalse(hard.available)
        self.assertFalse(hard.step2_blocked)
        self.assertIsNone(hard.stop_loss_pnl_share)


class StreakTests(unittest.TestCase):
    def test_overlapping_third_loss(self) -> None:
        # L L W L L L → trials at the W and at the last L.
        flags = [True, True, False, True, True, True]
        max_streak, trials, third, p = wse.streak_stats(flags)
        self.assertEqual(max_streak, 3)
        self.assertEqual(trials, 2)
        self.assertEqual(third, 1)
        self.assertAlmostEqual(p or 0, 0.5)

    def test_two_losses_without_a_third_have_no_probability(self) -> None:
        max_streak, trials, third, p = wse.streak_stats([True, True])
        self.assertEqual(max_streak, 2)
        self.assertEqual(trials, 0)
        self.assertEqual(third, 0)
        self.assertIsNone(p)

    def test_unresolved_breaks_the_streak(self) -> None:
        max_streak, trials, _third, p = wse.streak_stats([True, None, True, True])
        self.assertEqual(max_streak, 2)
        self.assertEqual(trials, 0)
        self.assertIsNone(p)

    def test_three_losses_is_one_trial(self) -> None:
        max_streak, trials, third, p = wse.streak_stats([True, True, True])
        self.assertEqual(max_streak, 3)
        self.assertEqual(trials, 1)
        self.assertEqual(third, 1)
        self.assertAlmostEqual(p or 0, 1.0)

    def test_no_losses(self) -> None:
        max_streak, trials, _third, p = wse.streak_stats([False, False, False])
        self.assertEqual(max_streak, 0)
        self.assertEqual(trials, 0)
        self.assertIsNone(p)

    def test_up_1h_beats_realized_sign(self) -> None:
        row = _out(1, ret=0.02, pnl=-10, executed=True)
        self.assertIs(row.loss_flag(), False)
        fallback = _out(2, ret=None, pnl=-1, executed=True)
        self.assertIs(fallback.loss_flag(), True)
        flat = _out(3, ret=0.0, pnl=-1, executed=True)
        self.assertIs(flat.loss_flag(), False)
        pending = _out(4, ret=None, pnl=None, executed=True)
        self.assertIsNone(pending.loss_flag())

    def test_paper_executed_cohort_ignores_other_buys(self) -> None:
        rows = [
            _out(1, ret=-0.1, executed=True),
            _out(2, ret=-0.2, executed=True),
            _out(3, ret=-0.3, executed=True),
            _out(4, ret=0.4, executed=False, purpose="sample"),
        ]
        groups = wse.observe_streaks(rows, strategy_id="jev_strength_v1")
        primary = groups[0]
        self.assertIsNone(primary.signal_source_id)
        self.assertEqual(primary.cohort, "paper_executed")
        self.assertEqual(primary.n_resolved, 3)
        self.assertEqual(primary.max_streak, 3)
        self.assertEqual(primary.trials_after_two, 1)
        self.assertAlmostEqual(primary.p_third_given_two or 0, 1.0)

    def test_live_candidate_when_nothing_was_filled(self) -> None:
        rows = [
            _out(1, ret=-0.1, purpose="live_candidate"),
            _out(2, ret=0.2, purpose="live_candidate"),
            _out(3, ret=-0.3, purpose="sample"),
        ]
        group = wse.observe_streaks(rows, strategy_id="jev_strength_v1")[0]
        self.assertEqual(group.cohort, "purpose=live_candidate")
        self.assertEqual(group.n_resolved, 2)
        self.assertEqual(group.max_streak, 1)

    def test_source_split_does_not_replace_strategy_row(self) -> None:
        rows = [
            _out(1, ret=-0.1, source="alpha"),
            _out(2, ret=-0.1, source="alpha"),
            _out(3, ret=0.2, source="beta"),
        ]
        groups = wse.observe_streaks(rows, strategy_id="jev_strength_v1")
        self.assertEqual(len(groups), 3)
        self.assertIsNone(groups[0].signal_source_id)
        self.assertEqual(groups[0].n_resolved, 3)
        sources = {group.signal_source_id: group for group in groups[1:]}
        self.assertEqual(sources["alpha"].max_streak, 2)
        self.assertEqual(sources["beta"].max_streak, 0)

    def test_strategies_do_not_share_a_streak(self) -> None:
        rows = [
            _out(1, ret=-0.1, strategy="jev_strength_v1"),
            _out(2, ret=-0.1, strategy="tech_momentum_v1"),
            _out(3, ret=-0.1, strategy="jev_strength_v1"),
        ]
        groups = wse.observe_streaks(rows, strategy_id="jev_strength_v1")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].max_streak, 2)
        self.assertEqual(groups[0].n_resolved, 2)


class SqliteFixtureTests(unittest.TestCase):
    def test_weekly_pass_reads_paper_positions_and_decision_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paper = root / "paper_trading.db"
            decision = root / "dexscreener.db"
            self._write_paper(paper)
            self._write_decisions(decision)
            conn = sqlite3.connect(f"file:{decision}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                side = wse.compute_for_weekly(
                    decision_conn=conn,
                    ret_1h_by_id={1: -0.01, 2: -0.02, 3: 0.03, 4: -0.04},
                    strategy_id="jev_strength_v1",
                    paper_db=paper,
                    window_days=7,
                    as_of=AS_OF,
                )
            finally:
                conn.close()
        self.assertTrue(side.hard.available)
        # stop_loss -96, max_hold -4, take_profit +12 → net -88, ratio capped at 1.
        self.assertAlmostEqual(side.hard.stop_loss_pnl_share or 0, 1.0)
        self.assertAlmostEqual(side.hard.total_pnl_usd or 0, -88.0)
        self.assertTrue(side.hard.flag_stop_loss_share)
        self.assertGreaterEqual(side.hard.max_daily_opens or 0, 20)
        self.assertTrue(side.hard.flag_daily_opens)
        self.assertTrue(side.hard.step2_blocked)
        self.assertIsNotNone(side.f1_primary)
        assert side.f1_primary is not None
        self.assertEqual(side.f1_primary.cohort, "paper_executed")
        # Executed ids 1,2,3 are L L W. The sample buy is not in the cohort.
        self.assertEqual(side.f1_primary.max_streak, 2)
        self.assertEqual(side.f1_primary.trials_after_two, 1)
        self.assertAlmostEqual(side.f1_primary.p_third_given_two or 0, 0.0)
        text = "\n".join(wse.render_markdown(side))
        self.assertIn("## F-1 信号连亏（只记不熔断）", text)
        self.assertIn("不可启动 Step2", text)
        self.assertIn(wse.STOP_LOSS_SHARE_FORMULA, text)
        self.assertIn("不调用 RiskManager", text)
        fields = wse.trend_fields(side)
        self.assertEqual(fields["hard_旁证_stop_loss_share"], "1")
        self.assertEqual(fields["hard_旁证_daily_opens"], "1")
        self.assertEqual(fields["step2_blocked"], "1")
        self.assertEqual(fields["f1_max_streak"], "2")
        self.assertEqual(fields["side_evidence"], "ok")

    def test_missing_paper_db_is_unavailable(self) -> None:
        side = wse.compute_for_weekly(
            decision_conn=None,
            ret_1h_by_id={},
            strategy_id="jev_strength_v1",
            paper_db=Path("/tmp/does-not-exist-weekly-side.db"),
            as_of=AS_OF,
        )
        self.assertFalse(side.hard.available)
        self.assertFalse(side.hard.step2_blocked)
        self.assertEqual(wse.trend_fields(side)["side_evidence"], "unavailable")
        self.assertEqual(wse.trend_fields(side)["step2_blocked"], "0")

    def _write_paper(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE positions (
                id TEXT PRIMARY KEY,
                opened_at TEXT,
                closed_at TEXT,
                status TEXT,
                exit_reason TEXT,
                pnl_usd REAL,
                meta_json TEXT
            )
            """
        )
        for i in range(20):
            conn.execute(
                """
                INSERT INTO positions
                    (id, opened_at, closed_at, status, exit_reason, pnl_usd, meta_json)
                VALUES (?, ?, ?, 'open', NULL, NULL, ?)
                """,
                (
                    f"open-{i}",
                    "2026-10-14T01:00:00Z",
                    None,
                    '{"strategy_id":"jev_strength_v1"}',
                ),
            )
        conn.execute(
            """
            INSERT INTO positions
                (id, opened_at, closed_at, status, exit_reason, pnl_usd, meta_json)
            VALUES ('c1', '2026-10-13T01:00:00Z', '2026-10-13T02:00:00Z',
                    'closed', 'stop_loss', -96, '{"decision_log_id":1}')
            """
        )
        conn.execute(
            """
            INSERT INTO positions
                (id, opened_at, closed_at, status, exit_reason, pnl_usd, meta_json)
            VALUES ('c2', '2026-10-13T03:00:00Z', '2026-10-13T04:00:00Z',
                    'closed', 'max_hold', -4, '{"decision_log_id":2}')
            """
        )
        conn.execute(
            """
            INSERT INTO positions
                (id, opened_at, closed_at, status, exit_reason, pnl_usd, meta_json)
            VALUES ('c3', '2026-10-13T05:00:00Z', '2026-10-13T06:00:00Z',
                    'closed', 'take_profit', 12, '{"decision_log_id":3}')
            """
        )
        conn.commit()
        conn.close()

    def _write_decisions(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE decision_log (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                strategy_id TEXT,
                action TEXT,
                purpose TEXT,
                raw_json TEXT
            )
            """
        )
        rows = [
            (1, "live_candidate", "{}"),
            (2, "live_candidate", "{}"),
            (3, "live_candidate", "{}"),
            (4, "sample", '{"signal_source_id":"sample-src"}'),
        ]
        for did, purpose, raw in rows:
            conn.execute(
                """
                INSERT INTO decision_log (id, ts, strategy_id, action, purpose, raw_json)
                VALUES (?, ?, 'jev_strength_v1', 'buy', ?, ?)
                """,
                (did, f"2026-10-13T00:00:0{did}Z", purpose, raw),
            )
        conn.commit()
        conn.close()


class DecisionLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = _SCRIPTS / "jev_validity.py"
        spec = importlib.util.spec_from_file_location("jev_validity_weekly_test", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        cls.jev = mod

    def test_sample_gates_still_promote_without_veto(self) -> None:
        text = self.jev.decision_line(20, 0.60, 12, two_week_ok=True, hard_veto=False)
        self.assertIn("可进 Step2", text)
        self.assertNotIn("不可启动 Step2", text)

    def test_hard_veto_blocks_promote(self) -> None:
        text = self.jev.decision_line(20, 0.60, 12, two_week_ok=True, hard_veto=True)
        self.assertIn("不可启动 Step2", text)
        self.assertNotIn("可进 Step2", text)
        self.assertIn("连续2周门槛：满足", text)

    def test_trend_rewrite_keeps_old_rows_and_adds_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jev_validity_trend.csv"
            path.write_text(
                "date,raw_n,filtered_n,unique_token,up_1h_rate,up_4h_rate,"
                "median_1h,median_4h,n_019plus,up_1h_019plus,n_1h,n_4h\n"
                "2026-10-10,40,14,9,0.714286,0.571429,0.010000,0.020000,4,1.000000,14,14\n",
                encoding="utf-8",
            )
            row = {key: "" for key in self.jev.TREND_FIELDS}
            row.update(
                {
                    "date": "2026-10-14",
                    "raw_n": "20",
                    "filtered_n": "16",
                    "unique_token": "12",
                    "up_1h_rate": "0.600000",
                    "step2_blocked": "1",
                    "hard_旁证_stop_loss_share": "1",
                    "hard_旁证_daily_opens": "0",
                    "side_evidence": "ok",
                }
            )
            prior = self.jev.append_trend_row(path, row)
            self.assertEqual(len(prior), 1)
            with path.open(encoding="utf-8", newline="") as handle:
                written = list(csv.DictReader(handle))
            self.assertEqual(written[0]["date"], "2026-10-10")
            self.assertEqual(written[0]["unique_token"], "9")
            self.assertEqual(written[0]["step2_blocked"], "")
            self.assertEqual(written[1]["step2_blocked"], "1")
            self.assertIn("hard_旁证_daily_opens", written[1])


class GuardTests(unittest.TestCase):
    def test_module_does_not_touch_risk_manager(self) -> None:
        text = (_SCRIPTS / "weekly_side_evidence.py").read_text(encoding="utf-8")
        self.assertNotIn("decision_engine.core.risk_manager", text)
        self.assertNotIn("import risk_manager", text)
        self.assertNotIn("position_manager", text)
        self.assertNotIn("DECISION_COOLDOWN_SEC", text)


if __name__ == "__main__":
    unittest.main()
