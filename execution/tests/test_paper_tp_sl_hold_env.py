"""Paper TP/SL/max_hold defaults via env (peer exit synth). Live/C-exit untouched.

Run from the repo root:

    python -m unittest execution.tests.test_paper_tp_sl_hold_env
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.executors.paper_executor import (  # noqa: E402
    DEFAULT_MAX_HOLD_MIN,
    DEFAULT_SL_PCT,
    DEFAULT_TP_PCT,
    PAPER_MAX_HOLD_MIN_ENV,
    PAPER_SL_PCT_ENV,
    PAPER_TP_PCT_ENV,
    paper_tp_sl_hold_defaults,
    tp_sl_hold_from_signal,
)

ENVS = (PAPER_TP_PCT_ENV, PAPER_SL_PCT_ENV, PAPER_MAX_HOLD_MIN_ENV)


class PaperTpSlHoldEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {name: os.environ.get(name) for name in ENVS}
        for name in ENVS:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._prev.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_hardcoded_defaults_when_env_unset(self) -> None:
        self.assertEqual(paper_tp_sl_hold_defaults(), (10.0, 5.0, 60.0))
        self.assertEqual(tp_sl_hold_from_signal({}), (10.0, 5.0, 60.0))
        self.assertEqual(DEFAULT_TP_PCT, 10.0)
        self.assertEqual(DEFAULT_SL_PCT, 5.0)
        self.assertEqual(DEFAULT_MAX_HOLD_MIN, 60.0)

    def test_env_fills_when_signal_omits_fields(self) -> None:
        os.environ[PAPER_TP_PCT_ENV] = "12.5"
        os.environ[PAPER_SL_PCT_ENV] = "7"
        os.environ[PAPER_MAX_HOLD_MIN_ENV] = "2880"
        self.assertEqual(paper_tp_sl_hold_defaults(), (12.5, 7.0, 2880.0))
        self.assertEqual(tp_sl_hold_from_signal({}), (12.5, 7.0, 2880.0))
        self.assertEqual(
            tp_sl_hold_from_signal({"extras": {}, "raw": {}}),
            (12.5, 7.0, 2880.0),
        )

    def test_explicit_signal_beats_env(self) -> None:
        os.environ[PAPER_TP_PCT_ENV] = "99"
        os.environ[PAPER_SL_PCT_ENV] = "99"
        os.environ[PAPER_MAX_HOLD_MIN_ENV] = "9999"
        self.assertEqual(
            tp_sl_hold_from_signal(
                {"tp_pct": 8.0, "sl_pct": 3.0, "max_hold_min": 120.0}
            ),
            (8.0, 3.0, 120.0),
        )
        self.assertEqual(
            tp_sl_hold_from_signal({"extras": {"tp": 9.0, "sl": 4.0, "max_hold": 45}}),
            (9.0, 4.0, 45.0),
        )
        self.assertEqual(
            tp_sl_hold_from_signal({"raw": {"tp_pct": 11.0, "max_hold_min": 4320}}),
            (11.0, 99.0, 4320.0),
        )

    def test_blank_or_bad_env_falls_back_to_hardcoded(self) -> None:
        os.environ[PAPER_TP_PCT_ENV] = "  "
        os.environ[PAPER_SL_PCT_ENV] = "nope"
        os.environ[PAPER_MAX_HOLD_MIN_ENV] = "nan"
        self.assertEqual(paper_tp_sl_hold_defaults(), (10.0, 5.0, 60.0))
        self.assertEqual(tp_sl_hold_from_signal({}), (10.0, 5.0, 60.0))


if __name__ == "__main__":
    unittest.main()
