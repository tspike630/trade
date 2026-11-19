"""Comprehensive verification suite for Regime v2 Phase-2 ruled strategy & freshness gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.macro_circuit_breaker import evaluate_macro_gate
from decision_engine.core.regime_strategy_router import (
    CONTRACT_ENV,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    apply_five_state_regime_router,
)
from decision_engine.core.regime_v2_acceptance import (
    build_report,
    load_emitted_signals,
    parse_env_file,
    profile_blocks,
    profile_path,
)
from decision_engine.core.regime_v2_reservation import active_reservations, release_reservation
from decision_engine.core.regime_v2_policy import (
    LiveEquityView,
    check_row_and_tick_freshness,
    enforce_regime_v2_sizing_and_caps,
    resolve_regime_v2_equity,
)
from decision_engine.core.signal_bus import PURPOSE_LIVE_BNB_BOLL_FADE, SignalBus
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from decision_engine.strategies.tech_boll_fade import TechBollFadeStrategy
from execution import binance_account_snapshot as snap_mod


# Wednesday 2026-11-11 15:00:00 UTC (US & CME active trading session)
NOW_MS = int(datetime(2026, 11, 11, 15, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)


def _dry_contract(db: Path) -> dict[str, str]:
    return {
        CONTRACT_ENV: "1",
        "REGIME_V2_OUTPUT_MODE": "dry_run",
        "REGIME_V2_DRY_RUN_EQUITY_USDT": "10000",
        "REGIME_V2_RESERVATION_DB": str(db),
    }


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _complete_market_row(**overrides: object) -> dict[str, object]:
    due_iso = due_cftc_asof(datetime.fromtimestamp(NOW_MS / 1000.0, tz=timezone.utc)).isoformat()
    row: dict[str, object] = {
        "binance_symbol": "SOLUSDT",
        "symbol": "SOL",
        "token_address": "SOLUSDT",
        "chain_id": "binance",
        "kline_low": 100.0,
        "boll_lower": 100.0,
        "boll_mid": 110.0,
        "boll_upper": 120.0,
        "price_usd": 102.5,
        "trade_size_ratio": 1.35,
        "taker_buy_ratio": 0.54,
        "obi_top5": 0.08,
        "obi_top5_source_ts_ms": NOW_MS - 30_000,
        "funding_rate": 0.00005,
        "funding_z": 0.10,
        "whale_retail_divergence": 1.10,
        "global_account_ls_ratio": 1.20,
        "top_position_ls_ratio": 1.32,
        "gamma_regime": "positive_gamma_pinned",
        "dvol_zscore": 0.25,
        "liq_cascade_regime": "normal",
        "max_venue_dislocation_bps": 2.0,
        "cot_regime": "cot_neutral",
        "nvda_1h_ret_pct": 0.30,
        "smh_1h_ret_pct": 0.25,
        "xs_breadth_1h": 0.45,
        "xs_rank_pct_1h": 0.80,
        "volume_z": 0.90,
        "volume_z_source_close_ms": NOW_MS - 600_000,
        "in_unlock_blackout": 0,
        "xs_systemic_crash": False,
        "liquidity_usd": 500_000.0,
        "pool_age_days": 30.0,
        "jev_strength": 0.50,
        "volatility_24h": 1.2,
        "change_5m": -2.5,
        "change_1h": 0.5,
        "rsi_14": 42.0,
        "volume_strength": 1.2,
        "tech_updated_at": _iso_utc(NOW_MS - 60_000),
        "_freshness_now_ms": NOW_MS,
        "quote_turnover_24h_usof": _iso_utc(NOW_MS - 60_000),
        "quote_turnover_24h_asof": _iso_utc(NOW_MS - 60_000),
        "quote_turnover_24h_usdt": 25_000_000.0,
        "quote_turnover_24h_source": "universe_symbols",
        "universe_member": True,
        "entry_kline_5m_close_ms": NOW_MS - 120_000,
        "_perp_counterparty_ts_ms": NOW_MS - 60_000,
        "_cross_exchange_ts_ms": NOW_MS - 60_000,
        "_deribit_gamma_ts_ms": NOW_MS - 120_000,
        "_unlock_schedule_verified": True,
        "news_fetched_at_ms": NOW_MS - 300_000,
        "_lseg_cross_asset_ts_ms": NOW_MS - 300_000,
        "_cot_max_date": due_iso,
    }
    row.update(overrides)
    return row


class State2SqueezeEntryTests(unittest.TestCase):
    def test_s2_squeeze_admits_verified_maker_and_rejects_weak_flow_or_chasing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, _dry_contract(Path(tmp) / "reservations.sqlite"), clear=True
        ):
            # 1) Valid S2 large squeeze pullback at alpha = 0.42 (rejected under calm S1, admitted under S2!)
            s2_row = _complete_market_row(
                price_usd=104.2,  # alpha = 0.42 in [0.08, 0.55]
                funding_z=-2.05,
                funding_rate=-0.00042,
                taker_buy_ratio=0.55,
                obi_top5=0.12,
                trade_size_ratio=1.40,
            )
            ez = evaluate_entry_zone(s2_row)
            self.assertTrue(ez.emit, ez.reject_reason)
            self.assertEqual(ez.entry_reason, "state2_squeeze_large_limit_maker")
            self.assertEqual(ez.order_type, "LIMIT_MAKER")
            assert ez.limit_price is not None and ez.stop_price is not None
            self.assertLess(ez.limit_price, 104.2)
            self.assertAlmostEqual(ez.limit_price, 103.0, places=6)  # L + 0.30*S (pre-mult odds 1.75 >= 1.50)
            self.assertEqual(ez.position_size_multiplier, 1.0)

            strat = TechBollFadeStrategy(position_pct=0.24)
            emitted = strat.on_tick({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "hot_tokens": [s2_row]})
            self.assertEqual(len(emitted), 1)
            # Base size_pct must be exact 0.24 (NOT 1.0 thick-pool and NOT 0.05 ordinary!)
            self.assertAlmostEqual(emitted[0]["size_pct"], 0.24, places=6)

            routed, _counts = apply_five_state_regime_router(emitted, [s2_row], now_ms=NOW_MS)
            self.assertEqual(len(routed), 1)
            sig = routed[0]
            self.assertEqual(sig["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)
            self.assertEqual(sig["regime_playbook"], "state2_squeeze_limit_maker")
            self.assertEqual(sig["recommended_order_type"], "LIMIT_MAKER")
            self.assertAlmostEqual(sig["regime_size_multiplier"], 1.20, places=6)
            self.assertAlmostEqual(sig["regime_tp_multiplier"], 1.35, places=6)
            self.assertAlmostEqual(sig["size_pct"], 0.288, places=6)  # 0.24 * 1.0 * 1.20
            self.assertAlmostEqual(sig["take_profit_price"], 110.0, places=6)
            for held in active_reservations(os.environ["REGIME_V2_RESERVATION_DB"]):
                release_reservation(
                    os.environ["REGIME_V2_RESERVATION_DB"], held["signal_key"], now_ms=NOW_MS
                )

            # 2) Neutral S2 squeeze (0.85 <= ratio < 1.30) uses 0.75 tier -> 0.24 * 0.75 * 1.20 = 0.216
            s2_neutral = _complete_market_row(
                price_usd=103.0,
                funding_z=-1.85,
                taker_buy_ratio=0.50,
                obi_top5=-0.02,
                trade_size_ratio=1.00,
            )
            emitted_n = strat.on_tick({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "hot_tokens": [s2_neutral]})
            routed_n, _ = apply_five_state_regime_router(emitted_n, [s2_neutral], now_ms=NOW_MS)
            self.assertEqual(len(routed_n), 1)
            self.assertAlmostEqual(routed_n[0]["size_pct"], 0.216, places=6)

            # 3) Weak taker absorption (< 0.48), heavy ask wall (< -0.05), OI collapse (<= -5%), and alpha > 0.55 all reject
            self.assertEqual(
                evaluate_entry_zone({**s2_row, "taker_buy_ratio": 0.44}).reject_reason,
                "state2_taker_buy_below_min",
            )
            self.assertEqual(
                evaluate_entry_zone({**s2_row, "obi_top5": -0.12}).reject_reason,
                "state2_obi_top5_below_min",
            )
            self.assertEqual(
                evaluate_entry_zone({**s2_row, "open_interest_change_1h": -5.5}).reject_reason,
                "state2_oi_collapsing",
            )
            self.assertEqual(
                evaluate_entry_zone({**s2_row, "price_usd": 106.0}).reject_reason,
                "state2_alpha_above_cap",
            )


class State4DeepDipEntryTests(unittest.TestCase):
    def test_s4_deep_dip_admits_whitelist_deep_maker_and_rejects_shallow_or_speculative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, _dry_contract(Path(tmp) / "reservations.sqlite"), clear=True
        ):
            # 1) Valid S4 deep dip on whitelist symbol SOLUSDT: boll_z = -2.40, alpha = 0.10 <= 0.15
            s4_row = _complete_market_row(
                binance_symbol="SOLUSDT",
                price_usd=101.0,  # alpha = (101 - 100) / 10 = 0.10 <= 0.15
                boll_z=-2.40,
                funding_z=2.05,
                cot_regime="cot_leveraged_long_crowded",
                global_account_ls_ratio=2.05,
                whale_retail_divergence=0.58,
                taker_buy_ratio=0.41,
                obi_top5=-0.08,
                trade_size_ratio=1.05,
            )
            ez = evaluate_entry_zone(s4_row)
            self.assertTrue(ez.emit, ez.reject_reason)
            self.assertEqual(ez.entry_reason, "state4_deep_limit_maker")
            self.assertEqual(ez.order_type, "LIMIT_MAKER")
            assert ez.limit_price is not None and ez.stop_price is not None
            self.assertAlmostEqual(ez.limit_price, 100.5, places=6)  # L + 0.05*S < 101.0
            self.assertAlmostEqual(ez.stop_price, 98.0, places=6)    # L - 0.20*S -> odds = 9.5/2.5 = 3.8 >= 3.0
            self.assertEqual(ez.position_size_multiplier, 0.5)

            strat = TechBollFadeStrategy(position_pct=0.24)
            emitted = strat.on_tick({"purpose": PURPOSE_LIVE_BNB_BOLL_FADE, "hot_tokens": [s4_row]})
            routed, _ = apply_five_state_regime_router(emitted, [s4_row], now_ms=NOW_MS)
            self.assertEqual(len(routed), 1)
            sig = routed[0]
            self.assertEqual(sig["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
            self.assertEqual(sig["regime_playbook"], "state4_defensive_deep_limit_maker")
            self.assertEqual(sig["recommended_order_type"], "LIMIT_MAKER")
            self.assertAlmostEqual(sig["regime_size_multiplier"], 0.30, places=6)
            self.assertAlmostEqual(sig["regime_tp_multiplier"], 0.85, places=6)
            self.assertAlmostEqual(sig["size_pct"], 0.036, places=6)  # 0.24 * 0.50 * 0.30

            # 2) Extreme lower-wick pierce (alpha = 0.02 <= 0.05) places passive bid below L
            wick_ez = evaluate_entry_zone({**s4_row, "price_usd": 100.2})
            self.assertTrue(wick_ez.emit, wick_ez.reject_reason)
            assert wick_ez.limit_price is not None
            self.assertLess(wick_ez.limit_price, 100.2)

            # 3) Speculative token, shallow boll_z, shallow alpha, weak taker flow, and extreme retail trap all reject
            self.assertEqual(
                evaluate_entry_zone({**s4_row, "binance_symbol": "ARBUSDT"}).reject_reason,
                "state4_speculative_symbol_rejected",
            )
            self.assertEqual(
                evaluate_entry_zone({**s4_row, "boll_z": -1.70}).reject_reason,
                "state4_boll_z_not_deep_enough",
            )
            self.assertEqual(
                evaluate_entry_zone({**s4_row, "price_usd": 102.2}).reject_reason,
                "state4_dip_not_deep_enough",
            )
            self.assertEqual(
                evaluate_entry_zone({**s4_row, "taker_buy_ratio": 0.35}).reject_reason,
                "state4_taker_buy_below_min",
            )
            self.assertEqual(
                evaluate_entry_zone(
                    {**s4_row, "global_account_ls_ratio": 2.30, "whale_retail_divergence": 0.40}
                ).reject_reason,
                "perp_retail_long_trap",
            )


class RealTimeEquityAndExposureCapTests(unittest.TestCase):
    def test_signed_account_snapshot_mtm_equity_and_concurrency_cluster_exposure_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap_file = Path(tmp) / "spot_acct_snap.json"
            s_key_name = snap_mod.API_SECRET_ENV
            sign_word = "unit_snap_sign_val_01"
            acct_payload = {
                "uid": 424242,
                "accountType": "SPOT",
                "canTrade": True,
                "permissions": ["SPOT"],
                "balances": [
                    {"asset": "USDT", "free": "700.00", "locked": "50.00"},
                    {"asset": "BTC", "free": "0.0025", "locked": "0.0"},
                ],
            }
            sealed = snap_mod.seal_snapshot(
                acct_payload,
                secret=sign_word,
                fetched_at_ms=NOW_MS - 30_000,
                restrictions={"enableReading": True, "enableSpotAndMarginTrading": True, "ipRestrict": True},
            )
            self.assertIsNone(snap_mod.write_snapshot_file(snap_file, sealed))

            hot_rows = [
                _complete_market_row(binance_symbol="BTCUSDT", price_usd=100_000.0),
                _complete_market_row(binance_symbol="SOLUSDT", price_usd=100.0),
            ]
            env = {
                CONTRACT_ENV: "1",
                "REGIME_V2_STALE_DATA_POLICY": "fail_closed",
                "REGIME_V2_EQUITY_SOURCE": "signed_account_snapshot",
                "BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH": str(snap_file),
                "REGIME_V2_RESERVATION_DB": str(Path(tmp) / "reservations.sqlite"),
                s_key_name: sign_word,
            }
            # Total equity = 700 + 50 + 0.0025 * 100000 = 1000.0 USDT; usdt_free = 700.0
            eq = resolve_regime_v2_equity(now_ms=NOW_MS, hot_rows=hot_rows, environ=env)
            self.assertTrue(eq.ok, eq.error)
            self.assertAlmostEqual(eq.equity_usdt, 1000.0, places=4)
            self.assertAlmostEqual(eq.usdt_free, 700.0, places=4)

            # Stale (> 300s) and MCP UID (1289917075) snapshots fail closed
            eq_stale = resolve_regime_v2_equity(
                now_ms=NOW_MS + 301_000, hot_rows=hot_rows, environ=env
            )
            self.assertFalse(eq_stale.ok)
            self.assertIn("stale", str(eq_stale.error))

            mcp_file = Path(tmp) / "mcp_snap.json"
            mcp_sealed = snap_mod.seal_snapshot(
                {**acct_payload, "uid": 1289917075},
                secret=sign_word,
                fetched_at_ms=NOW_MS - 1_000,
                restrictions={"enableReading": True, "enableSpotAndMarginTrading": True, "ipRestrict": True},
            )
            snap_mod.write_snapshot_file(mcp_file, mcp_sealed)
            eq_mcp = resolve_regime_v2_equity(
                now_ms=NOW_MS,
                hot_rows=hot_rows,
                environ={**env, "BINANCE_SPOT_ACCOUNT_SNAPSHOT_PATH": str(mcp_file)},
            )
            self.assertFalse(eq_mcp.ok)

            # Concurrency, cluster, total exposure (72%), and 20 USDT floor enforcement
            cands = [
                {
                    "action": "buy",
                    "binance_symbol": "SOLUSDT",  # major_l1
                    "market_regime": "positive_gamma_mean_reversion",
                    "size_pct": 0.24,
                },
                {
                    "action": "buy",
                    "binance_symbol": "BNBUSDT",  # also major_l1 -> blocked by cluster cap = 1!
                    "market_regime": "positive_gamma_mean_reversion",
                    "size_pct": 0.24,
                },
                {
                    "action": "buy",
                    "binance_symbol": "AAVEUSDT",  # defi_infra
                    "market_regime": "cross_asset_momentum_pullback",
                    "size_pct": 0.276,
                },
                {
                    "action": "buy",
                    "binance_symbol": "ARBUSDT",  # l2_scaling -> would push exposure > 72% when 1 open exists
                    "market_regime": "positive_gamma_mean_reversion",
                    "size_pct": 0.24,
                },
            ]
            open_pos = [{"symbol": "TRXUSDT", "status": "oco_working", "entry_cost_usdt": 220.0}]
            kept, rep = enforce_regime_v2_sizing_and_caps(
                cands,
                hot_rows=hot_rows,
                now_ms=NOW_MS,
                open_positions_override=open_pos,
                equity_override=eq,
                environ=env,
            )
            # TRXUSDT is major_l1 (open, 22%), so SOLUSDT & BNBUSDT both hit cluster_concurrency_cap_reached!
            # AAVEUSDT (27.6%, defi_infra) + ARBUSDT (24.0%, l2_scaling):
            # 22% + 27.6% = 49.6% <= 72% (AAVE kept); + 24% = 73.6% > 72% (ARBUSDT dropped by total_portfolio_exposure_cap_exceeded!)
            kept_syms = [s["binance_symbol"] for s in kept]
            self.assertEqual(kept_syms, ["AAVEUSDT"])
            self.assertEqual(rep["dropped"]["cluster_concurrency_cap_reached"], 2)
            self.assertEqual(rep["dropped"]["total_portfolio_exposure_cap_exceeded"], 1)
            self.assertAlmostEqual(kept[0]["planned_notional_usdt"], 276.0, places=4)

            # Unprotected inventory in ledger halts all new buys
            ledger_p = Path(tmp) / "unprotected_ledger.db"
            conn = sqlite3.connect(str(ledger_p))
            conn.execute(
                """
                CREATE TABLE binance_spot_positions (
                    position_id TEXT PRIMARY KEY, purpose TEXT, symbol TEXT, status TEXT,
                    entry_order_id TEXT, protection TEXT, oco_order_list_id TEXT,
                    oco_above_order_id TEXT, oco_below_order_id TEXT, residual_qty REAL
                )
                """
            )
            conn.execute(
                "CREATE TABLE canonical_positions (position_id TEXT PRIMARY KEY, purpose TEXT, symbol TEXT, status TEXT, entry_order_id TEXT)"
            )
            conn.execute(
                "INSERT INTO binance_spot_positions VALUES ('p1', 'live_bnb_boll_fade', 'BTCUSDT', 'oco_unknown', '1', 'none', NULL, NULL, NULL, 0)"
            )
            conn.commit()
            conn.close()
            kept_h, rep_h = enforce_regime_v2_sizing_and_caps(
                [{"action": "buy", "binance_symbol": "AAVEUSDT", "size_pct": 0.24}],
                now_ms=NOW_MS,
                equity_override=eq,
                environ={**env, "DECISION_EXECUTION_LEDGER": str(ledger_p)},
            )
            self.assertEqual(kept_h, [])
            self.assertIn("unprotected_or_dust_inventory", str(rep_h.get("halt_reason")))


class DatasourceFreshnessGateTests(unittest.TestCase):
    def test_all_nine_clocks_enforce_fail_closed_and_end_to_end_tick_and_cutover(self) -> None:
        base_row = _complete_market_row()
        env = {CONTRACT_ENV: "1", "REGIME_V2_STALE_DATA_POLICY": "fail_closed"}
        self.assertEqual(
            check_row_and_tick_freshness(
                base_row, now_ms=NOW_MS, require_all_clocks=True, environ=env
            ),
            [],
        )
        # Inclusive 3600000ms volume_z boundary passes; 3600001ms fails closed
        self.assertEqual(
            check_row_and_tick_freshness(
                {**base_row, "volume_z_source_close_ms": NOW_MS - 3_600_000},
                now_ms=NOW_MS,
                require_all_clocks=True,
                environ=env,
            ),
            [],
        )
        stale_mutations = [
            ("tech_updated_at", _iso_utc(NOW_MS - 3_601_000), "binance_indicators_stale"),
            ("quote_turnover_24h_asof", _iso_utc(NOW_MS - 3_601_000), "quote_turnover_stale"),
            ("entry_kline_5m_close_ms", NOW_MS - 901_000, "entry_5m_kline_stale"),
            ("volume_z_source_close_ms", NOW_MS - 3_600_001, "volume_z_source_close_stale"),
            ("obi_top5_source_ts_ms", NOW_MS - 3_601_000, "obi_top5_source_stale"),
            ("_perp_counterparty_ts_ms", NOW_MS - 3_601_000, "perp_counterparty_stale"),
            ("_cross_exchange_ts_ms", NOW_MS - 3_601_000, "cross_exchange_spot_baseline_stale"),
            ("_deribit_gamma_ts_ms", NOW_MS - 3_601_000, "deribit_options_gamma_stale"),
            ("news_fetched_at_ms", NOW_MS - 5_401_000, "news_fetched_at_stale"),
            ("_lseg_cross_asset_ts_ms", NOW_MS - 5_401_000, "lseg_cross_asset_bars_stale"),
            ("_cot_max_date", "2020-03-24", "cot_publication_not_current"),
        ]
        for key, bad_val, expected_reason in stale_mutations:
            reasons = check_row_and_tick_freshness(
                {**base_row, key: bad_val},
                now_ms=NOW_MS,
                require_all_clocks=True,
                environ=env,
            )
            self.assertIn(expected_reason, reasons)

        # Macro gate fails closed when LSEG DB is missing under fail_closed policy
        m_status = evaluate_macro_gate(
            "/tmp/non_existent_lseg_macro_v2_test.db",
            now_ms=NOW_MS,
            purpose=PURPOSE_LIVE_BNB_BOLL_FADE,
            environ=env,
        )
        self.assertEqual(m_status.halt_reason, "macro_lseg_stale_or_missing")

        # Cutover report passes cleanly when all local stores, ledgers, kill switch, and S2/S4 signals are valid
        with tempfile.TemporaryDirectory(prefix="regime-v2-full-cutover-") as tmp:
            root = Path(tmp)
            (root / "data/counterparty").mkdir(parents=True)
            (root / "data/lseg").mkdir(parents=True)
            (root / "data/institutional").mkdir(parents=True)

            real_now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            due_now_iso = due_cftc_asof(datetime.now(timezone.utc)).isoformat()

            # 1) public_derivatives.db
            d_conn = sqlite3.connect(str(root / "data/counterparty/public_derivatives.db"))
            d_conn.execute("CREATE TABLE binance_perp_counterparty (symbol TEXT, ts_ms INTEGER)")
            d_conn.execute("INSERT INTO binance_perp_counterparty VALUES ('SOLUSDT', ?)", (real_now_ms - 10_000,))
            d_conn.execute("CREATE TABLE cross_exchange_spot_baseline (symbol TEXT, ts_ms INTEGER)")
            d_conn.execute("INSERT INTO cross_exchange_spot_baseline VALUES ('SOLUSDT', ?)", (real_now_ms - 10_000,))
            d_conn.execute("CREATE TABLE deribit_options_gamma (currency TEXT, ts_ms INTEGER)")
            d_conn.execute("INSERT INTO deribit_options_gamma VALUES ('BTC', ?)", (real_now_ms - 10_000,))
            d_conn.execute("CREATE TABLE binance_spot_microstructure (symbol TEXT, obi_top5_source_ts_ms INTEGER)")
            d_conn.execute("INSERT INTO binance_spot_microstructure VALUES ('SOLUSDT', ?)", (real_now_ms - 10_000,))
            d_conn.commit()
            d_conn.close()

            # 2) news_events.db
            n_conn = sqlite3.connect(str(root / "news_events.db"))
            n_conn.execute("CREATE TABLE news_events (source_tier TEXT, fetched_at TEXT, published_at TEXT)")
            n_conn.execute(
                "INSERT INTO news_events VALUES ('whitelist', ?, ?)",
                (_iso_utc(real_now_ms - 60_000), _iso_utc(real_now_ms - 60_000)),
            )
            n_conn.commit()
            n_conn.close()

            # 3) lseg_macro.db
            l_conn = sqlite3.connect(str(root / "data/lseg/lseg_macro.db"))
            l_conn.execute("CREATE TABLE cross_asset_bars (ric TEXT, interval TEXT, utc_ms INTEGER, close REAL)")
            l_conn.execute(
                "INSERT INTO cross_asset_bars VALUES ('NQc1', '5min', ?, 20000.0)",
                (real_now_ms - 60_000,),
            )
            l_conn.commit()
            l_conn.close()

            # 4) cityu_institutional.db
            c_conn = sqlite3.connect(str(root / "data/institutional/cityu_institutional.db"))
            c_conn.execute("CREATE TABLE lseg_cftc_cot_positioning (symbol TEXT, date TEXT)")
            c_conn.execute(
                "INSERT INTO lseg_cftc_cot_positioning VALUES ('BTC', ?)",
                (due_now_iso,),
            )
            c_conn.commit()
            c_conn.close()

            # 5) fade & classic execution ledgers + isolated output/source DBs + kill file
            (root / "execution_ledger_live_bnb_boll_fade.db").write_bytes(b"")
            (root / "execution_ledger_live_bnb_boll_classic.db").write_bytes(b"")
            iso_dir = root / "isolated"
            iso_dir.mkdir()
            out_db = iso_dir / "decision.db"
            src_db = iso_dir / "source.db"
            out_db.write_bytes(b"")
            src_db.write_bytes(b"")
            kill_file = iso_dir / "LIVE_TRADING_DISABLED"
            kill_file.write_text("halt\n", encoding="utf-8")

            prof_env = parse_env_file(profile_path())
            self.assertEqual(profile_blocks(prof_env), [])

            valid_s2_sig = {
                "action": "buy",
                "strategy_id": "tech_boll_fade_v1",
                "market_regime": "short_squeeze_breakout",
                "regime_playbook": "state2_squeeze_limit_maker",
                "recommended_order_type": "LIMIT_MAKER",
                "size_pct": 0.288,
                "regime_classification": {"inputs": {"funding_z": -2.0}, "missing": []},
            }
            report_ok = build_report(
                env=prof_env,
                root=root,
                pack=None,
                signals=[valid_s2_sig],
                kill_present=True,
                dry_run=True,
                output_db=out_db,
                source_db=src_db,
                isolated_prefix=iso_dir,
                engine_exit_code=0,
            )
            self.assertEqual(report_ok["blocks"], [])
            self.assertTrue(report_ok["cutover_allowed"])


if __name__ == "__main__":
    unittest.main()
