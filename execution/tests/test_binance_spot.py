"""Binance spot adapter for live_bnb_boll_fade. No live HTTP.

    python -m unittest execution.tests.test_binance_spot
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CRAWLER = ROOT / "crawler"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CRAWLER))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    build_exit_plan,
    is_spot_usdt_symbol,
    protective_exit_reason,
    sign_query,
    symbol_status_trading,
)
from execution.gateway import (  # noqa: E402
    BINANCE_SPOT_EXECUTOR,
    STEP_A_EXECUTOR,
    ExecutionGateway,
)
from execution.run import _peer_isolation_guard  # noqa: E402

_TRADING = {
    "symbol": "BTCUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "isSpotTradingAllowed": True,
    "filters": [
        {
            "filterType": "LOT_SIZE",
            "minQty": "0.00001",
            "maxQty": "9000",
            "stepSize": "0.00001",
        },
        {
            "filterType": "PRICE_FILTER",
            "minPrice": "0.01",
            "maxPrice": "1000000",
            "tickSize": "0.01",
        },
        {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
    ],
}


class _FakeClient:
    def __init__(self, info: dict[str, Any] | None = None, order_id: str = "55") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.info = _TRADING if info is None else info
        self.order_id = order_id

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return self.info

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        return {"orderId": self.order_id, "symbol": "BTCUSDT", "status": "NEW"}


class _Boom:
    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"binance adapter must not run for {sig.get('purpose')}")


class _RecordingPaper:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.ledger = None

    def execute(self, sig: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(sig)
        return {"ok": True, "executor": "paper"}


def _sig(
    *,
    extras: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    base_extras: dict[str, Any] = {
        "binance_symbol": "BTCUSDT",
        "market": "spot",
        "quote_asset": "USDT",
        "quote_order_qty": "25",
        "venue": "binance",
        "source_token_address": "So11111111111111111111111111111111111111112",
    }
    if extras is not None:
        base_extras = extras
    base: dict[str, Any] = {
        "decision_log_id": 7,
        "strategy_id": "tech_boll_fade_v1",
        "action": "buy",
        "chain_id": "binance",
        "token_address": "BTCUSDT",
        "pair_address": "BTC/USDT",
        "size_pct": 0.05,
        "mode": "dry_run",
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "extras": base_extras,
    }
    base.update(overrides)
    return base


def _env(root: Path, *, enabled: str, kill_exists: bool) -> dict[str, str]:
    kill = root / "LIVE_TRADING_DISABLED"
    if kill_exists:
        kill.write_text("keep live off\n", encoding="utf-8")
    return {
        "BINANCE_SPOT_LIVE_ENABLED": enabled,
        "LIVE_KILL_SWITCH_PATH": str(kill),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_API_BASE": "https://api.binance.test",
        "BINANCE_SPOT_BOOK_USDT": "",
    }


def _decision_row(**overrides: Any) -> dict[str, Any]:
    sig = _sig(**overrides)
    extras = sig["extras"]
    raw = {
        "pair_address": sig["pair_address"],
        "purpose": sig["purpose"],
        "extras": extras,
    }
    return {
        "id": int(sig["decision_log_id"]),
        "ts": "2026-11-05T06:00:00Z",
        "strategy_id": sig["strategy_id"],
        "action": sig["action"],
        "chain_id": sig["chain_id"],
        "token_address": sig["token_address"],
        "size_pct": sig["size_pct"],
        "reason": "test",
        "risk_passed": 1,
        "mode": sig["mode"],
        "purpose": sig["purpose"],
        "raw_json": json.dumps(raw),
    }


class SymbolTests(unittest.TestCase):
    def test_spot_usdt_symbols(self) -> None:
        self.assertTrue(is_spot_usdt_symbol("BTCUSDT"))
        self.assertTrue(is_spot_usdt_symbol("ETHUSDT"))
        self.assertTrue(is_spot_usdt_symbol("BNBUSDT"))
        self.assertTrue(is_spot_usdt_symbol("1000SATSUSDT"))
        for bad in (
            "",
            "USDT",
            "btcusdt",
            "BTC/USDT",
            "BTC-USDT",
            "BTCUSDC",
            "USDCUSDT",
            "BTCUSDT:USDT",
            " BTCUSDT",
            "BTCUSDT ",
            None,
            1,
        ):
            self.assertFalse(is_spot_usdt_symbol(bad), bad)

    def test_exchange_info_must_be_trading_spot_usdt(self) -> None:
        self.assertTrue(symbol_status_trading(_TRADING, "BTCUSDT"))
        wrapped = {"symbols": [_TRADING, {"symbol": "ETHUSDT", "status": "BREAK"}]}
        self.assertTrue(symbol_status_trading(wrapped, "BTCUSDT"))
        self.assertFalse(symbol_status_trading(wrapped, "ETHUSDT"))
        halted = dict(_TRADING, status="HALT")
        self.assertFalse(symbol_status_trading(halted, "BTCUSDT"))
        wrong_quote = dict(_TRADING, quoteAsset="USDC")
        self.assertFalse(symbol_status_trading(wrong_quote, "BTCUSDT"))
        self.assertFalse(
            symbol_status_trading(dict(_TRADING, isSpotTradingAllowed=False), "BTCUSDT")
        )


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _adapter(self, client: _FakeClient | None, *, kill_exists: bool) -> BinanceSpotAdapter:
        kill = self.root / "LIVE_TRADING_DISABLED"
        if kill_exists:
            kill.write_text("keep live off\n", encoding="utf-8")
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=kill,
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )

    def test_refuse_without_symbol(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=False)
        for extras in (
            {},
            {"source_token_address": "mint", "market": "spot"},
            {"binance_symbol": "   "},
        ):
            result = adapter.execute(_sig(mode="live_stub", extras=extras, token_address=""))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "missing_binance_symbol")
            self.assertEqual(result["executed"], 0)
        self.assertEqual(client.calls, [])

    def test_live_does_not_invent_qty_from_size_pct(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=False)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(
                _sig(
                    mode="live_stub",
                    size_pct=0.05,
                    extras={"binance_symbol": "BTCUSDT", "market": "spot"},
                )
            )
        self.assertEqual(result["error"], "missing_book_usdt")
        self.assertEqual(result["executed"], 0)
        self.assertEqual(client.calls, [])

    def test_refuse_when_kill_switch_present(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=True)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=True)):
            result = adapter.execute(_sig(mode="live_stub"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["executed"], 0)
        self.assertTrue(str(result["error"]).startswith("kill_switch:"))
        self.assertFalse(result["submitted"])
        self.assertEqual(client.calls, [])
        self.assertTrue(result["gates"]["kill_switch_active"])
        self.assertTrue(result["gates"]["live_enabled"])

    def test_refuse_when_enable_flag_off(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=False)
        with mock.patch.dict(os.environ, _env(self.root, enabled="0", kill_exists=False)):
            result = adapter.execute(_sig(mode="live_stub"))
        self.assertEqual(result["error"], "binance_spot_live_disabled")
        self.assertEqual(client.calls, [])

    def test_dry_run_accepted_while_kill_switch_present(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=True)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=True)):
            result = adapter.execute(_sig(mode="dry_run"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["submitted"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["order"]["symbol"], "BTCUSDT")
        self.assertEqual(result["order"]["quoteOrderQty"], "25")
        self.assertNotIn("signature", result["order"])
        self.assertEqual(client.calls, [])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_mock_client_accepted_when_gates_open(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=False)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(_sig(mode="live_stub"))
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["dry_run"])
        self.assertEqual(result["executed"], 1)
        self.assertTrue(result["submitted"])
        self.assertEqual(result["exchange_order_id"], "55")
        self.assertEqual(client.calls[0][0], "exchange_info")
        kind, signed, api_key = client.calls[1]
        self.assertEqual(kind, "new_order")
        self.assertEqual(api_key, "test-key")
        query, _, signature = signed.rpartition("&signature=")
        expected = hmac.new(
            b"test-secret",
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(signature, expected)
        self.assertIn("symbol=BTCUSDT", query)
        self.assertIn("side=BUY", query)
        self.assertIn("type=MARKET", query)
        self.assertIn("quoteOrderQty=25", query)
        self.assertNotIn("test-secret", signed)
        self.assertNotIn("test-secret", json.dumps(result))

    def test_limit_sell_and_not_trading(self) -> None:
        limit_extras = {
            "binance_symbol": "ETHUSDT",
            "order_type": "LIMIT",
            "quantity": "0.01",
            "price": "3000",
        }
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            halted = _FakeClient(info={"symbol": "ETHUSDT", "status": "HALT", "quoteAsset": "USDT"})
            adapter_halt = self._adapter(halted, kill_exists=False)
            refused = adapter_halt.execute(
                _sig(
                    mode="live_stub",
                    action="sell",
                    token_address="ETHUSDT",
                    extras=limit_extras,
                )
            )
            self.assertEqual(refused["error"], "symbol_not_trading")
            self.assertEqual(halted.calls, [("exchange_info", "ETHUSDT")])

            trading = _FakeClient(
                info={
                    "symbols": [
                        {
                            "symbol": "ETHUSDT",
                            "status": "TRADING",
                            "quoteAsset": "USDT",
                            "isSpotTradingAllowed": True,
                        }
                    ]
                }
            )
            adapter_ok = self._adapter(trading, kill_exists=False)
            accepted = adapter_ok.execute(
                _sig(
                    mode="live_stub",
                    action="sell",
                    token_address="ETHUSDT",
                    extras=limit_extras,
                )
            )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["order"]["type"], "LIMIT")
        self.assertEqual(accepted["order"]["side"], "SELL")
        self.assertEqual(accepted["order"]["timeInForce"], "GTC")
        self.assertEqual(accepted["order"]["quantity"], "0.01")
        self.assertNotIn("quoteOrderQty", accepted["order"])

    def test_rejects_other_purposes_and_token_mismatch(self) -> None:
        client = _FakeClient()
        adapter = self._adapter(client, kill_exists=False)
        race = adapter.execute(_sig(purpose="race_tech_boll_fade", mode="live_stub"))
        self.assertEqual(race["error"], "purpose_not_live_bnb_boll_fade")
        frozen = adapter.execute(_sig(purpose="live_tech_boll_fade", mode="live_stub"))
        self.assertEqual(frozen["error"], "purpose_not_live_bnb_boll_fade")
        mismatch = adapter.execute(
            _sig(mode="live_stub", token_address="ETHUSDT")
        )
        self.assertEqual(mismatch["error"], "binance_symbol_mismatch")
        bad = adapter.execute(
            _sig(mode="dry_run", extras={"binance_symbol": "BTC/USDT"})
        )
        self.assertEqual(bad["error"], "invalid_binance_symbol")
        self.assertEqual(client.calls, [])


class HttpClientShapeTests(unittest.TestCase):
    def test_posts_signed_body_to_injected_opener_only(self) -> None:
        seen: dict[str, Any] = {}

        def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["data"] = request.data.decode("utf-8") if request.data else ""
            seen["key"] = request.get_header("X-mbx-apikey")
            seen["timeout"] = timeout
            return 200, b'{"orderId": 9, "symbol": "BTCUSDT"}'

        with mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        ):
            client = BinanceSpotHttpClient(
                base_url="https://api.binance.test",
                opener=opener,
            )
            signed = sign_query(
                [
                    ("symbol", "BTCUSDT"),
                    ("side", "BUY"),
                    ("type", "MARKET"),
                    ("quoteOrderQty", "25"),
                    ("recvWindow", "5000"),
                    ("timestamp", "1700000000000"),
                ],
                "test-secret",
            )
            placed = client.new_order(signed, api_key="test-key")
        self.assertEqual(placed["orderId"], 9)
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(str(seen["url"]).startswith("https://api.binance.test/api/v3/order"))
        self.assertNotIn("api.binance.com", str(seen["url"]))
        self.assertIn("symbol=BTCUSDT", seen["data"])
        self.assertIn("signature=", seen["data"])
        self.assertNotIn("test-secret", seen["data"])
        self.assertEqual(seen["key"], "test-key")


class GatewayHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _gateway(
        self,
        *,
        purposes: frozenset[str] | None,
        adapter: Any,
        paper: _RecordingPaper | None,
        paper_enabled: bool,
    ) -> ExecutionGateway:
        return ExecutionGateway(
            decision_db=self.root / "decision.db",
            ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
            paper_enabled=paper_enabled,
            paper_db=self.root / "paper_eq_unused.db",
            paper_executor=paper,
            pm_enabled=False,
            accept_purposes=purposes,
            binance_spot=adapter,
        )

    def test_default_book_rejects_purpose_without_adapter(self) -> None:
        with self._gateway(
            purposes=None,
            adapter=_Boom(),
            paper=_RecordingPaper(),
            paper_enabled=True,
        ) as gw:
            result = gw.process_one(_decision_row())
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["executor"], STEP_A_EXECUTOR)
        self.assertIn("purpose_invalid", result["validation_errors"])

    def test_kill_switch_refuses_live_fill_and_skips_paper(self) -> None:
        client = _FakeClient()
        paper = _RecordingPaper()
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        adapter = BinanceSpotAdapter(
            client=client,
            kill_switch_path=kill,
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )
        env = _env(self.root, enabled="1", kill_exists=True)
        with mock.patch.dict(os.environ, env):
            with self._gateway(
                purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                adapter=adapter,
                paper=paper,
                paper_enabled=True,
            ) as gw:
                result = gw.process_one(_decision_row(mode="live_stub"))
        self.assertEqual(result["executor"], BINANCE_SPOT_EXECUTOR)
        self.assertEqual(result["executed"], 0)
        self.assertTrue(str(result["error"]).startswith("kill_switch:"))
        self.assertEqual(result["receipt"]["status"], "live_refused")
        self.assertEqual(client.calls, [])
        self.assertEqual(paper.calls, [])
        self.assertNotIn("test-secret", json.dumps(result["receipt"]))

    def test_dry_run_and_mock_client_through_gateway(self) -> None:
        paper = _RecordingPaper()
        dry_client = _FakeClient()
        dry_adapter = BinanceSpotAdapter(
            client=dry_client,
            kill_switch_path=self.root / "missing-switch",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )
        env = _env(self.root, enabled="1", kill_exists=False)
        with mock.patch.dict(os.environ, env):
            with self._gateway(
                purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                adapter=dry_adapter,
                paper=paper,
                paper_enabled=True,
            ) as gw:
                dry = gw.process_one(_decision_row(mode="dry_run"))
        self.assertTrue(dry["validation_ok"])
        self.assertEqual(dry["executed"], 0)
        self.assertEqual(dry["receipt"]["status"], "dry_run_acked")
        self.assertEqual(dry_client.calls, [])
        self.assertEqual(paper.calls, [])

        live_client = _FakeClient(order_id="99")
        live_adapter = BinanceSpotAdapter(
            client=live_client,
            kill_switch_path=self.root / "missing-switch",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )
        with mock.patch.dict(os.environ, env):
            with self._gateway(
                purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                adapter=live_adapter,
                paper=paper,
                paper_enabled=True,
            ) as gw:
                live = gw.process_one(
                    _decision_row(mode="live_stub", decision_log_id=8)
                )
        self.assertEqual(live["executed"], 1, live)
        self.assertEqual(live["executor"], BINANCE_SPOT_EXECUTOR)
        self.assertEqual(live["receipt"]["status"], "binance_spot_submitted")
        self.assertEqual(live["exec_result"]["exchange_order_id"], "99")
        self.assertEqual(live_client.calls[0][0], "exchange_info")
        self.assertEqual(live_client.calls[1][0], "new_order")
        self.assertEqual(paper.calls, [])

    def test_paper_peer_and_frozen_onchain_purpose_stay_off_the_adapter(self) -> None:
        paper = _RecordingPaper()
        with self._gateway(
            purposes=frozenset({"race_tech_boll_fade"}),
            adapter=_Boom(),
            paper=paper,
            paper_enabled=True,
        ) as gw:
            race = gw.process_one(
                _decision_row(purpose="race_tech_boll_fade", mode="dry_run")
            )
        self.assertEqual(race["executor"], "paper")
        self.assertEqual(len(paper.calls), 1)

        paper2 = _RecordingPaper()
        with self._gateway(
            purposes=frozenset({"live_tech_boll_fade"}),
            adapter=_Boom(),
            paper=paper2,
            paper_enabled=True,
        ) as gw:
            frozen = gw.process_one(
                _decision_row(purpose="live_tech_boll_fade", mode="live_stub", decision_log_id=3)
            )
        self.assertEqual(frozen["executor"], "paper")
        self.assertEqual(len(paper2.calls), 1)


class PeerGuardTests(unittest.TestCase):
    def test_bnb_arm_is_isolated_and_race_peers_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main = root / "execution_ledger.db"
            bnb = root / "execution_ledger_live_bnb_boll_fade.db"
            race_ledger = root / "execution_ledger_race_tech_boll_fade.db"
            race_paper = root / "paper_eq_tech_boll_fade.db"
            with mock.patch(
                "execution.run._default_main_ledger_path",
                return_value=main,
            ):
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        ledger_path=bnb,
                        paper_db=None,
                        paper_enabled=False,
                    )
                )
                self.assertIn(
                    "isolated",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        ledger_path=main,
                        paper_db=None,
                        paper_enabled=False,
                    )
                    or "",
                )
                self.assertIn(
                    "paper",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        ledger_path=bnb,
                        paper_db=None,
                        paper_enabled=True,
                    )
                    or "",
                )
                self.assertIn(
                    "only",
                    _peer_isolation_guard(
                        accept_purposes=frozenset(
                            {PURPOSE_LIVE_BNB_BOLL_FADE, "race_tech_boll_fade"}
                        ),
                        ledger_path=bnb,
                        paper_db=race_paper,
                        paper_enabled=True,
                    )
                    or "",
                )
                self.assertIn(
                    "paper_eq_tech_boll_fade",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                        ledger_path=bnb,
                        paper_db=race_paper,
                        paper_enabled=False,
                    )
                    or "",
                )
                self.assertIn(
                    "paper-enabled",
                    _peer_isolation_guard(
                        accept_purposes=frozenset({"race_tech_boll_fade"}),
                        ledger_path=race_ledger,
                        paper_db=race_paper,
                        paper_enabled=False,
                    )
                    or "",
                )
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=frozenset({"race_tech_boll_fade"}),
                        ledger_path=race_ledger,
                        paper_db=race_paper,
                        paper_enabled=True,
                    )
                )
                self.assertIsNone(
                    _peer_isolation_guard(
                        accept_purposes=None,
                        ledger_path=main,
                        paper_db=root / "paper_trading.db",
                        paper_enabled=True,
                    )
                )


class DynamicExitPlanTests(unittest.TestCase):
    """Absolute stop / take-profit / limit. No SL5 or TP10 stand-in. No live HTTP."""

    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled in tests"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _adapter(self) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=_FakeClient(),
            kill_switch_path=self.root / "LIVE_TRADING_DISABLED",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )

    def _zone_extras(self, **overrides: Any) -> dict[str, Any]:
        extras = {
            "binance_symbol": "BNBUSDT",
            "market": "spot",
            "order_type": "MARKET",
            "price_usd": 102.0,
            "stop_price": 99.0,
            "take_profit_price": 110.0,
            "entry_reason": "large_trade_market",
            "entry_zone_min": 100.8,
            "entry_zone_max": 104.0,
            "alpha": 0.2,
            "trade_size_ratio": 1.6,
            "position_size_multiplier": 1.0,
        }
        extras.update(overrides)
        return extras

    def test_dry_run_keeps_absolute_stop_and_mid_take_profit(self) -> None:
        kill = self.root / "LIVE_TRADING_DISABLED"
        kill.write_text("off\n", encoding="utf-8")
        adapter = self._adapter()
        with mock.patch.dict(os.environ, _env(self.root, enabled="0", kill_exists=True)):
            result = adapter.execute(
                _sig(
                    mode="dry_run",
                    token_address="BNBUSDT",
                    extras=self._zone_extras(),
                )
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["submitted"])
        self.assertEqual(result["executed"], 0)
        plan = result["exit_plan"]
        self.assertEqual(plan["stop_price"], "99")
        self.assertEqual(plan["take_profit_price"], "110")
        self.assertEqual(plan["entry_price"], "102")
        self.assertFalse(plan["submitted"])
        self.assertFalse(plan["defaults_applied"])
        self.assertEqual(plan["protective_order"], "not_supported_not_sent")
        self.assertEqual(result["intent"]["order_type"], "MARKET")
        self.assertIsNone(result["intent"]["limit_price"])
        self.assertEqual(adapter.client.calls, [])
        blob = json.dumps(result)
        self.assertNotIn("tp_pct", blob)
        self.assertNotIn("sl_pct", blob)
        # 5% under 102 would be 96.9. The plan stops at 99, so 97 is still open.
        # 98 is still above a 5% stop (102 * 0.95 = 96.9) and at/under this plan's 99.
        self.assertIsNone(protective_exit_reason(plan, 100.5))
        self.assertEqual(protective_exit_reason(plan, 98.0), "stop_loss")
        self.assertEqual(protective_exit_reason(plan, 99.0), "stop_loss")
        # 10% above 102 would be 112.2. Touching the decision mid exits first.
        self.assertEqual(protective_exit_reason(plan, 110.0), "take_profit")
        self.assertIsNone(protective_exit_reason(plan, 109.0))

    def test_limit_intent_is_not_pretended_to_be_an_exchange_order(self) -> None:
        adapter = self._adapter()
        with mock.patch.dict(os.environ, _env(self.root, enabled="0", kill_exists=True)):
            result = adapter.execute(
                _sig(
                    mode="dry_run",
                    token_address="BNBUSDT",
                    extras=self._zone_extras(
                        order_type="LIMIT",
                        price_usd=108.0,
                        limit_price=102.0,
                        entry_reason="neutral_limit",
                        position_size_multiplier=0.5,
                    ),
                )
            )
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["order"])
        self.assertEqual(result["intent"]["order_type"], "LIMIT")
        self.assertEqual(result["intent"]["limit_price"], "102")
        self.assertFalse(result["intent"]["exchange_ready"])
        self.assertEqual(result["intent"]["quantity_status"], "deferred_size_pct")
        self.assertEqual(result["intent"]["protective_order"], "not_supported_not_sent")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["exit_plan"]["entry_price"], "102")
        self.assertEqual(adapter.client.calls, [])

    def test_stop_at_or_above_entry_is_rejected(self) -> None:
        plan, error = build_exit_plan(
            {"action": "buy"},
            {
                "order_type": "MARKET",
                "price_usd": 100.0,
                "stop_price": 100.0,
                "take_profit_price": 110.0,
                "entry_reason": "large_trade_market",
            },
        )
        self.assertIsNone(plan)
        self.assertEqual(error, "stop_not_below_entry")
        adapter = self._adapter()
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(
                _sig(
                    mode="live_stub",
                    token_address="BNBUSDT",
                    extras=self._zone_extras(stop_price=102.0),
                )
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stop_not_below_entry")
        self.assertFalse(result["submitted"])
        self.assertEqual(adapter.client.calls, [])

    def test_live_submit_does_not_attach_a_protective_order(self) -> None:
        client = _FakeClient(
            info={
                "symbol": "BNBUSDT",
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": [
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.00001",
                        "maxQty": "9000",
                        "stepSize": "0.00001",
                    },
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "0.01",
                        "maxPrice": "1000000",
                        "tickSize": "0.01",
                    },
                    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
                ],
            }
        )
        adapter = BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "missing-switch",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
        )
        extras = self._zone_extras(quote_order_qty="25")
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(
                _sig(mode="live_stub", token_address="BNBUSDT", extras=extras)
            )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["submitted"])
        self.assertEqual(result["order"]["type"], "MARKET")
        self.assertNotIn("stopPrice", result["order"])
        self.assertNotIn("stop_price", result["order"])
        self.assertFalse(result["exit_plan"]["submitted"])
        self.assertEqual(result["exit_plan"]["protective_order"], "not_supported_not_sent")
        kinds = [call[0] for call in client.calls]
        self.assertEqual(kinds, ["exchange_info", "new_order"])
        _kind, signed, _key = client.calls[1]
        self.assertNotIn("stopPrice", signed)
        self.assertNotIn("STOP_LOSS", signed)
        self.assertNotIn("TAKE_PROFIT", signed)

    def test_gateway_receipt_carries_the_plan(self) -> None:
        paper = _RecordingPaper()
        adapter = self._adapter()
        env = _env(self.root, enabled="0", kill_exists=True)
        with mock.patch.dict(os.environ, env):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                paper_enabled=True,
                paper_db=self.root / "paper_eq_unused.db",
                paper_executor=paper,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=adapter,
            ) as gw:
                result = gw.process_one(
                    _decision_row(
                        mode="dry_run",
                        token_address="BNBUSDT",
                        extras=self._zone_extras(),
                    )
                )
        self.assertEqual(result["receipt"]["status"], "dry_run_acked")
        plan = result["exec_result"]["exit_plan"]
        self.assertEqual(plan["stop_price"], "99")
        self.assertEqual(plan["take_profit_price"], "110")
        self.assertFalse(plan["defaults_applied"])
        self.assertEqual(paper.calls, [])
        self.assertEqual(adapter.client.calls, [])


if __name__ == "__main__":
    unittest.main()
