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

from decimal import Decimal  # noqa: E402

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    build_exit_plan,
    extract_symbol_filters,
    floor_to_step,
    is_spot_usdt_symbol,
    plan_maker_ack,
    protective_exit_reason,
    sign_query,
    symbol_status_trading,
)
from execution.executors.binance_spot_gate import REST_BOOK  # noqa: E402
from execution.executors.binance_spot_protect import (  # noqa: E402
    fill_average_price,
    reference_protection_prices,
)
from execution.tests.isolated_env import patch_isolated  # noqa: E402
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
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["submitted"])
        self.assertFalse(result["consumer_allowed"])
        self.assertEqual(result["error"], "missing_exit_prices")
        self.assertEqual(client.calls, [])
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
        self.assertEqual(live["executed"], 0, live)
        self.assertEqual(live["executor"], BINANCE_SPOT_EXECUTOR)
        self.assertEqual(live["error"], "missing_exit_prices")
        self.assertEqual(live_client.calls, [])
        self.assertEqual(paper.calls, [])

    def test_paper_peer_and_frozen_onchain_purpose_stay_off_the_adapter(self) -> None:
        paper = _RecordingPaper()
        # Risk and daily-loss are bound when execution.config is first imported.
        # This case is the default-off process, even if the parent shell is not.
        with mock.patch("execution.gateway.EXECUTION_RISK_ENABLED", False), mock.patch(
            "execution.gateway.EXECUTION_DAILY_LOSS_USD", None
        ), self._gateway(
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
        with mock.patch("execution.gateway.EXECUTION_RISK_ENABLED", False), mock.patch(
            "execution.gateway.EXECUTION_DAILY_LOSS_USD", None
        ), self._gateway(
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
        with patch_isolated(_env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(
                _sig(mode="live_stub", token_address="BNBUSDT", extras=extras)
            )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["consumer_allowed"])
        self.assertEqual(result["error"], "account_book_unselected")
        self.assertEqual(client.calls, [])
        self.assertNotIn("STOP_LOSS", json.dumps(result))
        self.assertNotIn("TAKE_PROFIT", json.dumps(result))

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


_MAKER_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "9000", "stepSize": "0.003"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
    {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "1.2", "bidMultiplierDown": "0.2", "askMultiplierUp": "5", "askMultiplierDown": "0.8", "avgPriceMins": 1},
    {"filterType": "MIN_NOTIONAL", "minNotional": "1", "applyToMarket": True},
]


def _maker_info(symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "filters": _MAKER_FILTERS,
    }


class _MakerClient:
    def __init__(self, *, bid: str = "100.019", ask: str = "100.03") -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.bid = bid
        self.ask = ask
        self.open_rows: list[dict[str, Any]] = []
        self.cancel_status = "CANCELED"
        self.cancel_executed = "0"

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _maker_info(symbol)

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("book_ticker", symbol))
        return {
            "symbol": symbol,
            "bidPrice": self.bid,
            "bidQty": "2",
            "askPrice": self.ask,
            "askQty": "2",
        }

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": self.bid}

    def open_orders(self, signed_query: str, *, api_key: str) -> list[dict[str, Any]]:
        self.calls.append(("open_orders", signed_query, api_key))
        return list(self.open_rows)

    def cancel_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("cancel_order", signed_query, api_key))
        return {"orderId": "8", "status": self.cancel_status, "executedQty": self.cancel_executed}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        return {"orderId": "9", "status": "FILLED", "executedQty": "1", "cummulativeQuoteQty": "100"}

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        raise AssertionError("maker refusal must not place an OCO")


