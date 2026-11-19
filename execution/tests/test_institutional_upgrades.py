"""Unit & integration tests for the 3-step Wall Street institutional upgrades.

Covers:
1. Step 1A: ``SpotUserDataStreamProcessor`` (real-time ``executionReport``, ``listStatus``,
   ``outboundAccountPosition``, out-of-order replay rejection, and SQLite OCO / maker
   reconciliation with explicit ``SELL`` leg verification).
2. Step 1B: Out-of-process ``RiskSignerSubprocessClient`` & ``RiskSignerEngine``
   (true separate-process key isolation with zero secrets in parent ``os.environ``,
   hard borrow / daily-loss / notional ceilings, MCP UID ``1289917075`` block, and
   ``LIVE_TRADING_DISABLED`` kill-switch enforcement).
3. Step 2A: Closed-loop 5s & 60s ``MarkoutTcaStore`` -> ``build_tca_report`` ->
   ``compute_markout_adaptive_adjustment`` pre-trade toxicity feedback.
4. Step 3: Event-driven ``L2QueueReplaySimulator`` with ``WallMid`` 1-tick pennying,
   gateway RTT latency, ``EXPIRED_IN_MATCH`` crossing rejection, conservative FIFO
   ``queue_ahead_qty`` penetration filtering out fake touch-without-penetration fills,
   and 5s/60s adverse markout measurement.
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from execution.l2_queue_replay_sim import L2QueueReplaySimulator
from execution.markout_tca import (
    ENV_MARKOUT_FEEDBACK_ENABLED,
    MarkoutTcaStore,
    compute_markout_adaptive_adjustment,
    markout_feedback_enabled,
)
from execution.risk_signer_daemon import (
    MCP_FORBIDDEN_UID,
    RiskPolicyLimits,
    RiskSignerEngine,
    RiskSignerRejected,
    RiskSignerSubprocessClient,
    risk_signer_enabled,
)
from execution.tca_report import build_tca_report, format_tca_markdown
from execution.user_data_stream import (
    SpotUserDataStreamProcessor,
    user_data_stream_enabled,
)


class InstitutionalUpgradeExecutionTests(unittest.TestCase):
    def test_all_new_institutional_switches_default_off(self) -> None:
        clean_env: dict[str, str] = {}
        self.assertFalse(risk_signer_enabled(clean_env))
        self.assertFalse(user_data_stream_enabled(clean_env))
        self.assertFalse(markout_feedback_enabled(clean_env))

    def test_out_of_process_risk_signer_isolates_keys_and_enforces_hard_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            kill_file = tmp_dir / "LIVE_TRADING_DISABLED"
            secret_file = tmp_dir / "signer_secrets.json"
            secret_file.write_text(
                json.dumps(
                    {
                        "api_key": "PUBKEY999",
                        "api_secret": "ULTRA_PRIVATE_SIGNER_SECRET_XYZ",
                        "approval_hmac_key": "OPERATOR_OFFLINE_HMAC_KEY_ABC",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(secret_file, 0o600)

            now_ms = int(__import__("time").time() * 1000)
            cfg_sha = "a" * 64
            limits = RiskPolicyLimits(
                approved_account_uid="888000111",
                approved_config_sha256=cfg_sha,
                approval_expires_at_ms=now_ms + 3_600_000,
                max_single_order_notional_usd=Decimal("300"),
                max_symbol_notional_usd=Decimal("500"),
                max_gross_exposure_usd=Decimal("800"),
                max_daily_loss_usd=Decimal("150"),
                max_single_borrow_usd=Decimal("400"),
                max_borrow_by_asset={"SOL": Decimal("3.0"), "USDT": Decimal("400")},
                asset_reference_prices_usd={"SOL": Decimal("100"), "SOLUSDT": Decimal("100")},
                kill_switch_path=str(kill_file),
            )

            with RiskSignerSubprocessClient(
                api_key="PUBKEY999",
                bootstrap_secret_file=secret_file,
                limits=limits,
                python_executable=sys.executable,
            ) as client:
                secret_file.unlink()
                self.assertNotEqual(client.worker_pid, os.getpid())
                for env_val in os.environ.values():
                    self.assertNotIn("ULTRA_PRIVATE_SIGNER_SECRET_XYZ", env_val)

                self.assertTrue(
                    client.attest_approval(
                        config_sha256=cfg_sha,
                        account_uid="888000111",
                        now_ms=now_ms,
                    )
                )
                self.assertFalse(
                    client.attest_approval(
                        config_sha256="b" * 64,
                        account_uid="888000111",
                        now_ms=now_ms,
                    )
                )
                self.assertFalse(
                    client.attest_approval(
                        config_sha256=cfg_sha,
                        account_uid=MCP_FORBIDDEN_UID,
                        now_ms=now_ms,
                    )
                )

                ts = str(int(__import__("time").time() * 1000))
                signed_ok = client.sign_request(
                    venue="binance_spot",
                    method="POST",
                    path="/api/v3/order",
                    pairs=[
                        ("symbol", "SOLUSDT"),
                        ("side", "BUY"),
                        ("type", "MARKET"),
                        ("quoteOrderQty", "200"),
                        ("recvWindow", "5000"),
                        ("timestamp", ts),
                    ],
                    account_uid="888000111",
                )
                self.assertIn("&signature=", signed_ok)

                ref_engine = RiskSignerEngine(
                    api_key="PUBKEY999",
                    api_secret="ULTRA_PRIVATE_SIGNER_SECRET_XYZ",
                    limits=limits,
                    clock_ms=lambda: int(ts),
                )
                expected_sig = ref_engine.sign_request(
                    venue="binance_spot",
                    method="POST",
                    path="/api/v3/order",
                    pairs=[
                        ("symbol", "SOLUSDT"),
                        ("side", "BUY"),
                        ("type", "MARKET"),
                        ("quoteOrderQty", "200"),
                        ("recvWindow", "5000"),
                        ("timestamp", ts),
                    ],
                    account_uid="888000111",
                )
                self.assertEqual(signed_ok, expected_sig)

                with self.assertRaises(RiskSignerRejected) as ctx_fat:
                    client.sign_request(
                        venue="binance_spot",
                        method="POST",
                        path="/api/v3/order",
                        pairs=[
                            ("symbol", "SOLUSDT"),
                            ("side", "BUY"),
                            ("type", "MARKET"),
                            ("quoteOrderQty", "350"),
                            ("recvWindow", "5000"),
                            ("timestamp", ts),
                        ],
                        account_uid="888000111",
                    )
                self.assertIn("max_single_order_notional_exceeded", ctx_fat.exception.reason)

                with self.assertRaises(RiskSignerRejected) as ctx_bor:
                    client.sign_request(
                        venue="binance_cross_margin",
                        method="POST",
                        path="/sapi/v1/margin/borrow-repay",
                        pairs=[
                            ("asset", "SOL"),
                            ("isIsolated", "FALSE"),
                            ("amount", "3.5"),
                            ("type", "BORROW"),
                            ("recvWindow", "5000"),
                            ("timestamp", ts),
                        ],
                        account_uid="888000111",
                    )
                self.assertIn("max_borrow_exceeded:SOL", ctx_bor.exception.reason)

                client.update_risk_state(daily_pnl_usd="-160.0")
                with self.assertRaises(RiskSignerRejected) as ctx_loss:
                    client.sign_request(
                        venue="binance_spot",
                        method="POST",
                        path="/api/v3/order",
                        pairs=[
                            ("symbol", "SOLUSDT"),
                            ("side", "BUY"),
                            ("type", "MARKET"),
                            ("quoteOrderQty", "50"),
                            ("recvWindow", "5000"),
                            ("timestamp", ts),
                        ],
                        account_uid="888000111",
                    )
                self.assertIn("daily_loss_limit_breached", ctx_loss.exception.reason)

                kill_file.write_text("halt\n", encoding="utf-8")
                signed_repay = client.sign_request(
                    venue="binance_cross_margin",
                    method="POST",
                    path="/sapi/v1/margin/borrow-repay",
                    pairs=[
                        ("asset", "SOL"),
                        ("isIsolated", "FALSE"),
                        ("amount", "1.0"),
                        ("type", "REPAY"),
                        ("recvWindow", "5000"),
                        ("timestamp", ts),
                    ],
                    account_uid="888000111",
                    for_protective_or_repay=True,
                )
                self.assertIn("&signature=", signed_repay)

    def test_user_data_stream_reconciles_sqlite_and_blocks_buy_oco_spoof_or_stale_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "exec_ledger.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                """
                CREATE TABLE binance_spot_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    status TEXT,
                    protection TEXT,
                    quantity TEXT,
                    entry_price TEXT,
                    entry_order_id TEXT,
                    oco_order_list_id TEXT,
                    oco_list_client_id TEXT,
                    oco_list_status TEXT,
                    oco_above_order_id TEXT,
                    oco_below_order_id TEXT,
                    exit_order_id TEXT,
                    exit_reason TEXT,
                    exit_filled_qty TEXT,
                    exit_price TEXT,
                    unprotected_qty TEXT,
                    manual_action TEXT,
                    last_error TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO binance_spot_positions (
                    symbol, status, protection, quantity, entry_price, entry_order_id
                ) VALUES ('SOLUSDT', 'maker_resting', 'maker_resting', NULL, '100.0', '5001')
                """
            )
            conn.execute(
                """
                INSERT INTO binance_spot_positions (
                    symbol, status, protection, quantity, entry_price, entry_order_id,
                    oco_order_list_id, oco_list_client_id, unprotected_qty, manual_action, last_error
                ) VALUES (
                    'AVAXUSDT', 'oco_unknown', 'pending_oco', '2.0', '25.0', '6001',
                    '9901', 'bnbfade-oco-99', '2.0', 'halt', 'oco_submit_timeout'
                )
                """
            )
            conn.commit()
            conn.close()

            proc = SpotUserDataStreamProcessor(ledger_path=db_path, max_silence_ms=10_000)

            r_bal = proc.ingest_raw(
                {
                    "e": "outboundAccountPosition",
                    "E": 1_700_000_001_000,
                    "u": 1_700_000_001_000,
                    "B": [{"a": "USDT", "f": "850.5", "l": "100.0"}],
                },
                now_mono_ms=1000,
            )
            self.assertTrue(r_bal["accepted"])
            bal = proc.get_balance("USDT", now_mono_ms=2000)
            assert bal is not None
            self.assertEqual(bal.free, Decimal("850.5"))

            r_stale = proc.ingest_raw(
                {
                    "e": "outboundAccountPosition",
                    "E": 1_700_000_000_500,
                    "u": 1_700_000_000_500,
                    "B": [{"a": "USDT", "f": "10.0", "l": "0.0"}],
                },
                now_mono_ms=2100,
            )
            self.assertFalse(r_stale["accepted"])
            self.assertEqual(r_stale["reason"], "stale_account_event")

            r_buy = proc.ingest_raw(
                {
                    "e": "executionReport",
                    "E": 1_700_000_002_000,
                    "T": 1_700_000_002_000,
                    "s": "SOLUSDT",
                    "i": 5001,
                    "c": "bnbfade-1",
                    "S": "BUY",
                    "o": "LIMIT_MAKER",
                    "x": "TRADE",
                    "X": "FILLED",
                    "q": "2.0",
                    "z": "2.0",
                    "Z": "200.0",
                    "l": "2.0",
                    "L": "100.0",
                    "n": "0.002",
                    "N": "SOL",
                    "t": 77,
                },
                now_mono_ms=2200,
            )
            self.assertEqual(r_buy["ledger_rows_updated"], 1)

            for oid, otype in ((7001, "LIMIT_MAKER"), (7002, "STOP_LOSS_LIMIT")):
                proc.ingest_raw(
                    {
                        "e": "executionReport",
                        "E": 1_700_000_003_000,
                        "T": 1_700_000_003_000,
                        "s": "AVAXUSDT",
                        "i": oid,
                        "S": "BUY",
                        "o": otype,
                        "x": "NEW",
                        "X": "NEW",
                        "g": 9901,
                        "q": "2.0",
                        "z": "0",
                        "Z": "0",
                    },
                    now_mono_ms=2300,
                )
            r_bad_list = proc.ingest_raw(
                {
                    "e": "listStatus",
                    "E": 1_700_000_003_100,
                    "T": 1_700_000_003_100,
                    "s": "AVAXUSDT",
                    "g": 9901,
                    "c": "OCO",
                    "l": "EXEC_STARTED",
                    "L": "EXECUTING",
                    "C": "bnbfade-oco-99",
                    "O": [
                        {"s": "AVAXUSDT", "i": 7001, "c": "tp"},
                        {"s": "AVAXUSDT", "i": 7002, "c": "sl"},
                    ],
                },
                now_mono_ms=2350,
            )
            self.assertEqual(r_bad_list["ledger_rows_updated"], 0)

            for oid, otype in ((8001, "LIMIT_MAKER"), (8002, "STOP_LOSS_LIMIT")):
                proc.ingest_raw(
                    {
                        "e": "executionReport",
                        "E": 1_700_000_004_000,
                        "T": 1_700_000_004_000,
                        "s": "AVAXUSDT",
                        "i": oid,
                        "S": "SELL",
                        "o": otype,
                        "x": "NEW",
                        "X": "NEW",
                        "g": 9901,
                        "q": "2.0",
                        "z": "0",
                        "Z": "0",
                    },
                    now_mono_ms=2400,
                )
            r_good_list = proc.ingest_raw(
                {
                    "e": "listStatus",
                    "E": 1_700_000_004_100,
                    "T": 1_700_000_004_100,
                    "s": "AVAXUSDT",
                    "g": 9901,
                    "c": "OCO",
                    "l": "EXEC_STARTED",
                    "L": "EXECUTING",
                    "C": "bnbfade-oco-99",
                    "O": [
                        {"s": "AVAXUSDT", "i": 8001, "c": "tp"},
                        {"s": "AVAXUSDT", "i": 8002, "c": "sl"},
                    ],
                },
                now_mono_ms=2450,
            )
            self.assertEqual(r_good_list["ledger_rows_updated"], 1)

            r_tp_fill = proc.ingest_raw(
                {
                    "e": "executionReport",
                    "E": 1_700_000_005_000,
                    "T": 1_700_000_005_000,
                    "s": "AVAXUSDT",
                    "i": 8001,
                    "S": "SELL",
                    "o": "LIMIT_MAKER",
                    "x": "TRADE",
                    "X": "FILLED",
                    "g": 9901,
                    "q": "2.0",
                    "z": "2.0",
                    "Z": "54.0",
                    "l": "2.0",
                    "L": "27.0",
                    "n": "0.05",
                    "N": "USDT",
                    "t": 88,
                },
                now_mono_ms=2500,
            )
            self.assertEqual(r_tp_fill["ledger_rows_updated"], 1)

            check_conn = sqlite3.connect(str(db_path))
            check_conn.row_factory = sqlite3.Row
            rows = {
                r["symbol"]: dict(r)
                for r in check_conn.execute("SELECT * FROM binance_spot_positions").fetchall()
            }
            check_conn.close()
            self.assertEqual(rows["SOLUSDT"]["status"], "open")
            self.assertEqual(rows["SOLUSDT"]["quantity"], "1.998")
            self.assertEqual(rows["AVAXUSDT"]["status"], "oco_filled")
            self.assertEqual(rows["AVAXUSDT"]["exit_reason"], "take_profit")
            self.assertEqual(rows["AVAXUSDT"]["exit_price"], "27")
            self.assertIsNone(rows["AVAXUSDT"]["unprotected_qty"])

    def test_closed_loop_markout_tca_store_report_and_adaptive_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_db = Path(tmp) / "execution_ledger.db"
            store = MarkoutTcaStore(ledger_db)

            for idx in range(4):
                t0 = 1_700_000_000_000 + idx * 100_000
                store.record_fill(
                    symbol="WIFUSDT",
                    order_id=f"wif-{idx}",
                    side="BUY",
                    order_type="LIMIT_MAKER",
                    fill_price=2.0000,
                    fill_qty=50.0,
                    fill_ts_ms=t0,
                    mid_at_fill=2.0000,
                )
                store.observe_mid_price(symbol="WIFUSDT", mid_price=1.9964, ts_ms=t0 + 5_200)
                store.observe_mid_price(symbol="WIFUSDT", mid_price=1.9936, ts_ms=t0 + 61_000)

            for idx in range(3):
                t0 = 1_700_001_000_000 + idx * 100_000
                store.record_fill(
                    symbol="BTCUSDT",
                    order_id=f"btc-{idx}",
                    side="BUY",
                    order_type="LIMIT_MAKER",
                    fill_price=90_000.0,
                    fill_qty=0.01,
                    fill_ts_ms=t0,
                    mid_at_fill=90_000.0,
                    mid_5s=90_036.0,
                    mid_60s=90_072.0,
                )

            report = build_tca_report(ledger_path=ledger_db)
            mk = report["markout_toxicity"]
            self.assertEqual(mk["observations_total"], 7)
            self.assertAlmostEqual(mk["by_symbol"]["WIFUSDT"]["mean_markout_5s_bps"], -18.0, places=2)
            self.assertAlmostEqual(mk["by_symbol"]["WIFUSDT"]["mean_markout_60s_bps"], -32.0, places=2)
            self.assertEqual(mk["by_symbol"]["WIFUSDT"]["toxic_fill_rate_5s"], 1.0)
            self.assertAlmostEqual(mk["by_symbol"]["BTCUSDT"]["mean_markout_5s_bps"], 4.0, places=2)
            md = format_tca_markdown(report)
            self.assertIn("Post-Fill Markout & Adverse Selection", md)

            adj_off = compute_markout_adaptive_adjustment(
                ledger_db, symbol="WIFUSDT", environ={}
            )
            self.assertFalse(adj_off.applied)
            self.assertEqual(adj_off.size_multiplier, 1.0)
            self.assertIsNone(adj_off.block_reason)

            adj_halt = compute_markout_adaptive_adjustment(
                ledger_db,
                symbol="WIFUSDT",
                environ={ENV_MARKOUT_FEEDBACK_ENABLED: "1"},
            )
            self.assertTrue(adj_halt.applied)
            self.assertEqual(adj_halt.size_multiplier, 0.0)
            self.assertEqual(adj_halt.block_reason, "markout_toxicity_halt:WIFUSDT")

            adj_btc = compute_markout_adaptive_adjustment(
                ledger_db,
                symbol="BTCUSDT",
                environ={ENV_MARKOUT_FEEDBACK_ENABLED: "1"},
            )
            self.assertTrue(adj_btc.applied)
            self.assertEqual(adj_btc.size_multiplier, 1.0)
            self.assertIsNone(adj_btc.block_reason)

    def test_l2_queue_replay_simulator_filters_fake_touch_alpha_and_measures_adverse_markout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mk_store = MarkoutTcaStore(Path(tmp) / "sim_tca.db")
            sim = L2QueueReplaySimulator(gateway_latency_ms=25, markout_store=mk_store)

            sim.step_raw_event(
                {
                    "stream": "solusdt@depth20@100ms",
                    "data": {
                        "E": 1000,
                        "s": "SOLUSDT",
                        "bids": [["100.02", "10"], ["100.01", "8"], ["100.00", "500"]],
                        "asks": [["100.05", "20"], ["100.06", "25"], ["100.10", "400"]],
                    },
                }
            )

            ord_queue = sim.submit_order(
                symbol="SOLUSDT",
                side="BUY",
                order_type="LIMIT_MAKER",
                quantity="2.0",
                limit_price="100.02",
                submit_ts_ms=1000,
                order_id="ord-behind-queue",
            )
            ord_cross = sim.submit_order(
                symbol="SOLUSDT",
                side="BUY",
                order_type="LIMIT_MAKER",
                quantity="1.0",
                limit_price="100.05",
                submit_ts_ms=1000,
                order_id="ord-post-only-cross",
            )

            sim.step_raw_event(
                {
                    "stream": "solusdt@bookTicker",
                    "data": {"E": 1030, "s": "SOLUSDT", "b": "100.02", "B": "10", "a": "100.05", "A": "20"},
                }
            )
            self.assertEqual(ord_cross.status, "EXPIRED_IN_MATCH")
            self.assertEqual(ord_queue.status, "NEW")
            self.assertEqual(ord_queue.queue_ahead_qty, Decimal("10"))

            sim.step_raw_event(
                {
                    "stream": "solusdt@aggTrade",
                    "data": {"e": "aggTrade", "E": 1100, "T": 1100, "s": "SOLUSDT", "p": "100.02", "q": "4.0", "m": True, "a": 1},
                }
            )
            self.assertTrue(ord_queue.naive_touch_would_fill)
            self.assertEqual(ord_queue.filled_qty, Decimal("0"))
            self.assertEqual(ord_queue.queue_ahead_qty, Decimal("6.0"))

            mid_summary = sim.summary()
            self.assertEqual(mid_summary["false_alpha_filtered_count"], 1)

            sim.step_raw_event(
                {
                    "stream": "solusdt@aggTrade",
                    "data": {"e": "aggTrade", "E": 1200, "T": 1200, "s": "SOLUSDT", "p": "100.02", "q": "9.0", "m": True, "a": 2},
                }
            )
            self.assertEqual(ord_queue.status, "FILLED")
            self.assertEqual(ord_queue.filled_qty, Decimal("2.0"))

            sim.step_raw_event(
                {
                    "stream": "solusdt@bookTicker",
                    "data": {"E": 6500, "s": "SOLUSDT", "b": "99.80", "B": "50", "a": "99.84", "A": "50"},
                }
            )
            sim.step_raw_event(
                {
                    "stream": "solusdt@bookTicker",
                    "data": {"E": 62000, "s": "SOLUSDT", "b": "99.50", "B": "50", "a": "99.54", "A": "50"},
                }
            )
            self.assertIsNotNone(ord_queue.markout_5s_bps)
            self.assertLess(ord_queue.markout_5s_bps or 0.0, -15.0)
            self.assertLess(ord_queue.markout_60s_bps or 0.0, -45.0)

            sim.step_raw_event(
                {
                    "stream": "solusdt@depth20@100ms",
                    "data": {
                        "E": 63000,
                        "s": "SOLUSDT",
                        "bids": [["99.52", "5"], ["99.51", "5"], ["99.50", "800"]],
                        "asks": [["99.56", "5"], ["99.57", "5"], ["99.60", "800"]],
                    },
                }
            )
            wm_order = sim.submit_wall_mid_maker_order(
                symbol="SOLUSDT",
                side="BUY",
                quantity="1.5",
                tick_size=0.01,
                submit_ts_ms=63000,
                order_id="ord-wallmid",
            )
            self.assertEqual(wm_order.limit_price, Decimal("99.51"))


if __name__ == "__main__":
    unittest.main()
