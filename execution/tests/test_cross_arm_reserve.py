"""Shared REST wallet reservation and cross-arm same-symbol BUY mutex.

No live HTTP. Does not sell a position or read a production secret.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawler"))

from execution.executors.binance_spot import BinanceSpotAdapter  # noqa: E402
from execution.executors.binance_spot_gate import (  # noqa: E402
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    REST_BOOK,
    buy_exposure_block,
    cross_arm_symbol_block,
    funding_after_reservation,
    reserved_quote_usdt,
)
from execution.executors.binance_spot_protect import ENTRY_MIN_NOTIONAL_USDT  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402
from execution.ledger import (  # noqa: E402
    ExecutionLedger,
    buy_reservation_lock,
    peer_live_bnb_ledger_path,
    read_spot_buy_blocks_readonly,
)
from execution.tests.test_live_bnb_boll_classic import (  # noqa: E402
    _adapter,
    _classic_extras,
    _decision_row,
    _env,
)

_FADE = "execution_ledger_live_bnb_boll_fade.db"
_CLASSIC = "execution_ledger_live_bnb_boll_classic.db"


def _held(
    purpose: str,
    symbol: str,
    *,
    status: str = "oco_working",
    qty: str = "2",
    entry: str = "50",
) -> dict[str, Any]:
    return {
        "purpose": purpose,
        "symbol": symbol,
        "status": status,
        "protection": "exchange_oco",
        "entry_order_id": "9",
        "oco_order_list_id": "3",
        "oco_above_order_id": "4",
        "oco_below_order_id": "5",
        "quantity": qty,
        "entry_price": entry,
    }


class CrossArmSymbolTests(unittest.TestCase):
    def test_same_symbol_blocks_either_arm_and_other_symbols_stay_open(self) -> None:
        fade = _held(PURPOSE_LIVE_BNB_BOLL_FADE, "CFGUSDT")
        classic = PURPOSE_LIVE_BNB_BOLL_CLASSIC
        self.assertEqual(
            cross_arm_symbol_block([fade], purpose=classic, symbol="CFGUSDT"),
            "cross_arm_symbol_blocks_buy",
        )
        self.assertEqual(
            cross_arm_symbol_block([fade], purpose=classic, symbol="cfgusdt"),
            "cross_arm_symbol_blocks_buy",
        )
        self.assertIsNone(
            cross_arm_symbol_block([fade], purpose=classic, symbol="BNBUSDT")
        )
        self.assertEqual(
            cross_arm_symbol_block([fade], purpose=classic, symbol=""),
            "cross_arm_symbol_blocks_buy",
        )
        naked = _held(PURPOSE_LIVE_BNB_BOLL_FADE, "CFGUSDT", status="oco_failed")
        self.assertEqual(
            cross_arm_symbol_block([naked], purpose=classic, symbol="CFGUSDT"),
            "cross_arm_symbol_blocks_buy",
        )
        self.assertIsNone(
            cross_arm_symbol_block([naked], purpose=classic, symbol="ETHUSDT")
        )
        closed = _held(PURPOSE_LIVE_BNB_BOLL_FADE, "CFGUSDT", status="oco_filled")
        self.assertIsNone(
            cross_arm_symbol_block([closed], purpose=classic, symbol="CFGUSDT")
        )
        # Same-arm rows stay with the existing helper, including MULTI_SYMBOL.
        self.assertIsNone(
            buy_exposure_block([fade], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="BNBUSDT")
        )
        self.assertEqual(
            buy_exposure_block([fade], [], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="CFGUSDT"),
            "position_open_blocks_buy",
        )
        self.assertIsNone(cross_arm_symbol_block([fade], purpose=PURPOSE_LIVE_BNB_BOLL_FADE, symbol="CFGUSDT"))


class FundingReservationTests(unittest.TestCase):
    def test_peer_quote_is_subtracted_before_the_twenty_usdt_floor(self) -> None:
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))
        peer = _held(PURPOSE_LIVE_BNB_BOLL_FADE, "ETHUSDT", qty="6", entry="50")
        self.assertEqual(reserved_quote_usdt([peer]), Decimal("300"))
        # 320 free - 300 reserved = 20. A 30 USDT plan no longer fits.
        self.assertEqual(
            funding_after_reservation("320", ENTRY_MIN_NOTIONAL_USDT, "30", Decimal("300")),
            "funding_block",
        )
        # Available 20 still meets the floor, so a 20 USDT plan is not a funding miss.
        self.assertIsNone(
            funding_after_reservation("320", ENTRY_MIN_NOTIONAL_USDT, "20", Decimal("300"))
        )
        # Dropping under 20 USDT available is still the same refusal.
        self.assertEqual(
            funding_after_reservation("310", ENTRY_MIN_NOTIONAL_USDT, "5", Decimal("300")),
            "funding_block",
        )
        unpriced = {"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "symbol": "ETHUSDT", "status": "oco_working"}
        self.assertEqual(reserved_quote_usdt([unpriced]), Decimal("0"))
        filled = _held(PURPOSE_LIVE_BNB_BOLL_FADE, "ETHUSDT", status="oco_filled", qty="6", entry="50")
        self.assertEqual(reserved_quote_usdt([filled]), Decimal("0"))


class SharedLedgerReservationTests(unittest.TestCase):
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

    def _arm_fade(self, *, symbol: str, qty: str, entry: str, status: str = "oco_working") -> None:
        ledger = ExecutionLedger(self.root / _FADE)
        armed = ledger.arm_binance_spot_position(
            {
                "decision_log_id": 501,
                "symbol": symbol,
                "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                "status": "open",
                "quantity": qty,
                "stop_price": "1",
                "take_profit_price": "3",
                "entry_price": entry,
                "entry_order_id": "9",
                "protection": "exchange_oco",
            }
        )
        ledger.update_binance_spot_position(
            int(armed["id"]),
            {
                "status": status,
                "oco_order_list_id": "3",
                "oco_above_order_id": "4",
                "oco_below_order_id": "5",
            },
        )
        ledger.close()

    def test_sibling_file_blocks_the_same_symbol_and_reserves_quote(self) -> None:
        self._arm_fade(symbol="BNBUSDT", qty="6", entry="50", status="oco_working")
        peer = peer_live_bnb_ledger_path(self.root / _CLASSIC)
        self.assertEqual(peer, self.root / _FADE)
        rows = read_spot_buy_blocks_readonly(peer)
        self.assertEqual(len(rows), 1)
        self.assertEqual(reserved_quote_usdt(rows), Decimal("300"))
        client_calls: list[str] = []

        class _NoOrder:
            def exchange_info(self, symbol: str) -> dict[str, Any]:
                raise AssertionError(symbol)

            def new_order(self, signed_query: str, *, api_key: str) -> dict[str, Any]:
                client_calls.append("new_order")
                raise AssertionError("same-symbol buy must not be sent")

            def ticker_price(self, symbol: str) -> dict[str, Any]:
                return {"symbol": symbol, "price": "100"}

        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / _CLASSIC,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(self.root, _NoOrder()),
            ) as gw:
                refused = gw.process_one(
                    _decision_row(
                        mode="live_stub",
                        token_address="BNBUSDT",
                        pair_address="BNB/USDT",
                        extras=_classic_extras(binance_symbol="BNBUSDT"),
                    )
                )
        self.assertEqual(refused["executed"], 0, refused)
        self.assertEqual(refused["receipt"]["error"], "cross_arm_symbol_blocks_buy")
        self.assertEqual(client_calls, [])
        self.assertFalse((self.root / _CLASSIC).stat().st_size == 0)

    def test_other_symbol_buys_when_the_reserved_balance_still_covers_it(self) -> None:
        self._arm_fade(symbol="ETHUSDT", qty="1", entry="10", status="oco_working")
        client = _adapter(self.root).client
        with mock.patch.dict(os.environ, _env(self.root)):
            with ExecutionGateway(
                decision_db=self.root / "decision.db",
                ledger_path=self.root / _CLASSIC,
                paper_enabled=False,
                pm_enabled=False,
                accept_purposes=frozenset({PURPOSE_LIVE_BNB_BOLL_CLASSIC}),
                binance_spot=_adapter(self.root, client, auto_flatten_on_oco_fail=True),
            ) as gw:
                opened = gw.process_one(_decision_row(mode="live_stub"))
        self.assertEqual(opened["executed"], 1, opened)
        self.assertEqual(opened["receipt"]["held_position"]["symbol"], "BNBUSDT")

    def test_priced_peer_hold_blocks_a_plan_the_snapshot_alone_would_allow(self) -> None:
        self._arm_fade(symbol="ETHUSDT", qty="6", entry="50", status="oco_working")
        snapshot = {
            "accountType": "SPOT",
            "canTrade": True,
            "permissions": ["SPOT"],
            "restrictions": {
                "enableSpotAndMarginTrading": True,
                "enableReading": True,
                "ipRestrict": True,
            },
            "balances": [{"asset": "USDT", "free": "320.00", "locked": "0"}],
        }
        adapter = BinanceSpotAdapter(
            client=_adapter(self.root).client,
            kill_switch_path=self.root / "absent-kill",
            api_key="test-key",
            api_secret="test-secret",
            clock_ms=lambda: 1_700_000_000_000,
            account_snapshot=snapshot,
            production_book=REST_BOOK,
            rest_expected={"account_type": "SPOT", "usdt_free": "320.00"},
            auto_flatten_on_oco_fail=True,
        )
        adapter.peer_buy_blocks = read_spot_buy_blocks_readonly(self.root / _FADE)
        extras = _classic_extras(quote_order_qty="30")
        with mock.patch.dict(os.environ, _env(self.root)):
            refused = adapter.execute(
                {
                    "decision_log_id": 147901,
                    "strategy_id": "tech_boll_fade_v1",
                    "action": "buy",
                    "chain_id": "binance",
                    "token_address": "BNBUSDT",
                    "pair_address": "BNB/USDT",
                    "size_pct": 0.24,
                    "mode": "live_stub",
                    "purpose": PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                    "extras": extras,
                }
            )
        self.assertEqual(refused["error"], "funding_block", refused)
        self.assertEqual(refused["executed"], 0)
        self.assertFalse(any(call[0] == "new_order" for call in adapter.client.calls))

    def test_buy_lock_is_exclusive_across_threads(self) -> None:
        path = self.root / _FADE
        path.write_bytes(b"")
        order: list[str] = []
        gate = threading.Event()

        def hold(name: str) -> None:
            with buy_reservation_lock(path):
                order.append(f"enter-{name}")
                if name == "a":
                    gate.set()
                else:
                    gate.wait(1)
                order.append(f"exit-{name}")

        first = threading.Thread(target=hold, args=("a",))
        second = threading.Thread(target=hold, args=("b",))
        first.start()
        self.assertTrue(gate.wait(1))
        second.start()
        first.join(1)
        second.join(1)
        self.assertEqual(order, ["enter-a", "exit-a", "enter-b", "exit-b"])


if __name__ == "__main__":
    unittest.main()