class MakerExecutionTests(unittest.TestCase):
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

    def _adapter(self, client: Any, **kwargs: Any) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "LIVE_TRADING_DISABLED",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot={
                "accountType": "SPOT",
                "canTrade": True,
                "permissions": ["SPOT"],
                "restrictions": {
                    "enableSpotAndMarginTrading": True,
                    "enableReading": True,
                    "ipRestrict": True,
                },
                "balances": [{"asset": "USDT", "free": "1000.00", "locked": "0"}],
            },
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "1000.00"},
            **kwargs,
        )

    def _buy(self, **extra: Any) -> dict[str, Any]:
        extras = {
            "binance_symbol": "BTCUSDT",
            "order_type": "LIMIT_MAKER",
            "quote_order_qty": "25",
            "price_usd": "100",
            "stop_price": "90",
            "take_profit_price": "120",
            "entry_reason": "large_trade_market",
        }
        extras.update(extra)
        return _sig(mode="live_stub", token_address="BTCUSDT", extras=extras)

    def test_floor_step_and_filter_precedence(self) -> None:
        self.assertEqual(floor_to_step("1", "0.3"), "0.9")
        self.assertEqual(floor_to_step(Decimal("1"), "0.3"), "0.9")
        self.assertEqual(floor_to_step("0.3", None), "0.3")
        with self.assertRaises(ValueError):
            floor_to_step(1.0, "0.3")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            floor_to_step("nan", "0.1")
        info = {
            "symbols": [
                _maker_info("ETHUSDT"),
                {"symbol": "BTCUSDT", "filters": [{"filterType": "NOTIONAL", "minNotional": "nope"}, {"filterType": "MIN_NOTIONAL", "minNotional": "1"}, {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1"}, {"filterType": "PRICE_FILTER", "tickSize": "0.1"}]},
            ]
        }
        self.assertIsNone(extract_symbol_filters(info, "BNBUSDT"))
        self.assertIsNone(extract_symbol_filters({"symbol": "ETHUSDT"}, "BTCUSDT"))
        broken = extract_symbol_filters(info, "BTCUSDT")
        self.assertIsNotNone(broken)
        assert broken is not None
        self.assertIsNone(broken["min_notional"])
        healthy = extract_symbol_filters(info, "ETHUSDT")
        self.assertIsNotNone(healthy)
        assert healthy is not None
        self.assertEqual(healthy["min_notional"], "5")
        self.assertEqual(healthy["step_size"], "0.003")

    def test_limit_maker_buy_uses_bid_and_omits_time_in_force(self) -> None:
        client = _MakerClient()
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(self._buy())
        self.assertEqual(result["error"], "maker_live_lifecycle_unsupported")
        self.assertFalse(result["submitted"])
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["consumer_allowed"])
        self.assertEqual(result["order"]["type"], "LIMIT_MAKER")
        self.assertEqual(result["order"]["side"], "BUY")
        self.assertEqual(result["order"]["price"], "100.01")
        self.assertEqual(result["order"]["quantity"], "0.249")
        self.assertNotIn("timeInForce", result["order"])
        self.assertNotIn("quoteOrderQty", result["order"])
        self.assertEqual([name for name, *_rest in client.calls], ["exchange_info", "book_ticker"])
        self.assertIn("PERCENT_PRICE_BY_SIDE", result["maker_lifecycle"]["unchecked_filters"])
        self.assertEqual(result["maker_lifecycle"]["state"], "pending")
        self.assertFalse(result["maker_lifecycle"]["protected"])
        self.assertFalse(result["maker_lifecycle"]["retry"])
        self.assertIsNone(result["maker_lifecycle"]["oco_qty"])

    def test_explicit_crossing_price_is_refused(self) -> None:
        client = _MakerClient()
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root, enabled="1", kill_exists=False)):
            result = adapter.execute(self._buy(price="101"))
        self.assertEqual(result["error"], "maker_price_crosses")
        self.assertEqual(client.calls, [("exchange_info", "BTCUSDT"), ("book_ticker", "BTCUSDT")])

    def test_sell_below_min_notional_or_qty_does_not_post(self) -> None:
        client = _MakerClient(bid="9.99", ask="10.00")
        adapter = self._adapter(client)
        env = _env(self.root, enabled="1", kill_exists=False)
        dust = {
            "binance_symbol": "BTCUSDT",
            "order_type": "LIMIT_MAKER",
            "quantity": "0.2",
            "price": "10",
        }
        with mock.patch.dict(os.environ, env):
            below_notional = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=dust)
            )
        self.assertEqual(below_notional["error"], "below_min_notional_or_lot")
        self.assertNotIn("new_order", [name for name, *_rest in client.calls])
        client.calls.clear()
        tiny = dict(dust, quantity="0.0004")
        with mock.patch.dict(os.environ, env):
            below_qty = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=tiny)
            )
        self.assertEqual(below_qty["error"], "below_min_notional_or_lot")
        self.assertEqual(client.calls[0][0], "exchange_info")
        self.assertNotIn("new_order", [name for name, *_rest in client.calls])

    def test_bad_book_and_filters_do_not_post(self) -> None:
        client = _MakerClient(bid="0", ask="1")
        adapter = self._adapter(client)
        env = _env(self.root, enabled="1", kill_exists=False)
        with mock.patch.dict(os.environ, env):
            bad_book = adapter.execute(self._buy())
        self.assertEqual(bad_book["error"], "maker_book_unavailable")
        self.assertNotIn("new_order", [name for name, *_rest in client.calls])

        class _NoFilters(_MakerClient):
            def exchange_info(self, symbol: str) -> dict[str, Any]:
                self.calls.append(("exchange_info", symbol))
                return {"symbol": symbol, "status": "TRADING", "quoteAsset": "USDT", "filters": []}

        bare = _NoFilters()
        with mock.patch.dict(os.environ, env):
            bad_filters = self._adapter(bare).execute(self._buy())
        self.assertEqual(bad_filters["error"], "maker_filters_unavailable")
        self.assertEqual([name for name, *_rest in bare.calls], ["exchange_info"])

    def test_stop_loss_cancels_resting_sell_before_market(self) -> None:
        client = _MakerClient()
        client.open_rows = [
            {"orderId": 8, "side": "SELL", "type": "LIMIT_MAKER", "status": "NEW", "orderListId": -1}
        ]
        adapter = self._adapter(client)
        env = _env(self.root, enabled="1", kill_exists=False)
        env["BINANCE_SPOT_MAKER_FIRST"] = "1"
        extras = {
            "binance_symbol": "BTCUSDT",
            "order_type": "LIMIT_MAKER",
            "quantity": "1",
            "exit_reason": "stop_loss",
        }
        with mock.patch.dict(os.environ, env):
            sold = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=extras)
            )
        names = [name for name, *_rest in client.calls]
        self.assertEqual(names, ["exchange_info", "open_orders", "cancel_order", "new_order"])
        self.assertTrue(sold["submitted"], sold)
        self.assertEqual(sold["order"]["type"], "MARKET")
        self.assertNotIn("timeInForce", sold["order"])
        client.calls.clear()
        client.cancel_status = "NEW"
        with mock.patch.dict(os.environ, env):
            stuck = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=extras)
            )
        self.assertEqual(stuck["error"], "stop_loss_cancel_unconfirmed")
        self.assertNotIn("new_order", [name for name, *_rest in client.calls])

    def test_kill_switch_sell_is_reducing_only_when_enabled(self) -> None:
        client = _MakerClient()
        adapter = self._adapter(client)
        env = _env(self.root, enabled="1", kill_exists=True)
        extras = {"binance_symbol": "BTCUSDT", "quantity": "0.5", "order_type": "MARKET"}
        with mock.patch.dict(os.environ, env):
            blocked = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=extras)
            )
            buy = adapter.execute(self._buy())
        self.assertEqual(blocked["error"], "inbound_sell_not_protective")
        self.assertTrue(str(buy["error"]).startswith("kill_switch:"))
        self.assertEqual(client.calls, [])
        env["BINANCE_SPOT_ALLOW_SELL_ON_KILL"] = "1"
        with mock.patch.dict(os.environ, env):
            unknown = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=extras)
            )
        self.assertEqual(unknown["error"], "sell_not_reducing_inventory")
        self.assertNotIn("new_order", [name for name, *_rest in client.calls])
        client.calls.clear()
        adapter.buy_blocks = [
            {"symbol": "BTCUSDT", "status": "open", "quantity": "1", "protection": "exchange_oco"}
        ]
        with mock.patch.dict(os.environ, env):
            allowed = adapter.execute(
                _sig(mode="live_stub", action="sell", token_address="BTCUSDT", extras=extras)
            )
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(allowed["order"]["side"], "SELL")
        self.assertEqual(allowed["order"]["quantity"], "0.5")
        self.assertIn("new_order", [name for name, *_rest in client.calls])

    def test_maker_ack_does_not_protect_partial_or_retry(self) -> None:
        pending = plan_maker_ack(
            {"status": "NEW", "executedQty": "0", "price": "100", "origQty": "1"},
            order_qty="1",
            limit_price="100",
        )
        self.assertEqual(pending["state"], "pending")
        self.assertFalse(pending["protected"])
        self.assertIsNone(pending["oco_qty"])
        self.assertEqual(pending["reserved_quote"], "100")
        self.assertFalse(pending["retry"])
        partial = plan_maker_ack(
            {"status": "PARTIALLY_FILLED", "executedQty": "0.4", "price": "999"},
            order_qty="1",
            limit_price="100",
        )
        self.assertEqual(partial["oco_qty"], "0.4")
        self.assertEqual(partial["reserved_quote"], "60")
        self.assertFalse(partial["protected"])
        filled = plan_maker_ack(
            {"status": "FILLED", "executedQty": "1", "cummulativeQuoteQty": "100", "price": "90"},
            order_qty="1",
            limit_price="100",
        )
        self.assertEqual(filled["state"], "filled")
        self.assertEqual(filled["oco_qty"], "1")
        self.assertFalse(filled["protected"])
        rejected = plan_maker_ack({"status": "REJECTED", "executedQty": "0"}, order_qty="1", limit_price="100")
        self.assertEqual(rejected["state"], "rejected")
        self.assertFalse(rejected["retry"])
        self.assertIsNone(fill_average_price({"status": "NEW", "executedQty": "0", "price": "100", "cummulativeQuoteQty": "0"}))
        self.assertEqual(fill_average_price({"executedQty": "1", "cummulativeQuoteQty": "100", "price": "90"}), "100")
        reference = reference_protection_prices("100")
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference["take_profit_price"], "110")
        self.assertEqual(reference["stop_price"], "95")
        self.assertEqual(Decimal(reference["stop_limit_price"]), Decimal("94.70"))
        self.assertEqual(reference["applied_to_live_orders"], "no")

    def test_book_ticker_get_shape(self) -> None:
        seen: dict[str, Any] = {}

        def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["agent"] = request.get_header("User-agent")
            return 200, b'{"symbol":"BTCUSDT","bidPrice":"1","askPrice":"2"}'

        client = BinanceSpotHttpClient(base_url="https://api.binance.test/", opener=opener)
        payload = client.book_ticker("BTCUSDT")
        self.assertEqual(payload["bidPrice"], "1")
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["agent"], "trade-binance-spot")
        self.assertTrue(str(seen["url"]).startswith("https://api.binance.test/api/v3/ticker/bookTicker?symbol=BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
