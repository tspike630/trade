"""Production live margin path. Approval is a pinned test file, not an injected object.

The only stand-in is the HTTP opener. Sockets stay blocked.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import sqlite3
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from decision_engine.core.engine import scan_limit_for_purpose
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.token_cooldown import TokenCooldown
from decision_engine.run import build_production_engine, load_explicit_account_route
from decision_engine.strategies import get_strategy
from decision_engine.tests.quiet_macro import write_quiet_macro
from execution.executors.binance_spot_gate import REPLAY_FLOOR_DECISION_ID
from execution.gateway import ExecutionGateway
from execution.margin_cross import http_client as hc
from execution.margin_cross.policy import (
    APPROVAL_HMAC_KEY_ENV,
    APPROVED_CONFIG_HMAC_ENV,
    APPROVED_CONFIG_PATH_ENV,
    APPROVED_CONFIG_SHA256_ENV,
    ENABLE_STATE2_LONG_ENV,
    ENABLE_STATE4_LONG_ENV,
    FILL_TP_FORMULA_B_ENV,
    KILL_SWITCH_ENV,
    LIVE_ENABLED_ENV,
    MCP_FORBIDDEN_UID,
    PARAMETERS_APPROVED_ENV,
    SUPPORTED_REGIME_STRATEGIES,
)
from execution.tests.test_margin_cross_http_and_live import (
    _InMemoryMarginTransport,
    _fresh_clocks,
    _state4_short_market_row,
)
from execution.tests import test_margin_cross_http_and_live as transport_mod
def _production_host() -> Any:
    from execution.tests.test_margin_production_chain import MarginProductionChainTests

    return MarginProductionChainTests("test_default_tick_stays_on_the_spot_adapter")

_TEST_API_KEY = "test-margin-key"
_TEST_API_SECRET = "test-margin-secret"
_TEST_HMAC_KEY = "test-fixture-hmac-not-a-secret"
_UID = "88001122"
_ENV_KEYS = (
    PARAMETERS_APPROVED_ENV,
    APPROVED_CONFIG_PATH_ENV,
    APPROVED_CONFIG_SHA256_ENV,
    APPROVAL_HMAC_KEY_ENV,
    APPROVED_CONFIG_HMAC_ENV,
    LIVE_ENABLED_ENV,
    ENABLE_STATE2_LONG_ENV,
    ENABLE_STATE4_LONG_ENV,
    FILL_TP_FORMULA_B_ENV,
    KILL_SWITCH_ENV,
    "BINANCE_MARGIN_API_KEY",
    "BINANCE_MARGIN_API_SECRET",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_SPOT_PRODUCTION_BOOK",
)


def _approved_file(directory: Path, now_ms: int, *, uid: str = _UID, hmac_ok: bool = True, ttl_ms: int = 86_400_000) -> dict[str, str]:
    doc = {
        "approval_scope": "binance_cross_margin_only",
        "approved_by": "operator_test_fixture",
        "approved_account_uid": uid,
        "approved_at_ms": now_ms - 60_000,
        "expires_at_ms": now_ms - 60_000 + ttl_ms,
        "max_leverage": "3",
        "max_effective_leverage": "1.60",
        "max_total_liability_net_ratio": "0.60",
        "max_single_borrow_net_ratio": "0.55",
        "max_borrowable_pool_ratio": "0.50",
        "min_entry_margin_level": "2.50",
        "warn_margin_level": "2.00",
        "emergency_margin_level": "1.60",
        "min_order_notional_usdt": "20",
        "max_snapshot_age_ms": 5000,
        "interest_horizon_hours": "2",
        "base_position_pct": "1.00",
        "approved_strategies": sorted(SUPPORTED_REGIME_STRATEGIES),
    }
    raw = json.dumps(doc).encode("utf-8")
    path = directory / "test-approval.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    mac = hmac.new(_TEST_HMAC_KEY.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac_ok:
        mac = "0" * 64
    return {
        PARAMETERS_APPROVED_ENV: "1",
        APPROVED_CONFIG_PATH_ENV: str(path),
        APPROVED_CONFIG_SHA256_ENV: digest,
        APPROVAL_HMAC_KEY_ENV: _TEST_HMAC_KEY,
        APPROVED_CONFIG_HMAC_ENV: mac,
        LIVE_ENABLED_ENV: "1",
        ENABLE_STATE2_LONG_ENV: "1",
        ENABLE_STATE4_LONG_ENV: "1",
        FILL_TP_FORMULA_B_ENV: "1",
    }


def _order_fields(now_ms: int, *, uid: str = _UID) -> dict[str, Any]:
    return {
        "intent_id": "live-prod-short-1",
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "requested_action": "OPEN_SHORT",
        "entry_tier": "1.0",
        "take_profit_price": "97000",
        "stop_price": "101500",
        "stop_limit_price": "101600",
        "market_row": _state4_short_market_row(),
        "clocks": _fresh_clocks(now_ms),
        "expected_uid": uid,
        "fee_rate": "0.001",
        "fee_asset": "USDT",
    }


class _Blocked(socket.socket):
    def connect(self, address):  # type: ignore[override]
        raise OSError(f"live_test_network_blocked:{address!r}")

    def connect_ex(self, address):  # type: ignore[override]
        raise OSError(f"live_test_network_blocked:{address!r}")


class LiveProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {key: os.environ.get(key) for key in _ENV_KEYS}
        self._saved_socket = socket.socket
        self._saved_create = socket.create_connection
        self._saved_opener = hc._default_opener
        self._saved_now = transport_mod.NOW_MS
        socket.socket = _Blocked  # type: ignore[misc]
        socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))  # type: ignore[assignment]
        self.host = _production_host()
        self.host.setUp()
        self.now_ms = int(time.time() * 1000)
        transport_mod.NOW_MS = self.now_ms
        self.transport = _InMemoryMarginTransport(cred_id=_TEST_API_KEY, sign_id=_TEST_API_SECRET, uid=int(_UID))
        self.transport.max_borrow_btc = self.transport.max_borrow_btc  # approved size stays inside the book
        hc._default_opener = self.transport
        self.kill = self.host.root / "no-kill-switch"
        os.environ[KILL_SWITCH_ENV] = str(self.kill)
        os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
        os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
        os.environ.pop("BINANCE_SPOT_PRODUCTION_BOOK", None)

    def tearDown(self) -> None:
        hc._default_opener = self._saved_opener
        transport_mod.NOW_MS = self._saved_now
        socket.socket = self._saved_socket  # type: ignore[misc]
        socket.create_connection = self._saved_create  # type: ignore[assignment]
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.host.tearDown()

    def _apply(self, extra: dict[str, str]) -> None:
        for key in (
            PARAMETERS_APPROVED_ENV,
            APPROVED_CONFIG_PATH_ENV,
            APPROVED_CONFIG_SHA256_ENV,
            APPROVAL_HMAC_KEY_ENV,
            APPROVED_CONFIG_HMAC_ENV,
            LIVE_ENABLED_ENV,
            ENABLE_STATE2_LONG_ENV,
            ENABLE_STATE4_LONG_ENV,
            FILL_TP_FORMULA_B_ENV,
        ):
            os.environ.pop(key, None)
        os.environ.update(extra)

    def _row(self, row_id: int, extras: dict[str, Any]) -> None:
        decision = self.host.root / "live-decision.db"
        raw = {"pair_address": "BTCUSDT", "purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "extras": extras}
        conn = sqlite3.connect(decision)
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
            INSERT OR REPLACE INTO decision_log VALUES (
                ?, '2026-11-19T00:00:00Z', ?, 'buy', 'binance', 'BTCUSDT',
                0.1, 'probe', 1, 'live_stub', ?, ?
            )
            """,
            (row_id, str(extras.get("strategy_id") or "probe"), PURPOSE_LIVE_BNB_BOLL_FADE, json.dumps(raw)),
        )
        conn.commit()
        conn.close()

    def _gateway(self) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=str(self.host.root / "live-decision.db"),
            ledger_path=str(self.host.root / f"exec-{time.time_ns()}.db"),
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            paper_enabled=False,
        )

    def _consume(self, extras: dict[str, Any]) -> dict[str, Any]:
        row_id = REPLAY_FLOOR_DECISION_ID + 20
        self._row(row_id, extras)
        stats = self._gateway().process_pending()
        self.assertEqual(stats.processed, 1)
        return stats.receipts[0]

    def test_unconfigured_live_stub_stays_on_the_spot_skip(self) -> None:
        self._apply({})
        before = len(self.transport.requests)
        receipt = self._consume({})
        self.assertEqual(receipt["executor"], "binance_spot")
        self.assertEqual(receipt["receipt"]["status"], "gate_skip")
        self.assertEqual(receipt["receipt"]["gate_reason"], "protection_tool_absent")
        self.assertEqual(len(self.transport.requests), before)

    def test_explicit_live_route_reaches_the_track_and_does_not_borrow_twice(self) -> None:
        now_ms = self.now_ms
        self._apply(_approved_file(self.host.root, now_ms))
        route = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": True,
            "strategy_id": "regime5_crisis_contagion_short",
            "execution_mode": "live",
            "margin_ledger_path": str(self.host.root / "live-margin.db"),
            **_order_fields(now_ms),
        }
        path = self.host.root / "route.json"
        path.write_text(json.dumps(route), encoding="utf-8")
        loaded, error = load_explicit_account_route(str(path))
        self.assertIsNone(error)
        self.assertNotIn("offline_approval", loaded or {})
        decision = self.host.root / "routed-live.db"
        book = self.host.root / "routed-live-book.db"
        self.host._seed(decision, book, now_ms)
        os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(book)
        os.environ["NEWS_EVENTS_DB_PATH"] = str(self.host.root / "missing-news.db")
        adapter = IndicatorDataAdapter(decision)
        bus = SignalBus(decision, dry_run=False, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
        try:
            assert bus._rw is not None
            bus._rw.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES ('decision_log', ?)",
                (REPLAY_FLOOR_DECISION_ID,),
            )
            bus._rw.commit()
            with patch("execution.margin_cross.entry.time.time", return_value=now_ms / 1000.0):
                payload = build_production_engine(
                    adapter,
                    bus,
                    [get_strategy("tech_boll_fade_v1")],
                    risk=RiskManager(cooldown_sec=0),
                    hot_limit=scan_limit_for_purpose(PURPOSE_LIVE_BNB_BOLL_FADE, None),
                    token_cooldown=TokenCooldown(0),
                    entry_ih_min_hours=None,
                    account_route=loaded,
                    macro_db=write_quiet_macro(decision.with_name("routed-live-macro.db")),
                ).tick()
        finally:
            bus.close()
            adapter.close()
        self.assertEqual(payload["emitted"], 1)
        conn = sqlite3.connect(decision)
        raw = json.loads(conn.execute("SELECT raw_json FROM decision_log").fetchone()[0])
        conn.close()
        self.assertEqual(raw["extras"]["execution_mode"], "live")
        self.assertNotIn("offline_approval", raw["extras"])
        gateway = ExecutionGateway(
            decision_db=str(decision),
            ledger_path=str(self.host.root / "routed-exec.db"),
            accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
            paper_enabled=False,
        )
        with patch("execution.margin_cross.entry.time.time", return_value=now_ms / 1000.0):
            first = gateway.process_pending()
        self.assertEqual(first.processed, 1)
        receipt = first.receipts[0]
        self.assertEqual(receipt["executor"], "cross_margin_track")
        self.assertEqual(receipt["exec_result"]["entry"], "execution.margin_cross.entry.open_live_document")
        self.assertTrue(receipt["exec_result"]["margin_called"], receipt["exec_result"])
        self.assertTrue(receipt["exec_result"]["ok"], receipt["exec_result"])
        self.assertEqual(receipt["exec_result"]["open"]["state"], "protected")
        posts = [item for item in self.transport.requests if item[0] == "POST"]
        self.assertEqual(
            [path for _method, path, _params in posts],
            [
                "/sapi/v1/margin/borrow-repay",
                "/sapi/v1/margin/order",
                "/sapi/v1/margin/order/oco",
            ],
        )
        extras = dict(raw["extras"])
        extras["strategy_id"] = "regime5_crisis_contagion_short"
        conn = sqlite3.connect(decision)
        conn.execute(
            """
            INSERT INTO decision_log (
                id, ts, strategy_id, action, chain_id, token_address, size_pct,
                reason, risk_passed, mode, purpose, raw_json
            ) VALUES (?, '2026-11-19T00:00:00Z', ?, 'buy', 'binance', 'BTCUSDT', 0.1, 'probe', 1, 'live_stub', ?, ?)
            """,
            (
                REPLAY_FLOOR_DECISION_ID + 2,
                "regime5_crisis_contagion_short",
                PURPOSE_LIVE_BNB_BOLL_FADE,
                json.dumps({"pair_address": "BTCUSDT", "purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "extras": extras}),
            ),
        )
        conn.commit()
        conn.close()
        with patch("execution.margin_cross.entry.time.time", return_value=now_ms / 1000.0):
            second = gateway.process_pending()
        again = [item for item in self.transport.requests if item[0] == "POST"]
        self.assertEqual(len(again), 3)
        self.assertTrue(second.receipts[0]["exec_result"]["margin_called"])
        self.assertEqual(second.receipts[0]["exec_result"]["open"]["state"], "protected")

    def test_live_rejections_do_not_post_or_fall_back_to_spot(self) -> None:
        now_ms = self.now_ms
        base = {
            "account_domain": "binance_cross_margin",
            "spot_book_full": True,
            "strategy_id": "regime5_crisis_contagion_short",
            "execution_mode": "live",
            "margin_ledger_path": str(self.host.root / "reject-margin.db"),
            **_order_fields(now_ms),
        }
        cases = {
            "no_approval": ({}, dict(base)),
            "bad_hmac": (_approved_file(self.host.root, now_ms, hmac_ok=False), dict(base)),
            "expired": (_approved_file(self.host.root, now_ms, ttl_ms=-1_000), dict(base)),
            "mcp_uid": (_approved_file(self.host.root, now_ms, uid=MCP_FORBIDDEN_UID), dict(base)),
            "live_off": ({**_approved_file(self.host.root, now_ms), LIVE_ENABLED_ENV: "0"}, dict(base)),
            "no_credentials": (_approved_file(self.host.root, now_ms), dict(base)),
            "kill": (_approved_file(self.host.root, now_ms), dict(base)),
        }
        posts_before = [item for item in self.transport.requests if item[0] == "POST"]
        with patch("execution.margin_cross.entry.time.time", return_value=now_ms / 1000.0):
            for name, (env, extras) in cases.items():
                self._apply(env)
                if name == "no_credentials":
                    os.environ.pop("BINANCE_MARGIN_API_KEY", None)
                    os.environ.pop("BINANCE_MARGIN_API_SECRET", None)
                    os.environ.pop("BINANCE_API_KEY", None)
                    os.environ.pop("BINANCE_API_SECRET", None)
                else:
                    os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
                    os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
                if name == "kill":
                    killed = self.host.root / "KILL"
                    killed.write_text("stop\n", encoding="utf-8")
                    os.environ[KILL_SWITCH_ENV] = str(killed)
                receipt = self._consume(extras)
                self.assertFalse(receipt["exec_result"]["margin_called"], name)
                self.assertFalse(receipt["exec_result"]["spot_called"], name)
                self.assertNotEqual(receipt["executor"], "binance_spot", name)
                self.assertNotEqual(receipt["receipt"]["status"], "gate_skip", name)
        self.assertEqual([item for item in self.transport.requests if item[0] == "POST"], posts_before)
        os.environ[KILL_SWITCH_ENV] = str(self.kill)
        os.environ["BINANCE_MARGIN_API_KEY"] = _TEST_API_KEY
        os.environ["BINANCE_MARGIN_API_SECRET"] = _TEST_API_SECRET
        dry = dict(base)
        dry["execution_mode"] = "approved_dry_run"
        self._apply(_approved_file(self.host.root, now_ms))
        with patch("execution.margin_cross.entry.time.time", return_value=now_ms / 1000.0):
            dry_receipt = self._consume(dry)
        self.assertFalse(dry_receipt["exec_result"]["margin_called"])
        self.assertEqual(
            dry_receipt["exec_result"]["entry"],
            "execution.margin_cross.strategy_gate.build_approved_position_plan",
        )
        self.assertEqual([item for item in self.transport.requests if item[0] == "POST"], posts_before)


if __name__ == "__main__":
    unittest.main()
