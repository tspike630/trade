"""Gateway + shadow runner jev backstop: jev_stale / jev_age_missing / jev_stance_not_bullish."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import execution.gateway as gateway_mod
from execution import c_exit_shadow_run as run
from execution.c_exit_shadow_ledger import CExitShadowLedger
from execution.gateway import (
    JEV_AGE_MISSING,
    JEV_STALE,
    JEV_STANCE_NOT_BULLISH,
    ExecutionGateway,
    jev_guard_reason,
)
from execution.tests.test_purpose_gate import (
    PURPOSE_LIVE_CANDIDATE,
    ROOT,
    _DecisionDB,
    _errors,
    _receipt,
    _RecordingExecutor,
)

ON = {"EXECUTION_JEV_GUARD": "1", "EXECUTION_PAPER_OPEN_PAUSE_FLAG": "/nonexistent/flag",
      "EXECUTION_PAPER_OPEN_PAUSED": ""}


class ReasonTests(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(jev_guard_reason({}), JEV_AGE_MISSING)
        self.assertEqual(jev_guard_reason(None), JEV_AGE_MISSING)
        self.assertEqual(jev_guard_reason({"jev_age_min": None}), JEV_AGE_MISSING)
        self.assertEqual(jev_guard_reason({"jev_age_min": ""}), JEV_AGE_MISSING)
        self.assertEqual(jev_guard_reason({"jev_age_min": "abc"}), JEV_AGE_MISSING)
        self.assertEqual(jev_guard_reason({"jev_age_min": 1441}), JEV_STALE)
        self.assertIsNone(jev_guard_reason({"jev_age_min": 1440}))
        self.assertIsNone(jev_guard_reason({"jev_age_min": "30"}))
        self.assertIsNone(jev_guard_reason({"jev_age_min": 30, "jev_stance_raw": "BULLISH"}))
        self.assertIsNone(jev_guard_reason({"jev_age_min": 30, "jev_stance_raw": "bullish"}))
        self.assertEqual(jev_guard_reason({"jev_age_min": 30, "jev_stance_raw": "NEUTRAL"}),
                         JEV_STANCE_NOT_BULLISH)
        self.assertEqual(jev_guard_reason({"jev_age_min": 30, "jev_stance_raw": ""}),
                         JEV_STANCE_NOT_BULLISH)
        # other strategies: all three gates pass
        self.assertIsNone(jev_guard_reason({}, "factor_x_v1"))
        self.assertIsNone(jev_guard_reason({"jev_age_min": 9999, "jev_stance_raw": "BEARISH"}, "other"))
        self.assertEqual(jev_guard_reason({}, "jev_strength_v1"), JEV_AGE_MISSING)
        self.assertIsNone(jev_guard_reason({}, "factor_macd_cross_v1"))

    def test_factor_jev_same_rules(self):
        cases = [({}, JEV_AGE_MISSING), ({"jev_age_min": ""}, JEV_AGE_MISSING),
                 ({"jev_age_min": 1441}, JEV_STALE), ({"jev_age_min": 1440}, None),
                 ({"jev_age_min": 5, "jev_stance_raw": "NEUTRAL"}, JEV_STANCE_NOT_BULLISH),
                 ({"jev_age_min": 5, "jev_stance_raw": "BULLISH"}, None)]
        for ex, want in cases:
            for strat in ("jev_strength_v1", "factor_jev_strength_v1",
                          "combined_jev_tech_v1", "factor_combined_jev_tech_v1"):
                self.assertEqual(jev_guard_reason(ex, strat), want, (ex, strat))
        # stale wins over stance
        self.assertEqual(jev_guard_reason({"jev_age_min": 5000, "jev_stance_raw": "BEARISH"}), JEV_STALE)

    def test_default_on_without_env(self):
        env = os.environ.copy()
        env.pop("EXECUTION_JEV_GUARD", None)
        code = (f"import sys; sys.path[:0]=[{str(ROOT)!r}, {str(ROOT / 'crawler')!r}]\n"
                "from execution.gateway import jev_guard_enabled\n"
                "assert jev_guard_enabled() is True\n")
        proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class StrategyListParityTests(unittest.TestCase):
    """Execution list must match decision_engine's JEV strategies (PR #111 scope)."""

    def _decision_side(self):
        import decision_engine.strategies as de
        from decision_engine.strategies.combined_strategy import CombinedJevTechStrategy
        from decision_engine.strategies.jev_strategy import JevStrengthStrategy

        legacy = {JevStrengthStrategy.strategy_id, CombinedJevTechStrategy.strategy_id}
        single = {sid for sid, (buy, sell, _p, _m) in de._FACTOR_COMBOS.items()
                  if buy in (["jev_strength"], ["combined_jev_tech"]) and sell == ["noop"]}
        others = {sid for sid, (buy, *_r) in de._FACTOR_COMBOS.items()
                  if {"jev_strength", "combined_jev_tech"} & set(buy)} - single
        return legacy | single, others

    def test_exec_list_equals_decision_jev_strategies(self):
        from execution.gateway import JEV_GUARD_STRATEGIES

        covered, _ = self._decision_side()
        self.assertEqual(set(JEV_GUARD_STRATEGIES), covered)
        for sid in covered:
            self.assertIn(sid, __import__("decision_engine.strategies", fromlist=["x"]).STRATEGY_REGISTRY)

    def test_known_jev_combos_outside_guard(self):
        # Combos that also use the jev_strength factor but are not gated here.
        # If decision_engine adds/removes one, this fails so the list is revisited.
        _, others = self._decision_side()
        self.assertEqual(others, {"factor_jev_timeexit_v1", "factor_jev_trail_v1", "factor_jev_rsi_and_v1"})


