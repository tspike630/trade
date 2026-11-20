#!/usr/bin/env python3
"""Offline Socket-Blocked Unit & Fault-Injection Tests for Proactive Watchdog Suite."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest

# Block all outbound network sockets during tests
_REAL_SOCKET = socket.socket


class _BlockedSocket(_REAL_SOCKET):  # type: ignore[misc, valid-type]
    def connect(self, address: object, /) -> None:  # noqa: ANN001
        raise RuntimeError(f"offline test blocked socket connect to {address!r}")

    def connect_ex(self, address: object, /) -> int:  # noqa: ANN001
        raise RuntimeError(f"offline test blocked socket connect_ex to {address!r}")


socket.socket = _BlockedSocket  # type: ignore[misc]

_BUNDLE_DIR = Path(__file__).resolve().parent
if str(_BUNDLE_DIR) not in sys.path:
    sys.path.insert(0, str(_BUNDLE_DIR))

import readonly_alert_watchdog as watchdog  # noqa: E402
import readonly_live_preflight as preflight  # noqa: E402

RUNTIME_SNAPSHOT_DIR = Path("/Users/yangyuxuan/Downloads/runtime-source")
V3_WORKTREE_DIR = _BUNDLE_DIR.parent / "pr224_worktree"


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_ms(iso_text: str) -> int:
    dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _exec_db(path: Path, fn) -> None:  # noqa: ANN001
    with closing(sqlite3.connect(str(path))) as conn:
        with conn:
            fn(conn)


def _seed_healthy_dbs(root: Path, now_ms: int) -> dict[str, Path]:
    main_db = root / "dexscreener.db"
    deriv_db = root / "data" / "counterparty" / "public_derivatives.db"
    lseg_db = root / "data" / "lseg" / "lseg_macro.db"
    cityu_db = root / "data" / "cityu" / "cityu_institutional.db"
    spot_db = root / "execution_ledger_live_bnb_boll_fade.db"
    cm_db = root / "execution_ledger_live_bnb_cross_margin.db"

    for p in (main_db, deriv_db, lseg_db, cityu_db, spot_db, cm_db):
        p.parent.mkdir(parents=True, exist_ok=True)

    now_iso = preflight.fmt_ms_utc(now_ms)
    due_cot = preflight.eval_due_cftc_asof(now_ms).isoformat()

    def init_main(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY,
                tech_updated_at TEXT,
                kline_close_time_ms INTEGER
            );
            CREATE TABLE news_events (
                id INTEGER PRIMARY KEY,
                source_tier TEXT,
                fetched_at TEXT
            );
            CREATE TABLE decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                risk_passed INTEGER,
                purpose TEXT,
                reason TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO binance_indicators VALUES ('BTCUSDT', ?, ?)",
            (now_iso, now_ms - 60_000),
        )
        conn.execute("INSERT INTO news_events VALUES (1, 'whitelist', ?)", (now_iso,))
        conn.execute(
            "INSERT INTO decision_log (ts, strategy_id, action, risk_passed, purpose, reason) VALUES (?, 's1', 'buy', 1, 'live_bnb_cross_margin', 'ok')",
            (now_iso,),
        )

    def init_deriv(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE deribit_options_gamma (
                currency TEXT NOT NULL,
                ts_ms INTEGER NOT NULL,
                collected_at_ms INTEGER,
                PRIMARY KEY (currency, ts_ms)
            );
            CREATE TABLE binance_perp_counterparty (
                symbol TEXT NOT NULL,
                ts_ms INTEGER NOT NULL,
                PRIMARY KEY (symbol, ts_ms)
            );
            CREATE TABLE binance_spot_microstructure (
                symbol TEXT NOT NULL,
                ts_ms INTEGER NOT NULL,
                obi_top5_source_ts_ms INTEGER,
                PRIMARY KEY (symbol, ts_ms)
            );
            CREATE TABLE collector_fetch_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collected_at_ms INTEGER NOT NULL,
                category TEXT NOT NULL,
                error_code TEXT,
                message TEXT NOT NULL,
                fails_round INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO deribit_options_gamma VALUES ('BTC', ?, ?)",
            (now_ms - 120_000, now_ms - 5_000),
        )
        conn.execute(
            "INSERT INTO binance_perp_counterparty VALUES ('BTCUSDT', ?)",
            (now_ms - 60_000,),
        )
        conn.execute(
            "INSERT INTO binance_spot_microstructure VALUES ('BTCUSDT', ?, ?)",
            (now_ms - 30_000, now_ms - 30_000),
        )

    def init_lseg(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE cross_asset_bars (
                ric TEXT NOT NULL,
                interval TEXT NOT NULL,
                utc_ms INTEGER NOT NULL,
                close REAL,
                PRIMARY KEY (ric, interval, utc_ms)
            )
            """
        )
        for ric in ("NVDA.O", "SMH.O"):
            for i in range(13):
                conn.execute(
                    "INSERT INTO cross_asset_bars VALUES (?, '5min', ?, ?)",
                    (ric, now_ms - (i + 1) * 300_000, 140.0 + i * 0.1),
                )

    def init_cityu(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE lseg_cftc_cot_positioning (
                symbol TEXT,
                date TEXT,
                open_interest TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO lseg_cftc_cot_positioning VALUES ('BTC', ?, '25000')",
            (due_cot,),
        )

    def init_spot(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE execution_cursor (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
            CREATE TABLE execution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                validation_ok INTEGER,
                validation_errors TEXT,
                status TEXT
            );
            CREATE TABLE binance_spot_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_log_id INTEGER,
                symbol TEXT,
                status TEXT,
                last_error TEXT,
                unprotected_qty TEXT,
                residual_qty TEXT
            );
            """
        )
        conn.execute("INSERT INTO execution_cursor VALUES ('last_processed_id', '1', ?)", (now_iso,))

    def init_cm(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE intents (
                intent_id TEXT PRIMARY KEY,
                purpose TEXT NOT NULL,
                canonical TEXT NOT NULL,
                state TEXT NOT NULL,
                baseline_base TEXT,
                payload_json TEXT NOT NULL,
                updated_ms INTEGER NOT NULL
            );
            CREATE TABLE operations (
                intent_id TEXT NOT NULL,
                op_type TEXT NOT NULL,
                client_key TEXT NOT NULL,
                state TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_json TEXT,
                PRIMARY KEY (intent_id, op_type, client_key)
            );
            """
        )
        conn.execute(
            "INSERT INTO intents VALUES ('dl-149673', 'live_bnb_cross_margin', 'c1', 'clean_flat', '0', ?, ?)",
            (json.dumps({"intent": {"symbol": "DOGEUSDT"}}), now_ms - 600_000),
        )
        conn.execute(
            "INSERT INTO operations VALUES ('dl-149673', 'oco', 'dl-149673-oco-list', 'confirmed', '{}', ?)",
            (json.dumps({"oco_lot_residual_qty": "0.0714"}),),
        )

    _exec_db(main_db, init_main)
    _exec_db(deriv_db, init_deriv)
    _exec_db(lseg_db, init_lseg)
    _exec_db(cityu_db, init_cityu)
    _exec_db(spot_db, init_spot)
    _exec_db(cm_db, init_cm)

    return {
        "main_db": main_db,
        "deriv_db": deriv_db,
        "lseg_db": lseg_db,
        "cityu_db": cityu_db,
        "spot_db": spot_db,
        "cm_db": cm_db,
    }


