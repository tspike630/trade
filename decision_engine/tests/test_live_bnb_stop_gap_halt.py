"""Stop-gap and unprotected-book halt for live Binance spot buys.

    python -m unittest decision_engine.tests.test_live_bnb_stop_gap_halt
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.protection_halt import (
    ProtectionBook,
    purpose_halt_reason,
    read_protection_book,
    resolve_ledger_path,
)
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.core.stop_gap import (
    MIN_ENTRY_STOP_GAP,
    buy_stop_gap_reason,
    stop_distance_reason,
)
from decision_engine.strategies.base_strategy import BaseStrategy
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone, reward_risk
from decision_engine.strategies.tech_boll_fade import CLASSIC_SL_PCT, TechBollFadeStrategy
from decision_engine.tests.test_live_bnb_entry_zone import _row

# Decision 148592. Signal 48.08, stop 48.02605, fill avg ~48.02.
_DASH_PRICE = 48.08
_DASH_STOP = 48.02605
_DASH_ALPHA = 0.08
_DASH_SPAN = (_DASH_PRICE - _DASH_STOP) / (_DASH_ALPHA + 0.1)
_DASH_LOW = _DASH_PRICE - _DASH_ALPHA * _DASH_SPAN
_FADE = PURPOSE_LIVE_BNB_BOLL_FADE
_CLASSIC = PURPOSE_LIVE_BNB_BOLL_CLASSIC


def _span_for_gap(low: float, alpha: float, gap: float) -> float:
    """Span whose market entry sits ``gap`` under the signal. Stop is L-0.1*S."""
    return (gap * low) / ((alpha + 0.1) - gap * alpha)


def _market(symbol: str, price: float, stop: float, **extra: object) -> dict:
    row = {
        "action": "buy",
        "strategy_id": "tech_boll_fade_v1",
        "chain_id": "binance",
        "token_address": symbol,
        "binance_symbol": symbol,
        "size_pct": 0.05,
        "reason": "guard",
        "liquidity_usd": 90_000.0,
        "order_type": "MARKET",
        "price_usd": price,
        "stop_price": stop,
        "take_profit_price": price * 1.10,
        "volume_strength": 1.0,
    }
    row.update(extra)
    return row


def _held(
    status: str,
    symbol: str,
    *,
    purpose: str = _FADE,
    protection: str = "exchange_oco",
) -> dict:
    verified = status in {"oco_working", "open", "exit_submitted"}
    return {
        "purpose": purpose,
        "symbol": symbol,
        "status": status,
        "entry_order_id": "100",
        "protection": protection,
        "oco_order_list_id": "1" if verified else None,
        "oco_above_order_id": "2" if verified else None,
        "oco_below_order_id": "3" if verified else None,
    }


def _ledger(path: Path, positions: list[dict], canonical: list[dict] | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE binance_spot_positions (
            purpose TEXT, symbol TEXT, status TEXT, entry_order_id TEXT,
            protection TEXT, oco_order_list_id TEXT, oco_above_order_id TEXT,
            oco_below_order_id TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE canonical_positions (
            purpose TEXT, symbol TEXT, status TEXT, entry_order_id TEXT
        )
        """
    )
    for row in positions:
        conn.execute(
            """
            INSERT INTO binance_spot_positions (
                purpose, symbol, status, entry_order_id, protection,
                oco_order_list_id, oco_above_order_id, oco_below_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("purpose"),
                row.get("symbol"),
                row.get("status"),
                row.get("entry_order_id"),
                row.get("protection"),
                row.get("oco_order_list_id"),
                row.get("oco_above_order_id"),
                row.get("oco_below_order_id"),
            ),
        )
    for row in canonical or []:
        conn.execute(
            """
            INSERT INTO canonical_positions (purpose, symbol, status, entry_order_id)
            VALUES (?, ?, ?, ?)
            """,
            (row.get("purpose"), row.get("symbol"), row.get("status"), row.get("entry_order_id")),
        )
    conn.commit()
    conn.close()


class _Fixed(BaseStrategy):
    strategy_id = "tech_boll_fade_v1"

    def __init__(self, signals: list[dict]) -> None:
        super().__init__()
        self._signals = signals

    def on_tick(self, data: dict) -> list[dict]:
        return [dict(item) for item in self._signals]


class StopGapTests(unittest.TestCase):
    def test_dash_geometry_passes_odds_and_is_not_emitted(self) -> None:
        """11.2 bps of room cleared the ratio and still cannot cover the fill."""
        price = _DASH_LOW + _DASH_ALPHA * _DASH_SPAN
        stop = _DASH_LOW - 0.1 * _DASH_SPAN
        self.assertAlmostEqual(price, _DASH_PRICE, places=6)
        self.assertAlmostEqual(stop, _DASH_STOP, places=6)
        odds = reward_risk(_DASH_LOW + _DASH_SPAN, price, stop)
        self.assertIsNotNone(odds)
        assert odds is not None
        self.assertGreaterEqual(odds, 1.5)
        self.assertEqual(stop_distance_reason(price, stop), "stop_too_close_to_entry")

        decision = evaluate_entry_zone(
            _row(_DASH_ALPHA, 1.3, low=_DASH_LOW, span=_DASH_SPAN)
        )
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "stop_too_close_to_entry")
        self.assertAlmostEqual(decision.stop_price or 0.0, _DASH_STOP, places=5)

        strategy = TechBollFadeStrategy()
        signals = strategy.on_tick(
            {
                "hot_tokens": [_row(_DASH_ALPHA, 1.3, low=_DASH_LOW, span=_DASH_SPAN)],
                "purpose": _FADE,
            }
        )
        self.assertEqual(signals, [])
        self.assertEqual(strategy.rejected.get("stop_too_close_to_entry"), 1)

    def test_exact_margin_emits_and_a_tighter_band_does_not(self) -> None:
        at_floor = _span_for_gap(100.0, 0.08, MIN_ENTRY_STOP_GAP)
        decision = evaluate_entry_zone(_row(0.08, 1.3, low=100.0, span=at_floor))
        self.assertTrue(decision.emit, decision.reject_reason)
        self.assertEqual(decision.entry_reason, "large_trade_market")

        tighter = _span_for_gap(100.0, 0.08, MIN_ENTRY_STOP_GAP - 1e-4)
        refused = evaluate_entry_zone(_row(0.08, 1.3, low=100.0, span=tighter))
        self.assertFalse(refused.emit)
        self.assertEqual(refused.reject_reason, "stop_too_close_to_entry")

    def test_limit_uses_the_limit_not_the_signal(self) -> None:
        low = 48.0
        span = 0.2
        limit = low + 0.20 * span
        stop = low - 0.1 * span
        self.assertEqual(
            stop_distance_reason(limit, stop),
            "stop_too_close_to_entry",
        )
        # The signal itself clears 50 bps. The resting limit does not, so the
        # limit path is the one that has to refuse.
        far_price = low + 1.2 * span
        self.assertIsNone(stop_distance_reason(far_price, stop))
        decision = evaluate_entry_zone(
            _row(1.2, 1.0, low=low, span=span)
        )
        self.assertFalse(decision.emit)
        self.assertEqual(decision.reject_reason, "stop_too_close_to_entry")
        self.assertEqual(decision.tier, "neutral")

        wide = evaluate_entry_zone(_row(0.28, 0.85))
        self.assertTrue(wide.emit, wide.reject_reason)
        self.assertEqual(wide.order_type, "LIMIT")
        self.assertIsNone(buy_stop_gap_reason({
            "action": "buy",
            "order_type": "LIMIT",
            "limit_price": wide.limit_price,
            "price_usd": wide.price,
            "stop_price": wide.stop_price,
        }))

    def test_classic_shell_and_entry_zone_fixture_still_clear_the_gap(self) -> None:
        self.assertEqual(CLASSIC_SL_PCT, 5.0)
        self.assertEqual(MIN_ENTRY_STOP_GAP, 0.005)
        classic = TechBollFadeStrategy().on_tick(
            {
                "hot_tokens": [{
                    "binance_symbol": "ONLYCEXUSDT",
                    "chain_id": "binance",
                    "token_address": "ONLYCEXUSDT",
                    "change_5m": -4.0,
                    "volume_strength": 0.9,
                    "boll_lower": 1.0,
                    "price_usd": 0.8,
                    "liquidity_usd": 90_000.0,
                }],
                "purpose": _CLASSIC,
            }
        )
        self.assertEqual(len(classic), 1)
        self.assertAlmostEqual(classic[0]["stop_price"], 0.8 * 0.95)
        self.assertAlmostEqual(classic[0]["take_profit_price"], 0.8 * 1.10)
        self.assertIsNone(buy_stop_gap_reason(classic[0]))

        zone = evaluate_entry_zone(_row(0.08, 1.3))
        self.assertTrue(zone.emit, zone.reject_reason)
        self.assertEqual(zone.entry_reason, "large_trade_market")
        paper = TechBollFadeStrategy().on_tick(
            {
                "hot_tokens": [{
                    "chain_id": "solana",
                    "token_address": "MintA",
                    "change_5m": -3.0,
                    "volume_strength": 0.9,
                    "boll_lower": 1.0,
                    "price_usd": 0.9,
                    "liquidity_usd": 90_000.0,
                }],
                "purpose": PURPOSE_RACE_TECH_BOLL_FADE,
            }
        )
        self.assertEqual(len(paper), 1)
        self.assertNotIn("stop_price", paper[0])
        self.assertIsNone(buy_stop_gap_reason(paper[0]))

    def test_pre_accept_drops_a_buy_the_strategy_already_built(self) -> None:
        tight = _market("DASHUSDT", _DASH_PRICE, _DASH_STOP)
        wide = _market("BNBUSDT", 100.0, 95.0)
        sell = {"action": "sell", "token_address": "BNBUSDT", "size_pct": 1.0}
        kept, report = apply_live_buy_guards(
            [tight, wide, sell],
            purpose=_FADE,
            book=ProtectionBook.unwired(),
        )
        self.assertEqual([item["token_address"] for item in kept], ["BNBUSDT", "BNBUSDT"])
        self.assertEqual(kept[1]["action"], "sell")
        self.assertEqual(report["stop_gap_rejected"], 1)
        self.assertIsNone(report["protection_halt"])
        self.assertEqual(buy_stop_gap_reason(tight), "stop_too_close_to_entry")
        self.assertEqual(
            buy_stop_gap_reason({**tight, "stop_price": "not-a-price"}),
            "stop_gap_invalid",
        )


class ProtectionHaltTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = patch.dict(
            os.environ,
            {"BINANCE_SPOT_ALLOW_MULTI_SYMBOL": "1"},
            clear=False,
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_oco_failed_blocks_the_purpose_and_a_clear_book_does_not(self) -> None:
        failed = ProtectionBook.ready([_held("oco_failed", "DASHUSDT")], [])
        buys = [
            _market("BNBUSDT", 100.0, 95.0),
            _market("CFGUSDT", 1.0, 0.95),
        ]
        kept, report = apply_live_buy_guards(buys, purpose=_FADE, book=failed)
        self.assertEqual(kept, [])
        self.assertEqual(report["protection_halt"], "oco_failed_blocks_buy")
        self.assertEqual(purpose_halt_reason(_FADE, failed), "oco_failed_blocks_buy")

        clear = ProtectionBook.ready([], [])
        kept_clear, clear_report = apply_live_buy_guards(buys, purpose=_FADE, book=clear)
        self.assertEqual(len(kept_clear), 2)
        self.assertIsNone(clear_report["protection_halt"])

        closed = ProtectionBook.ready([_held("closed", "DASHUSDT")], [])
        self.assertIsNone(purpose_halt_reason(_FADE, closed))

    def test_other_symbol_stays_open_and_the_same_symbol_does_not_pyramid(self) -> None:
        book = ProtectionBook.ready([_held("oco_working", "CFGUSDT")], [])
        other, other_report = apply_live_buy_guards(
            [_market("BNBUSDT", 100.0, 95.0)],
            purpose=_FADE,
            book=book,
        )
        self.assertEqual(len(other), 1)
        self.assertIsNone(other_report["protection_halt"])
        same, same_report = apply_live_buy_guards(
            [_market("CFGUSDT", 1.0, 0.95), _market("BNBUSDT", 100.0, 95.0)],
            purpose=_FADE,
            book=book,
        )
        self.assertEqual([item["binance_symbol"] for item in same], ["BNBUSDT"])
        self.assertIsNone(same_report["protection_halt"])
        self.assertEqual(same_report["protection_dropped"], 1)

    def test_fade_failure_does_not_halt_classic_or_paper(self) -> None:
        book = ProtectionBook.ready([_held("oco_failed", "DASHUSDT", purpose=_FADE)], [])
        classic, classic_report = apply_live_buy_guards(
            [_market("BNBUSDT", 100.0, 95.0)],
            purpose=_CLASSIC,
            book=book,
        )
        self.assertEqual(len(classic), 1)
        self.assertIsNone(classic_report["protection_halt"])
        paper, paper_report = apply_live_buy_guards(
            [_market("BNBUSDT", 100.0, 95.0)],
            purpose=PURPOSE_RACE_TECH_BOLL_FADE,
            book=book,
        )
        self.assertEqual(len(paper), 1)
        self.assertIsNone(paper_report["protection_halt"])

        untagged = ProtectionBook.ready(
            [{**_held("oco_failed", "DASHUSDT"), "purpose": ""}],
            [],
        )
        self.assertEqual(purpose_halt_reason(_FADE, untagged), "oco_failed_blocks_buy")
        self.assertIsNone(purpose_halt_reason(_CLASSIC, untagged))

    def test_unknown_book_fails_closed_and_unwired_does_not(self) -> None:
        unknown = ProtectionBook.failed("ledger_unreadable:OperationalError")
        kept, report = apply_live_buy_guards(
            [_market("BNBUSDT", 100.0, 95.0)],
            purpose=_FADE,
            book=unknown,
        )
        self.assertEqual(kept, [])
        self.assertEqual(report["protection_halt"], "protection_status_unknown")
        kept_open, open_report = apply_live_buy_guards(
            [_market("BNBUSDT", 100.0, 95.0)],
            purpose=_FADE,
            book=ProtectionBook.unwired(),
        )
        self.assertEqual(len(kept_open), 1)
        self.assertIsNone(open_report["protection_halt"])

    def test_flag_off_still_blocks_a_second_symbol(self) -> None:
        book = ProtectionBook.ready([_held("oco_working", "CFGUSDT")], [])
        with patch.dict(os.environ, {"BINANCE_SPOT_ALLOW_MULTI_SYMBOL": "0"}):
            self.assertEqual(
                purpose_halt_reason(_FADE, book),
                "position_open_blocks_buy",
            )

    def test_ledger_read_is_the_execution_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution_ledger.db"
            _ledger(path, [_held("oco_failed", "DASHUSDT")])
            book = read_protection_book(path)
            self.assertTrue(book.configured)
            self.assertIsNone(book.error)
            self.assertEqual(book.positions[0]["status"], "oco_failed")
            self.assertEqual(purpose_halt_reason(_FADE, book), "oco_failed_blocks_buy")

            closed = Path(tmp) / "closed.db"
            _ledger(closed, [_held("oco_filled", "DASHUSDT")])
            self.assertIsNone(purpose_halt_reason(_FADE, read_protection_book(closed)))

            missing = read_protection_book(Path(tmp) / "absent.db")
            self.assertEqual(missing.error, "ledger_unreadable:OperationalError")
            self.assertTrue(missing.configured)

            naked = Path(tmp) / "canonical.db"
            _ledger(
                naked,
                [],
                [{"purpose": _FADE, "symbol": "DASHUSDT", "status": "held_naked", "entry_order_id": "9"}],
            )
            self.assertEqual(
                purpose_halt_reason(_FADE, read_protection_book(naked)),
                "naked_holds_new_buys",
            )

            short = Path(tmp) / "short.db"
            conn = sqlite3.connect(short)
            conn.execute("CREATE TABLE binance_spot_positions (purpose TEXT, status TEXT)")
            conn.execute(
                "CREATE TABLE canonical_positions (purpose TEXT, symbol TEXT, status TEXT, entry_order_id TEXT)"
            )
            conn.commit()
            conn.close()
            incomplete = read_protection_book(short)
            self.assertEqual(
                incomplete.error,
                "protection_column_missing:binance_spot_positions.symbol",
            )

        env = {
            "DECISION_EXECUTION_LEDGER": "/tmp/decision.db",
            "EXECUTION_LEDGER_PATH": "/tmp/execution.db",
            "EXECUTION_LEDGER_DB": "/tmp/fallback.db",
        }
        self.assertEqual(resolve_ledger_path(env), Path("/tmp/decision.db"))
        self.assertIsNone(resolve_ledger_path({}))


class EngineHaltTests(unittest.TestCase):
    def _tick(self, purpose: str, book: ProtectionBook | None, signal: dict, ledger: Path | None = None) -> tuple[dict, list]:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "decision.db"
            sqlite3.connect(db).close()
            now_ms = int(time.time() * 1000)
            hot = [{
                "venue": "binance",
                "chain_id": "binance",
                "token_address": signal["token_address"],
                "binance_symbol": signal["binance_symbol"],
                "ccxt_symbol": "BNB/USDT",
                "liquidity_usd": 90_000.0,
                "universe_member": True,
                "quote_turnover_24h_usdt": 3_000_000.0,
                "quote_turnover_24h_asof": now_ms,
                "quote_turnover_24h_source": "test",
                "volume_strength": 1.0,
            }]
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=purpose)
            engine = DecisionEngine(
                adapter,
                bus,
                [_Fixed([signal])],
                risk=RiskManager(cooldown_sec=0, min_liq_usd=50_000),
                protection_book=book,
                protection_ledger=ledger,
            )
            try:
                with patch.object(
                    DecisionEngine,
                    "_load_live_bnb_hot",
                    lambda self, dex: (hot, "binance_indicators"),
                ), patch(
                    "decision_engine.core.engine.filter_live_bnb_hot",
                    lambda conn, db_path, rows, now_ms=None: (list(rows), {"scanned": len(rows), "passed": len(rows)}),
                ):
                    payload = engine.tick()
                written_conn = sqlite3.connect(db)
                written_conn.row_factory = sqlite3.Row
                try:
                    written = written_conn.execute(
                        "SELECT token_address, action FROM decision_log"
                    ).fetchall()
                    rows = [dict(row) for row in written]
                finally:
                    written_conn.close()
            finally:
                bus.close()
                adapter.close()
            return payload, rows

    def test_happy_path_publishes_and_halt_and_tight_stop_do_not(self) -> None:
        wide = _market("BNBUSDT", 100.0, 95.0)
        payload, rows = self._tick(_CLASSIC, ProtectionBook.ready([], []), wide)
        self.assertEqual(payload["emitted"], 1)
        self.assertNotIn("protection_halt", payload)
        self.assertNotIn("stop_gap_rejected", payload)
        self.assertEqual(rows, [{"token_address": "BNBUSDT", "action": "buy"}])

        halted, halted_rows = self._tick(
            _FADE,
            ProtectionBook.ready([_held("oco_failed", "DASHUSDT")], []),
            wide,
        )
        self.assertEqual(halted["emitted"], 0)
        self.assertEqual(halted["protection_halt"], "oco_failed_blocks_buy")
        self.assertEqual(halted_rows, [])

        tight, tight_rows = self._tick(
            _CLASSIC,
            ProtectionBook.ready([], []),
            _market("BNBUSDT", _DASH_PRICE, _DASH_STOP),
        )
        self.assertEqual(tight["emitted"], 0)
        self.assertEqual(tight["stop_gap_rejected"], 1)
        self.assertEqual(tight_rows, [])

    def test_engine_reads_the_ledger_and_fails_closed_when_it_is_missing(self) -> None:
        wide = _market("BNBUSDT", 100.0, 95.0)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution_ledger.db"
            _ledger(ledger, [_held("oco_unknown", "DASHUSDT", purpose=_CLASSIC)])
            payload, rows = self._tick(_CLASSIC, None, wide, ledger=ledger)
        self.assertEqual(payload["protection_halt"], "oco_failed_blocks_buy")
        self.assertEqual(rows, [])

        missing, missing_rows = self._tick(
            _FADE,
            None,
            wide,
            ledger=Path("/tmp/does-not-exist-decision-ledger-14b1.db"),
        )
        self.assertEqual(missing["protection_halt"], "protection_status_unknown")
        self.assertEqual(missing_rows, [])


if __name__ == "__main__":
    unittest.main()
