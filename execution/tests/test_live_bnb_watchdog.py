"""Fail-closed preflight for the live_bnb execution watchdog. No live HTTP."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.binance_account_snapshot import (  # noqa: E402
    seal_snapshot,
    write_snapshot_file,
)
from execution.live_bnb_watchdog import (  # noqa: E402
    RESTART_DELAY_SEC,
    child_argv,
    preflight,
    supervise,
)

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
    "balances": [{"asset": "USDT", "free": "250.50", "locked": "0"}],
}

SECRET = "super-secret-value"


class _Proc:
    def __init__(self, code: int = 0) -> None:
        self.code = code

    def wait(self) -> int:
        return self.code


class WatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kill = self.root / "LIVE_TRADING_DISABLED"
        self.kill.write_text("keep live off\n", encoding="utf-8")
        self.env = {
            "LIVE_KILL_SWITCH_PATH": str(self.kill),
            "BINANCE_SPOT_LIVE_ENABLED": "0",
            "BINANCE_SPOT_BOOK_USDT": "",
            "BINANCE_API_KEY": "",
            "BINANCE_API_SECRET": "",
            "LIVE_EXCHANGE_API_KEY": "",
            "LIVE_EXCHANGE_API_SECRET": "",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_restart_bound_and_argv_is_only_this_arm(self) -> None:
        self.assertLessEqual(RESTART_DELAY_SEC, 3.0)
        argv = child_argv("python3")
        self.assertEqual(argv[1:3], ["-m", "execution.run"])
        self.assertIn("--accept-purpose", argv)
        self.assertEqual(argv[argv.index("--accept-purpose") + 1], "live_bnb_boll_fade")
        self.assertNotIn("--paper-enabled", argv)
        self.assertEqual(argv.count("live_bnb_boll_fade"), 1)

    def test_dry_preflight_refuses_and_does_not_touch_gates(self) -> None:
        before = self.kill.read_bytes()
        with mock.patch.dict(os.environ, self.env, clear=False):
            flag_before = os.environ.get("BINANCE_SPOT_LIVE_ENABLED")
            report = preflight()
            self.assertEqual(os.environ.get("BINANCE_SPOT_LIVE_ENABLED"), flag_before)
        self.assertFalse(report["would_start"])
        self.assertTrue(any(str(r).startswith("kill_switch:") for r in report["reasons"]))
        self.assertIn("binance_spot_live_disabled", report["reasons"])
        self.assertIn("missing_api_credentials", report["reasons"])
        self.assertIn("missing_book_usdt", report["reasons"])
        self.assertIn("production_book_unselected", report["reasons"])
        self.assertIsNone(report["argv"])
        self.assertEqual(self.kill.read_bytes(), before)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_no_key_does_not_start_even_when_gates_are_open(self) -> None:
        self.kill.unlink()
        opened = dict(self.env)
        opened["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        opened["BINANCE_SPOT_BOOK_USDT"] = "1000"
        opened["BINANCE_API_KEY"] = ""
        opened["BINANCE_API_SECRET"] = SECRET
        with mock.patch.dict(os.environ, opened, clear=False):
            report = preflight()
        self.assertFalse(report["would_start"])
        self.assertEqual(
            report["reasons"],
            ["missing_api_credentials", "production_book_unselected"],
        )
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertFalse(self.kill.exists())

    def test_ready_preflight_still_does_not_spawn_on_dry_run(self) -> None:
        self.kill.unlink()
        ready = dict(self.env)
        ready["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        ready["BINANCE_SPOT_BOOK_USDT"] = "1000"
        ready["BINANCE_API_KEY"] = "test-key"
        ready["BINANCE_API_SECRET"] = SECRET
        ready["BINANCE_SPOT_PRODUCTION_BOOK"] = "rest_api_key"
        ready["BINANCE_SPOT_REST_USDT_FREE"] = "250.50"
        snap = self.root / "account.json"
        sealed = seal_snapshot(
            _ACCOUNT, secret=SECRET, fetched_at_ms=1_700_000_000_000, restrictions=_RESTRICTIONS
        )
        self.assertIsNone(write_snapshot_file(snap, sealed))
        ready["BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"] = str(snap)
        with mock.patch.dict(os.environ, ready, clear=False), mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("watchdog must not call Binance"),
        ), mock.patch(
            "execution.binance_account_snapshot.time.time",
            return_value=1_700_000_000,
        ):
            report = preflight()
            code = __import__("execution.live_bnb_watchdog", fromlist=["main"]).main(["--dry-run"])
        self.assertTrue(report["would_start"], report)
        self.assertEqual(code, 2 if False else 0)
        self.assertNotIn(SECRET, json.dumps(report))
        self.assertNotIn("--paper-enabled", report["argv"])
        self.assertEqual(report["purpose"], "live_bnb_boll_fade")

    def test_zero_usdt_is_funding_block_and_does_not_spawn(self) -> None:
        self.kill.unlink()
        broke = dict(self.env)
        broke["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        broke["BINANCE_SPOT_BOOK_USDT"] = "439.13716903"
        broke["BINANCE_API_KEY"] = "test-key"
        broke["BINANCE_API_SECRET"] = SECRET
        broke["BINANCE_SPOT_PRODUCTION_BOOK"] = "rest_api_key"
        broke["BINANCE_SPOT_REST_USDT_FREE"] = "0"
        empty = dict(_ACCOUNT)
        empty["balances"] = [{"asset": "USDT", "free": "0", "locked": "0"}]
        snap = self.root / "empty.json"
        sealed = seal_snapshot(
            empty, secret=SECRET, fetched_at_ms=1_700_000_000_000, restrictions=_RESTRICTIONS
        )
        self.assertIsNone(write_snapshot_file(snap, sealed))
        broke["BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"] = str(snap)
        with mock.patch.dict(os.environ, broke, clear=False), mock.patch(
            "execution.binance_account_snapshot.time.time",
            return_value=1_700_000_000,
        ), mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("funding preflight must not call Binance"),
        ):
            report = preflight()
        self.assertFalse(report["would_start"])
        self.assertEqual(report["reasons"], ["funding_block"])
        self.assertIsNone(report["argv"])
        self.assertNotIn("437.63809903", json.dumps(report))

    def test_supervise_restarts_within_three_seconds_and_one_lock(self) -> None:
        self.kill.unlink()
        ready = dict(self.env)
        ready["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        ready["BINANCE_SPOT_BOOK_USDT"] = "1000"
        ready["BINANCE_API_KEY"] = "test-key"
        ready["BINANCE_API_SECRET"] = SECRET
        ready["BINANCE_SPOT_PRODUCTION_BOOK"] = "rest_api_key"
        ready["BINANCE_SPOT_REST_USDT_FREE"] = "250.50"
        snap = self.root / "account.json"
        sealed = seal_snapshot(
            _ACCOUNT, secret=SECRET, fetched_at_ms=1_700_000_000_000, restrictions=_RESTRICTIONS
        )
        self.assertIsNone(write_snapshot_file(snap, sealed))
        ready["BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH"] = str(snap)
        seen: list[list[str]] = []
        delays: list[float] = []

        def runner(argv: list[str]) -> _Proc:
            seen.append(argv)
            return _Proc(7)

        def sleeper(seconds: float) -> None:
            delays.append(seconds)

        with mock.patch.dict(os.environ, ready, clear=False), mock.patch(
            "execution.binance_account_snapshot.time.time",
            return_value=1_700_000_000,
        ):
            code = supervise(
                runner=runner,
                sleeper=sleeper,
                max_cycles=2,
                lock_path=self.root / "one.lock",
            )
            blocked = supervise(
                runner=runner,
                sleeper=sleeper,
                max_cycles=1,
                lock_path=self.root / "one.lock",
            )
        self.assertEqual(code, 7)
        self.assertEqual(delays, [3.0])
        self.assertLessEqual(delays[0], 3.0)
        self.assertEqual(len(seen), 3)
        self.assertNotIn(SECRET, json.dumps(seen))
        self.assertIn("live_bnb_boll_fade", seen[0])
        self.assertNotIn("--paper-enabled", seen[0])
        # The first supervise released the lock. Hold it and the next refuses.
        import fcntl

        held = (self.root / "one.lock").open("a", encoding="utf-8")
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.dict(os.environ, ready, clear=False), mock.patch(
                "execution.binance_account_snapshot.time.time",
                return_value=1_700_000_000,
            ):
                busy = supervise(
                    runner=runner,
                    sleeper=sleeper,
                    max_cycles=1,
                    lock_path=self.root / "one.lock",
                )
        finally:
            held.close()
        self.assertEqual(busy, 3)
        self.assertEqual(blocked, 7)

    def test_dry_wrapper_cannot_supervise(self) -> None:
        script = ROOT / "scripts" / "live_bnb_watchdog_dry.sh"
        prod = ROOT / "scripts" / "live_bnb_execution_watchdog.sh"
        self.assertIn("--dry-run", script.read_text(encoding="utf-8"))
        self.assertNotIn("--supervise", script.read_text(encoding="utf-8"))
        prod_text = prod.read_text(encoding="utf-8")
        self.assertNotIn("BINANCE_SPOT_LIVE_ENABLED=1", prod_text)
        self.assertNotIn("rm ", prod_text)
        self.assertNotIn("unlink", prod_text)
        mode = script.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        env = os.environ.copy()
        env.update(self.env)
        env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'crawler'}"
        done = subprocess.run(
            [str(script)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        report = json.loads(done.stdout)
        self.assertFalse(report["would_start"])
        self.assertEqual(self.kill.read_text(encoding="utf-8"), "keep live off\n")
        self.assertNotIn(SECRET, done.stdout)


if __name__ == "__main__":
    unittest.main()
