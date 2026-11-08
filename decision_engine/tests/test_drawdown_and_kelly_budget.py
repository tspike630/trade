"""Tests for Milestone 16: Institutional Pod-Level Drawdown Circuit Breaker & Rolling Sharpe / Half-Kelly Risk Budgeting."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from decision_engine.core.drawdown_and_kelly_budget import (
    evaluate_pod_drawdown_and_kelly,
    infer_closed_trade_return,
    reconstruct_closed_trade_returns,
)
from decision_engine.core.live_buy_guards import apply_live_buy_guards
from decision_engine.core.protection_halt import ProtectionBook, read_protection_book
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE
from execution.ledger import ExecutionLedger


def _closed_row(
    decision_id: int,
    *,
    symbol: str = "BNBUSDT",
    entry: str = "100.00",
    stop: str = "95.00",
    tp: str = "108.00",
    exit_reason: str = "take_profit",
    size_pct: float = 0.10,
    updated_at: str = "2026-11-08T12:00:00Z",
) -> dict[str, Any]:
    return {
        "id": decision_id,
        "decision_log_id": decision_id,
        "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
        "symbol": symbol,
        "status": "oco_filled" if exit_reason in {"take_profit", "stop_loss"} else "closed",
        "quantity": "1.0",
        "entry_price": entry,
        "stop_price": stop,
        "take_profit_price": tp,
        "exit_reason": exit_reason,
        "size_pct": size_pct,
        "entry_order_id": str(1000 + decision_id),
        "protection": "exchange_oco",
        "oco_order_list_id": str(2000 + decision_id),
        "oco_above_order_id": str(3000 + decision_id),
        "oco_below_order_id": str(4000 + decision_id),
        "updated_at": updated_at,
    }


class TestDrawdownAndKellyBudget(unittest.TestCase):
    def test_infer_closed_trade_return_tp_sl_and_time_stop(self) -> None:
        tp_row = _closed_row(1, entry="100.00", stop="95.00", tp="106.00", exit_reason="take_profit")
        sample_tp = infer_closed_trade_return(tp_row, round_trip_fee_pct=0.0020)
        assert sample_tp is not None
        # +6.0% gross - 0.2% fee = +5.8% net
        self.assertAlmostEqual(sample_tp.net_return_pct, 0.058, places=6)

        sl_row = _closed_row(2, entry="100.00", stop="96.00", tp="106.00", exit_reason="stop_loss")
        sample_sl = infer_closed_trade_return(sl_row, round_trip_fee_pct=0.0020)
        assert sample_sl is not None
        # -4.0% gross - 0.2% fee = -4.2% net
        self.assertAlmostEqual(sample_sl.net_return_pct, -0.042, places=6)

    def test_hard_max_drawdown_circuit_breaker_halts_buys(self) -> None:
        # Three large stop-loss trades causing > 5% book drawdown
        rows = [
            _closed_row(i, entry="100.00", stop="80.00", tp="110.00", exit_reason="stop_loss", size_pct=0.10)
            for i in range(1, 4)
        ]
        res = evaluate_pod_drawdown_and_kelly(
            rows,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            max_drawdown_halt_pct=0.05,
            soft_drawdown_pct=0.025,
            max_consecutive_losses=10,
        )
        self.assertFalse(res.allow_buy)
        self.assertEqual(res.halt_reason, "pod_max_drawdown_halt")
        self.assertEqual(res.risk_budget_multiplier, 0.0)
        self.assertGreaterEqual(res.current_drawdown_pct, 0.05)

    def test_soft_drawdown_linearly_throttles_position_size(self) -> None:
        # Two stop-loss trades causing ~3.75% book drawdown (between 2.5% soft and 5.0% hard)
        rows = [
            _closed_row(1, entry="100.00", stop="81.40", tp="110.00", exit_reason="stop_loss", size_pct=0.10),
            _closed_row(2, entry="100.00", stop="81.40", tp="110.00", exit_reason="stop_loss", size_pct=0.10),
        ]
        res = evaluate_pod_drawdown_and_kelly(
            rows,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            max_drawdown_halt_pct=0.05,
            soft_drawdown_pct=0.025,
            max_consecutive_losses=10,
        )
        self.assertTrue(res.allow_buy)
        self.assertIsNone(res.halt_reason)
        self.assertEqual(res.regime, "throttled_soft_drawdown")
        self.assertLess(res.drawdown_throttle_scalar, 0.75)
        self.assertGreaterEqual(res.drawdown_throttle_scalar, 0.25)

    def test_consecutive_loss_streak_cooldown_halts_and_expires(self) -> None:
        rows = [
            _closed_row(
                i,
                entry="100.00",
                stop="98.00",
                tp="105.00",
                exit_reason="stop_loss",
                size_pct=0.02,
                updated_at="2026-11-08T12:00:00Z",
            )
            for i in range(1, 5)
        ]
        # 10 minutes after the 4th loss (< 1800s cooldown) -> halted
        last_ts = infer_closed_trade_return(rows[-1]).closed_ts  # type: ignore[union-attr]
        assert last_ts is not None
        halted = evaluate_pod_drawdown_and_kelly(
            rows,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            now_ts=last_ts + 600,
            max_consecutive_losses=4,
            loss_cooldown_sec=1800,
        )
        self.assertFalse(halted.allow_buy)
        self.assertEqual(halted.halt_reason, "pod_consecutive_loss_cooldown")
        self.assertEqual(halted.consecutive_losses, 4)

        # 35 minutes after the 4th loss (> 1800s cooldown) -> cooldown expired, allowed
        resumed = evaluate_pod_drawdown_and_kelly(
            rows,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            now_ts=last_ts + 2100,
            max_consecutive_losses=4,
            loss_cooldown_sec=1800,
        )
        self.assertTrue(resumed.allow_buy)
        self.assertIsNone(resumed.halt_reason)

    def test_rolling_half_kelly_and_sharpe_upscales_strong_edge(self) -> None:
        # 5 wins (+7.8% net) and 1 small loss (-1.8% net) -> high win rate & high Sharpe
        rows = [
            _closed_row(i, entry="100.00", stop="98.40", tp="108.00", exit_reason="take_profit", size_pct=0.05)
            for i in range(1, 6)
        ] + [
            _closed_row(6, entry="100.00", stop="98.40", tp="108.00", exit_reason="stop_loss", size_pct=0.05)
        ]
        res = evaluate_pod_drawdown_and_kelly(
            rows,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            min_trades_for_kelly=5,
        )
        self.assertTrue(res.allow_buy)
        self.assertGreater(res.kelly_sharpe_scalar, 1.05)
        self.assertEqual(res.regime, "offensive_kelly_upscale")
        assert res.rolling_sharpe is not None
        self.assertGreater(res.rolling_sharpe, 0.5)

    def test_live_buy_guards_integration_with_sqlite_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "execution_ledger_live_bnb_boll_fade.db"
            with ExecutionLedger(db_path) as ledger:
                for i in range(1, 4):
                    ledger.arm_binance_spot_position(
                        {
                            "decision_log_id": i,
                            "symbol": f"TOK{i}USDT",
                            "purpose": PURPOSE_LIVE_BNB_BOLL_FADE,
                            "status": "oco_working",
                            "quantity": "10",
                            "stop_price": "80.0",
                            "take_profit_price": "110.0",
                            "entry_price": "100.0",
                            "entry_order_id": str(100 + i),
                            "protection": "exchange_oco",
                        }
                    )
                    ledger.update_binance_spot_position(
                        i,
                        {
                            "status": "oco_filled",
                            "exit_reason": "stop_loss",
                            "oco_order_list_id": str(200 + i),
                            "oco_above_order_id": str(300 + i),
                            "oco_below_order_id": str(400 + i),
                        },
                    )
            book = read_protection_book(db_path)
            self.assertIsNone(book.error)
            self.assertEqual(len(book.positions), 3)
            samples = reconstruct_closed_trade_returns(book.positions, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(len(samples), 3)

            signals = [
                {
                    "action": "buy",
                    "binance_symbol": "SOLUSDT",
                    "order_type": "MARKET",
                    "price_usd": 150.0,
                    "stop_price": 144.0,
                    "take_profit_price": 159.0,
                    "size_pct": 0.08,
                }
            ]
            # With soft drawdown (~3.0% from three 20% stops at 5% weight), signal is throttled
            kept, report = apply_live_buy_guards(
                signals,
                purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                book=book,
                pod_risk_gate=True,
            )
            self.assertEqual(len(kept), 1)
            self.assertLess(kept[0]["size_pct"], 0.08)
            self.assertEqual(kept[0]["pod_risk_regime"], "throttled_soft_drawdown")
            self.assertIn("pod_risk_budget", report)

            # With tighter 2.5% hard drawdown cap, the same book halts new BUYs
            with patch.dict(os.environ, {"LIVE_BNB_MAX_DRAWDOWN_HALT_PCT": "0.025"}, clear=False):
                kept_halt, report_halt = apply_live_buy_guards(
                    signals,
                    purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
                    book=book,
                    pod_risk_gate=True,
                )
            self.assertEqual(len(kept_halt), 0)
            self.assertEqual(report_halt.get("drawdown_halt"), "pod_max_drawdown_halt")
            self.assertEqual(report_halt.get("drawdown_dropped"), 1)


if __name__ == "__main__":
    unittest.main()
