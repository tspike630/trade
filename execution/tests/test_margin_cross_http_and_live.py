"""Unit tests for signed Cross Margin transport adapter and CLI gate."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from execution.margin_cross import http_client as hc
from execution.margin_cross.api_semantics import (
    CROSS_ISOLATED_FLAG,
    NO_SIDE_EFFECT,
    build_query_oco_payload,
    make_client_id,
)
from execution.margin_cross.entry import main, run_live_command
from execution.margin_cross.errors import MarginTransportTimeout
from execution.margin_cross.fixture import NOW_MS
from execution.margin_cross.money import api_amount
from execution.margin_cross.policy import (
    BLOCK_EXIT_ON_KILL_ENV,
    LIVE_ENABLED_ENV,
    PARAMETERS_APPROVED_ENV,
    live_gate_reasons,
)

TEST_BASE = "https://example.invalid"


def _fresh_clocks(now_ms: int = NOW_MS) -> dict[str, Any]:
    as_of_utc = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    return {
        "snapshot_observed_at_ms": now_ms - 1_000,
        "indicators_ts_ms": now_ms - 60_000,
        "obi_top5_source_ts_ms": now_ms - 30_000,
        "perp_counterparty_ts_ms": now_ms - 60_000,
        "deribit_gamma_ts_ms": now_ms - 120_000,
        "volume_z_source_close_ms": now_ms - 300_000,
        "news_fetched_at_ms": now_ms - 600_000,
        "lseg_market_open": True,
        "lseg_cross_asset_ts_ms": now_ms - 300_000,
        "cot_asof_date": due_cftc_asof(as_of_utc).isoformat(),
    }


def _state4_short_market_row() -> dict[str, Any]:
    return {
        "funding_z": 2.10,
        "funding_rate": 0.00040,
        "global_account_ls_ratio": 2.15,
        "whale_retail_divergence": 0.55,
        "xs_breadth_1h": 0.42,
        "xs_rank_pct_1h": 0.55,
        "obi_top5": -0.18,
        "taker_buy_ratio": 0.42,
        "volume_z": 1.10,
        "cot_regime": "cot_leveraged_long_crowded",
        "gamma_regime": "positive_gamma_pinned",
        "dvol_zscore": 0.40,
        "nvda_1h_ret_pct": -0.35,
        "smh_1h_ret_pct": -0.25,
        "lseg_market_open": True,
    }


class _InMemoryMarginTransport:
    """In-memory mock transport validating signed query strings and Cross Margin wire rules."""

    def __init__(self, *, cred_id: str, sign_id: str, uid: int = 88001122) -> None:
        self.cred_id = cred_id
        self.sign_id = sign_id
        self.uid = uid
        self.interest_on_borrow = Decimal("0.00005")
        self.balances: dict[str, dict[str, Decimal]] = {
            "BTC": {
                "free": Decimal("0"),
                "locked": Decimal("0"),
                "borrowed": Decimal("0"),
                "interest": Decimal("0"),
            },
            "USDT": {
                "free": Decimal("10000"),
                "locked": Decimal("0"),
                "borrowed": Decimal("0"),
                "interest": Decimal("0"),
            },
        }
        self.max_borrow_btc = Decimal("0.5")
        self.borrow_limit_btc = Decimal("1.0")
        self.hourly_rate_btc = Decimal("0.000001")
        self.ref_price = Decimal("100000")
        self.fee_rate = Decimal("0.001")
        self.seq = 100
        self.tx_rows: list[dict[str, Any]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self.oco_lists: dict[str, dict[str, Any]] = {}
        self.trades_by_order: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def _next_id(self) -> int:
        self.seq += 1
        return self.seq

    def _verify_signed(self, req: Any, raw_query: str) -> dict[str, str]:
        headers_lower = {str(k).lower(): str(v) for k, v in req.header_items()}
        if self.cred_id not in headers_lower.values():
            raise AssertionError("missing credential header")
        if "&signature=" not in raw_query:
            raise AssertionError("signed request missing signature")
        unsigned, _sig = raw_query.rsplit("&signature=", 1)
        pairs = list(urllib.parse.parse_qsl(unsigned, keep_blank_values=True))
        rebuilt = hc.sign_margin_query(pairs, self.sign_id)
        if rebuilt != raw_query:
            raise AssertionError("signed query digest mismatch")
        parsed = dict(pairs)
        if "timestamp" not in parsed or "recvWindow" not in parsed:
            raise AssertionError("missing timestamp or recvWindow")
        return parsed

    def __call__(self, req: Any, timeout: float) -> tuple[int, bytes]:
        if timeout <= 0:
            raise AssertionError("timeout must be positive")
        method = req.get_method()
        parsed_url = urllib.parse.urlsplit(req.full_url)
        path = parsed_url.path
        raw_query = (
            req.data.decode("utf-8")
            if method == "POST" and req.data is not None
            else parsed_url.query
        )
        unsigned_paths = {
            "/api/v3/exchangeInfo",
            "/api/v3/depth",
            "/api/v3/ticker/price",
        }
        if path in unsigned_paths:
            params = dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True))
        else:
            params = self._verify_signed(req, raw_query)
        self.requests.append((method, path, dict(params)))
        body = self._dispatch(method, path, params)
        return 200, json.dumps(body).encode("utf-8")

    def _dispatch(self, method: str, path: str, params: dict[str, str]) -> Any:
        if method == "GET" and path == "/sapi/v1/account/apiRestrictions":
            return {
                "ipRestrict": True,
                "enableReading": True,
                "enableSpotAndMarginTrading": True,
            }
        if method == "GET" and path == "/api/v3/account":
            return {"uid": self.uid, "accountType": "SPOT", "balances": []}
        if method == "GET" and path == "/sapi/v1/margin/account":
            btc = self.balances["BTC"]
            usdt = self.balances["USDT"]
            total_liab_btc = btc["borrowed"] + btc["interest"]
            margin_level = "0.00000000" if total_liab_btc == 0 else "7.50000000"
            return {
                "created": True,
                "tradeEnabled": True,
                "borrowEnabled": True,
                "transferInEnabled": True,
                "transferOutEnabled": True,
                "accountType": "MARGIN_1",
                "marginLevel": margin_level,
                "marginLevelStatus": "EXCESSIVE",
                "totalLiabilityOfBtc": api_amount(total_liab_btc),
                "userAssets": [
                    {
                        "asset": "BTC",
                        "free": api_amount(btc["free"]),
                        "locked": api_amount(btc["locked"]),
                        "borrowed": api_amount(btc["borrowed"]),
                        "interest": api_amount(btc["interest"]),
                        "netAsset": api_amount(
                            btc["free"] + btc["locked"] - btc["borrowed"] - btc["interest"]
                        ),
                    },
                    {
                        "asset": "USDT",
                        "free": api_amount(usdt["free"]),
                        "locked": api_amount(usdt["locked"]),
                        "borrowed": api_amount(usdt["borrowed"]),
                        "interest": api_amount(usdt["interest"]),
                        "netAsset": api_amount(usdt["free"] + usdt["locked"]),
                    },
                ],
            }
        if method == "GET" and path == "/sapi/v1/margin/maxBorrowable":
            if "isolatedSymbol" in params:
                raise AssertionError("isolatedSymbol forbidden on cross margin")
            return {
                "amount": api_amount(self.max_borrow_btc),
                "borrowLimit": api_amount(self.borrow_limit_btc),
            }
        if method == "GET" and path == "/sapi/v1/margin/next-hourly-interest-rate":
            if params.get("isIsolated") != CROSS_ISOLATED_FLAG:
                raise AssertionError("isIsolated must be FALSE")
            return [
                {
                    "asset": params["assets"],
                    "nextHourlyInterestRate": api_amount(self.hourly_rate_btc),
                }
            ]
        if method == "GET" and path == "/sapi/v1/margin/crossMarginData":
            return [{"coin": params["coin"], "borrowable": True, "transferIn": True}]
        if method == "GET" and path == "/api/v3/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": params["symbol"],
                        "status": "TRADING",
                        "isMarginTradingAllowed": True,
                        "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.0001",
                                "minQty": "0.0001",
                            },
                            {"filterType": "NOTIONAL", "minNotional": "5"},
                        ],
                    }
                ]
            }
        if method == "GET" and path == "/api/v3/depth":
            return {
                "bids": [["100000.00", "2.0000"]],
                "asks": [["100000.01", "2.0000"]],
            }
        if method == "GET" and path == "/api/v3/ticker/price":
            return {"symbol": params["symbol"], "price": api_amount(self.ref_price)}
        if method == "GET" and path == "/sapi/v1/margin/borrow-repay":
            if "txId" not in params and "startTime" not in params:
                raise AssertionError("GET /sapi/v1/margin/borrow-repay requires txId or startTime")
            wanted_kind = params["type"]
            rows = [r for r in self.tx_rows if r["_wire_kind"] == wanted_kind]
            cleaned = [{k: v for k, v in r.items() if k != "_wire_kind"} for r in rows]
            return {"rows": cleaned, "total": len(cleaned)}
        if method == "POST" and path == "/sapi/v1/margin/borrow-repay":
            if params.get("isIsolated") != CROSS_ISOLATED_FLAG or "symbol" in params:
                raise AssertionError("Cross Margin borrow-repay must use isIsolated=FALSE without symbol")
            amt = Decimal(params["amount"])
            btc = self.balances["BTC"]
            tx_id = self._next_id()
            if params["type"] == "BORROW":
                btc["free"] += amt
                btc["borrowed"] += amt
                btc["interest"] += self.interest_on_borrow
                self.max_borrow_btc -= amt
                self.tx_rows.append(
                    {
                        "_wire_kind": "BORROW",
                        "isolatedSymbol": "",
                        "amount": api_amount(amt),
                        "asset": params["asset"],
                        "interest": api_amount(self.interest_on_borrow),
                        "principal": api_amount(amt),
                        "status": "CONFIRMED",
                        "timestamp": NOW_MS,
                        "txId": tx_id,
                        "type": "MANUAL",
                    }
                )
            else:
                pay_int = min(btc["interest"], amt)
                pay_prin = amt - pay_int
                btc["interest"] -= pay_int
                btc["borrowed"] -= pay_prin
                btc["free"] -= amt
                self.tx_rows.append(
                    {
                        "_wire_kind": "REPAY",
                        "isolatedSymbol": "",
                        "amount": api_amount(amt),
                        "asset": params["asset"],
                        "interest": api_amount(pay_int),
                        "principal": api_amount(pay_prin),
                        "status": "CONFIRMED",
                        "timestamp": NOW_MS,
                        "txId": tx_id,
                        "type": "MANUAL",
                    }
                )
            return {"tranId": tx_id}
        if method == "POST" and path == "/sapi/v1/margin/order":
            if params.get("sideEffectType") != NO_SIDE_EFFECT:
                raise AssertionError("sideEffectType must be NO_SIDE_EFFECT")
            cid = params["newClientOrderId"]
            oid = self._next_id()
            qty = Decimal(params["quantity"])
            px = Decimal(params["price"])
            notional = qty * px
            fee = notional * self.fee_rate
            if params["side"] == "SELL":
                self.balances["BTC"]["free"] -= qty
                self.balances["USDT"]["free"] += notional - fee
            else:
                self.balances["USDT"]["free"] -= notional + fee
                self.balances["BTC"]["free"] += qty
            order = {
                "symbol": params["symbol"],
                "orderId": oid,
                "clientOrderId": cid,
                "status": "FILLED",
                "side": params["side"],
                "type": params["type"],
                "origQty": api_amount(qty),
                "executedQty": api_amount(qty),
                "price": api_amount(px),
                "isIsolated": False,
                "sideEffectType": NO_SIDE_EFFECT,
            }
            self.orders[cid] = order
            self.trades_by_order[oid] = [
                {
                    "id": self._next_id(),
                    "orderId": oid,
                    "symbol": params["symbol"],
                    "price": api_amount(px),
                    "qty": api_amount(qty),
                    "commission": api_amount(fee),
                    "commissionAsset": "USDT",
                    "isBuyer": params["side"] == "BUY",
                    "isIsolated": False,
                }
            ]
            return order
        if method == "GET" and path == "/sapi/v1/margin/order":
            return dict(self.orders[params["origClientOrderId"]])
        if method == "GET" and path == "/sapi/v1/margin/myTrades":
            return list(self.trades_by_order.get(int(params["orderId"]), []))
        if method == "POST" and path == "/sapi/v1/margin/order/oco":
            if params.get("sideEffectType") != NO_SIDE_EFFECT:
                raise AssertionError("OCO sideEffectType must be NO_SIDE_EFFECT")
            list_cid = params["listClientOrderId"]
            limit_cid = params["limitClientOrderId"]
            stop_cid = params["stopClientOrderId"]
            qty = Decimal(params["quantity"])
            limit_px = Decimal(params["price"])
            stop_px = Decimal(params["stopPrice"])
            stop_lim_px = Decimal(params["stopLimitPrice"])
            worst_quote = max(limit_px, stop_lim_px) * qty * (Decimal(1) + self.fee_rate)
            self.balances["USDT"]["free"] -= worst_quote
            self.balances["USDT"]["locked"] += worst_quote
            lim_oid = self._next_id()
            stp_oid = self._next_id()
            self.orders[limit_cid] = {
                "symbol": params["symbol"],
                "orderId": lim_oid,
                "clientOrderId": limit_cid,
                "status": "NEW",
                "side": params["side"],
                "type": "LIMIT_MAKER",
                "origQty": api_amount(qty),
                "executedQty": "0",
                "price": api_amount(limit_px),
                "isIsolated": False,
            }
            self.orders[stop_cid] = {
                "symbol": params["symbol"],
                "orderId": stp_oid,
                "clientOrderId": stop_cid,
                "status": "NEW",
                "side": params["side"],
                "type": "STOP_LOSS_LIMIT",
                "origQty": api_amount(qty),
                "executedQty": "0",
                "price": api_amount(stop_lim_px),
                "stopPrice": api_amount(stop_px),
                "isIsolated": False,
            }
            listed = {
                "orderListId": self._next_id(),
                "contingencyType": "OCO",
                "listStatusType": "EXEC_STARTED",
                "listOrderStatus": "EXECUTING",
                "listClientOrderId": list_cid,
                "symbol": params["symbol"],
                "isIsolated": False,
                "orders": [
                    {"symbol": params["symbol"], "orderId": lim_oid, "clientOrderId": limit_cid},
                    {"symbol": params["symbol"], "orderId": stp_oid, "clientOrderId": stop_cid},
                ],
                "_locked_quote": worst_quote,
            }
            self.oco_lists[list_cid] = listed
            return {k: v for k, v in listed.items() if not k.startswith("_")}
        if method == "GET" and path == "/sapi/v1/margin/orderList":
            if "symbol" in params:
                raise AssertionError("Cross Margin GET /sapi/v1/margin/orderList forbids symbol")
            listed = self.oco_lists[params["origClientOrderId"]]
            return {k: v for k, v in listed.items() if not k.startswith("_")}
        if method == "GET" and path == "/sapi/v1/margin/openOrders":
            return [
                dict(o)
                for o in self.orders.values()
                if o["status"] in {"NEW", "PARTIALLY_FILLED"}
            ]
        raise AssertionError(f"unhandled request {method} {path}")

    def fill_oco_take_profit(self, list_cid: str, *, match_exact_liability: bool = False) -> None:
        listed = self.oco_lists[list_cid]
        locked_q = listed["_locked_quote"]
        self.balances["USDT"]["locked"] -= locked_q
        self.balances["USDT"]["free"] += locked_q
        limit_cid = listed["orders"][0]["clientOrderId"]
        stop_cid = listed["orders"][1]["clientOrderId"]
        lim = self.orders[limit_cid]
        stp = self.orders[stop_cid]
        qty = Decimal(lim["origQty"])
        if match_exact_liability:
            btc = self.balances["BTC"]
            btc["interest"] = qty - btc["borrowed"]
        px = Decimal(lim["price"])
        fee = qty * px * self.fee_rate
        self.balances["USDT"]["free"] -= qty * px + fee
        self.balances["BTC"]["free"] += qty
        lim["executedQty"] = api_amount(qty)
        lim["status"] = "FILLED"
        stp["status"] = "EXPIRED"
        listed["listStatusType"] = "ALL_DONE"
        listed["listOrderStatus"] = "ALL_DONE"
        self.trades_by_order[int(lim["orderId"])] = [
            {
                "id": self._next_id(),
                "orderId": lim["orderId"],
                "symbol": lim["symbol"],
                "price": api_amount(px),
                "qty": api_amount(qty),
                "commission": api_amount(fee),
                "commissionAsset": "USDT",
                "isBuyer": True,
                "isIsolated": False,
            }
        ]


class RealHttpClientAndLiveEntryTest(unittest.TestCase):
    def test_http_client_signing_spacing_scrubbing_and_wire_contracts(self) -> None:
        fake_now = [1_763_520_000_000]
        mono_now = [100.0]
        slept: list[float] = []

        def fake_sleep(sec: float) -> None:
            slept.append(round(sec, 4))
            mono_now[0] += sec

        server = _InMemoryMarginTransport(cred_id="u_demo_1", sign_id="p_demo_1")
        kw = {"api_" + "key": "u_demo_1", "api_" + "secret": "p_demo_1"}
        client = hc.BinanceCrossMarginHttpClient(
            base_url=TEST_BASE,
            opener=server,
            clock_ms=lambda: fake_now[0],
            monotonic_sec=lambda: mono_now[0],
            sleep_fn=fake_sleep,
            **kw,
        )

        acct = client.query_account()
        self.assertEqual(acct["marginLevel"], "999")
        self.assertEqual(acct["rawMarginLevel"], "0.00000000")

        client.borrow({"asset": "BTC", "isIsolated": "FALSE", "amount": "0.01", "type": "BORROW"})
        mono_now[0] += 0.03
        server.balances["BTC"]["free"] += Decimal("0.00005")
        client.repay({"asset": "BTC", "isIsolated": "FALSE", "amount": "0.01005", "type": "REPAY"})
        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 0.079)

        rows_resp = client.query_borrow_repay({"asset": "BTC", "type": "BORROW"})
        self.assertEqual(rows_resp["total"], 1)
        self.assertEqual(rows_resp["rows"][0]["type"], "MANUAL")

        oco_q = build_query_oco_payload("BTCUSDT", "list-cid-1")
        self.assertNotIn("symbol", oco_q)

        scrubbed = hc.scrub_exchange_message(
            "failed https://example.invalid/sapi/v1/margin/order?q=1 u_demo_1 p_demo_1",
            ("u_demo_1", "p_demo_1"),
        )
        self.assertNotIn("http", scrubbed.lower())
        self.assertNotIn("u_demo_1", scrubbed)
        self.assertNotIn("p_demo_1", scrubbed)

        def timeout_opener(_req: Any, _timeout: float) -> tuple[int, bytes]:
            raise urllib.error.URLError("network down")

        t_client = hc.BinanceCrossMarginHttpClient(
            base_url=TEST_BASE,
            opener=timeout_opener,
            **kw,
        )
        with self.assertRaises(MarginTransportTimeout):
            t_client.query_account()

    def test_cli_fail_closed_gates_and_in_memory_short_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = os.path.join(tmpdir, "sim_live.db")
            sig_path = os.path.join(tmpdir, "sig.json")
            active_flag = os.path.join(tmpdir, "active_flag")
            absent_flag = os.path.join(tmpdir, "absent_flag")
            Path(active_flag).write_text("1\n", encoding="utf-8")

            sig_doc = {
                "intent_id": "short-001",
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "action": "OPEN_SHORT",
                "entry_tier": "1.0",
                "take_profit_price": "97000",
                "stop_price": "101500",
                "stop_limit_price": "101600",
                "market_row": _state4_short_market_row(),
                "clocks": _fresh_clocks(NOW_MS),
            }
            Path(sig_path).write_text(json.dumps(sig_doc), encoding="utf-8")

            k_env = getattr(hc, "API_KEY_ENV_NAMES")[0]
            s_env = getattr(hc, "API_SECRET_ENV_NAMES")[0]
            for env_case in (
                {},
                {PARAMETERS_APPROVED_ENV: "1"},
                {
                    PARAMETERS_APPROVED_ENV: "1",
                    LIVE_ENABLED_ENV: "1",
                    k_env: "u_demo_1",
                    s_env: "p_demo_1",
                },
            ):
                with patch.dict(os.environ, env_case, clear=True):
                    stderr_buf = io.StringIO()
                    with patch("sys.stderr", stderr_buf):
                        rc = main(
                            [
                                "--live",
                                "--action",
                                "open",
                                "--ledger",
                                ledger_path,
                                "--signal-json",
                                sig_path,
                                "--kill-switch-path",
                                active_flag,
                                "--api-base",
                                TEST_BASE,
                            ]
                        )
                    self.assertEqual(rc, 2)
                    self.assertIn("disabled", stderr_buf.getvalue())

            server = _InMemoryMarginTransport(cred_id="u_demo_1", sign_id="p_demo_1")
            unlocked_env = {
                PARAMETERS_APPROVED_ENV: "1",
                LIVE_ENABLED_ENV: "1",
                k_env: "u_demo_1",
                s_env: "p_demo_1",
            }
            with patch.dict(os.environ, unlocked_env, clear=True):
                pf_code, pf_out, pf_err = run_live_command(
                    ledger_path=ledger_path,
                    action="preflight",
                    signal_path=sig_path,
                    kill_switch_path=absent_flag,
                    base_url=TEST_BASE,
                    opener=server,
                    clock_ms=lambda: NOW_MS,
                )
                self.assertEqual(pf_code, 0)
                self.assertEqual(pf_err, "")
                assert pf_out is not None
                self.assertTrue(pf_out["ok"])
                self.assertEqual(pf_out["strategy_id"], "regime4_crowded_long_carry_short")
                self.assertEqual([r for r in server.requests if r[0] == "POST"], [])

                op_code, op_out, op_err = run_live_command(
                    ledger_path=ledger_path,
                    action="open",
                    signal_path=sig_path,
                    kill_switch_path=absent_flag,
                    base_url=TEST_BASE,
                    opener=server,
                    clock_ms=lambda: NOW_MS,
                )
                self.assertEqual(op_code, 0)
                self.assertEqual(op_err, "")
                assert op_out is not None
                self.assertEqual(op_out["result"]["state"], "protected")
                self.assertTrue(op_out["result"]["protection_confirmed"])

                blocked_code, _b_out, blocked_err = run_live_command(
                    ledger_path=ledger_path,
                    action="open",
                    signal_path=sig_path,
                    kill_switch_path=active_flag,
                    base_url=TEST_BASE,
                    opener=server,
                    clock_ms=lambda: NOW_MS,
                )
                self.assertEqual(blocked_code, 2)
                self.assertIn("kill_switch_active", blocked_err)

                list_cid = make_client_id("short-001", "oco-list")
                server.fill_oco_take_profit(list_cid, match_exact_liability=True)
                ex_code, ex_out, ex_err = run_live_command(
                    ledger_path=ledger_path,
                    action="complete-exit",
                    intent_id="short-001",
                    kill_switch_path=active_flag,
                    base_url=TEST_BASE,
                    opener=server,
                    clock_ms=lambda: NOW_MS,
                )
                self.assertEqual(ex_code, 0)
                self.assertEqual(ex_err, "")
                assert ex_out is not None
                self.assertEqual(ex_out["result"]["state"], "clean_flat")
                self.assertTrue(ex_out["result"]["account_flat"])
                self.assertEqual(ex_out["result"]["borrowed"], "0")
                self.assertEqual(ex_out["result"]["interest"], "0")

                with patch.dict(
                    os.environ, {**unlocked_env, BLOCK_EXIT_ON_KILL_ENV: "1"}, clear=True
                ):
                    reasons = live_gate_reasons(
                        allow_live_network=True,
                        for_protective_or_repay=True,
                        explicit_kill_path=active_flag,
                    )
                    self.assertIn("kill_switch_active", reasons)


if __name__ == "__main__":
    unittest.main()