class GatewayTests(unittest.TestCase):
    def test_receipts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, ON):
            root = Path(tmp)
            db = _DecisionDB(root / "decision.db", purpose_column=True)
            db.seed_price()
            ok = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE,
                        extras={"jev_age_min": 12, "jev_stance_raw": "BULLISH"})
            stale = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, extras={"jev_age_min": 2000})
            missing = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, extras={})
            bear = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE,
                          extras={"jev_age_min": 5, "jev_stance_raw": "BEARISH"})
            sell = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, action="sell", extras={})
            other = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, strategy_id="combined_v1", extras={})
            comb = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, strategy_id="combined_jev_tech_v1",
                          extras={"jev_age_min": 5, "jev_stance_raw": "NEUTRAL"})
            fcomb = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, strategy_id="factor_combined_jev_tech_v1",
                           extras={})
            fjev = db.add(column_purpose=PURPOSE_LIVE_CANDIDATE, strategy_id="factor_jev_strength_v1",
                          extras={"jev_age_min": 3000})
            db.close()
            paper = _RecordingExecutor()
            prev = gateway_mod.EXECUTION_RISK_ENABLED
            gateway_mod.EXECUTION_RISK_ENABLED = False
            try:
                with ExecutionGateway(decision_db=db.path, ledger_path=root / "exec.db",
                                      paper_enabled=True, paper_db=root / "paper.db",
                                      paper_executor=paper, pm_enabled=False) as gw:
                    gw.process_pending(since_id=0)
                    rows = {int(r["decision_log_id"]): r for r in gw.ledger.recent(limit=20)}
            finally:
                gateway_mod.EXECUTION_RISK_ENABLED = prev
            for did, reason in ((stale, JEV_STALE), (missing, JEV_AGE_MISSING), (fjev, JEV_STALE),
                                (comb, JEV_STANCE_NOT_BULLISH), (fcomb, JEV_AGE_MISSING),
                                (bear, JEV_STANCE_NOT_BULLISH)):
                self.assertEqual(rows[did]["status"], "rejected")
                self.assertEqual(rows[did]["executed"], 0)
                self.assertEqual(_errors(rows[did]), [reason])
                self.assertEqual(_receipt(rows[did])["reason"], reason)
                self.assertIn("jev guard", _receipt(rows[did])["note"])
            self.assertEqual(sorted(c["decision_log_id"] for c in paper.calls), [ok, sell, other])

    def test_switch_off(self):
        with mock.patch.dict(os.environ, {"EXECUTION_JEV_GUARD": "0"}):
            self.assertFalse(gateway_mod.jev_guard_enabled())


class RunnerTests(unittest.TestCase):
    def test_runner_guard(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ON):
            dec = Path(d) / "dexscreener.db"
            c = sqlite3.connect(dec)
            c.execute("CREATE TABLE decision_log (id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT,"
                      " action TEXT, chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,"
                      " risk_passed INTEGER, mode TEXT, raw_json TEXT, purpose TEXT)")
            for i, ex in enumerate([{"jev_age_min": 3, "jev_stance_raw": "BULLISH"},
                                    {"jev_age_min": 9999}, {}, {"jev_age_min": 3, "jev_stance_raw": "NEUTRAL"}], 1):
                raw = {"action": "buy", "chain_id": "solana", "pair_address": f"p{i}", "size_pct": 0.05,
                       "purpose": "live_candidate", "extras": ex}
                c.execute("INSERT INTO decision_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (i, "2026-10-21T01:00:00Z", "jev_strength_v1", "buy", "solana", f"t{i}", 0.05, "", 1,
                           "dry_run", json.dumps(raw), "live_candidate"))
            c.commit(); c.close()
            with mock.patch.object(run, "_latest_mid", return_value=(1.0, 1e6)), \
                 CExitShadowLedger(Path(d) / "c.db") as led:
                out = run.process_once(led, decision_db=dec, crawler_db=dec)
            self.assertEqual(out["raw_buys"], 1)
            self.assertEqual(out["opened"], 8)
            self.assertEqual(out["jev_rejected"],
                             {JEV_STALE: 1, JEV_AGE_MISSING: 1, JEV_STANCE_NOT_BULLISH: 1})


if __name__ == "__main__":
    unittest.main()