class ProactiveWatchdogSuiteTest(unittest.TestCase):
    def test_01_strict_readonly_never_creates_or_mutates_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty_repo"
            root.mkdir(parents=True)
            absent_main = root / "nonexistent_dexscreener.db"
            report = preflight.inspect_system(
                root,
                now_ms=_utc_ms("2026-11-23T15:00:00Z"),
                main_db=str(absent_main),
                require_dbs=True,
            )
            self.assertFalse(absent_main.exists())
            self.assertEqual(report.overall_status, "RED")
            codes = {f.code for f in report.findings}
            self.assertIn("main_decision_db_absent", codes)
            self.assertIn("lseg_db_absent", codes)

            seeded_root = Path(tmp) / "seeded"
            now_ms = _utc_ms("2026-11-23T15:30:00Z")
            dbs = _seed_healthy_dbs(seeded_root, now_ms)
            before_hashes = {k: _sha256_bytes(p) for k, p in dbs.items()}
            preflight.inspect_system(
                seeded_root,
                now_ms=now_ms,
                spot_ledgers=[str(dbs["spot_db"])],
                require_dbs=True,
                ack_dust_intents=["dl-149673"],
            )
            after_hashes = {k: _sha256_bytes(p) for k, p in dbs.items()}
            self.assertEqual(before_hashes, after_hashes)

    def test_02_real_runtime_snapshot_and_v3_worktree_code_states(self) -> None:
        if RUNTIME_SNAPSHOT_DIR.is_dir():
            snap_report = preflight.inspect_system(
                RUNTIME_SNAPSHOT_DIR,
                now_ms=_utc_ms("2026-11-21T00:35:00+08:00"),
                require_dbs=False,
            )
            self.assertEqual(snap_report.code_state, "LIVE_PRE_PR224")
            self.assertEqual(snap_report.metrics["code"]["drifted_count"], 0)
            self.assertEqual(snap_report.red_count, 0)

        if V3_WORKTREE_DIR.is_dir():
            v3_report = preflight.inspect_system(
                V3_WORKTREE_DIR,
                now_ms=_utc_ms("2026-11-21T00:35:00+08:00"),
                require_dbs=False,
            )
            self.assertEqual(v3_report.code_state, "V3_PATCHED")
            self.assertEqual(v3_report.metrics["code"]["drifted_count"], 0)
            codes = {f.code for f in v3_report.findings}
            self.assertIn("code_integrity_v3_verified", codes)

    def test_03_detects_single_byte_drift_partial_install_and_deploy_sha_mismatch(self) -> None:
        if not V3_WORKTREE_DIR.is_dir():
            self.skipTest("pr224_worktree not present")
        with tempfile.TemporaryDirectory() as tmp:
            mini = Path(tmp) / "repo"
            for rel in preflight.KNOWN_FILE_HASHES:
                src = V3_WORKTREE_DIR / rel
                dst = mini / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

            (mini / "decision_engine" / "DEPLOY_SHA").write_text(
                "e7c8b68140af\n0000000000000000000000000000000000000000000000000000000000000000\n",
                encoding="utf-8",
            )
            rep_sha = preflight.inspect_system(mini, now_ms=_utc_ms("2026-11-21T00:00:00Z"))
            self.assertIn("deploy_sha_tree_mismatch", {f.code for f in rep_sha.findings if f.severity == "RED"})
            (mini / "decision_engine" / "DEPLOY_SHA").unlink()

            shutil.copy2(
                RUNTIME_SNAPSHOT_DIR / "source" / "execution" / "gateway.py",
                mini / "execution" / "gateway.py",
            )
            rep_mix = preflight.inspect_system(mini, now_ms=_utc_ms("2026-11-21T00:00:00Z"))
            self.assertEqual(rep_mix.code_state, "MIXED_PARTIAL_INSTALL")
            self.assertIn("partial_patch_mixed_state", {f.code for f in rep_mix.findings if f.severity == "RED"})

            with (mini / "execution" / "gateway.py").open("ab") as fh:
                fh.write(b"\n# unauthorized hotfix\n")
            rep_drift = preflight.inspect_system(mini, now_ms=_utc_ms("2026-11-21T00:00:00Z"))
            self.assertEqual(rep_drift.code_state, "UNTRACKED_DRIFT")
            self.assertIn("code_sha256_drift", {f.code for f in rep_drift.findings if f.severity == "RED"})

    def test_04_lseg_offhours_yellow_vs_market_open_red_and_early_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            mon_open_ms = _utc_ms("2026-11-23T15:00:00Z")  # Mon 10:00 EST
            dbs = _seed_healthy_dbs(root, mon_open_ms)

            frozen_ms = _utc_ms("2026-11-09T09:25:00Z")

            def freeze_lseg(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM cross_asset_bars")
                for ric in ("NVDA.O", "SMH.O"):
                    for i in range(13):
                        conn.execute(
                            "INSERT INTO cross_asset_bars VALUES (?, '5min', ?, 140.0)",
                            (ric, frozen_ms - i * 300_000),
                        )

            _exec_db(dbs["lseg_db"], freeze_lseg)

            # Saturday 2026-11-21T16:00:00Z = Saturday 11:00 EST (NYSE closed) -> YELLOW
            sat_ms = _utc_ms("2026-11-21T16:00:00Z")
            rep_sat = preflight.inspect_system(root, now_ms=sat_ms, spot_ledgers=[str(dbs["spot_db"])])
            sat_codes = {f.code: f.severity for f in rep_sat.findings}
            self.assertEqual(sat_codes.get("lseg_nvda_smh_stale_offhours"), "YELLOW")

            # Monday 10:00 EST regular cash open -> RED!
            rep_mon = preflight.inspect_system(root, now_ms=mon_open_ms, spot_ledgers=[str(dbs["spot_db"])])
            mon_codes = {f.code: f.severity for f in rep_mon.findings}
            self.assertEqual(mon_codes.get("lseg_nvda_smh_stale_market_open"), "RED")

            self.assertTrue(preflight.eval_nyse_cash_market_open(_utc_ms("2026-11-27T17:59:00Z")))
            self.assertFalse(preflight.eval_nyse_cash_market_open(_utc_ms("2026-11-27T18:00:00Z")))
            self.assertIsNone(preflight.eval_nyse_cash_market_open(_utc_ms("2029-03-05T15:00:00Z")))

    def test_05_deribit_collected_at_never_masks_expired_ts_ms_and_cot_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            now_ms = _utc_ms("2026-11-20T20:31:00Z")
            dbs = _seed_healthy_dbs(root, now_ms)

            _exec_db(
                dbs["deriv_db"],
                lambda c: (
                    c.execute("DELETE FROM deribit_options_gamma"),
                    c.execute(
                        "INSERT INTO deribit_options_gamma VALUES ('BTC', ?, ?)",
                        (now_ms - 3_605_000, now_ms - 1_000),
                    ),
                ),
            )
            _exec_db(
                dbs["cityu_db"],
                lambda c: c.execute("UPDATE lseg_cftc_cot_positioning SET date = '2026-11-10'"),
            )

            rep = preflight.inspect_system(root, now_ms=now_ms, spot_ledgers=[str(dbs["spot_db"])])
            red_codes = {f.code for f in rep.findings if f.severity == "RED"}
            self.assertIn("deribit_gamma_stale_or_invalid", red_codes)
            self.assertIn("cot_publication_not_current", red_codes)

    def test_06_spot_and_cross_margin_ledger_hazards_and_acked_dust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            now_ms = _utc_ms("2026-11-21T16:00:00Z")
            dbs = _seed_healthy_dbs(root, now_ms)

            rep_unacked = preflight.inspect_system(
                root, now_ms=now_ms, spot_ledgers=[str(dbs["spot_db"])]
            )
            self.assertIn(
                "cross_margin_lot_residual_dust",
                {f.code for f in rep_unacked.findings if f.severity == "YELLOW"},
            )

            rep_acked = preflight.inspect_system(
                root,
                now_ms=now_ms,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
            )
            self.assertNotIn(
                "cross_margin_lot_residual_dust",
                {f.code for f in rep_acked.findings},
            )

            def inject_cm_hazards(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "INSERT INTO intents VALUES ('dl-200001', 'live_bnb_cross_margin', 'c2', 'protection_unknown', '0', '{\"intent\":{\"symbol\":\"FETUSDT\"}}', ?)",
                    (now_ms - 5_000,),
                )
                conn.execute(
                    "INSERT INTO intents VALUES ('dl-200002', 'live_bnb_cross_margin', 'c3', 'reserved', '0', '{\"intent\":{\"symbol\":\"SOLUSDT\"}}', ?)",
                    (now_ms - 22_000,),
                )
                conn.execute(
                    "INSERT INTO operations VALUES ('dl-200001', 'oco', 'dl-200001-oco-list', 'unknown', '{}', NULL)"
                )

            _exec_db(dbs["cm_db"], inject_cm_hazards)
            _exec_db(
                dbs["spot_db"],
                lambda c: c.execute(
                    "INSERT INTO binance_spot_positions (decision_log_id, symbol, status, last_error, unprotected_qty, residual_qty) VALUES (99, 'PEPEUSDT', 'oco_failed', 'fee_unverified', '1000', '0')"
                ),
            )

            rep_haz = preflight.inspect_system(
                root,
                now_ms=now_ms,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
            )
            red_codes = {f.code for f in rep_haz.findings if f.severity == "RED"}
            self.assertIn("cross_margin_unresolved_hazard_intents", red_codes)
            self.assertIn("cross_margin_reserved_stuck_over_15s", red_codes)
            self.assertIn("cross_margin_operations_indeterminate", red_codes)
            self.assertIn(f"spot_unprotected_or_unknown:{dbs['spot_db'].name}", red_codes)

    def test_07_watchdog_path_guards_dedup_2bar_escalation_and_resolved_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "live_repo"
            state_dir = base / "external_state"
            state_dir.mkdir(parents=True)
            state_file = state_dir / "watchdog_state.json"
            jsonl_file = state_dir / "alerts.jsonl"

            t0 = _utc_ms("2026-11-23T15:00:00Z")  # Mon 10:00 EST (NYSE Open)
            dbs = _seed_healthy_dbs(repo, t0)
            if V3_WORKTREE_DIR.is_dir():
                for rel in preflight.KNOWN_FILE_HASHES:
                    dst = repo / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(V3_WORKTREE_DIR / rel, dst)

            with self.assertRaises(watchdog.UnsafeWatchdogPathError):
                watchdog.validate_external_state_path(repo / "state.json", repo)
            with self.assertRaises(watchdog.UnsafeWatchdogPathError):
                watchdog.validate_external_state_path(state_dir / "bad.db", repo)

            pushed: list[tuple[str, dict]] = []

            def fake_poster(url: str, payload: object) -> tuple[bool, str]:
                pushed.append((url, dict(payload)))  # type: ignore[arg-type]
                return True, "http_200"

            _exec_db(
                dbs["deriv_db"],
                lambda c: c.execute("UPDATE deribit_options_gamma SET ts_ms = ?", (t0 - 4_000_000,)),
            )

            r1 = watchdog.run_watchdog_once(
                repo,
                state_file=state_file,
                alert_jsonl=jsonl_file,
                now_ms=t0,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
                webhook_url="https://hooks.example.invalid/quant",
                http_poster=fake_poster,
            )
            self.assertTrue(r1["should_push"])
            self.assertEqual(r1["event_type"], "NEW_ALERT")
            self.assertEqual(r1["consecutive_lockout_bars"], 1)
            self.assertEqual(len(pushed), 1)

            r1_dup = watchdog.run_watchdog_once(
                repo,
                state_file=state_file,
                alert_jsonl=jsonl_file,
                now_ms=t0 + 45_000,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
                webhook_url="https://hooks.example.invalid/quant",
                http_poster=fake_poster,
            )
            self.assertFalse(r1_dup["should_push"])
            self.assertEqual(r1_dup["event_type"], "QUIET")
            self.assertEqual(len(pushed), 1)

            t1 = t0 + 300_000
            r2 = watchdog.run_watchdog_once(
                repo,
                state_file=state_file,
                alert_jsonl=jsonl_file,
                now_ms=t1,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
                webhook_url="https://hooks.example.invalid/quant",
                http_poster=fake_poster,
            )
            self.assertTrue(r2["should_push"])
            self.assertEqual(r2["event_type"], "ESCALATED_SILENT_LOCKOUT")
            self.assertEqual(r2["consecutive_lockout_bars"], 2)
            self.assertIn("consecutive_5m_silent_clock_lockout", r2["red_codes"])
            self.assertEqual(len(pushed), 2)

            t2 = t1 + 300_000
            _exec_db(
                dbs["deriv_db"],
                lambda c: (
                    c.execute("UPDATE deribit_options_gamma SET ts_ms = ?", (t2 - 30_000,)),
                    c.execute("UPDATE binance_perp_counterparty SET ts_ms = ?", (t2 - 30_000,)),
                    c.execute(
                        "UPDATE binance_spot_microstructure SET ts_ms = ?, obi_top5_source_ts_ms = ?",
                        (t2 - 20_000, t2 - 20_000),
                    ),
                ),
            )
            _exec_db(
                dbs["main_db"],
                lambda c: (
                    c.execute(
                        "UPDATE binance_indicators SET tech_updated_at = ?, kline_close_time_ms = ?",
                        (preflight.fmt_ms_utc(t2), t2 - 30_000),
                    ),
                    c.execute("UPDATE news_events SET fetched_at = ?", (preflight.fmt_ms_utc(t2),)),
                ),
            )

            def refresh_lseg(conn: sqlite3.Connection) -> None:
                conn.execute("DELETE FROM cross_asset_bars")
                for ric in ("NVDA.O", "SMH.O"):
                    for i in range(13):
                        conn.execute(
                            "INSERT INTO cross_asset_bars VALUES (?, '5min', ?, 141.0)",
                            (ric, t2 - (i + 1) * 300_000),
                        )

            _exec_db(dbs["lseg_db"], refresh_lseg)

            r3 = watchdog.run_watchdog_once(
                repo,
                state_file=state_file,
                alert_jsonl=jsonl_file,
                now_ms=t2,
                spot_ledgers=[str(dbs["spot_db"])],
                ack_dust_intents=["dl-149673"],
                webhook_url="https://hooks.example.invalid/quant",
                http_poster=fake_poster,
            )
            self.assertTrue(r3["should_push"])
            self.assertEqual(r3["event_type"], "RESOLVED")
            self.assertEqual(r3["consecutive_lockout_bars"], 0)
            self.assertEqual(r3["red_codes"], [])
            self.assertEqual(len(pushed), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
