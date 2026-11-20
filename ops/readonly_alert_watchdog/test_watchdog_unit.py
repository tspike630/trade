#!/usr/bin/env python3
"""Network-blocked unit tests for the watchdog body.

These tests do not import or imitate readonly_live_preflight. A passing run
is not acceptance of the full preflight suite and is not an install approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import warnings

_REAL_SOCKET = socket.socket


class _BlockedSocket(_REAL_SOCKET):  # type: ignore[misc]
    def connect(self, address: object, /) -> None:
        raise RuntimeError(f"offline test blocked socket connect to {address!r}")

    def connect_ex(self, address: object, /) -> int:
        raise RuntimeError(f"offline test blocked socket connect_ex to {address!r}")


socket.socket = _BlockedSocket  # type: ignore[misc]

_PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(_PKG))
import readonly_alert_watchdog as watchdog  # noqa: E402
import readonly_live_preflight as preflight  # noqa: E402


@dataclass
class Finding:
    code: str
    severity: str = "RED"
    summary: str = ""
    detail: dict = field(default_factory=dict)
    surface: str = "clock"

    def __post_init__(self) -> None:
        if not self.summary:
            self.summary = self.code


@dataclass
class Report:
    now_ms: int
    findings: list
    overall_status: str = "RED"
    metrics: dict = field(default_factory=dict)
    checked_at_utc: str = "2026-11-20T12:00:00Z"
    code_state: str = "UNIT"
    nyse_cash_open: bool = False


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WatchdogBodyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.state = self.base / "state" / "watchdog_state.json"
        self.ledger = self.repo / "execution_ledger_live_bnb_cross_margin.db"
        self.ledger.write_bytes(b"not-a-real-ledger")
        self.ledger_hash = _sha(self.ledger)

    def tearDown(self) -> None:
        self.assertEqual(_sha(self.ledger), self.ledger_hash)
        self.assertFalse(self.repo.joinpath("readonly_live_preflight.py").exists())
        self.tmp.cleanup()

    def _run(self, report: Report, poster=None, **kwargs):
        def inspect_fn(_root, **_kw):
            return report

        return watchdog.run_watchdog_once(
            self.repo,
            state_file=self.state,
            now_ms=report.now_ms,
            inspect_fn=inspect_fn,
            http_poster=poster,
            webhook_url=kwargs.get("webhook_url", "https://hooks.example.invalid/quant"),
            bark_url=kwargs.get("bark_url", ""),
            telegram_bot_token=kwargs.get("telegram_bot_token", ""),
            telegram_chat_id=kwargs.get("telegram_chat_id", ""),
            alert_jsonl=kwargs.get("alert_jsonl"),
            renotify_sec=kwargs.get("renotify_sec", 1800),
        )

    def _state(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_original_upload_hashes_and_volume_code_was_absent(self) -> None:
        original = _PKG / "originals" / "readonly_alert_watchdog.py"
        self.assertEqual(
            _sha(original),
            "0e42dba33ee1ba3235f5bbacdfabe39332afa40fb371689f03764c8adb671357",
        )
        self.assertEqual(
            _sha(_PKG / "originals" / "test_proactive_watchdog.py"),
            "eaddeeeee67cdef264b2b21b0dc969a341ae4ddc47a0d983ed6f960164686920",
        )
        text = original.read_text(encoding="utf-8")
        self.assertNotIn("volume_z_source_close_stale", text)
        self.assertIn("volume_z_source_close_stale", watchdog.CLOCK_FAMILY_CODES["1h_volume_source_bar"])
        for code in watchdog.NOT_LOCKOUT_CODES:
            self.assertFalse(watchdog.is_lockout_code(code), code)
        for family, codes in watchdog.CLOCK_FAMILY_CODES.items():
            self.assertTrue(codes, family)
            for code in codes:
                self.assertTrue(watchdog.is_lockout_code(code), code)

    def test_failed_alert_is_not_acked_and_retries(self) -> None:
        calls: list[str] = []

        def poster(url: str, _payload: object) -> tuple[bool, str]:
            calls.append(url)
            return False, "simulated_network_failure"

        report = Report(
            1_800_000_000_000,
            [Finding("deribit_options_gamma_stale", detail={"source_bar_close_ms": 1_800_000_000_000})],
        )
        first = self._run(report, poster)
        self.assertTrue(first["should_push"])
        self.assertEqual(first["event_type"], "NEW_ALERT")
        self.assertEqual(first["delivery_outcome"], "failed")
        self.assertFalse(first["delivery_succeeded"])
        self.assertTrue(first["pending_retry"])
        self.assertEqual(self._state()["notified_red_fingerprints"], [])
        self.assertEqual(self._state()["last_success_ms"], 0)
        self.assertEqual(self._state()["last_attempt_ms"], report.now_ms)
        second = self._run(report, poster)
        self.assertEqual(second["event_type"], "NEW_ALERT")
        self.assertEqual(second["delivery_outcome"], "failed")
        self.assertEqual(len(calls), 2)

    def test_partial_channel_success_retries_only_the_failed_channel(self) -> None:
        calls: list[str] = []

        def poster(url: str, _payload: object) -> tuple[bool, str]:
            calls.append(url)
            if "bark.example" in url:
                return True, "http_200"
            return False, "simulated_network_failure"

        report = Report(1_800_000_000_000, [Finding("entry_5m_kline_stale", detail={"entry_kline_5m_close_ms": 1_800_000_000_000})])
        first = self._run(report, poster, bark_url="https://bark.example.invalid/device")
        self.assertEqual(first["delivery_outcome"], "partial")
        self.assertFalse(first["delivery_succeeded"])
        self.assertEqual(self._state()["notified_red_fingerprints"], [])
        self.assertIn("bark", self._state()["pending_channel_ok"])
        calls.clear()

        def recover(url: str, _payload: object) -> tuple[bool, str]:
            calls.append(url)
            return True, "http_200"

        second = self._run(report, recover, bark_url="https://bark.example.invalid/device")
        self.assertEqual(second["delivery_outcome"], "delivered")
        self.assertTrue(second["delivery_succeeded"])
        self.assertEqual(calls, ["https://hooks.example.invalid/quant"])
        self.assertFalse(self._state()["pending_retry"])

    def test_recovery_failure_retries_on_the_next_round(self) -> None:
        phase = {"fail_clear": False}

        def poster(_url: str, _payload: object) -> tuple[bool, str]:
            if phase["fail_clear"]:
                return False, "simulated_network_failure"
            return True, "http_200"

        stamp = 1_800_000_000_000
        red = Report(stamp, [Finding("obi_top5_source_stale", detail={"obi_top5_source_ts_ms": stamp, "symbol": "BTCUSDT"})])
        opened = self._run(red, poster)
        self.assertEqual(opened["delivery_outcome"], "delivered")
        self.assertTrue(self._state()["notified_red_fingerprints"])
        phase["fail_clear"] = True
        clear = Report(stamp + 300_000, [], overall_status="GREEN")
        failed = self._run(clear, poster)
        self.assertEqual(failed["event_type"], "RESOLVED")
        self.assertEqual(failed["delivery_outcome"], "failed")
        self.assertTrue(self._state()["notified_red_fingerprints"])
        phase["fail_clear"] = False
        retried = self._run(clear, poster)
        self.assertEqual(retried["event_type"], "RESOLVED")
        self.assertEqual(retried["delivery_outcome"], "delivered")
        self.assertEqual(self._state()["notified_red_fingerprints"], [])
        quiet = self._run(clear, poster)
        self.assertEqual(quiet["event_type"], "QUIET")
        self.assertFalse(quiet["should_push"])

    def test_same_bucket_gap_and_adjacent_source_bars(self) -> None:
        def poster(_url: str, _payload: object) -> tuple[bool, str]:
            return True, "http_200"

        start = 1_800_000_000_000
        hold = {"renotify_sec": 86_400}
        first = self._run(
            Report(start, [Finding("entry_5m_kline_stale", detail={"source_bar_close_ms": start})]),
            poster,
            **hold,
        )
        self.assertEqual(first["consecutive_lockout_bars"], 1)
        self.assertEqual(first["event_type"], "NEW_ALERT")
        same = self._run(
            Report(start + 45_000, [Finding("entry_5m_kline_stale", detail={"source_bar_close_ms": start})]),
            poster,
            **hold,
        )
        self.assertEqual(same["consecutive_lockout_bars"], 1)
        self.assertFalse(same["should_push"])
        gapped = self._run(
            Report(
                start + 3_600_000,
                [Finding("entry_5m_kline_stale", detail={"source_bar_close_ms": start + 3_600_000})],
            ),
            poster,
            **hold,
        )
        self.assertEqual(gapped["consecutive_lockout_bars"], 1)
        self.assertNotEqual(gapped["event_type"], "ESCALATED_SILENT_LOCKOUT")
        self.assertFalse(gapped["should_push"])
        adjacent = self._run(
            Report(
                start + 3_900_000,
                [Finding("entry_5m_kline_stale", detail={"source_bar_close_ms": start + 3_900_000})],
            ),
            poster,
            **hold,
        )
        self.assertEqual(adjacent["consecutive_lockout_bars"], 2)
        self.assertEqual(adjacent["event_type"], "ESCALATED_SILENT_LOCKOUT")

    def test_computed_time_does_not_count_as_source_hour(self) -> None:
        def poster(_url: str, _payload: object) -> tuple[bool, str]:
            return True, "http_200"

        source_hour = 1_800_000_000_000
        first = self._run(
            Report(
                source_hour + 3_600_000,
                [Finding(
                    "volume_z_source_close_stale",
                    detail={"source_close_time_ms": source_hour, "computed_at_ms": source_hour + 3_600_000},
                )],
            ),
            poster,
        )
        self.assertEqual(first["source_bucket"], source_hour // watchdog.FIVE_MIN_MS)
        second = self._run(
            Report(
                source_hour + 7_200_000,
                [Finding(
                    "volume_z_source_close_stale",
                    detail={"volume_z_source_close_ms": source_hour, "computed_at_ms": source_hour + 7_200_000, "now_ms": source_hour + 7_200_000},
                )],
            ),
            poster,
        )
        self.assertEqual(second["consecutive_lockout_bars"], 1)
        self.assertNotEqual(second["event_type"], "ESCALATED_SILENT_LOCKOUT")
        unstamped = self._run(
            Report(source_hour + 10_800_000, [Finding("volume_z_source_close_stale", detail={"computed_at_ms": source_hour + 10_800_000})]),
            poster,
        )
        self.assertIsNone(unstamped["source_bucket"])
        self.assertEqual(unstamped["consecutive_lockout_bars"], 0)

    def test_no_entry_signal_is_not_a_lockout_and_data_lock_is_not_healthy(self) -> None:
        quiet = self._run(
            Report(
                1_800_000_000_000,
                [],
                overall_status="GREEN",
                metrics={"signal_funnel": {"posture": "no_entry_signal", "evaluated": True, "buy_count": 0, "data_lockout": False}},
            ),
            webhook_url="",
        )
        self.assertEqual(quiet["entry_posture"], "no_entry_signal")
        self.assertEqual(quiet["watchdog_status"], "NO_SIGNAL")
        self.assertFalse(quiet["fault"])
        self.assertFalse(quiet["should_push"])
        self.assertEqual(quiet["consecutive_lockout_bars"], 0)
        self.assertNotIn("consecutive_5m_silent_clock_lockout", quiet["red_codes"])
        locked = self._run(
            Report(
                1_800_000_000_000,
                [Finding("volume_z_source_close_stale", detail={"source_close_time_ms": 1_796_400_000_000})],
                overall_status="GREEN",
                metrics={"signal_funnel": {"posture": "no_entry_signal", "evaluated": True, "buy_count": 0}},
            ),
            webhook_url="",
        )
        self.assertEqual(locked["entry_posture"], "data_lockout")
        self.assertTrue(locked["fault"])
        self.assertEqual(locked["watchdog_status"], "RED")
        self.assertNotEqual(locked["watchdog_status"], quiet["watchdog_status"])

    def test_same_code_new_symbol_is_not_deduped(self) -> None:
        def poster(_url: str, _payload: object) -> tuple[bool, str]:
            return True, "http_200"

        stamp = 1_800_000_000_000
        first = self._run(Report(stamp, [Finding("obi_top5_source_stale", detail={"symbol": "BTCUSDT", "obi_top5_source_ts_ms": stamp})]), poster)
        self.assertEqual(first["event_type"], "NEW_ALERT")
        second = self._run(Report(stamp + 10_000, [Finding("obi_top5_source_stale", detail={"symbol": "ETHUSDT", "obi_top5_source_ts_ms": stamp})]), poster)
        self.assertTrue(second["should_push"])
        self.assertEqual(second["event_type"], "NEW_ALERT")

    def test_missing_preflight_sql_error_and_unsafe_path_are_faults(self) -> None:
        saved = sys.modules.get("readonly_live_preflight")
        sys.modules["readonly_live_preflight"] = None
        try:
            missing = watchdog.run_watchdog_once(self.repo, state_file=self.state, now_ms=1_800_000_000_000)
        finally:
            if saved is None:
                sys.modules.pop("readonly_live_preflight", None)
            else:
                sys.modules["readonly_live_preflight"] = saved
        self.assertEqual(missing["red_codes"], ["watchdog_preflight_missing"])
        self.assertTrue(missing["fault"])
        self.assertFalse(missing["preflight_present"])
        self.assertFalse(missing["install_allowed"])
        self.assertNotEqual(missing["watchdog_status"], "NO_SIGNAL")
        (self.base / "empty_root").mkdir(exist_ok=True)
        proc_missing = subprocess.run(
            [
                sys.executable,
                str(_PKG / "readonly_alert_watchdog.py"),
                "--repo-root",
                str(self.base / "empty_root"),
                "--state-file",
                str(self.base / "state" / "cli_state.json"),
                "--now-ms",
                "1800000000000",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(_PKG)},
        )
        self.assertEqual(proc_missing.returncode, 0, proc_missing.stdout + proc_missing.stderr)
        self.assertIn("code_tree_not_in_root", proc_missing.stdout)
        self.assertIn('"install_allowed": false', proc_missing.stdout)
        self.assertNotIn("NO_SIGNAL", proc_missing.stdout)
        self.assertFalse((self.base / "empty_root" / "dexscreener.db").exists())

        def boom(_root, **_kw):
            raise sqlite3.OperationalError("no such table: cross_asset_bars")

        failed = watchdog.run_watchdog_once(
            self.repo,
            state_file=self.state,
            now_ms=1_800_000_300_000,
            inspect_fn=boom,
        )
        self.assertEqual(failed["red_codes"], ["watchdog_sql_error"])
        self.assertTrue(failed["fault"])
        self.assertEqual(list(self.repo.glob("*.db")), [self.ledger])

        proc = subprocess.run(
            [sys.executable, str(_PKG / "readonly_alert_watchdog.py"), "--repo-root", str(self.repo), "--state-file", str(self.repo / "inside.json"), "--now-ms", "1800000000000"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("watchdog_unsafe_path", proc.stdout)
        self.assertFalse((self.repo / "inside.json").exists())

    def test_symlink_into_repo_and_protected_suffix_are_refused(self) -> None:
        link = self.base / "into_repo"
        link.symlink_to(self.repo)
        with self.assertRaises(watchdog.UnsafeWatchdogPathError):
            watchdog.validate_external_state_path(link / "state.json", self.repo)
        with self.assertRaises(watchdog.UnsafeWatchdogPathError):
            watchdog.validate_external_state_path(self.base / "bad.db", self.repo)
        swapped = self.base / "state.json"
        swapped.symlink_to(self.ledger)
        with self.assertRaises(watchdog.UnsafeWatchdogPathError):
            watchdog.validate_external_state_path(swapped, self.repo)

    def test_webhook_helper_cannot_open_a_socket(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            ok, detail = watchdog.post_json_webhook(
                "http://127.0.0.1:9/watchdog",
                {"title": "offline"},
                timeout=1.0,
            )
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("network_err:"))

    def test_summary_does_not_echo_a_channel_secret(self) -> None:
        def poster(_url: str, _payload: object) -> tuple[bool, str]:
            return False, "simulated_network_failure"

        result = self._run(
            Report(1_800_000_000_000, [Finding("quote_turnover_clock_absent")]),
            poster,
            telegram_bot_token="unit-secret-token",
            telegram_chat_id="1",
        )
        self.assertNotIn("unit-secret-token", json.dumps(result))
        self.assertEqual(result["delivery_outcome"], "failed")

    def test_real_preflight_readonly_volume_hour_and_funnel(self) -> None:
        empty = self.base / "empty_root"
        empty.mkdir(exist_ok=True)
        absent = empty / "missing.db"
        report = preflight.inspect_system(
            empty,
            now_ms=1_800_000_000_000,
            main_db=str(absent),
            deriv_db=str(empty / "no-deriv.db"),
            lseg_db=str(empty / "no-lseg.db"),
            cityu_db=str(empty / "no-cityu.db"),
            cross_margin_ledger=str(empty / "no-cm.db"),
            spot_ledgers=[],
            require_dbs=True,
        )
        self.assertFalse(absent.exists())
        self.assertIn("main_decision_db_absent", {item.code for item in report.findings})
        self.assertFalse(report.metrics["install_approval"])
        self.assertEqual(report.metrics["funnel"]["posture"], "unspecified")
        self.assertNotIn("quote_turnover_stale", {item.code for item in report.findings})

        db = self.base / "volume.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE decision_volume_z (
                symbol TEXT, source_close_time_ms INTEGER, computed_at_ms INTEGER
            )
            """
        )
        source_close = 1_800_000_000_000 - 3_600_001
        conn.execute(
            "INSERT INTO decision_volume_z VALUES ('BTCUSDT', ?, ?)",
            (source_close, 1_800_000_000_000),
        )
        conn.commit()
        conn.close()
        ro = preflight.connect_readonly_sqlite(db)
        self.assertIsNotNone(ro)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO decision_volume_z VALUES ('ETHUSDT', 1, 1)")
        ro.close()
        clocks, _metrics = preflight.inspect_data_clocks(
            now_ms=1_800_000_000_000,
            nyse_open=False,
            expected_cot="2026-11-17",
            main_db=db,
            deriv_db=self.base / "no-deriv.db",
            lseg_db=self.base / "no-lseg.db",
            cityu_db=self.base / "no-cityu.db",
            require_dbs=False,
        )
        codes = {item.code for item in clocks}
        self.assertIn("volume_z_source_close_stale", codes)
        volume = next(item for item in clocks if item.code == "volume_z_source_close_stale")
        self.assertEqual(volume.detail["source_close_time_ms"], source_close)
        self.assertTrue(volume.detail["computed_at_ignored"])
        self.assertFalse((self.base / "no-deriv.db").exists())

        fresh_close = 1_800_000_000_000 - 3_600_000
        conn = sqlite3.connect(db)
        conn.execute("UPDATE decision_volume_z SET source_close_time_ms = ?", (fresh_close,))
        conn.commit()
        conn.close()
        fresh_clocks, _ = preflight.inspect_data_clocks(
            now_ms=1_800_000_000_000,
            nyse_open=False,
            expected_cot="2026-11-17",
            main_db=db,
            deriv_db=self.base / "no-deriv.db",
            lseg_db=self.base / "no-lseg.db",
            cityu_db=self.base / "no-cityu.db",
            require_dbs=False,
        )
        self.assertNotIn("volume_z_source_close_stale", {item.code for item in fresh_clocks})

        funnel: dict = {"decision_rows_in_window": 4, "risk_passed_by_purpose": {}}
        preflight.annotate_funnel_posture([], funnel)
        self.assertEqual(funnel["posture"], "no_entry_signal")
        locked: dict = {"decision_rows_in_window": 4, "risk_passed_by_purpose": {}}
        preflight.annotate_funnel_posture([volume], locked)
        self.assertEqual(locked["posture"], "data_lockout")
        self.assertNotEqual(funnel["posture"], locked["posture"])

        bare = watchdog._empty_state()
        first, obs = watchdog.evaluate_alert_transition(
            Report(1_800_000_000_000, [Finding("volume_z_source_close_absent", detail={"computed_at_ignored": True})]),
            bare,
        )
        bare.update(obs)
        second, _obs2 = watchdog.evaluate_alert_transition(
            Report(
                1_800_000_000_000 + watchdog.FIVE_MIN_MS,
                [Finding("volume_z_source_close_absent", detail={"computed_at_ignored": True})],
            ),
            bare,
        )
        self.assertEqual(first.consecutive_lockout_bars, 0)
        self.assertEqual(second.consecutive_lockout_bars, 0)
        self.assertIsNone(second.source_bucket)
        self.assertNotEqual(second.event_type, "ESCALATED_SILENT_LOCKOUT")
        self.assertTrue(second.should_push)

    def test_quote_clock_uses_proven_schema_only(self) -> None:
        now = 1_800_000_000_000
        db = self.base / "quotes.db"

        def clocks() -> set[str]:
            found, _info = preflight.inspect_data_clocks(
                now_ms=now,
                nyse_open=False,
                expected_cot="2026-11-17",
                main_db=db,
                deriv_db=self.base / "no-deriv.db",
                lseg_db=self.base / "no-lseg.db",
                cityu_db=self.base / "no-cityu.db",
                require_dbs=False,
                repo_root=self.base / "no-repo",
            )
            return {item.code for item in found}

        sqlite3.connect(db).close()
        self.assertIn("quote_turnover_source_unobserved", clocks())
        self.assertNotIn("quote_turnover_stale", clocks())

        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE symbols (symbol TEXT, quoteVolume_24h REAL, updated_at TEXT)"
        )
        conn.execute("INSERT INTO symbols VALUES ('BMTUSDT', 9000000, ?)", (preflight.fmt_ms_utc(now),))
        conn.commit()
        conn.close()
        self.assertIn("quote_turnover_clock_absent", clocks())
        absent, _info = preflight.inspect_data_clocks(
            now_ms=now,
            nyse_open=False,
            expected_cot="2026-11-17",
            main_db=db,
            deriv_db=self.base / "no-deriv.db",
            lseg_db=self.base / "no-lseg.db",
            cityu_db=self.base / "no-cityu.db",
            require_dbs=False,
        )
        missing = next(item for item in absent if item.code == "quote_turnover_clock_absent")
        self.assertIsNone(missing.detail["quote_source_stamp_ms"])

        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE symbols ADD COLUMN quoteVolume_collected_at TEXT")
        conn.execute(
            "UPDATE symbols SET quoteVolume_collected_at = ?",
            (str(now + 121_000),),
        )
        conn.commit()
        conn.close()
        self.assertIn("quote_turnover_stale", clocks())

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE symbols SET quoteVolume_collected_at = ?",
            (str(now - 3_600_001),),
        )
        conn.commit()
        conn.close()
        stale, _info = preflight.inspect_data_clocks(
            now_ms=now,
            nyse_open=False,
            expected_cot="2026-11-17",
            main_db=db,
            deriv_db=self.base / "no-deriv.db",
            lseg_db=self.base / "no-lseg.db",
            cityu_db=self.base / "no-cityu.db",
            require_dbs=False,
        )
        row = next(item for item in stale if item.code == "quote_turnover_stale")
        self.assertEqual(row.detail["quote_source_stamp_ms"], now - 3_600_001)
        self.assertNotEqual(row.detail["quote_source_stamp_ms"], now)

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE symbols SET quoteVolume_collected_at = ?",
            (str(now - 3_600_000),),
        )
        conn.commit()
        conn.close()
        self.assertNotIn("quote_turnover_stale", clocks())

    def test_module_does_not_open_sqlite(self) -> None:
        text = (_PKG / "readonly_alert_watchdog.py").read_text(encoding="utf-8")
        self.assertNotIn("sqlite3.connect", text)
        preflight_text = (_PKG / "readonly_live_preflight.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", preflight_text)
        self.assertIn("PRAGMA query_only = ON", preflight_text)
        self.assertNotIn("FROM quote_turnover", preflight_text)
        self.assertIn("quoteVolume_collected_at", preflight_text)
        self.assertIn("ccxt_ticks", preflight_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
