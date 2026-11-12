"""Sanitized Binance rejection diagnostics. No live HTTP.

A rejected spot order used to persist only ``binance_http_error:<status>``.
The response code and msg were discarded, so a later receipt could not tell
filter failure, precision, and insufficient balance apart. These tests keep
the status, a bounded code/msg, and a safe fallback for bodies that are not
a Binance error object. They do not retry an order.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_FADE,
    BinanceRequestError,
    BinanceSpotAdapter,
    BinanceSpotHttpClient,
    public_exchange_error,
    sign_query,
)
from execution.executors.binance_spot_gate import REST_BOOK  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import initialize_spot_ledger_binding  # noqa: E402

_API_KEY = "test-key"
_API_SECRET = "test-secret"
_SIGNATURE = "0123456789abcdef0123456789abcdef"
_REQUEST_URL = "https://api.binance.com/api/v3/order?signature=" + _SIGNATURE
_DECISION_ID = 148060
_OBSERVED_QUOTE = "21.8819049515"
_WIRE_QUOTE = "21.88190495"

_FILTERS = [
    {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "maxQty": "900000", "stepSize": "0.01"},
    {"filterType": "PRICE_FILTER", "minPrice": "0.0001", "maxPrice": "100000", "tickSize": "0.0001"},
    {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True},
]
_INFO = {
    "symbol": "CYBERUSDT",
    "status": "TRADING",
    "quoteAsset": "USDT",
    "quoteAssetPrecision": 8,
    "isSpotTradingAllowed": True,
    "filters": _FILTERS,
}
_BOUND = {
    "accountType": "SPOT",
    "canTrade": True,
    "permissions": ["SPOT"],
    "restrictions": {
        "enableSpotAndMarginTrading": True,
        "enableReading": True,
        "ipRestrict": True,
    },
    "balances": [{"asset": "USDT", "free": "1000.00", "locked": "0"}],
}
_EXPECTED = {"account_type": "SPOT", "usdt_free": "1000"}


def _body(code: int, msg: str) -> bytes:
    return json.dumps({"code": code, "msg": msg}).encode("utf-8")


def _reject(status: int, body: bytes) -> BinanceRequestError:
    captured: dict[str, Any] = {}

    def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
        captured["url"] = request.full_url
        captured["header"] = request.get_header("X-mbx-apikey")
        captured["timeout"] = timeout
        return status, body

    client = BinanceSpotHttpClient(base_url="https://api.binance.test", opener=opener)
    signed = sign_query(
        [
            ("symbol", "CYBERUSDT"),
            ("side", "BUY"),
            ("type", "MARKET"),
            ("quoteOrderQty", _WIRE_QUOTE),
            ("recvWindow", "5000"),
            ("timestamp", "1700000000000"),
        ],
        _API_SECRET,
    )
    try:
        client.new_order(signed, api_key=_API_KEY)
    except BinanceRequestError as exc:
        captured_error = exc
    else:
        raise AssertionError("expected BinanceRequestError")
    shown = str(captured_error)
    if _API_KEY in shown or _SIGNATURE in shown or "api.binance.com" in shown:
        raise AssertionError(shown)
    if "X-MBX-APIKEY" in shown or "signature=" in shown.lower():
        raise AssertionError(shown)
    if captured["header"] != _API_KEY:
        raise AssertionError("opener did not see the test key")
    return captured_error


class DecodeRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        )
        self._net.start()

    def tearDown(self) -> None:
        self._net.stop()

    def test_status_only_when_the_body_was_discarded(self) -> None:
        bare = BinanceRequestError(400)
        self.assertEqual(str(bare), "binance_http_error:400")
        self.assertEqual(bare.status, 400)
        self.assertIsNone(bare.code)
        self.assertIsNone(bare.exchange_message)
        self.assertEqual(public_exchange_error(bare.public_view()), {"status": 400})

    def test_filter_precision_and_balance_codes_are_kept(self) -> None:
        cases = (
            (-1013, "Filter failure: LOT_SIZE"),
            (-1013, "Filter failure: MIN_NOTIONAL"),
            (-1013, "Filter failure: PRICE_FILTER"),
            (-1111, "Precision is over the maximum defined for this asset."),
            (-2010, "Account has insufficient balance for requested action."),
            (-1022, "Signature for this request is not valid."),
            (-2015, "Invalid API-key, IP, or permissions for action."),
        )
        for code, msg in cases:
            with self.subTest(code=code, msg=msg):
                exc = _reject(400, _body(code, msg))
                self.assertEqual(exc.status, 400)
                self.assertEqual(exc.code, code)
                self.assertEqual(exc.exchange_message, msg)
                self.assertEqual(str(exc), f"binance_http_error:400:{code}:{msg}")
                self.assertEqual(
                    public_exchange_error(exc.public_view((_API_KEY, _API_SECRET))),
                    {"status": 400, "code": code, "msg": msg},
                )

    def test_exchange_info_and_oco_use_the_same_decoder(self) -> None:
        seen: list[str] = []

        def opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
            del timeout
            seen.append(request.full_url)
            return 400, _body(-1013, "Filter failure: PERCENT_PRICE_BY_SIDE")

        client = BinanceSpotHttpClient(base_url="https://api.binance.test", opener=opener)
        with self.assertRaises(BinanceRequestError) as info_ctx:
            client.exchange_info("CYBERUSDT")
        signed = sign_query([("symbol", "CYBERUSDT"), ("timestamp", "1")], _API_SECRET)
        with self.assertRaises(BinanceRequestError) as oco_ctx:
            client.new_oco(signed, api_key=_API_KEY)
        self.assertEqual(info_ctx.exception.code, -1013)
        self.assertEqual(oco_ctx.exception.code, -1013)
        self.assertEqual(oco_ctx.exception.exchange_message, "Filter failure: PERCENT_PRICE_BY_SIDE")
        self.assertTrue(all("api.binance.com" not in url for url in seen))
        blob = str(oco_ctx.exception)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn(_SIGNATURE, blob)

    def test_malformed_non_json_and_non_utf8_stay_bounded(self) -> None:
        malformed = _reject(400, b'{"code":-1111,"msg":')
        self.assertEqual(malformed.status, 400)
        self.assertIsNone(malformed.code)
        self.assertIsNone(malformed.exchange_message)
        self.assertLess(len(str(malformed)), 220)
        self.assertNotIn("{", str(malformed))

        plain = _reject(400, b"Bad Request")
        self.assertEqual(plain.status, 400)
        self.assertIsNone(plain.code)
        self.assertEqual(plain.detail, "Bad Request")
        self.assertEqual(str(plain), "binance_http_error:400:detail:Bad Request")

        binary = _reject(400, b"\xff\xfe not utf-8 \x00")
        self.assertEqual(str(binary), "binance_http_error:400")
        self.assertIsNone(binary.detail)
        self.assertNotIn("\xff", str(binary))

        listed = _reject(400, b"[1, 2, 3]")
        self.assertEqual(str(listed), "binance_http_error:400")
        self.assertIsNone(listed.code)

    def test_huge_and_secret_bodies_do_not_leak(self) -> None:
        secret_msg = (
            "balance "
            + _API_KEY
            + " "
            + _API_SECRET
            + " "
            + _REQUEST_URL
            + " X-MBX-APIKEY: "
            + _API_KEY
            + " signature="
            + _SIGNATURE
        )
        leaked = _reject(400, _body(-2010, secret_msg))
        safe_text = leaked.redacted_text((_API_KEY, _API_SECRET))
        blob = json.dumps(leaked.public_view((_API_KEY, _API_SECRET)))
        self.assertEqual(leaked.code, -2010)
        self.assertNotIn(_API_KEY, blob)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn(_API_SECRET, safe_text)
        self.assertNotIn(_SIGNATURE, blob)
        self.assertNotIn("api.binance.com", blob)
        self.assertNotIn("X-MBX-APIKEY", blob)
        self.assertNotIn("signature", blob.lower())
        self.assertLess(len(blob), 400)
        self.assertIn("balance", blob)
        url_only = _reject(400, _body(-1000, _REQUEST_URL))
        self.assertEqual(url_only.code, -1000)
        self.assertIsNone(url_only.exchange_message)
        self.assertNotIn("https://", str(url_only))
        self.assertNotIn("api.binance.com", str(url_only))
        self.assertNotIn(_SIGNATURE, str(url_only))

        huge = b"A" * 300_000 + _API_SECRET.encode("utf-8") + b"https://api.binance.com/secret"
        huge_exc = _reject(413, huge)
        huge_text = str(huge_exc)
        self.assertLess(len(huge_text), 220)
        self.assertNotIn(_API_SECRET, huge_text)
        self.assertNotIn("api.binance.com", huge_text)
        self.assertNotIn("A" * 200, huge_text)

    def test_public_view_drops_header_query_and_url_fields(self) -> None:
        raw = {
            "status": 400,
            "code": -1111,
            "msg": "Precision is over the maximum defined for this asset.",
            "detail": "https://api.binance.com/api/v3/order?signature=" + _SIGNATURE,
            "signature": _SIGNATURE,
            "headers": {"X-MBX-APIKEY": _API_KEY},
            "url": _REQUEST_URL,
            "request": "quoteOrderQty=21.8819049515&signature=" + _SIGNATURE,
        }
        cleaned = public_exchange_error(raw)
        self.assertEqual(
            cleaned,
            {
                "status": 400,
                "code": -1111,
                "msg": "Precision is over the maximum defined for this asset.",
            },
        )
        dumped = json.dumps(cleaned)
        self.assertNotIn(_API_KEY, dumped)
        self.assertNotIn(_SIGNATURE, dumped)
        self.assertNotIn("http", dumped)


class _RejectingClient:
    def __init__(self, exc: BaseException) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.exc = exc

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("exchange_info", symbol))
        return _INFO

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        self.calls.append(("ticker_price", symbol))
        return {"symbol": symbol, "price": "1.5"}

    def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_order", signed_query, api_key))
        raise self.exc

    def new_oco(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("new_oco", signed_query, api_key))
        raise AssertionError("oco must not be posted when the buy was rejected")

    def get_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
        self.calls.append(("get_order", signed_query, api_key))
        raise AssertionError("a rejected buy must not be requeried into a second order")


def _buy() -> dict[str, Any]:
    return {
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "action": "buy",
        "mode": "live_stub",
        "decision_log_id": _DECISION_ID,
        "strategy_id": "tech_boll_fade_v1",
        "chain_id": "binance",
        "token_address": "CYBERUSDT",
        "pair_address": "CYBER/USDT",
        "size_pct": 0.05,
        "reason": "cyber-http-400",
        "extras": {
            "binance_symbol": "CYBERUSDT",
            "market": "spot",
            "order_type": "MARKET",
            "quote_order_qty": _OBSERVED_QUOTE,
            "price_usd": "1.5",
            "stop_price": "1.4",
            "take_profit_price": "1.7",
            "entry_reason": "large_trade_market",
        },
    }


def _decision_row() -> dict[str, Any]:
    sig = _buy()
    raw = {
        "pair_address": sig["pair_address"],
        "purpose": sig["purpose"],
        "extras": sig["extras"],
    }
    return {
        "id": _DECISION_ID,
        "ts": "2026-11-06T05:10:52Z",
        "strategy_id": sig["strategy_id"],
        "action": sig["action"],
        "chain_id": sig["chain_id"],
        "token_address": sig["token_address"],
        "size_pct": sig["size_pct"],
        "reason": sig["reason"],
        "risk_passed": 1,
        "mode": sig["mode"],
        "purpose": sig["purpose"],
        "raw_json": json.dumps(raw),
    }


def _env(root: Path) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "LIVE_KILL_SWITCH_PATH": str(root / "absent-kill"),
        "BINANCE_API_KEY": "",
        "BINANCE_API_SECRET": "",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
        "BINANCE_SPOT_API_BASE": "https://api.binance.test",
        "BINANCE_SPOT_PRODUCTION_BOOK": "",
        "BINANCE_SPOT_ACCOUNT_UID": "",
    }


class ReceiptPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("live binance HTTP is disabled"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _adapter(self, client: _RejectingClient) -> BinanceSpotAdapter:
        return BinanceSpotAdapter(
            client=client,
            kill_switch_path=self.root / "absent-kill",
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=_BOUND,
            production_book=REST_BOOK,
            rest_expected=_EXPECTED,
            auto_flatten_on_oco_fail=True,
        )

    def test_http_400_is_one_post_and_lands_in_the_ledger(self) -> None:
        exc = _reject(400, _body(-1013, "Filter failure: LOT_SIZE"))
        client = _RejectingClient(exc)
        adapter = self._adapter(client)
        snap = dict(adapter.account_snapshot)
        snap["uid"] = 424242
        adapter.account_snapshot = snap
        env = _env(self.root)
        ledger_path = self.root / "execution_ledger_live_bnb_boll_fade.db"
        registry = self.root / "registry.json"
        registry.write_text(json.dumps({"ledgers": [str(ledger_path.resolve())]}), encoding="utf-8")
        env["BINANCE_SPOT_LEDGER_REGISTRY"] = str(registry)
        with mock.patch.dict(os.environ, env):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / "execution_ledger_live_bnb_boll_fade.db",
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_FADE}),
                binance_spot=adapter,
            ) as gw:
                initialize_spot_ledger_binding(ledger_path, "424242")
                result = gw.process_one(_decision_row())
                stored = gw.ledger.conn.execute(
                    "SELECT receipt_json, executed, status FROM execution_log WHERE decision_log_id = ?",
                    (_DECISION_ID,),
                ).fetchone()
                position = gw.ledger.binance_spot_position_by_decision(_DECISION_ID)
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["receipt"]["status"], "live_refused")
        self.assertFalse(result["exec_result"]["submitted"])
        self.assertEqual(result["error"], "binance_http_error:400:-1013:Filter failure: LOT_SIZE")
        self.assertEqual(result["receipt"]["error"], result["error"])
        self.assertEqual(result["receipt"]["note"], "binance spot live fill refused; no retry")
        self.assertEqual(
            result["receipt"]["exchange_error"],
            {"status": 400, "code": -1013, "msg": "Filter failure: LOT_SIZE"},
        )
        self.assertEqual(result["exec_result"]["exchange_error"], result["receipt"]["exchange_error"])
        self.assertEqual(result["exec_result"]["order"]["quoteOrderQty"], _WIRE_QUOTE)
        self.assertNotEqual(result["exec_result"]["order"]["quoteOrderQty"], _OBSERVED_QUOTE)
        buy = parse_qs(next(call[1] for call in client.calls if call[0] == "new_order"))
        self.assertEqual(buy["quoteOrderQty"], [_WIRE_QUOTE])
        self.assertEqual(buy["symbol"], ["CYBERUSDT"])
        self.assertEqual(buy["side"], ["BUY"])
        self.assertEqual(
            [call[0] for call in client.calls],
            ["exchange_info", "ticker_price", "new_order"],
        )
        self.assertIsNone(position)
        assert stored is not None
        self.assertEqual(stored["executed"], 0)
        self.assertEqual(stored["status"], "rejected")
        receipt = json.loads(stored["receipt_json"])
        self.assertEqual(receipt["exchange_error"]["code"], -1013)
        self.assertEqual(receipt["exec_result"]["order"]["quoteOrderQty"], _WIRE_QUOTE)
        blob = stored["receipt_json"]
        self.assertNotIn(_API_KEY, blob)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn("signature", blob)
        self.assertNotIn("api.binance.com", blob)
        self.assertNotIn("X-MBX-APIKEY", blob)

    def test_timeout_and_network_errors_do_not_carry_the_request(self) -> None:
        reason = urllib.error.URLError(f"timed out {_REQUEST_URL} {_API_KEY} {_API_SECRET}")
        client = _RejectingClient(reason)
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(_buy())
        self.assertEqual(result["error"], "binance_http_error:URLError")
        self.assertFalse(result["submitted"])
        self.assertNotIn("exchange_error", result)
        self.assertEqual(
            [call[0] for call in client.calls],
            ["exchange_info", "ticker_price", "new_order"],
        )
        blob = json.dumps(result)
        self.assertNotIn(_API_KEY, blob)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn(_SIGNATURE, blob)
        self.assertNotIn("api.binance.com", blob)
        self.assertNotIn(_REQUEST_URL, blob)

        network = _RejectingClient(TimeoutError(f"read timed out {_REQUEST_URL}"))
        timeout_adapter = self._adapter(network)
        with mock.patch.dict(os.environ, _env(self.root)):
            timed = timeout_adapter.execute(_buy())
        self.assertEqual(timed["error"], "binance_http_error:TimeoutError")
        self.assertNotIn("api.binance.com", json.dumps(timed))
        self.assertEqual(
            [call[0] for call in network.calls],
            ["exchange_info", "ticker_price", "new_order"],
        )

    def test_secret_response_stays_out_of_the_adapter_result(self) -> None:
        secret_msg = (
            "balance "
            + _API_KEY
            + " "
            + _API_SECRET
            + " "
            + _REQUEST_URL
            + " signature="
            + _SIGNATURE
        )
        client = _RejectingClient(_reject(400, _body(-2010, secret_msg)))
        adapter = self._adapter(client)
        with mock.patch.dict(os.environ, _env(self.root)):
            result = adapter.execute(_buy())
        blob = json.dumps(result)
        self.assertEqual(result["exchange_error"]["code"], -2010)
        self.assertEqual(result["exchange_error"]["status"], 400)
        self.assertIn("balance", result["error"])
        self.assertNotIn(_API_KEY, blob)
        self.assertNotIn(_API_SECRET, blob)
        self.assertNotIn(_SIGNATURE, blob)
        self.assertNotIn("api.binance.com", blob)
        self.assertNotIn("signature", blob.lower())
        self.assertEqual(
            [call[0] for call in client.calls],
            ["exchange_info", "ticker_price", "new_order"],
        )
        self.assertFalse(result["submitted"])

    def test_below_min_notional_is_refused_before_a_post(self) -> None:
        client = _RejectingClient(BinanceRequestError(400))
        adapter = self._adapter(client)
        signal = _buy()
        signal["extras"] = dict(signal["extras"])
        signal["extras"]["quote_order_qty"] = "4.50"
        with mock.patch.dict(os.environ, _env(self.root)):
            refused = adapter.execute(signal)
        self.assertEqual(refused["error"], "entry_notional_below_min")
        self.assertFalse(refused["submitted"])
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
