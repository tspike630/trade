"""Stage-2 gaps: reservation, live equity, source clocks, and S2 revalidation."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.regime_strategy_router import apply_five_state_regime_router
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from decision_engine.core.regime_v2_policy import (
    CONTRACT_ENV,
    check_row_and_tick_freshness,
    resolve_regime_v2_equity,
    verify_s2_router_admission,
)
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.core.regime_v2_reservation import (
    RELEASE_INTERFACE_BLOCK,
    active_reservations,
    release_reservation,
    try_reserve,
)
NOW_MS = int(datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _row(**extra: object) -> dict[str, object]:
    due = due_cftc_asof(datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc)).isoformat()
    row: dict[str, object] = {
        "binance_symbol": "SOLUSDT",
        "kline_low": 100.0,
        "boll_mid": 110.0,
        "boll_lower": 100.0,
        "price_usd": 104.2,
        "trade_size_ratio": 1.40,
        "taker_buy_ratio": 0.55,
        "obi_top5": 0.12,
        "funding_z": -2.05,
        "funding_rate": -0.0004,
        "whale_retail_divergence": 1.40,
        "global_account_ls_ratio": 1.10,
        "gamma_regime": "positive_gamma",
        "dvol_zscore": 0.2,
        "liq_cascade_regime": "normal",
        "max_venue_dislocation_bps": 2.0,
        "cot_regime": "neutral",
        "nvda_1h_ret_pct": 0.2,
        "smh_1h_ret_pct": 0.1,
        "xs_breadth_1h": 0.50,
        "tech_updated_at": "2026-11-11T14:59:00Z",
        "quote_turnover_24h_asof": "2026-11-11T14:59:00Z",
        "entry_kline_5m_close_ms": NOW_MS - 60_000,
        "volume_z_source_close_ms": NOW_MS - 60_000,
        "obi_top5_source_ts_ms": NOW_MS - 60_000,
        "_perp_counterparty_ts_ms": NOW_MS - 60_000,
        "_cross_exchange_ts_ms": NOW_MS - 60_000,
        "_deribit_gamma_ts_ms": NOW_MS - 60_000,
        "_unlock_schedule_verified": True,
        "news_fetched_at_ms": NOW_MS - 60_000,
        "_lseg_cross_asset_ts_ms": NOW_MS - 60_000,
        "_cot_max_date": due,
        "_freshness_now_ms": NOW_MS,
    }
    row.update(extra)
    return row


def _reserve_one(path: str, symbol: str) -> str:
    result = try_reserve(
        path,
        {"binance_symbol": symbol, "purpose": "live_bnb_boll_fade", "entry_reason": "x", "size_pct": 0.24},
        now_ms=NOW_MS,
        open_symbols=[],
        open_count=0,
        cluster_counts={},
        open_exposure_pct=0.0,
        max_concurrent=1,
        state4_max_concurrent=1,
        max_per_cluster=1,
        max_total_pct=0.72,
        state4_max_total_pct=0.30,
        size_pct=0.24,
        cluster_key="",
        is_state4=False,
    )
    return "ok" if result.ok else (result.reason or "reject")


class Stage2GapTests(unittest.TestCase):
    def test_pasted_s2_reason_does_not_skip_the_zone(self) -> None:
        row = _row(funding_z=0.0, funding_rate=0.0001, liq_cascade_regime="normal", cot_regime="neutral")
        ok, reason = verify_s2_router_admission(
            {"entry_reason": "state2_squeeze_limit_maker", "order_type": "LIMIT_MAKER", "limit_price": 103.0},
            row,
            environ={CONTRACT_ENV: "1"},
            now_ms=NOW_MS,
        )
        self.assertFalse(ok)
        self.assertNotEqual(reason, None)

    def test_real_s2_row_still_admits(self) -> None:
        ok, reason = verify_s2_router_admission(
            None,
            _row(),
            environ={CONTRACT_ENV: "1"},
            now_ms=NOW_MS,
        )
        self.assertTrue(ok, reason)

    def test_updated_at_is_not_an_indicator_clock(self) -> None:
        row = _row()
        row.pop("tech_updated_at")
        row["updated_at"] = "2026-11-11T14:59:00Z"
        reasons = check_row_and_tick_freshness(
            row, now_ms=NOW_MS, require_all_clocks=False, environ={CONTRACT_ENV: "1"}
        )
        self.assertIn("binance_indicators_clock_absent", reasons)

    def test_seconds_in_a_millisecond_field_fail_closed(self) -> None:
        row = _row(entry_kline_5m_close_ms=int(NOW_MS / 1000))
        reasons = check_row_and_tick_freshness(
            row, now_ms=NOW_MS, environ={CONTRACT_ENV: "1"}
        )
        self.assertIn("entry_5m_kline_clock_unit_mismatch", reasons)

    def test_contract_without_snapshot_does_not_use_book_or_dry_equity(self) -> None:
        env = {
            CONTRACT_ENV: "1",
            "BINANCE_SPOT_BOOK_USDT": "10000",
            "REGIME_V2_DRY_RUN_EQUITY_USDT": "10000",
        }
        view = resolve_regime_v2_equity(now_ms=NOW_MS, environ=env)
        self.assertFalse(view.ok)
        self.assertEqual(view.error, "account_snapshot_stale_or_absent")
        self.assertNotEqual(view.source, "legacy_book_env")
        self.assertIsNone(view.fetched_at_ms)

    def test_explicit_dry_run_equity_is_not_a_live_order(self) -> None:
        env = {
            CONTRACT_ENV: "1",
            "REGIME_V2_OUTPUT_MODE": "dry_run",
            "REGIME_V2_DRY_RUN_EQUITY_USDT": "10000",
            "REGIME_V2_RESERVATION_DB": "",
        }
        view = resolve_regime_v2_equity(now_ms=NOW_MS, environ=env)
        self.assertTrue(view.ok, view.error)
        self.assertEqual(view.source, "dry_run_override")
        self.assertIsNone(view.fetched_at_ms)
        self.assertNotEqual(view.fetched_at_ms, NOW_MS)

    def test_two_processes_cannot_both_take_the_only_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "reservations.sqlite")
            ctx = multiprocessing.get_context("spawn")
            first = ctx.Process(target=_reserve_one, args=(path, "SOLUSDT"))
            second = ctx.Process(target=_reserve_one, args=(path, "BTCUSDT"))
            first.start()
            second.start()
            first.join(10)
            second.join(10)
            rows = active_reservations(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)

    def test_restart_keeps_the_reservation_and_release_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "reservations.sqlite")
            self.assertEqual(_reserve_one(path, "SOLUSDT"), "ok")
            again = _reserve_one(path, "ETHUSDT")
            self.assertEqual(again, "max_concurrent_positions_reached")
            key = active_reservations(path)[0]["signal_key"]
            self.assertTrue(release_reservation(path, key, now_ms=NOW_MS))
            self.assertEqual(active_reservations(path), [])
            self.assertIn("gateway", RELEASE_INTERFACE_BLOCK)

    def test_router_dry_run_tick_reserves_once_and_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "reservations.sqlite")
            env = {
                CONTRACT_ENV: "1",
                "REGIME_V2_OUTPUT_MODE": "dry_run",
                "REGIME_V2_DRY_RUN_EQUITY_USDT": "10000",
                "REGIME_V2_RESERVATION_DB": db,
            }
            old = os.environ.copy()
            os.environ.clear()
            os.environ.update(env)
            try:
                hot = dict(_row())
                zone = evaluate_entry_zone(dict(hot))
                self.assertTrue(zone.emit, zone.reject_reason)
                signal = {
                    "action": "buy",
                    "binance_symbol": "SOLUSDT",
                    "size_pct": 0.24,
                    "order_type": "LIMIT_MAKER",
                    "limit_price": zone.limit_price,
                    "stop_price": zone.stop_price,
                    "take_profit_price": zone.take_profit_price,
                    "entry_reason": zone.entry_reason,
                    "price_usd": 104.2,
                    "purpose": "live_bnb_boll_fade",
                }
                first, _counts = apply_five_state_regime_router([signal], [hot], now_ms=NOW_MS)
                second, _counts2 = apply_five_state_regime_router([dict(signal)], [hot], now_ms=NOW_MS)
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertFalse(first[0]["live_order_permitted"])
            self.assertEqual(first[0]["mode"], "dry_run")
            self.assertAlmostEqual(first[0]["take_profit_price"], 110.0, places=6)
            self.assertEqual(len(active_reservations(db)), 1)
            report = {
                "first_id": first[0].get("reservation_key"),
                "idempotent": second[0].get("reservation_idempotent"),
                "live_order_permitted": first[0]["live_order_permitted"],
            }
            Path(tmp, "router.json").write_text(json.dumps(report), encoding="utf-8")
            self.assertTrue(report["idempotent"])

    def test_clockless_indicator_db_engine_tick_emits_no_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "indicators.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE token_indicators ("
                "chain_id TEXT, token_address TEXT, tier TEXT, jev_strength REAL, "
                "updated_at TEXT, tech_updated_at TEXT, price_usd REAL, binance_symbol TEXT)"
            )
            conn.execute(
                "INSERT INTO token_indicators VALUES "
                "('56', 'SOLUSDT', 'hot', 1.0, '2026-11-19T09:00:00Z', NULL, 100.0, 'SOLUSDT')"
            )
            conn.commit()
            conn.close()
            bus_db = Path(tmp) / "signals.sqlite"
            old = os.environ.copy()
            os.environ[CONTRACT_ENV] = "1"
            os.environ["REGIME_V2_OUTPUT_MODE"] = "dry_run"
            os.environ["REGIME_V2_DRY_RUN_EQUITY_USDT"] = "10000"
            os.environ["REGIME_V2_RESERVATION_DB"] = str(Path(tmp) / "reservations.sqlite")
            adapter = IndicatorDataAdapter(db, use_ro_uri=False)
            bus = SignalBus(bus_db, purpose=PURPOSE_LIVE_BNB_BOLL_FADE)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [TechBollFadeStrategy(position_pct=0.24)],
                    protection_book=ProtectionBook.unwired(),
                    macro_db=Path(tmp) / "missing_macro.sqlite",
                )
                payload = engine.tick()
            finally:
                bus.close()
                adapter.close()
                os.environ.clear()
                os.environ.update(old)
            self.assertEqual(payload["emitted"], 0)
            self.assertEqual(payload["candidates"], 0)
            self.assertEqual(active_reservations(str(Path(tmp) / "reservations.sqlite")), [])
