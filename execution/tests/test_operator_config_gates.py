"""Explicit production-style config. Not the default-off suite.

These cases set the values themselves. They do not read a host ``.env`` and
they do not place orders. Default-off tests import this package only after
``execution.tests`` has dropped operator config, and they do not copy
``os.environ`` into a child.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.binance_account_snapshot import seal_snapshot, write_snapshot_file  # noqa: E402
from execution.live_bnb_watchdog import preflight  # noqa: E402
from execution.tests.isolated_env import isolated_environ, patch_isolated  # noqa: E402

_SECRET = "operator-config-test-secret"
_RESTRICTIONS = {
    "enableSpotAndMarginTrading": True,
    "enableReading": True,
    "ipRestrict": True,
    "enableMargin": False,
    "enableFutures": False,
    "enableWithdrawals": False,
}
_ACCOUNT = {
    "accountType": "SPOT",
    "canTrade": True,
    "permissions": ["SPOT"],
    "balances": [{"asset": "USDT", "free": "10.00", "locked": "0"}],
}


class OperatorConfigGateTests(unittest.TestCase):
    def test_explicit_daily_loss_paper_and_risk_load_in_a_fresh_process(self) -> None:
        env = isolated_environ(
            {
                "EXECUTION_DAILY_LOSS_USD": "200",
                "EXECUTION_RISK_ENABLED": "1",
                "EXECUTION_PAPER_ENABLED": "1",
            }
        )
        code = (
            "import sys\n"
            f"sys.path[:0] = [{str(ROOT)!r}, {str(CRAWLER)!r}]\n"
            "import execution.config as cfg\n"
            "from execution.gateway import _env_bool\n"
            "assert cfg.EXECUTION_DAILY_LOSS_USD == 200.0, cfg.EXECUTION_DAILY_LOSS_USD\n"
            "assert cfg.EXECUTION_RISK_ENABLED is True\n"
            "assert _env_bool('EXECUTION_PAPER_ENABLED', False) is True\n"
            "print('operator_config_loaded')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("operator_config_loaded", proc.stdout)
        self.assertIsNone(os.environ.get("EXECUTION_DAILY_LOSS_USD"))

    def test_unselected_book_and_rest_uid_mismatch_block_without_http(self) -> None:
        kill = ROOT / "no-such-kill-switch"
        base = {
            "BINANCE_SPOT_LIVE_ENABLED": "1",
            "BINANCE_SPOT_BOOK_USDT": "1000",
            "BINANCE_API_KEY": "test-key",
            "BINANCE_API_SECRET": _SECRET,
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
            "LIVE_KILL_SWITCH_PATH": str(kill),
        }
        with patch_isolated(base), mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("operator-config test must not call Binance"),
        ):
            unselected = preflight()
        self.assertFalse(unselected["would_start"])
        self.assertIn("production_book_unselected", unselected["reasons"])
        self.assertNotIn(_SECRET, str(unselected))

        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "account.json"
            sealed = seal_snapshot(
                _ACCOUNT,
                secret=_SECRET,
                fetched_at_ms=1_700_000_000_000,
                restrictions=_RESTRICTIONS,
            )
            self.assertIsNone(write_snapshot_file(snap, sealed))
            mismatch = dict(base)
            mismatch["BINANCE_SPOT_PRODUCTION_BOOK"] = "rest_api_key"
            mismatch["BINANCE_SPOT_REST_USDT_FREE"] = "10.00"
            mismatch["BINANCE_SPOT_REST_UID"] = "1"
            mismatch["BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"] = str(snap)
            with patch_isolated(mismatch), mock.patch(
                "execution.binance_account_snapshot.time.time",
                return_value=1_700_000_000,
            ), mock.patch(
                "execution.executors.binance_spot.urllib.request.urlopen",
                side_effect=AssertionError("operator-config test must not call Binance"),
            ):
                report = preflight()
        self.assertFalse(report["would_start"])
        self.assertEqual(report["reasons"], ["rest_uid_mismatch"])
        self.assertNotIn(_SECRET, str(report))
