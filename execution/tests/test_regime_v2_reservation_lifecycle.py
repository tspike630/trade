"""Gateway and consumer lifecycle for regime v2 reservations.

The adapter is in-process and does not open a socket. The gateway, ledger,
and reservation store are the real ones.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from decision_engine.core.regime_v2_policy import enforce_regime_v2_sizing_and_caps
from decision_engine.core.regime_v2_reservation import (
    active_reservations,
    get_reservation,
    try_reserve,
)
from execution.executors.binance_spot import (
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceSpotAdapter,
)
from execution.gateway import ExecutionGateway
from execution.ledger import ExecutionLedger
from execution.reservation_lifecycle import bind_reservation_before_submit


def _reserve(
    path: str,
    *,
    decision_id: int,
    symbol: str,
    permitted: bool,
    max_concurrent: int = 3,
    max_total_pct: float = 0.72,
) -> str:
    result = try_reserve(
        path,
        {
            "binance_symbol": symbol,
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "entry_reason": "state1",
            "size_pct": 0.24,
            "decision_log_id": decision_id,
            "live_order_permitted": permitted,
            "mode": "live_stub" if permitted else "dry_run",
        },
        now_ms=1,
        open_symbols=[],
        open_count=0,
        cluster_counts={},
        open_exposure_pct=0.0,
        max_concurrent=max_concurrent,
        state4_max_concurrent=1,
        max_per_cluster=1,
        max_total_pct=max_total_pct,
        state4_max_total_pct=0.30,
        size_pct=0.24,
        cluster_key="",
        is_state4=False,
    )
    if not result.ok:
        raise RuntimeError(result.reason)
    return result.signal_key


def _row(
    *,
    decision_id: int,
    symbol: str,
    key: str,
    mode: str,
    permitted: bool,
) -> dict[str, object]:
    extras = {
        "binance_symbol": symbol,
        "market": "spot",
        "quote_asset": "USDT",
        "quote_order_qty": "25",
        "reservation_key": key,
        "live_order_permitted": permitted,
    }
    raw = {"pair_address": f"{symbol[:-4]}/USDT", "purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "extras": extras}
    return {
        "id": decision_id,
        "ts": "2026-11-19T00:00:00Z",
        "strategy_id": "tech_boll_fade_v1",
        "action": "buy",
        "chain_id": "binance",
        "token_address": symbol,
        "size_pct": 0.24,
        "reason": "test",
        "risk_passed": 1,
        "mode": mode,
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "raw_json": json.dumps(raw),
    }


def _write_decision(db: Path, row: dict[str, object]) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_log (
            id INTEGER PRIMARY KEY,
            ts TEXT, strategy_id TEXT, action TEXT, chain_id TEXT,
            token_address TEXT, size_pct REAL, reason TEXT, risk_passed INTEGER,
            mode TEXT, raw_json TEXT, purpose TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO decision_log (
            id, ts, strategy_id, action, chain_id, token_address, size_pct,
            reason, risk_passed, mode, raw_json, purpose
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["id"], row["ts"], row["strategy_id"], row["action"], row["chain_id"],
            row["token_address"], row["size_pct"], row["reason"], row["risk_passed"],
            row["mode"], row["raw_json"], row["purpose"],
        ),
    )
    conn.commit()
    conn.close()


class ScriptedSpot:
    """In-process adapter. ``execute`` records the call and returns a scripted result."""

    def __init__(self, result: dict[str, object], maintain_status: str = "open") -> None:
        self.result = result
        self.maintain_status = maintain_status
        self.flat_quantity: object = "0"
        self.flat_residual: object = "0"
        self.calls: list[dict[str, object]] = []

    def execute(self, sig: dict[str, object]) -> dict[str, object]:
        self.calls.append(dict(sig))
        return dict(self.result)

    def refresh_filled_qty(self, row: dict[str, object]) -> None:
        return None

    def maintain_held_position(
        self,
        row: dict[str, object],
        mark: object,
        persist: object = None,
    ) -> dict[str, object]:
        if callable(persist) and self.maintain_status != str(row.get("status") or ""):
            persist(
                {
                    "status": self.maintain_status,
                    "quantity": self.flat_quantity if self.maintain_status == "closed" else row.get("quantity"),
                    "residual_qty": self.flat_residual if self.maintain_status == "closed" else None,
                }
            )
        return {"book": self.maintain_status, "submitted": False}


def _held_result(decision_id: int, symbol: str) -> dict[str, object]:
    return {
        "ok": True,
        "dry_run": False,
        "executed": 1,
        "submitted": True,
        "order_post_attempted": True,
        "error": None,
        "symbol": symbol,
        "exchange_order_id": "99",
        "consumer_allowed": True,
        "arm_position": {
            "decision_log_id": decision_id,
            "symbol": symbol,
            "quantity": "0.01",
            "stop_price": "90",
            "take_profit_price": "110",
            "entry_price": "100",
            "entry_order_id": "99",
            "status": "open",
            "protection": "native_oco",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        },
    }


def _bind_race(path: str, key: str, decision_id: int, symbol: str, out_path: str) -> None:
    os.environ["REGIME_V2_RESERVATION_DB"] = path
    err = bind_reservation_before_submit(
        {
            "decision_log_id": decision_id,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "token_address": symbol,
            "action": "buy",
            "extras": {
                "binance_symbol": symbol,
                "reservation_key": key,
                "live_order_permitted": True,
            },
        }
    )
    Path(out_path).write_text("ok" if err is None else str(err), encoding="utf-8")


def _consume_held(root: str, decision_id: int, symbol: str, key: str) -> None:
    os.environ["REGIME_V2_RESERVATION_DB"] = str(Path(root) / "reservations.sqlite")
    row = _row(
        decision_id=decision_id,
        symbol=symbol,
        key=key,
        mode="live_stub",
        permitted=True,
    )
    gateway = ExecutionGateway(
        decision_db=Path(root) / "decision.db",
        ledger_path=Path(root) / "ledger.db",
        paper_enabled=False,
        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
        binance_spot=ScriptedSpot(_held_result(decision_id, symbol)),
    )
    try:
        gateway.process_pending()
    finally:
        gateway.close()


class ReservationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = str(self.root / "reservations.sqlite")
        self._saved = os.environ.get("REGIME_V2_RESERVATION_DB")
        os.environ["REGIME_V2_RESERVATION_DB"] = self.db

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("REGIME_V2_RESERVATION_DB", None)
        else:
            os.environ["REGIME_V2_RESERVATION_DB"] = self._saved
        self._tmp.cleanup()

    def _gateway(self, adapter: object) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=self.root / "ledger.db",
            paper_enabled=False,
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            binance_spot=adapter,
        )

    def test_consumer_kill_switch_releases_a_definite_refusal(self) -> None:
        key = _reserve(self.db, decision_id=147841, symbol="BTCUSDT", permitted=True)
        row = _row(decision_id=147841, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True)
        _write_decision(self.root / "decision.db", row)
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        adapter = BinanceSpotAdapter(
            client=object(),
            kill_switch_path=kill,
            api_key="k",
            api_secret="s",
            clock_ms=lambda: 1,
        )
        os.environ["BINANCE_SPOT_LIVE_ENABLED"] = "1"
        os.environ["LIVE_KILL_SWITCH_PATH"] = str(kill)
        with self._gateway(adapter) as gateway:
            stats = gateway.process_pending()
        self.assertEqual(stats.processed, 1)
        self.assertEqual(active_reservations(self.db), [])
        stored = get_reservation(self.db, key)
        self.assertEqual(stored["status"], "released")
        self.assertEqual(stored["release_reason"], "definite_no_fill")

    def test_timeout_stays_unknown_across_replay_and_blocks_the_next_name(self) -> None:
        key = _reserve(self.db, decision_id=8, symbol="ETHUSDT", permitted=True)
        row = _row(decision_id=8, symbol="ETHUSDT", key=key, mode="live_stub", permitted=True)
        timeout = {
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "order_post_attempted": True,
            "error": "binance_timeout",
            "symbol": "ETHUSDT",
            "exchange_order_id": None,
            "consumer_allowed": False,
        }
        adapter = ScriptedSpot(timeout)
        with self._gateway(adapter) as gateway:
            first = gateway.process_one(row)
            second = gateway.process_one(dict(row))
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(first["error"], "binance_timeout")
        self.assertTrue(second.get("duplicate"))
        stored = get_reservation(self.db, key)
        self.assertEqual(stored["status"], "unknown")
        self.assertIsNone(stored["released_at_ms"])
        again = get_reservation(self.db, key)
        self.assertEqual(again["status"], "unknown")
        blocked = try_reserve(
            self.db,
            {
                "binance_symbol": "SOLUSDT",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "entry_reason": "state1",
                "size_pct": 0.24,
                "decision_log_id": 9,
                "live_order_permitted": True,
            },
            now_ms=2,
            open_symbols=[],
            open_count=0,
            cluster_counts={},
            open_exposure_pct=0.0,
            max_concurrent=1,
            state4_max_concurrent=1,
            max_per_cluster=1,
            max_total_pct=0.72,
            state4_max_total_pct=0.30,
            size_pct=0.24,
            cluster_key="",
            is_state4=False,
        )
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.reason, "max_concurrent_positions_reached")

    def test_exchange_400_releases_and_partial_then_flat_close_does_not_double_count(self) -> None:
        rejected_key = _reserve(self.db, decision_id=11, symbol="SOLUSDT", permitted=True)
        rejected = _row(
            decision_id=11, symbol="SOLUSDT", key=rejected_key, mode="live_stub", permitted=True
        )
        with self._gateway(ScriptedSpot({
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "order_post_attempted": True,
            "error": "binance_http_error:400:-1013:Filter failure",
            "symbol": "SOLUSDT",
            "exchange_error": {"status": 400, "code": -1013, "msg": "Filter failure"},
            "consumer_allowed": False,
        })) as gateway:
            gateway.process_one(rejected)
        self.assertEqual(get_reservation(self.db, rejected_key)["release_reason"], "exchange_rejected")

        key = _reserve(self.db, decision_id=147842, symbol="BTCUSDT", permitted=True)
        row = _row(decision_id=147842, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True)
        _write_decision(self.root / "decision.db", row)
        adapter = ScriptedSpot(_held_result(147842, "BTCUSDT"), maintain_status="oco_unknown")
        with self._gateway(adapter) as gateway:
            stats = gateway.process_pending()
            self.assertEqual(stats.executed_ok, 1)
            self.assertEqual(get_reservation(self.db, key)["status"], "held")
            kept = gateway.tick_binance_protective_exits(marks={"BTCUSDT": "100"})
            self.assertEqual(kept[0]["position_status"], "oco_unknown")
            self.assertEqual(get_reservation(self.db, key)["status"], "held")
            adapter.maintain_status = "closed"
            gateway.tick_binance_protective_exits(marks={"BTCUSDT": "100"})
        self.assertEqual(active_reservations(self.db), [])
        self.assertEqual(get_reservation(self.db, key)["release_reason"], "flat_close")

    def test_dry_run_cannot_release_or_submit_a_live_reservation(self) -> None:
        key = _reserve(self.db, decision_id=13, symbol="BTCUSDT", permitted=True)
        row = _row(decision_id=13, symbol="BTCUSDT", key=key, mode="dry_run", permitted=True)
        adapter = ScriptedSpot({"ok": True, "dry_run": True, "executed": 0, "submitted": False})
        with self._gateway(adapter) as gateway:
            result = gateway.process_one(row)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(result["error"], "reservation_mode_mismatch")
        self.assertEqual(get_reservation(self.db, key)["status"], "reserved")
        self.assertEqual(int(get_reservation(self.db, key)["live_order_permitted"]), 1)

    def test_identity_mismatch_and_missing_key_fail_closed(self) -> None:
        key = _reserve(self.db, decision_id=14, symbol="ETHUSDT", permitted=True)
        mismatched = _row(
            decision_id=14, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True
        )
        adapter = ScriptedSpot(_held_result(14, "BTCUSDT"))
        with self._gateway(adapter) as gateway:
            refused = gateway.process_one(mismatched)
            missing = gateway.process_one(
                _row(decision_id=15, symbol="BTCUSDT", key="", mode="live_stub", permitted=True)
            )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(refused["error"], "reservation_identity_mismatch")
        self.assertEqual(missing["error"], "reservation_identity_missing")
        self.assertEqual(get_reservation(self.db, key)["status"], "reserved")

    def test_two_processes_consume_one_decision_once(self) -> None:
        key = _reserve(self.db, decision_id=147843, symbol="BTCUSDT", permitted=True)
        row = _row(decision_id=147843, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True)
        _write_decision(self.root / "decision.db", row)
        ExecutionLedger(self.root / "ledger.db").close()
        ctx = multiprocessing.get_context("spawn")
        first = ctx.Process(target=_consume_held, args=(str(self.root), 147843, "BTCUSDT", key))
        second = ctx.Process(target=_consume_held, args=(str(self.root), 147843, "BTCUSDT", key))
        first.start()
        second.start()
        first.join(15)
        second.join(15)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        rows = active_reservations(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "held")

    def test_decision_cap_ignores_a_dry_run_reservation(self) -> None:
        _reserve(self.db, decision_id=17, symbol="SOLUSDT", permitted=False)
        os.environ["REGIME_V2_CONTRACT"] = "1"
        os.environ["REGIME_V2_OUTPUT_MODE"] = "dry_run"
        os.environ["REGIME_V2_DRY_RUN_EQUITY_USDT"] = "100000"
        try:
            kept, report = enforce_regime_v2_sizing_and_caps(
                [{
                    "action": "buy",
                    "binance_symbol": "BTCUSDT",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                    "size_pct": 0.24,
                    "market_regime": "positive_gamma_mean_reversion",
                    "live_order_permitted": False,
                    "mode": "dry_run",
                }],
                open_positions_override=[],
            )
        finally:
            os.environ.pop("REGIME_V2_CONTRACT", None)
            os.environ.pop("REGIME_V2_OUTPUT_MODE", None)
            os.environ.pop("REGIME_V2_DRY_RUN_EQUITY_USDT", None)
        self.assertEqual(len(kept), 1, report)
        live_rows = [row for row in active_reservations(self.db) if row["mode"] == "live"]
        dry_rows = [row for row in active_reservations(self.db) if row["mode"] == "dry_run"]
        self.assertEqual(len(dry_rows), 2)
        self.assertEqual(live_rows, [])

    def _reject_through_gateway(
        self,
        *,
        decision_id: int,
        symbol: str,
        exchange_error: dict[str, object],
        pending: bool,
    ) -> str:
        key = _reserve(
            self.db,
            decision_id=decision_id,
            symbol=symbol,
            permitted=True,
            max_concurrent=8,
            max_total_pct=2.0,
        )
        row = _row(
            decision_id=decision_id,
            symbol=symbol,
            key=key,
            mode="live_stub",
            permitted=True,
        )
        payload = {
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "order_post_attempted": True,
            "error": f"binance_http_error:400:{exchange_error.get('code')}:{exchange_error.get('msg')}",
            "symbol": symbol,
            "exchange_error": exchange_error,
            "exchange_order_id": None,
            "consumer_allowed": False,
        }
        if pending:
            _write_decision(self.root / "decision.db", row)
        with self._gateway(ScriptedSpot(payload)) as gateway:
            if pending:
                gateway.process_pending()
            else:
                gateway.process_one(row)
        return key

    def test_duplicate_client_id_2010_is_not_a_proven_no_fill(self) -> None:
        messages = (
            "Duplicate order sent.",
            "Duplicate order sent",
            "duplicate order sent.",
            "ClientOrderId is already in use.",
            "",
            "unlisted matching-engine text",
        )
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT")
        for index, message in enumerate(messages):
            key = self._reject_through_gateway(
                decision_id=147860 + index,
                symbol=symbols[index],
                exchange_error={"status": 400, "code": -2010, "msg": message},
                pending=index == 0,
            )
            stored = get_reservation(self.db, key)
            self.assertEqual(stored["status"], "unknown", message)
            self.assertIsNone(stored["released_at_ms"], message)
            reopened = sqlite3.connect(self.db)
            try:
                status, released = reopened.execute(
                    "SELECT status, released_at_ms FROM regime_v2_reservation WHERE signal_key = ?",
                    (key,),
                ).fetchone()
            finally:
                reopened.close()
            self.assertEqual(status, "unknown", message)
            self.assertIsNone(released, message)

    def test_insufficient_balance_2010_still_releases_and_filter_reject_stays(self) -> None:
        balance = self._reject_through_gateway(
            decision_id=147870,
            symbol="SOLUSDT",
            exchange_error={
                "status": 400,
                "code": -2010,
                "msg": "Account has insufficient balance for requested action.",
            },
            pending=False,
        )
        self.assertEqual(get_reservation(self.db, balance)["release_reason"], "exchange_rejected")
        filtered = self._reject_through_gateway(
            decision_id=147871,
            symbol="BNBUSDT",
            exchange_error={"status": 400, "code": -1013, "msg": "Filter failure: LOT_SIZE"},
            pending=True,
        )
        self.assertEqual(get_reservation(self.db, filtered)["release_reason"], "exchange_rejected")
        text_code = self._reject_through_gateway(
            decision_id=147872,
            symbol="XRPUSDT",
            exchange_error={"status": 400, "code": "-2010", "msg": "Duplicate order sent."},
            pending=False,
        )
        self.assertEqual(get_reservation(self.db, text_code)["status"], "unknown")

    def test_code_1013_duplicate_sentence_stays_occupied_and_is_not_posted_again(self) -> None:
        key = self._reject_through_gateway(
            decision_id=147880,
            symbol="BTCUSDT",
            exchange_error={"status": 400, "code": -1013, "msg": "Duplicate order sent."},
            pending=False,
        )
        stored = get_reservation(self.db, key)
        self.assertEqual(stored["status"], "unknown")
        self.assertIsNone(stored["released_at_ms"])
        row = _row(
            decision_id=147880,
            symbol="BTCUSDT",
            key=key,
            mode="live_stub",
            permitted=True,
        )
        _write_decision(self.root / "decision.db", row)
        adapter = ScriptedSpot(
            {
                "ok": False,
                "submitted": False,
                "order_post_attempted": True,
                "exchange_error": {"status": 400, "code": -1013, "msg": "Duplicate order sent."},
            }
        )
        with self._gateway(adapter) as gateway:
            gateway.process_one(row)
        self.assertEqual(adapter.calls, [])
        again = get_reservation(self.db, key)
        self.assertEqual(again["status"], "unknown")
        self.assertIsNone(again["released_at_ms"])

    def test_http_400_unknown_execution_code_stays_occupied(self) -> None:
        key = _reserve(self.db, decision_id=147850, symbol="BTCUSDT", permitted=True)
        row = _row(
            decision_id=147850, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True
        )
        with self._gateway(ScriptedSpot({
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "order_post_attempted": True,
            "error": "binance_http_error:400:-1007:Timeout waiting for response from backend server",
            "symbol": "BTCUSDT",
            "exchange_error": {"status": 400, "code": -1007, "msg": "Timeout waiting for response from backend server. Send status unknown; execution status unknown."},
            "exchange_order_id": None,
            "consumer_allowed": False,
        })) as gateway:
            gateway.process_one(row)
        stored = get_reservation(self.db, key)
        self.assertEqual(stored["status"], "unknown")
        self.assertIsNone(stored["released_at_ms"])

    def test_submit_right_is_once_and_crash_does_not_post_again(self) -> None:
        key = _reserve(self.db, decision_id=147851, symbol="ETHUSDT", permitted=True)
        sig = {
            "decision_log_id": 147851,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            "token_address": "ETHUSDT",
            "action": "buy",
            "extras": {
                "binance_symbol": "ETHUSDT",
                "reservation_key": key,
                "live_order_permitted": True,
            },
        }
        results: list[str | None] = []

        def _once() -> None:
            err = bind_reservation_before_submit(dict(sig))
            results.append(err)

        threads = [threading.Thread(target=_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(results.count(None), 1)
        self.assertIn("reservation_submit_not_owned", results)
        self.assertEqual(get_reservation(self.db, key)["status"], "submit_pending")

        calls: list[int] = []

        class _CrashAfterPost:
            def execute(self, signal: dict[str, object]) -> dict[str, object]:
                calls.append(1)
                raise RuntimeError("crash_after_post")

            def refresh_filled_qty(self, row: dict[str, object]) -> None:
                return None

            def maintain_held_position(self, row: dict[str, object], mark: object, persist: object = None) -> dict[str, object]:
                return {"book": "open", "submitted": False}

        fresh_key = _reserve(self.db, decision_id=147852, symbol="SOLUSDT", permitted=True)
        fresh = _row(
            decision_id=147852, symbol="SOLUSDT", key=fresh_key, mode="live_stub", permitted=True
        )
        with self._gateway(_CrashAfterPost()) as gateway:
            crashed = gateway.process_one(fresh)
            self.assertIn("crash_after_post", str(crashed.get("error")))
            self.assertEqual(get_reservation(self.db, fresh_key)["status"], "submit_pending")
            gateway.ledger.conn.execute(
                """
                UPDATE execution_log
                SET status = 'claimed', status_updated_at = '2000-01-01T00:00:00+00:00'
                WHERE decision_log_id = ?
                """,
                (147852,),
            )
            gateway.ledger.conn.commit()
            gateway.process_one(dict(fresh))
        self.assertEqual(calls, [1])
        self.assertEqual(get_reservation(self.db, fresh_key)["status"], "submit_pending")
        self.assertIsNone(get_reservation(self.db, fresh_key)["released_at_ms"])

        race_key = _reserve(self.db, decision_id=147854, symbol="BNBUSDT", permitted=True)
        ctx = multiprocessing.get_context("spawn")
        first_out = str(self.root / "race-a.txt")
        second_out = str(self.root / "race-b.txt")
        first = ctx.Process(
            target=_bind_race,
            args=(self.db, race_key, 147854, "BNBUSDT", first_out),
        )
        second = ctx.Process(
            target=_bind_race,
            args=(self.db, race_key, 147854, "BNBUSDT", second_out),
        )
        first.start()
        second.start()
        first.join(15)
        second.join(15)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        outcomes = {
            Path(first_out).read_text(encoding="utf-8"),
            Path(second_out).read_text(encoding="utf-8"),
        }
        self.assertEqual(outcomes, {"ok", "reservation_submit_not_owned"})
        self.assertEqual(get_reservation(self.db, race_key)["status"], "submit_pending")

    def test_nonzero_qty_without_residual_is_not_a_flat_close(self) -> None:
        key = _reserve(self.db, decision_id=147853, symbol="BTCUSDT", permitted=True)
        row = _row(decision_id=147853, symbol="BTCUSDT", key=key, mode="live_stub", permitted=True)
        _write_decision(self.root / "decision.db", row)
        posted = {
            "ok": False,
            "dry_run": False,
            "executed": 0,
            "submitted": False,
            "order_post_attempted": True,
            "error": "binance_http_error:400:-1007:Timeout",
            "symbol": "BTCUSDT",
            "exchange_error": {"status": 400, "code": -1007, "msg": "execution status unknown"},
            "consumer_allowed": False,
        }
        adapter = ScriptedSpot(posted, maintain_status="closed")
        adapter.flat_quantity = "1"
        adapter.flat_residual = None
        with self._gateway(adapter) as gateway:
            gateway.process_one(row)
            self.assertEqual(get_reservation(self.db, key)["status"], "unknown")
            gateway.ledger.arm_binance_spot_position({
                "decision_log_id": 147853,
                "symbol": "BTCUSDT",
                "quantity": "1",
                "stop_price": "90",
                "take_profit_price": "110",
                "entry_price": "100",
                "entry_order_id": "99",
                "status": "open",
                "protection": "native_oco",
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
            })
            gateway.tick_binance_protective_exits(marks={"BTCUSDT": "100"})
        stored = get_reservation(self.db, key)
        self.assertIsNone(stored["released_at_ms"])
        self.assertNotEqual(stored["release_reason"], "flat_close")
        self.assertIn(stored["status"], {"unknown", "held", "submit_pending"})
