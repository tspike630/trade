"""Regime v2 cutover. Source defaults stay off. The 24% baseline is not a new Kelly."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from decision_engine.core.regime_strategy_router import (
    REGIME_1_POSITIVE_GAMMA_PINNED,
    REGIME_2_SHORT_SQUEEZE_BREAKOUT,
    REGIME_3_CROSS_ASSET_BULL_PULLBACK,
    REGIME_4_CROWDED_LONG_DEFENSIVE,
    REGIME_5_CRISIS_LIQUIDATION_HALT,
    apply_five_state_regime_router,
    is_optimal_kelly_enabled,
)
from decision_engine.core.regime_v2_acceptance import (
    build_report,
    pack_clock_blocks,
    parse_env_file,
    profile_blocks,
    profile_path,
    signal_blocks,
)
from decision_engine.core.cityu_institutional_alpha import due_cftc_asof
from decision_engine.core.volume_z_1h import source_close_is_fresh
from decision_engine.strategies.live_bnb_entry_zone import evaluate_entry_zone
from execution.executors.binance_spot_protect import (
    max_hold_sec,
    orderbook_gate_enabled,
    post_only_limit_enabled,
    sor_maker_fallback_enabled,
    trailing_ratchet_enabled,
)

_ROOT = Path(__file__).resolve().parents[2]
_SWITCHES = (
    "USE_OPTIMAL_KELLY_MATRIX",
    "EXPAND_REGIME_OCO_TP",
    "ENABLE_PERP_SHORT_ENGINE",
    "BINANCE_PERP_SHORT_LIVE_ENABLED",
    "ENFORCE_REAL_DEMAND_WHITELIST",
    "BINANCE_SPOT_TRAILING_RATCHET",
    "BINANCE_SPOT_MAX_HOLD_SEC",
    "BINANCE_SPOT_REANCHOR_OCO_ON_FILL",
    "LIVE_BNB_POST_ONLY_LIMIT",
    "BINANCE_SPOT_POST_ONLY_LIMIT",
    "BINANCE_SPOT_ORDERBOOK_GATE",
    "BINANCE_SPOT_SOR_MAKER_FALLBACK",
    "LIVE_BNB_INVERSE_VOL_SIZING",
)

# Bytes this PR must leave alone. The first hash is main. The second is the
# installed production file from the 2026-11-14 pack. Neither may be replaced
# by a third rewrite in this branch.
_UNTOUCHED = {
    "execution/executors/binance_spot.py": (
        "d8c31acabf25b7b564c86dc56d60bc2d41cf0877e93e6d1552b23b68a41659bf",
        "87ef5ed7ed6566bab4f6b7ff0abd4fd33a96811f03cf8f7477a5eb03bede2f00",
    ),
    "execution/executors/binance_spot_gate.py": (
        "f7e2e3c63d42ad606516c9fc73bc822b868eba8686874248192671f03595cf38",
        "5c3022e0553b6a74452577418098ea7a0dfc78870a745fd0b99a0b0411932d28",
    ),
    "execution/executors/binance_spot_protect.py": (
        "6686b214b2cc2b028f6b17c686107368d16a13462f61ad8953b211231874aa1f",
        "70aecd695825a7b71216b336d2a71c3c1b3bcb7302370cae8dd5ac44405c1ad9",
    ),
    "execution/gateway.py": (
        "e99f6d6f290d02a96328c8e4ecf287b16df4539a7bbd5ff2299d9cb129f8466c",
        "06ee774eedd91ff18bbde603c65a7e474a561fa4ea976c011e21ed61343906ed",
    ),
    "execution/ledger.py": (
        "5e07b4307fb4c385029531895fec7db89fd00c547a37104799bfc0b54f40f308",
        "6039fd02e734acd8c031bc4ac80806b32b7e8ef9b049387ae6f8b5b6b81d82ac",
    ),
    "execution/run.py": (
        "fc30c933d19820a120d3896d8174fb8f54be118399736843540eede2e2500e94",
        "ff44a21b317788a8a04a25e77633641e2f33d32fa2e79795bef473e9c7f637ed",
    ),
    "decision_engine/core/engine.py": (
        "bac2a0590190973af9059eae5c035804b27390b277d833d10663d9e03b4cfb08",
        "2b5787626f42bb5c40542e39164445688f1bbda0afa3727a9fd7e20d959bd7b3",
    ),
    "decision_engine/core/live_buy_guards.py": (
        "6b3023ed5bdcbb2fdd4b4372ea0d360b147be96ac68016b4d7631fc671991da7",
        "a550474c89c120ca9243771b528a4f29caa1eead724b0b73d3f382e469b7b492",
    ),
    "decision_engine/core/protection_halt.py": (
        "947465bca09c0f4f907d8e94f718353c84a9a8cf829a3c2db9f099bdacb4fc00",
        "b3c02ee7b3d9ea4ba2b0ecaa78fb0ed9658c58b23febb36de12e8f6786051ee2",
    ),
}


def _quiet(**extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "funding_rate": 0.0001,
        "funding_z": 0.1,
        "whale_retail_divergence": 1.0,
        "global_account_ls_ratio": 1.2,
        "gamma_regime": "positive_gamma",
        "dvol_zscore": 0.1,
        "liq_cascade_regime": "normal",
        "max_venue_dislocation_bps": 1.0,
        "cot_regime": "neutral",
        "nvda_1h_ret_pct": 0.1,
        "smh_1h_ret_pct": 0.1,
        "xs_breadth_1h": 0.40,
        "xs_rank_pct_1h": 0.80,
        "obi_top5": 0.10,
        "taker_buy_ratio": 0.55,
        "volume_z": 0.4,
        "in_unlock_blackout": 0,
        "xs_systemic_crash": False,
    }
    row.update(extra)
    return row


def _route(hot: dict[str, object], *, size: float = 0.24, take_profit: float = 110.0) -> list[dict]:
    signal = {
        "action": "buy",
        "strategy_id": "tech_boll_fade_v1",
        "binance_symbol": "SOLUSDT",
        "size_pct": size,
        "order_type": "MARKET",
        "price_usd": 100.0,
        "take_profit_price": take_profit,
        "stop_price": 90.0,
        "entry_reason": "large_trade_market",
    }
    out, _counts = apply_five_state_regime_router(
        [signal],
        [{"binance_symbol": "SOLUSDT", **hot}],
    )
    return out


class SwitchesStayOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in _SWITCHES}
        for key in _SWITCHES:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_unset_switches_do_not_select_kelly_or_expand_tp(self) -> None:
        self.assertFalse(is_optimal_kelly_enabled({}))
        self.assertFalse(trailing_ratchet_enabled())
        self.assertEqual(max_hold_sec(), 0)
        self.assertFalse(orderbook_gate_enabled())
        self.assertFalse(post_only_limit_enabled())
        self.assertFalse(sor_maker_fallback_enabled())

        state1 = _route(_quiet())
        self.assertEqual(len(state1), 1)
        self.assertEqual(state1[0]["strategy_id"], "tech_boll_fade_v1")
        self.assertEqual(state1[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertEqual(state1[0]["regime_playbook"], "tech_boll_fade_standard")
        self.assertAlmostEqual(state1[0]["size_pct"], 0.24, places=6)
        self.assertAlmostEqual(state1[0]["take_profit_price"], 110.0, places=6)
        self.assertFalse(state1[0]["regime_classification"]["state1_unconfirmed"])
        self.assertEqual(state1[0]["recommended_order_type"], "LIMIT_MAKER")
        self.assertFalse(state1[0]["require_perp_beta_hedge"])
        self.assertNotIn("regime_tp_expanded", state1[0])

        whale = _route(_quiet(whale_retail_divergence=1.40, funding_z=0.2))
        self.assertEqual(whale[0]["regime_playbook"], "tech_boll_fade_whale_bull_skew")
        self.assertAlmostEqual(whale[0]["size_pct"], 0.2592, places=6)

        shallow = _route(_quiet(whale_retail_divergence=0.60, boll_z=-1.0))
        self.assertEqual(shallow, [])
        deep = _route(_quiet(whale_retail_divergence=0.60, boll_z=-2.5))
        self.assertAlmostEqual(deep[0]["size_pct"], 0.204, places=6)
        self.assertAlmostEqual(deep[0]["take_profit_price"], 110.0, places=6)

        state2 = _route(_quiet(funding_z=-2.0))
        self.assertEqual(state2[0]["market_regime"], REGIME_2_SHORT_SQUEEZE_BREAKOUT)
        self.assertAlmostEqual(state2[0]["size_pct"], 0.288, places=6)
        self.assertAlmostEqual(state2[0]["take_profit_price"], 110.0, places=6)
        self.assertTrue(state2[0]["enable_trailing_ratchet"])
        self.assertFalse(trailing_ratchet_enabled())

        state3 = _route(_quiet(xs_breadth_1h=0.75, xs_rank_pct_1h=0.90))
        self.assertEqual(state3[0]["market_regime"], REGIME_3_CROSS_ASSET_BULL_PULLBACK)
        self.assertAlmostEqual(state3[0]["size_pct"], 0.276, places=6)
        self.assertEqual(state3[0]["order_type"], "MARKET")
        self.assertEqual(state3[0]["recommended_order_type"], "LIMIT_MAKER")

        state4 = _route(_quiet(global_account_ls_ratio=2.5, whale_retail_divergence=0.50))
        self.assertEqual(state4[0]["market_regime"], REGIME_4_CROWDED_LONG_DEFENSIVE)
        self.assertAlmostEqual(state4[0]["size_pct"], 0.132, places=6)
        self.assertAlmostEqual(state4[0]["regime_tp_multiplier"], 0.80, places=6)
        self.assertAlmostEqual(state4[0]["take_profit_price"], 110.0, places=6)
        self.assertAlmostEqual(state4[0]["stop_price"], 90.0, places=6)
        self.assertTrue(state4[0]["require_perp_beta_hedge"])
        self.assertNotEqual(state4[0]["size_pct"], 0.05)

        trapped = _route(_quiet(global_account_ls_ratio=2.30, whale_retail_divergence=0.40))
        self.assertEqual(trapped, [])

        halted = _route(_quiet(max_venue_dislocation_bps=40.0))
        self.assertEqual(halted, [])

    def test_missing_features_remain_state1_and_are_marked_unconfirmed(self) -> None:
        shell = _route({"price_usd": 0.03})
        self.assertEqual(shell[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        self.assertEqual(shell[0]["regime_playbook"], "tech_boll_fade_standard")
        self.assertTrue(shell[0]["regime_classification"]["state1_unconfirmed"])
        self.assertIn("funding_z", shell[0]["regime_classification"]["missing"])
        self.assertIn("whale_retail_divergence", shell[0]["regime_classification"]["missing"])
        self.assertIsNone(shell[0]["regime_classification"]["inputs"]["funding_z"])
        blocked = signal_blocks(shell[0], kelly_off=True)
        self.assertIn("state1_unconfirmed", {item["code"] for item in blocked})

    def test_renaming_the_strategy_does_not_pass(self) -> None:
        routed = _route(_quiet())
        forged = dict(routed[0])
        forged["strategy_id"] = "five_state_v2"
        codes = {item["code"] for item in signal_blocks(forged, kelly_off=True)}
        self.assertIn("strategy_is_not_the_entry_module", codes)
        stripped = dict(routed[0])
        stripped.pop("regime_classification")
        codes = {item["code"] for item in signal_blocks(stripped, kelly_off=True)}
        self.assertIn("classification_not_replayable", codes)

    def test_kelly_fraction_is_rejected_while_the_switch_is_off(self) -> None:
        routed = _route(_quiet())
        forged = dict(routed[0])
        forged["size_pct"] = 0.185
        codes = {item["code"] for item in signal_blocks(forged, kelly_off=True)}
        self.assertIn("size_matches_unruled_kelly", codes)


class EntryOrderTests(unittest.TestCase):
    def test_entry_zone_still_sets_the_order_and_post_only_is_opt_in(self) -> None:
        row = {
            "binance_symbol": "BNBUSDT",
            "kline_low": 100.0,
            "boll_mid": 110.0,
            "price_usd": 100.8,
            "trade_size_ratio": 1.3,
            "change_5m": -0.5,
            "volume_strength": 0.1,
            "boll_lower": 100.0,
            "liquidity_usd": 90_000.0,
        }
        os.environ.pop("LIVE_BNB_POST_ONLY_LIMIT", None)
        large = evaluate_entry_zone(row)
        self.assertTrue(large.emit, large.reject_reason)
        self.assertEqual(large.entry_reason, "large_trade_market")
        self.assertEqual(large.order_type, "MARKET")
        self.assertAlmostEqual(large.take_profit_price or 0.0, 110.0, places=6)
        neutral_row = dict(row)
        neutral_row["price_usd"] = 102.8
        neutral_row["trade_size_ratio"] = 0.85
        neutral = evaluate_entry_zone(neutral_row)
        self.assertEqual(neutral.order_type, "LIMIT")
        os.environ["LIVE_BNB_POST_ONLY_LIMIT"] = "1"
        try:
            posted = evaluate_entry_zone(neutral_row)
            self.assertEqual(posted.order_type, "LIMIT_MAKER")
            self.assertEqual(posted.entry_reason, "neutral_limit")
        finally:
            os.environ.pop("LIVE_BNB_POST_ONLY_LIMIT", None)
        restored = evaluate_entry_zone(neutral_row)
        self.assertEqual(restored.order_type, "LIMIT")


class ProfileAndPackTests(unittest.TestCase):
    def test_profile_keeps_risk_switches_off_and_names_the_gates(self) -> None:
        env = parse_env_file(profile_path())
        self.assertEqual(profile_blocks(env), [])
        self.assertEqual(env["REGIME_V2_POSITION_PCT"], "0.24")
        self.assertEqual(env["ENABLE_PERP_SHORT_ENGINE"], "0")
        self.assertEqual(env["BINANCE_SPOT_TRAILING_RATCHET"], "0")
        self.assertEqual(env["BINANCE_SPOT_MAX_HOLD_SEC"], "0")
        self.assertNotEqual(
            env["DECISION_EXECUTION_LEDGER_FADE"],
            env["DECISION_EXECUTION_LEDGER_CLASSIC"],
        )

    def test_pack_clocks_are_not_all_green(self) -> None:
        document = {
            "written_hkt": "2026-11-14 11:20:42 HKT",
            "databases": {
                "public_derivatives": {
                    "exists": True,
                    "max_time": {
                        "binance_perp_counterparty.ts_ms": 1794626075001,
                        "binance_spot_microstructure.ts_ms": 1794626258060,
                        "cross_exchange_spot_baseline.ts_ms": 1794626275967,
                        "deribit_options_gamma.ts_ms": 1794625200000,
                        "stablecoin_supply.ts_ms": 1794290137192,
                        "onchain_holder_bot_summary.ts_ms": None,
                    },
                    "samples": {"token_unlock_schedule": []},
                },
                "news_events": {"exists": True, "max_time": {}, "samples": {"news_events": []}},
                "lseg_macro": {
                    "exists": True,
                    "max_time": {"cross_asset_bars.utc_ts": "2026-11-09T10:00:00Z"},
                    "samples": {"cross_asset_bars": [{"interval": "1h"}]},
                },
            },
        }
        blocks, observations = pack_clock_blocks(document)
        codes = {item["code"] for item in blocks}
        self.assertIn("binance_perp_counterparty.fresh", {item["code"] for item in observations})
        self.assertNotIn("binance_perp_counterparty.stale_or_absent", codes)
        self.assertIn("obi_top5_source_clock_not_in_evidence", codes)
        self.assertIn("news_record_clock_absent", codes)
        self.assertIn("lseg_cross_asset_bars_stale", codes)
        self.assertIn("token_unlock_values_not_in_evidence", codes)
        self.assertIn("cot_store_not_in_pack", codes)
        self.assertIn("volume_z_store_not_in_pack", codes)
        self.assertIn("stablecoin_supply_not_a_router_input", {item["code"] for item in observations})

    def test_supplement_clocks_use_reader_windows_and_do_not_invent_cot_or_unlock_ttl(self) -> None:
        now_ms = 1794627280536
        close_ms = 1794625199999
        computed_ms = 1794625917744
        self.assertTrue(source_close_is_fresh(close_ms, now_ms))
        self.assertTrue(source_close_is_fresh(computed_ms, now_ms))
        self.assertFalse(source_close_is_fresh(now_ms - 3_600_001, now_ms))
        document = {
            "written_hkt": "2026-11-14 11:34:40 HKT",
            "now_ms": now_ms,
            "mtime_hkt": "2026-11-14 11:34:40 HKT",
            "public_derivatives": {
                "path": "/workspace/dexscreener-crawler/data/counterparty/public_derivatives.db",
                "exists": True,
                "microstructure_max": {
                    "max_obi_top5_source_ts_ms": 1794627068491,
                    "max_obi_source_hkt": "2026-11-14 11:31:08 HKT",
                },
                "perp_counterparty_max": {
                    "max_ts_ms": 1794626931000,
                    "max_ts_hkt": "2026-11-14 11:28:51 HKT",
                },
                "token_unlock_schedule_columns": [
                    "symbol",
                    "next_unlock_utc",
                    "hours_to_unlock",
                    "in_unlock_blackout",
                ],
            },
            "decision_volume_z": {
                "max_source_close_time_ms": close_ms,
                "max_computed_at_ms": computed_ms,
            },
            "news_events": {
                "max_clocks": {"fetched_at": {"raw": "2026-11-13T11:33:42Z"}},
            },
            "lseg_macro": {"cross_asset_bars_max_utc_ts": "2026-11-09T10:00:00Z"},
            "cityu_institutional": {
                "exists": True,
                "readonly_clocks": {
                    "lseg_cftc_cot_positioning": {"max_date": {"raw": "2026-11-03"}},
                },
            },
            "collectors_observed_this_check": {
                "news_events collector process": {"observed_by_process_name_search": False},
                "lseg collector process": {"observed_by_process_name_search": False},
                "public_derivatives_loop": {"observed": True},
            },
        }
        blocks, observations = pack_clock_blocks(document)
        block_codes = {item["code"] for item in blocks}
        observation_codes = {item["code"] for item in observations}
        self.assertIn("obi_top5_source.fresh", observation_codes)
        self.assertIn("binance_perp_counterparty.fresh", observation_codes)
        self.assertIn("volume_z_source_close.fresh", observation_codes)
        self.assertIn("volume_z_computed_at_not_the_reader_clock", observation_codes)
        self.assertIn("news_fetched_at_stale", block_codes)
        self.assertIn("lseg_cross_asset_bars_stale", block_codes)
        self.assertNotIn("obi_top5_source_stale_or_absent", block_codes)
        self.assertNotIn("cot_stale", block_codes)
        self.assertNotIn("token_unlock_stale", block_codes)
        cot = next(item for item in observations if item["code"] == "cot_publication_cycle_not_a_ttl")
        self.assertIn("max_date=2026-11-03", cot["detail"])
        self.assertIn("no seconds TTL", cot["detail"])
        due = due_cftc_asof(datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc))
        self.assertIn(f"due_cftc_asof={due.isoformat()}", cot["detail"])
        self.assertNotEqual(due.isoformat(), "2026-11-03")
        unlock = next(item for item in observations if item["code"] == "token_unlock_record_clock_absent")
        self.assertIn("mtime is not a freshness clock", unlock["detail"])
        news = next(item for item in blocks if item["code"] == "news_fetched_at_stale")
        self.assertIn("collector_observed=False", news["detail"])

    def test_production_evidence_does_not_create_local_databases(self) -> None:
        env = parse_env_file(profile_path())
        supplement = {
            "now_ms": 1794627280536,
            "public_derivatives": {
                "exists": True,
                "path": "/workspace/dexscreener-crawler/data/counterparty/public_derivatives.db",
                "microstructure_max": {"max_obi_top5_source_ts_ms": 1794627068491},
                "perp_counterparty_max": {"max_ts_ms": 1794626931000},
                "token_unlock_schedule_columns": ["next_unlock_utc"],
            },
            "volume_z_search": {
                "decision_volume_z": {"max_source_close_time_ms": {"raw": 1794625199999}},
            },
            "news_events": {"max_clocks": {"fetched_at": {"raw": "2026-11-13T11:33:42Z"}}},
            "lseg_macro": {"cross_asset_bars_max_utc_ts": "2026-11-09T10:00:00Z"},
            "cityu_institutional": {
                "readonly_clocks": {
                    "lseg_cftc_cot_positioning": {"max_date": {"raw": "2026-11-03"}},
                },
            },
        }
        with tempfile.TemporaryDirectory(prefix="regime-v2-report-") as tmp:
            root = Path(tmp)
            report = build_report(
                env=env,
                root=_ROOT,
                pack=supplement,
                signals=[],
                kill_present=True,
                dry_run=True,
                output_db=root / "decision.db",
                source_db=root / "indicator_source.db",
                isolated_prefix=root,
                engine_exit_code=0,
            )
        codes = {item["code"] for item in report["blocks"]}
        self.assertFalse(report["cutover_allowed"])
        self.assertIn("public_derivatives_missing", codes)
        self.assertIn("news_events_missing", codes)
        self.assertIn("lseg_macro_missing", codes)
        self.assertIn("news_fetched_at_stale", codes)
        self.assertIn("lseg_cross_asset_bars_stale", codes)
        self.assertNotIn("obi_top5_source_stale_or_absent", codes)
        self.assertIn(
            "production_store_present_in_evidence",
            {item["code"] for item in report["observations"]},
        )

    def test_safety_bytes_are_main_or_the_production_pack_not_a_third_file(self) -> None:
        for relative, allowed in _UNTOUCHED.items():
            digest = hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()
            self.assertIn(digest, allowed, relative)
            self.assertEqual(digest, allowed[0], relative)

    def test_isolated_dry_run_blocks_cutover_without_a_live_order(self) -> None:
        script = _ROOT / "scripts" / "regime_v2_kill_dry_run.sh"
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        with tempfile.TemporaryDirectory(prefix="regime-v2-isolated-") as tmp:
            out = Path(tmp) / "decision.db"
            kill = Path(tmp) / "LIVE_TRADING_DISABLED"
            kill.write_text("test kill\n", encoding="utf-8")
            refused = subprocess.run(
                ["bash", str(script), "--output-db", str(out)],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(refused.returncode, 4, refused.stderr)
            self.assertFalse(out.exists())
            completed = subprocess.run(
                [
                    "bash",
                    str(script),
                    "--output-db",
                    str(out),
                    "--kill-file",
                    str(kill),
                ],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
            report_path = Path(tmp) / "regime_v2_acceptance.json"
            self.assertTrue(report_path.is_file(), completed.stderr + completed.stdout)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["cutover_allowed"])
            self.assertEqual(completed.returncode, 1)
            self.assertIn("keeps purpose=live_bnb_boll_fade", completed.stdout)
            self.assertNotIn("forcing purpose=shadow_010", completed.stdout)
            codes = {item["code"] for item in report["blocks"]}
            self.assertIn("public_derivatives_missing", codes)
            self.assertIn("news_events_missing", codes)
            self.assertIn("lseg_macro_missing", codes)
            self.assertIn("purpose_ledger_not_attached", codes)
            self.assertIn("no_replayable_signal", codes)
            self.assertNotIn("kill_file_absent", codes)
            self.assertEqual(report["kelly"], "0")
            self.assertEqual(report["perp"], "0")
            self.assertEqual(report["trailing_ratchet"], "0")
            self.assertEqual(report["max_hold_sec"], "0")
            self.assertEqual(report["position_pct_pending"], "0.24")
            env = os.environ.copy()
            env["REGIME_V2_KILL_FILE"] = str(kill)
            outside = subprocess.run(
                ["bash", str(script), "--output-db", str(_ROOT / "decision.db"), "--kill-file", str(kill)],
                cwd=_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(outside.returncode, 2, outside.stderr)
            self.assertFalse((_ROOT / "decision.db").exists())


class ReportShapeTests(unittest.TestCase):
    def test_macro_halt_regime_is_state5(self) -> None:
        routed = _route(_quiet())
        self.assertEqual(routed[0]["market_regime"], REGIME_1_POSITIVE_GAMMA_PINNED)
        halted, _counts = apply_five_state_regime_router(
            [
                {
                    "action": "buy",
                    "strategy_id": "tech_boll_fade_v1",
                    "binance_symbol": "SOLUSDT",
                    "size_pct": 0.24,
                }
            ],
            [{"binance_symbol": "SOLUSDT", **_quiet()}],
            macro_halt="central_bank_blackout",
        )
        self.assertEqual(halted, [])
        self.assertEqual(REGIME_5_CRISIS_LIQUIDATION_HALT, "crisis_liquidation_halt")

    def test_build_report_does_not_allow_cutover_on_an_empty_isolated_run(self) -> None:
        env = parse_env_file(profile_path())
        with tempfile.TemporaryDirectory(prefix="regime-v2-report-") as tmp:
            root = Path(tmp)
            output = root / "decision.db"
            source = root / "indicator_source.db"
            output.write_bytes(b"")
            source.write_bytes(b"")
            kill = root / "LIVE_TRADING_DISABLED"
            kill.write_text("x", encoding="utf-8")
            report = build_report(
                env=env,
                root=_ROOT,
                pack=None,
                signals=[],
                kill_present=True,
                dry_run=True,
                output_db=output,
                source_db=source,
                isolated_prefix=root,
                engine_exit_code=0,
            )
        self.assertFalse(report["cutover_allowed"])
        self.assertTrue(any(item["code"] == "no_replayable_signal" for item in report["blocks"]))
