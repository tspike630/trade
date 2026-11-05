"""Production live_bnb gate on an isolated ledger. No HTTP.

    python -m unittest execution.tests.test_live_bnb_gate_dispatch
"""

from __future__ import annotations

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

from execution.executors.binance_spot import BinanceSpotAdapter  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import ExecutionLedger  # noqa: E402
from execution.live_bnb_dispatch import (  # noqa: E402
    VERIFIED_FET_FILL,
    CatalogProbe,
    dispatch_live_bnb,
    import_verified_fet_fill,
    live_bnb_paper_env_block,
)

_PROD_CURSOR = 147840


def _env(root: Path) -> dict[str, str]:
    return {
        "BINANCE_SPOT_LIVE_ENABLED": "1",
        "LIVE_KILL_SWITCH_PATH": str(root / "no-such-kill-switch"),
        "EXECUTION_PAPER_ENABLED": "",
        "PAPER_DB_PATH": "",
        "EXECUTION_PAPER_DB": "",
        "BINANCE_API_KEY": "sentinelsecret-not-read",
        "BINANCE_API_SECRET": "sentinelsecret-not-read",
        "LIVE_EXCHANGE_API_KEY": "",
        "LIVE_EXCHANGE_API_SECRET": "",
    }


def _row(decision_id: int, action: str = "buy") -> dict[str, Any]:
    raw = {
        "pair_address": "FET/USDT",
        "purpose": "live_bnb_boll_fade",
        "extras": {
            "binance_symbol": "FETUSDT",
            "quote_order_qty": "21.93",
            "order_type": "MARKET",
        },
    }
    return {
        "id": decision_id,
        "ts": "2026-11-05T12:00:00Z",
        "strategy_id": "tech_boll_fade_v1",
        "action": action,
        "chain_id": "binance",
        "token_address": "FETUSDT",
        "size_pct": 0.05,
        "reason": "test",
        "risk_passed": 1,
        "mode": "live_stub",
        "purpose": "live_bnb_boll_fade",
        "raw_json": json.dumps(raw),
    }


class LiveBnbDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._net = mock.patch(
            "execution.executors.binance_spot.urllib.request.urlopen",
            side_effect=AssertionError("outbound HTTP is disabled"),
        )
        self._net.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ledger_path = self.root / "execution_ledger_live_bnb_boll_fade.db"
        self.env = _env(self.root)

    def tearDown(self) -> None:
        self._net.stop()
        self._tmp.cleanup()

    def _ledger(self) -> ExecutionLedger:
        ledger = ExecutionLedger(self.ledger_path)
        ledger.set_last_processed_id(_PROD_CURSOR)
        return ledger

    def test_verified_fill_is_idempotent_and_leaves_the_cursor(self) -> None:
        ledger = self._ledger()
        self.assertEqual(
            ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM binance_spot_positions WHERE symbol = ?",
                ("FETUSDT",),
            ).fetchone()["n"],
            0,
        )
        first = import_verified_fet_fill(ledger)
        second = import_verified_fet_fill(ledger)
        self.assertEqual(ledger.get_last_processed_id(), _PROD_CURSOR)
        self.assertEqual(len(first["canonical_positions"]), 1)
        self.assertEqual(len(second["canonical_positions"]), 1)
        self.assertEqual(len(second["spent_orders"]), 1)
        row = second["canonical_positions"][0]
        self.assertEqual(row["symbol"], "FETUSDT")
        self.assertEqual(row["status"], "held_naked")
        self.assertEqual(row["quantity"], "140.1")
        self.assertEqual(row["entry_order_id"], "3735698106")
        self.assertEqual(row["client_order_id"], "bnbfade-147831")
        self.assertEqual(row["avg_price"], "0.1566")
        self.assertNotIn("0.156425", json.dumps(second))
        self.assertEqual(
            ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM binance_spot_positions WHERE symbol = ?",
                ("FETUSDT",),
            ).fetchone()["n"],
            0,
        )
        spent = second["spent_orders"][0]
        self.assertEqual(spent["commission"], "0.00002310")
        self.assertEqual(spent["commission_asset"], "BNB")
        with self.assertRaises(ValueError):
            ledger.import_verified_fill(
                {**VERIFIED_FET_FILL, "take_profit_price": "0.156425"}
            )
        self.assertEqual(ledger.get_last_processed_id(), _PROD_CURSOR)
        ledger.close()

    def test_gateway_blocks_buys_and_sells_without_http(self) -> None:
        ledger = self._ledger()
        import_verified_fet_fill(ledger)
        probe = CatalogProbe(("spot.orderList.oco",), reachable=False)
        with mock.patch.dict(os.environ, self.env, clear=False):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger=ledger,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({"live_bnb_boll_fade"}),
                mcp_transport=probe,
            ) as gw:
                with self.assertLogs("dexscreener.execution.gateway", level="INFO") as logs:
                    duplicate = gw.process_one(_row(147831))
                    other = gw.process_one(_row(147999))
                    sell = gw.process_one(_row(147832, action="sell"))
        text = json.dumps(
            {"duplicate": duplicate, "other": other, "sell": sell}
        )
        self.assertNotIn("sentinelsecret-not-read", text)
        self.assertEqual(duplicate["executed"], 0)
        self.assertEqual(duplicate["receipt"]["status"], "gate_skip")
        self.assertEqual(duplicate["receipt"]["gate_reason"], "spent_client_order")
        self.assertEqual(other["receipt"]["gate_reason"], "naked_holds_new_buys")
        self.assertEqual(sell["receipt"]["gate_reason"], "hold_naked_until_native_oco")
        self.assertEqual(sell["exec_result"]["allow_sell"], False)
        self.assertEqual(sell["exec_result"]["parent_calls"], [])
        self.assertFalse(sell["exec_result"]["protection_tool_present"])
        self.assertFalse(sell["exec_result"]["handshake"])
        self.assertEqual(probe.invocations, 0)
        self.assertTrue(any("live_bnb gate" in line for line in logs.output))
        self.assertEqual(
            ledger.conn.execute(
                "SELECT COUNT(*) AS n FROM binance_spot_positions WHERE symbol = ?",
                ("FETUSDT",),
            ).fetchone()["n"],
            0,
        )
        ledger.close()

    def test_empty_book_and_other_purpose_fail_closed(self) -> None:
        ledger = self._ledger()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger=ledger,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({"live_bnb_boll_fade"}),
            ) as gw:
                missing = gw.process_one(_row(147841))
                other = gw.process_one(
                    {
                        **_row(147842),
                        "purpose": "race_tech_boll_fade",
                        "raw_json": json.dumps(
                            {
                                "pair_address": "FET/USDT",
                                "purpose": "race_tech_boll_fade",
                                "extras": {"binance_symbol": "FETUSDT"},
                            }
                        ),
                    }
                )
        self.assertEqual(missing["receipt"]["gate_reason"], "canonical_fet_missing")
        self.assertEqual(missing["executed"], 0)
        self.assertEqual(missing["exec_result"]["parent_calls"], [])
        self.assertEqual(other["executed"], 0)
        self.assertIn("purpose", str(other.get("error") or other.get("validation_errors")))
        ledger.close()

    def test_unreachable_parent_catalog_does_not_release_oco(self) -> None:
        ledger = self._ledger()
        book = import_verified_fet_fill(ledger)
        probe = CatalogProbe(("spot.orderList.oco",), reachable=False)
        result = dispatch_live_bnb(_row_signal(147999), book, probe)
        self.assertEqual(result["reason"], "naked_holds_new_buys")
        self.assertFalse(result["handshake"])
        self.assertFalse(result["protection_tool_present"])
        self.assertEqual(probe.invocations, 0)
        self.assertTrue(all(item["release"] is False for item in result["parent_queue"]))
        ledger.close()

    def test_uninjected_adapter_does_not_open_http(self) -> None:
        kill = self.root / "no-such-kill-switch"
        adapter = BinanceSpotAdapter(
            client=None,
            kill_switch_path=kill,
            api_key="sentinelsecret-not-read",
            api_secret="sentinelsecret-not-read",
            clock_ms=lambda: 1_700_000_000_000,
        )
        with mock.patch.dict(os.environ, self.env, clear=False):
            result = adapter.execute(
                {
                    "purpose": "live_bnb_boll_fade",
                    "action": "buy",
                    "mode": "live_stub",
                    "decision_log_id": 147999,
                    "extras": {
                        "binance_symbol": "FETUSDT",
                        "quote_order_qty": "25",
                        "order_type": "MARKET",
                    },
                }
            )
        self.assertEqual(result["error"], "mcp_gate_required")
        self.assertEqual(result["executed"], 0)
        self.assertFalse(result["submitted"])
        self.assertNotIn("sentinelsecret-not-read", json.dumps(result))

    def test_paper_env_must_be_explicitly_cleared(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"PAPER_DB_PATH": "paper_trading.db", "EXECUTION_PAPER_DB": "", "EXECUTION_PAPER_ENABLED": ""},
            clear=False,
        ):
            blocked = live_bnb_paper_env_block(paper_enabled=False, accept_live_bnb=True)
        self.assertIn("PAPER_DB_PATH", blocked or "")
        with mock.patch.dict(
            os.environ,
            {
                "PAPER_DB_PATH": "",
                "EXECUTION_PAPER_DB": "",
                "EXECUTION_PAPER_ENABLED": "",
            },
            clear=False,
        ):
            self.assertIsNone(
                live_bnb_paper_env_block(paper_enabled=False, accept_live_bnb=True)
            )
        self.assertIsNone(
            live_bnb_paper_env_block(paper_enabled=True, accept_live_bnb=False)
        )

    def test_urlopen_was_the_patched_socket(self) -> None:
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("https://api.binance.com/api/v3/ping")  # noqa: S310


def _row_signal(decision_id: int) -> dict[str, Any]:
    return {
        "purpose": "live_bnb_boll_fade",
        "action": "buy",
        "mode": "live_stub",
        "decision_log_id": decision_id,
        "extras": {"binance_symbol": "FETUSDT", "quote_order_qty": "21.93"},
    }


if __name__ == "__main__":
    unittest.main()
