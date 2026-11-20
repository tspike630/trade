"""Decision rows without intent_id stay refused.

The intent id is the decision row when the emitter did not send one.
A short -3045 snapshot stays a terminal borrow refusal and does not borrow
again. A cash long borrows nothing, so the same code on the unused base
does not refuse the open.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import sqlite3
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import patch

from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_CROSS_MARGIN
from execution.margin_cross import http_client as hc
from execution.margin_cross.entry import (
    _plan_from_signal_doc,
    _refused_plan,
    bind_document_intent_id,
    stable_decision_intent_id,
)
from execution.margin_cross.errors import MarginExchangeError
from execution.margin_cross.http_client import fetch_live_cross_margin_snapshot
from execution.margin_cross.policy import SUPPORTED_REGIME_STRATEGIES
from execution.run import main as execution_main
from execution.tests.test_margin_cross_http_and_live import (
    _InMemoryMarginTransport,
    _fresh_clocks,
)
from execution.tests.test_margin_live_production_path import (
    _ENV_KEYS,
    _TEST_API_KEY,
    _TEST_API_SECRET,
    _UID,
    _approved_file,
)


class _Blocked(socket.socket):
    def connect(self, address):  # type: ignore[override]
        raise OSError(f"intent_contract_network_blocked:{address!r}")


class _MaxBorrow3045(_InMemoryMarginTransport):
    def __call__(self, req: Any, timeout: float) -> tuple[int, bytes]:
        path = urllib.parse.urlsplit(req.full_url).path
        if path == "/sapi/v1/margin/maxBorrowable":
            self.requests.append(("GET", path, {}))
            body = json.dumps(
                {"code": -3045, "msg": "The system does not have enough asset now."}
            ).encode("utf-8")
            return 400, body
        return super().__call__(req, timeout)


class _BaseNotBorrowable(_InMemoryMarginTransport):
    """The base coin is listed and the pool query succeeds, but it cannot be borrowed."""

    def _dispatch(self, method: str, path: str, params: dict[str, str]) -> Any:
        if method == "GET" and path == "/sapi/v1/margin/crossMarginData":
            return [{"coin": params.get("coin", ""), "borrowable": False, "transferIn": True}]
        return super()._dispatch(method, path, params)


def _cash_long_squeeze_row() -> dict[str, Any]:
    """Regime 2 inputs that already pass the state-2 long gate. No threshold changes."""
    return {
        "funding_z": -2.2,
        "funding_rate": -0.0004,
        "global_account_ls_ratio": 1.15,
        "whale_retail_divergence": 1.05,
        "xs_breadth_1h": 0.45,
        "xs_rank_pct_1h": 0.70,
        "obi_top5": 0.12,
        "taker_buy_ratio": 0.54,
        "volume_z": 0.90,
        "cot_regime": "cot_neutral",
        "gamma_regime": "positive_gamma_pinned",
        "dvol_zscore": 0.20,
        "nvda_1h_ret_pct": 0.25,
        "smh_1h_ret_pct": 0.20,
        "lseg_market_open": True,
    }


def _evidence_extras(strategy_id: str, *, ledger: Path, intent_id: str | None = None) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "binance_symbol": "BTCUSDT",
        "requested_action": "OPEN_SHORT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "account_domain": "binance_cross_margin",
        "execution_mode": "live",
        "spot_book_full": False,
        "strategy_id": strategy_id,
        "size_pct": 1.0,
        "regime_size_multiplier": 1.0,
        "margin_ledger_path": str(ledger),
    }
    if intent_id is not None:
        extras["intent_id"] = intent_id
    return extras


class IntentContractTests(unittest.TestCase):
    def test_binding_is_stable_and_does_not_invent_a_random_id(self) -> None:
        self.assertEqual(stable_decision_intent_id(148661), "dl-148661")
        self.assertEqual(stable_decision_intent_id("148661"), "dl-148661")
        self.assertIsNone(stable_decision_intent_id(None))
        self.assertIsNone(stable_decision_intent_id(""))
        self.assertIsNone(stable_decision_intent_id(0))
        kept = bind_document_intent_id({"intent_id": " operator-1 ", "decision_log_id": 148661})
        self.assertEqual(kept["intent_id"], "operator-1")
        bound = bind_document_intent_id({"decision_log_id": 148661})
        self.assertEqual(bound["intent_id"], "dl-148661")
        absent = bind_document_intent_id({})
        self.assertNotIn("intent_id", absent)

    def test_plan_refuses_missing_prices_without_keyerror(self) -> None:
        raw = {
            "pair_address": "BNB/USDT",
            "purpose": PURPOSE_LIVE_BNB_CROSS_MARGIN,
            "extras": _evidence_extras("regime4_crowded_long_carry_short", ledger=Path("/tmp/unused")),
        }
        again = json.loads(json.dumps(raw))
        self.assertNotIn("intent_id", again)
        self.assertNotIn("intent_id", again["extras"])
        plan = _plan_from_signal_doc(
            {**again["extras"], "action": "OPEN_SHORT", "decision_log_id": 148661},
            {"uid": "1"},
            1,
        )
        self.assertFalse(plan.allowed)
        self.assertEqual(
            plan.reasons,
            ("order_fields_missing:take_profit_price,stop_price,stop_limit_price",),
        )
        self.assertIsNone(plan.intent)

    def test_snapshot_3045_names_the_subcall_and_does_not_borrow(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.borrowed = False

            def query_api_restrictions(self) -> dict[str, Any]:
                return {}

            def query_spot_account(self) -> dict[str, Any]:
                return {}

            def query_account(self) -> dict[str, Any]:
                return {}

            def query_max_borrow(self, _payload: dict[str, Any]) -> dict[str, Any]:
                raise MarginExchangeError(
                    -3045,
                    "The system does not have enough asset now.",
                    method="query_max_borrow",
                )

            def borrow(self, _payload: dict[str, Any]) -> dict[str, Any]:
                self.borrowed = True
                raise AssertionError("borrow must not run after -3045")

        client = _Client()
        snapshot, reason = fetch_live_cross_margin_snapshot(
            client,  # type: ignore[arg-type]
            symbol="BMTUSDT",
            base_asset="BMT",
            quote_asset="USDT",
            borrow_asset="BMT",
        )
        self.assertIsNone(snapshot)
        self.assertEqual(reason, "live_snapshot_exchange_error:-3045:query_max_borrow")
        self.assertFalse(client.borrowed)

    def test_consumer_binds_decision_intent_and_keeps_3045_terminal(self) -> None:
        self.assertEqual(len(SUPPORTED_REGIME_STRATEGIES), 8)
        saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        transport = _MaxBorrow3045(cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID))
        hc._default_opener = transport
        try:
            with self._workspace() as raw_root:
                root = Path(raw_root)
                decision = root / "dexscreener.db"
                exec_ledger = root / "cross-margin-exec.db"
                margin_ledger = root / "margin-book.db"
                now_ms = int(time.time() * 1000)
                os.environ.update(_approved_file(root, now_ms))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill-switch")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                row_id = 148668
                self._insert(
                    decision,
                    row_id,
                    "regime2_short_squeeze_breakout",
                    "buy",
                    _evidence_extras(
                        "regime2_short_squeeze_breakout",
                        ledger=margin_ledger,
                    ),
                )
                first = self._once(decision, exec_ledger)
                self.assertEqual(first["processed"], 1)
                sample = first["sample"][0]
                self.assertEqual(sample["validation_ok"], True)
                self.assertEqual(sample["executed"], 0)
                self.assertEqual(
                    sample["exec_result"]["reason"],
                    "live_snapshot_exchange_error:-3045:query_max_borrow",
                )
                self.assertEqual(sample["exec_result"]["snapshot_call"], "query_max_borrow")
                self.assertEqual(sample["exec_result"]["exchange_code"], -3045)
                self.assertTrue(sample["exec_result"]["terminal_borrow_reject"])
                self.assertFalse(sample["exec_result"]["borrow_retried"])
                self.assertFalse(sample["exec_result"]["margin_called"])
                posts = [item for item in transport.requests if item[0] == "POST"]
                self.assertEqual(posts, [])
                borrow_reads = [
                    item for item in transport.requests if item[1] == "/sapi/v1/margin/maxBorrowable"
                ]
                self.assertEqual(len(borrow_reads), 1)
                before = len(transport.requests)
                second = self._once(decision, exec_ledger)
                self.assertEqual(second["processed"], 0)
                self.assertEqual(len(transport.requests), before)
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_cash_long_is_not_refused_when_unused_base_max_borrow_is_3045(self) -> None:
        """A cash OPEN_LONG borrows nothing. -3045 on the base must not kill it.

        The venue text is the in-repo fixture: the system does not have enough
        of that asset. A short that must borrow the same asset stays terminal.
        """
        transport = _MaxBorrow3045(cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID))
        sample = self._consume_live(transport, self._cash_long_extras)
        self.assertTrue(sample["exec_result"]["ok"], sample["exec_result"])
        self.assertFalse(sample["exec_result"].get("terminal_borrow_reject", False))
        self.assertNotIn("live_snapshot_exchange_error", str(sample["exec_result"].get("reason")))
        self.assertNotEqual(sample["exec_result"].get("reason"), "asset_not_borrowable")
        posts = [item for item in transport.requests if item[0] == "POST"]
        self.assertEqual(
            [item for item in posts if item[1] == "/sapi/v1/margin/borrow-repay"],
            [],
        )
        self.assertEqual(
            [item[1] for item in posts],
            ["/sapi/v1/margin/order", "/sapi/v1/margin/order/oco"],
        )

    def test_cash_long_is_not_refused_when_unused_base_is_not_borrowable(self) -> None:
        transport = _BaseNotBorrowable(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        sample = self._consume_live(transport, self._cash_long_extras)
        self.assertTrue(sample["exec_result"]["ok"], sample["exec_result"])
        self.assertNotEqual(sample["exec_result"].get("reason"), "asset_not_borrowable")
        self.assertFalse(sample["exec_result"].get("terminal_borrow_reject", False))
        posts = [item for item in transport.requests if item[0] == "POST"]
        self.assertEqual(
            [item for item in posts if item[1] == "/sapi/v1/margin/borrow-repay"],
            [],
        )
        self.assertIn("/sapi/v1/margin/order", [item[1] for item in posts])

    def test_short_still_refuses_when_the_base_is_not_borrowable(self) -> None:
        transport = _BaseNotBorrowable(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        sample = self._consume_live(
            transport,
            lambda _ledger: _evidence_extras("regime4_crowded_long_carry_short", ledger=_ledger),
        )
        self.assertEqual(sample["executed"], 0)
        self.assertEqual(sample["exec_result"]["reason"], "asset_not_borrowable")
        self.assertEqual([item for item in transport.requests if item[0] == "POST"], [])

    def test_consumer_passes_the_stable_id_into_the_plan_and_keeps_a_risk_refusal(self) -> None:
        saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        transport = _InMemoryMarginTransport(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        hc._default_opener = transport
        seen: list[str] = []

        def _spy(**kwargs: Any):
            seen.append(str(kwargs.get("intent_id")))
            return _refused_plan("regime_required_inputs_missing")

        try:
            with self._workspace() as raw_root:
                root = Path(raw_root)
                decision = root / "dexscreener.db"
                exec_ledger = root / "cross-margin-exec.db"
                margin_ledger = root / "margin-book.db"
                now_ms = int(time.time() * 1000)
                os.environ.update(_approved_file(root, now_ms))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill-switch")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                extras = _evidence_extras("regime4_crowded_long_carry_short", ledger=margin_ledger)
                extras.update(
                    {
                        "take_profit_price": "97000",
                        "stop_price": "101500",
                        "stop_limit_price": "101600",
                        "market_row": {"funding_z": 2.1},
                        "clocks": {"snapshot_observed_at_ms": now_ms},
                    }
                )
                self._insert(decision, 148661, "regime4_crowded_long_carry_short", "sell", extras)
                explicit = _evidence_extras(
                    "regime1_positive_gamma_fade",
                    ledger=margin_ledger,
                    intent_id="operator-intent-148662",
                )
                explicit.update(extras)
                explicit["intent_id"] = "operator-intent-148662"
                explicit["strategy_id"] = "regime1_positive_gamma_fade"
                self._insert(decision, 148662, "regime1_positive_gamma_fade", "buy", explicit)
                with patch(
                    "execution.margin_cross.entry.build_approved_position_plan",
                    side_effect=_spy,
                ):
                    summary = self._once(decision, exec_ledger)
                self.assertEqual(summary["processed"], 2)
                self.assertEqual(summary["executed_ok"], 0)
                self.assertEqual(seen, ["dl-148661", "operator-intent-148662"])
                conn = sqlite3.connect(exec_ledger)
                reasons = [
                    json.loads(row[0]).get("error")
                    for row in conn.execute("SELECT receipt_json FROM execution_log ORDER BY id")
                ]
                conn.close()
                self.assertEqual(reasons, ["regime_required_inputs_missing", "regime_required_inputs_missing"])
                self.assertEqual([item for item in transport.requests if item[0] == "POST"], [])
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_eight_strategies_do_not_crash_or_open_without_an_order_document(self) -> None:
        saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        transport = _InMemoryMarginTransport(
            cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID)
        )
        hc._default_opener = transport
        try:
            with self._workspace() as raw_root:
                root = Path(raw_root)
                decision = root / "dexscreener.db"
                exec_ledger = root / "cross-margin-exec.db"
                margin_ledger = root / "margin-book.db"
                now_ms = int(time.time() * 1000)
                os.environ.update(_approved_file(root, now_ms))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill-switch")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                for offset, strategy_id in enumerate(sorted(SUPPORTED_REGIME_STRATEGIES)):
                    self._insert(
                        decision,
                        148700 + offset,
                        strategy_id,
                        "sell",
                        _evidence_extras(strategy_id, ledger=margin_ledger),
                    )
                summary = self._once(decision, exec_ledger)
                self.assertEqual(summary["processed"], 8)
                self.assertEqual(summary["executed_ok"], 0)
                conn = sqlite3.connect(exec_ledger)
                rows = conn.execute(
                    "SELECT executed, status, receipt_json FROM execution_log ORDER BY decision_log_id"
                ).fetchall()
                conn.close()
                self.assertEqual(len(rows), 8)
                for executed, status, receipt_json in rows:
                    receipt = json.loads(receipt_json)
                    self.assertEqual(executed, 0)
                    self.assertEqual(status, "rejected")
                    self.assertNotIn("KeyError", json.dumps(receipt))
                    self.assertIn("order_fields_missing", str(receipt.get("error")))
                self.assertEqual(
                    [item for item in transport.requests if item[0] == "POST"],
                    [],
                )
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _workspace(self):
        import tempfile

        return tempfile.TemporaryDirectory()

    def _insert(self, db: Path, row_id: int, strategy_id: str, action: str, extras: dict[str, Any]) -> None:
        raw = {
            "strategy_id": strategy_id,
            "action": action,
            "chain_id": "binance",
            "token_address": "BTCUSDT",
            "pair_address": "BTC/USDT",
            "size_pct": 1.0,
            "reason": strategy_id,
            "risk_passed": True,
            "mode": "live_stub",
            "purpose": PURPOSE_LIVE_BNB_CROSS_MARGIN,
            "extras": extras,
        }
        encoded = json.dumps(raw)
        decoded = json.loads(encoded)
        self.assertNotIn("intent_id", decoded)
        if "intent_id" not in extras:
            self.assertNotIn("intent_id", decoded["extras"])
        else:
            self.assertEqual(decoded["extras"]["intent_id"], extras["intent_id"])
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY, ts TEXT, strategy_id TEXT, action TEXT,
                chain_id TEXT, token_address TEXT, size_pct REAL, reason TEXT,
                risk_passed INTEGER, mode TEXT, purpose TEXT, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO decision_log VALUES (
                ?, '2026-11-19T21:04:15Z', ?, ?, 'binance', 'BTCUSDT',
                1.0, ?, 1, 'live_stub', ?, ?
            )
            """,
            (row_id, strategy_id, action, strategy_id, PURPOSE_LIVE_BNB_CROSS_MARGIN, encoded),
        )
        conn.commit()
        conn.close()

    def _cash_long_extras(self, ledger: Path) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        extras = _evidence_extras(
            "regime2_short_squeeze_breakout",
            ledger=ledger,
        )
        extras.update(
            {
                "symbol": "BTCUSDT",
                "binance_symbol": "BTCUSDT",
                "base_asset": "BTC",
                "requested_action": "OPEN_LONG",
                "entry_tier": "1",
                "limit_price": "99990",
                "take_profit_price": "102000",
                "stop_price": "98000",
                "stop_limit_price": "97900",
                "expected_uid": _UID,
                "fee_rate": "0.001",
                "fee_asset": "USDT",
                "market_row": _cash_long_squeeze_row(),
                "clocks": _fresh_clocks(now_ms),
            }
        )
        return extras

    def _consume_live(self, transport: _InMemoryMarginTransport, extras_for) -> dict[str, Any]:
        saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        saved_socket = socket.socket
        saved_create = socket.create_connection
        saved_opener = hc._default_opener
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        hc._default_opener = transport
        try:
            with self._workspace() as raw_root:
                root = Path(raw_root)
                decision = root / "dexscreener.db"
                exec_ledger = root / "cross-margin-exec.db"
                margin_ledger = root / "margin-book.db"
                now_ms = int(time.time() * 1000)
                os.environ.update(_approved_file(root, now_ms))
                os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                os.environ["LIVE_KILL_SWITCH_PATH"] = str(root / "no-kill-switch")
                os.environ.pop("CROSS_MARGIN_LEDGER_PATH", None)
                extras = extras_for(margin_ledger)
                self._insert(
                    decision,
                    149655,
                    str(extras.get("strategy_id") or "regime2_short_squeeze_breakout"),
                    "buy",
                    extras,
                )
                summary = self._once(decision, exec_ledger)
                self.assertEqual(summary["processed"], 1, summary)
                return summary["sample"][0]
        finally:
            hc._default_opener = saved_opener
            socket.socket = saved_socket  # type: ignore[misc]
            socket.create_connection = saved_create  # type: ignore[assignment]
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _once(self, decision: Path, ledger: Path) -> dict[str, Any]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = execution_main(
                [
                    "--once",
                    "--json",
                    "--db",
                    str(decision),
                    "--ledger",
                    str(ledger),
                    "--accept-purpose",
                    PURPOSE_LIVE_BNB_CROSS_MARGIN,
                ]
            )
        self.assertEqual(code, 0, buf.getvalue())
        lines = [line for line in buf.getvalue().splitlines() if line.startswith("{")]
        return json.loads(lines[-1])


if __name__ == "__main__":
    unittest.main()
