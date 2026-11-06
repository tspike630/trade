"""3M quoteVolume floor and at most Top 2 new buys for the two Binance spot purposes.

Paper rows and sells are not filtered. Indicator-cache quote volume is not a
turnover source. Counts from the feed report are not fixtures.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import decision_engine  # noqa: F401 — crawler/ on path for logging_setup
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core import live_bnb_turnover as turnover_gate
from decision_engine.core.live_bnb_turnover import (
    ENV_MIN_QUOTE_VOLUME,
    ENV_TOP_BUYS,
    REASON_TURNOVER_BELOW,
    REASON_TURNOVER_INVALID,
    REASON_TURNOVER_MISSING,
    REASON_TURNOVER_STALE,
    REASON_TURNOVER_TIME_MISSING,
    REASON_UNIVERSE_NOT_ELIGIBLE,
    SOURCE_TICKER,
    SOURCE_UNIVERSE,
    _Obs,
    cap_live_bnb_buys,
    evaluate_membership_and_turnover,
    min_quote_volume_usdt,
    top_buys_per_tick,
)
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    PURPOSE_RACE_TECH_BOLL_FADE,
    SignalBus,
)
from decision_engine.core.token_cooldown import TokenCooldown
from decision_engine.strategies import get_strategy
from decision_engine.strategies.live_bnb_size_floor import ENTRY_MIN_NOTIONAL_USDT
from decision_engine.strategies.tech_boll_fade import CLASSIC_TP_PCT
from decision_engine.tests.test_bnb_spot_map import _seed
from indicators.binance_universe_quote import (
    COLLECTED_AT_COLUMN,
    QUOTE_COLUMN,
    build_snapshot_rows,
    collection_timestamp,
    min_quote_volume_usdt as collector_floor,
    refresh_universe,
)

_ENVS = (
    "LIVE_BNB_IGNORE_REPO_UNIVERSE",
    "LIVE_BNB_FACTOR_SOURCE",
    ENV_MIN_QUOTE_VOLUME,
    ENV_TOP_BUYS,
    "BINANCE_SPOT_UNIVERSE_DB",
    "BINANCE_SPOT_UNIVERSE_JSON",
    "BINANCE_INDICATORS_FRESH_SEC",
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ms() -> int:
    return int(time.time() * 1000)


class TurnoverContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = {key: os.environ.get(key) for key in _ENVS}
        os.environ["LIVE_BNB_IGNORE_REPO_UNIVERSE"] = "1"
        for key in _ENVS:
            if key != "LIVE_BNB_IGNORE_REPO_UNIVERSE":
                os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_floor_default_is_the_feed_env_at_3m(self) -> None:
        self.assertEqual(min_quote_volume_usdt(), 3_000_000.0)
        self.assertEqual(top_buys_per_tick(), 2)
        self.assertEqual(ENTRY_MIN_NOTIONAL_USDT, Decimal("20"))
        os.environ[ENV_MIN_QUOTE_VOLUME] = "3e6"
        self.assertEqual(min_quote_volume_usdt(), 3_000_000.0)
        os.environ[ENV_MIN_QUOTE_VOLUME] = "nope"
        self.assertEqual(min_quote_volume_usdt(), 3_000_000.0)
        os.environ[ENV_MIN_QUOTE_VOLUME] = "0"
        self.assertEqual(min_quote_volume_usdt(), 3_000_000.0)
        os.environ.pop(ENV_MIN_QUOTE_VOLUME, None)
        os.environ[ENV_TOP_BUYS] = "0"
        self.assertEqual(top_buys_per_tick(), 2)
        os.environ[ENV_TOP_BUYS] = "1"
        self.assertEqual(top_buys_per_tick(), 1)

    def test_exact_3m_passes_and_below_fails(self) -> None:
        now = _now_ms()
        members = {"KEEPUSDT"}
        window = 3_600_000
        common = dict(
            primary=True,
            members=members,
            universe_quotes={},
            now_ms=now,
            window_ms=window,
            floor=3_000_000.0,
        )
        row = {"binance_symbol": "KEEPUSDT", "token_address": "KEEPUSDT", "chain_id": "binance"}
        at, reason = evaluate_membership_and_turnover(
            row,
            tickers={"KEEPUSDT": _Obs(3_000_000.0, now, invalid=False, time_missing=False)},
            **common,
        )
        self.assertEqual(reason, "")
        assert at is not None
        self.assertEqual(at["quote_turnover_24h_usdt"], 3_000_000.0)
        self.assertEqual(at["quote_turnover_24h_source"], SOURCE_TICKER)
        below, why = evaluate_membership_and_turnover(
            row,
            tickers={"KEEPUSDT": _Obs(2_999_999.99, now, invalid=False, time_missing=False)},
            **common,
        )
        self.assertIsNone(below)
        self.assertEqual(why, REASON_TURNOVER_BELOW)

    def test_invalid_stale_and_base_volume_do_not_pass(self) -> None:
        now = _now_ms()
        row = {
            "binance_symbol": "KEEPUSDT",
            "token_address": "KEEPUSDT",
            "chain_id": "binance",
            "quote_volume_24h": 50_000_000.0,
            "liquidity_usd": 50_000_000.0,
            "volume_24h": 1_000_000_000.0,
            "factor_source": "binance_indicators",
        }
        common = dict(
            primary=True,
            members={"KEEPUSDT"},
            universe_quotes={},
            now_ms=now,
            window_ms=3_600_000,
            floor=3_000_000.0,
        )
        missing, why_missing = evaluate_membership_and_turnover(row, tickers={}, **common)
        self.assertIsNone(missing)
        self.assertEqual(why_missing, REASON_TURNOVER_MISSING)
        invalid, why_invalid = evaluate_membership_and_turnover(
            row,
            tickers={"KEEPUSDT": _Obs(None, now, invalid=True, time_missing=False)},
            **common,
        )
        self.assertEqual(why_invalid, REASON_TURNOVER_INVALID)
        stale, why_stale = evaluate_membership_and_turnover(
            row,
            tickers={
                "KEEPUSDT": _Obs(20_000_000.0, now - 3_600_001, invalid=False, time_missing=False)
            },
            **common,
        )
        self.assertEqual(why_stale, REASON_TURNOVER_STALE)
        undated, why_time = evaluate_membership_and_turnover(
            row,
            tickers={},
            universe_quotes={
                "KEEPUSDT": _Obs(9_000_000.0, None, invalid=False, time_missing=True)
            },
            **{key: value for key, value in common.items() if key != "universe_quotes"},
        )
        self.assertEqual(why_time, REASON_TURNOVER_TIME_MISSING)
        fresh_low, why_low = evaluate_membership_and_turnover(
            row,
            tickers={"KEEPUSDT": _Obs(100.0, now, invalid=False, time_missing=False)},
            universe_quotes={
                "KEEPUSDT": _Obs(9_000_000.0, now, invalid=False, time_missing=False)
            },
            **{key: value for key, value in common.items() if key != "universe_quotes"},
        )
        self.assertIsNone(fresh_low)
        self.assertEqual(why_low, REASON_TURNOVER_BELOW)
        removed, why_removed = evaluate_membership_and_turnover(
            {"binance_symbol": "CFGUSDT", "token_address": "CFGUSDT", "chain_id": "binance"},
            tickers={"CFGUSDT": _Obs(20_000_000.0, now, invalid=False, time_missing=False)},
            **common,
        )
        self.assertEqual(why_removed, REASON_UNIVERSE_NOT_ELIGIBLE)

    def test_sells_are_kept_and_buys_cap_at_two_without_padding(self) -> None:
        now = _now_ms()
        asof = _iso(datetime.now(timezone.utc))

        def _buy(symbol: str, strength: float, quote: float = 3_000_000.0) -> dict:
            return {
                "action": "buy",
                "chain_id": "binance",
                "token_address": symbol,
                "binance_symbol": symbol,
                "volume_strength": strength,
                "quote_turnover_24h_usdt": quote,
                "quote_turnover_24h_source": SOURCE_TICKER,
                "quote_turnover_24h_asof": asof,
                "universe_member": True,
            }

        sell_cfg = {
            "action": "sell",
            "chain_id": "binance",
            "token_address": "CFGUSDT",
            "binance_symbol": "CFGUSDT",
        }
        sell_other = {"action": "sell", "chain_id": "binance", "token_address": "ETHUSDT"}
        kept, report = cap_live_bnb_buys(
            [
                sell_cfg,
                _buy("LOWUSDT", 0.2),
                _buy("HIGHUSDT", 3.0),
                sell_other,
                _buy("HIGHUSDT", 1.0),
                _buy("MIDUSDT", 1.0),
            ],
            now_ms=now,
        )
        buys = [row for row in kept if row["action"] == "buy"]
        sells = [row for row in kept if row["action"] == "sell"]
        self.assertEqual([row["binance_symbol"] for row in buys], ["HIGHUSDT", "MIDUSDT"])
        self.assertEqual(buys[0]["volume_strength"], 3.0)
        self.assertEqual(sells, [sell_cfg, sell_other])
        self.assertEqual(report["selected"], ["HIGHUSDT", "MIDUSDT"])
        self.assertEqual(report["buys_kept"], 2)
        self.assertNotIn("LOWUSDT", report["selected"])
        alone, alone_report = cap_live_bnb_buys([_buy("ONLYUSDT", 0.4)], now_ms=now)
        self.assertEqual(len(alone), 1)
        self.assertEqual(alone_report["selected"], ["ONLYUSDT"])
        empty, empty_report = cap_live_bnb_buys([sell_cfg], now_ms=now)
        self.assertEqual(empty, [sell_cfg])
        self.assertEqual(empty_report["selected"], [])
        self.assertEqual(empty_report["buys_kept"], 0)


class EngineTurnoverTests(TurnoverContractTests):
    def _spec(self, **overrides: object) -> dict:
        spec = {
            "symbol": "KEEPUSDT",
            "base": "KEEP",
            "price": 0.8,
            "change": -4.0,
            "vs": 1.0,
            "lower": 1.0,
            "liq": 80_000.0,
            "tick": 3_000_000.0,
            "tick_age_ms": 0,
            "volume_24h": 5.0,
            "in_universe": True,
            "zone": False,
            "universe_quote": None,
            "universe_age_ms": 0,
        }
        spec.update(overrides)
        return spec

    def _write(self, db: Path, specs: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY,
                base_asset TEXT,
                quote_asset TEXT,
                status TEXT,
                price_usd REAL,
                change_5m REAL,
                volume_strength REAL,
                boll_lower REAL,
                boll_mid REAL,
                kline_low REAL,
                trade_size_ratio REAL,
                quote_volume_24h REAL,
                tech_updated_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE symbols (
                symbol TEXT,
                baseAsset TEXT,
                quoteAsset TEXT,
                status TEXT,
                quoteVolume_24h REAL,
                quoteVolume_collected_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE ccxt_ticks (
                exchange TEXT, symbol TEXT, ts INTEGER,
                quote_volume_24h REAL, volume_24h REAL
            )
            """
        )
        stamp = _iso(now)
        for spec in specs:
            conn.execute(
                """
                INSERT INTO binance_indicators VALUES (
                    ?, ?, 'USDT', 'TRADING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    spec["symbol"],
                    spec["base"],
                    spec["price"],
                    spec["change"],
                    spec["vs"],
                    spec["lower"],
                    spec["price"] + 0.2 if spec["zone"] else None,
                    spec["price"] - 0.05 if spec["zone"] else None,
                    1.5 if spec["zone"] else None,
                    spec["liq"],
                    stamp,
                    stamp,
                ),
            )
            if spec["in_universe"]:
                quote = spec["universe_quote"]
                asof = None
                if quote is not None:
                    asof = _iso(now - timedelta(milliseconds=int(spec["universe_age_ms"])))
                conn.execute(
                    """
                    INSERT INTO symbols (
                        symbol, baseAsset, quoteAsset, status,
                        quoteVolume_24h, quoteVolume_collected_at
                    ) VALUES (?, ?, 'USDT', 'TRADING', ?, ?)
                    """,
                    (spec["symbol"], spec["base"], quote, asof),
                )
            if spec["tick"] is not None:
                conn.execute(
                    "INSERT INTO ccxt_ticks VALUES ('binance', ?, ?, ?, ?)",
                    (
                        f"{spec['base']}/USDT",
                        now_ms - int(spec["tick_age_ms"]),
                        spec["tick"],
                        spec["volume_24h"],
                    ),
                )
        conn.commit()
        conn.close()

    def _tick(
        self,
        db: Path,
        purpose: str,
        *,
        hot_limit: int | None = 50,
        strategies: list | None = None,
        cooldown: TokenCooldown | None = None,
    ) -> tuple[list[sqlite3.Row], dict]:
        adapter = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=purpose)
        try:
            kwargs: dict = {"risk": RiskManager(cooldown_sec=0)}
            if hot_limit is not None:
                kwargs["hot_limit"] = hot_limit
            if cooldown is not None:
                kwargs["token_cooldown"] = cooldown
            engine = DecisionEngine(
                adapter,
                bus,
                strategies or [get_strategy("tech_boll_fade_v1")],
                **kwargs,
            )
            payload = engine.tick()
        finally:
            bus.close()
            adapter.close()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            rows = list(
                conn.execute(
                    "SELECT * FROM decision_log WHERE purpose = ? ORDER BY id",
                    (purpose,),
                )
            )
        finally:
            conn.close()
        return rows, payload

    def test_engine_boundary_source_and_ignores_indicator_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="KEEPUSDT", base="KEEP", tick=3_000_000.0, liq=90_000.0),
                    self._spec(
                        symbol="LOWUSDT",
                        base="LOW",
                        tick=2_999_999.99,
                        vs=9.0,
                        liq=90_000_000.0,
                    ),
                    self._spec(symbol="CACHEUSDT", base="CACHE", tick=None, liq=50_000_000.0, vs=8.0),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(payload["quote_turnover"]["scanned"], 3)
            self.assertEqual(payload["hot_loaded"], 3)
            self.assertEqual([row["token_address"] for row in rows], ["KEEPUSDT"])
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["quote_turnover_24h_source"], SOURCE_TICKER)
            self.assertEqual(extras["quote_turnover_24h_usdt"], 3_000_000.0)
            self.assertEqual(extras["and_mode"], "tech_fade&boll_lower")
            self.assertAlmostEqual(extras["take_profit_price"], 0.8 * 1.10)
            self.assertEqual(extras["tp_pct"], CLASSIC_TP_PCT)
            self.assertNotIn("entry_reason", extras)
            reasons = payload["quote_turnover"]["reasons"]
            self.assertGreaterEqual(reasons.get(REASON_TURNOVER_BELOW, 0), 1)
            self.assertGreaterEqual(reasons.get(REASON_TURNOVER_MISSING, 0), 1)
            self.assertEqual(payload["live_bnb_top_buys"]["selected"], ["KEEPUSDT"])

    def test_cfg_removed_from_universe_blocks_only_the_new_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [self._spec(symbol="CFGUSDT", base="CFG", tick=20_000_000.0, in_universe=False, vs=9.0)],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(rows, [])
            self.assertEqual(
                payload["quote_turnover"]["reasons"].get(REASON_UNIVERSE_NOT_ELIGIBLE),
                1,
            )
            sell = {
                "action": "sell",
                "chain_id": "binance",
                "token_address": "CFGUSDT",
                "binance_symbol": "CFGUSDT",
            }
            kept, report = cap_live_bnb_buys([sell], now_ms=_now_ms())
            self.assertEqual(kept, [sell])
            self.assertEqual(report["sells_kept"], 1)
            self.assertEqual(report["buys_kept"], 0)

    def test_stale_ticker_does_not_fall_through_to_the_indicator_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(
                        tick=25_000_000.0,
                        tick_age_ms=7_200_000,
                        liq=25_000_000.0,
                        vs=4.0,
                    )
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(rows, [])
            self.assertEqual(
                payload["quote_turnover"]["reasons"].get(REASON_TURNOVER_STALE),
                1,
            )

    def test_universe_quote_propagates_when_the_ticker_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [self._spec(tick=None, universe_quote=3_000_000.0)],
            )
            rows, _payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(len(rows), 1)
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["quote_turnover_24h_source"], SOURCE_UNIVERSE)
            self.assertEqual(extras["quote_turnover_24h_usdt"], 3_000_000.0)

    def test_strongest_eligible_buy_wins_ties_by_turnover_then_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="MIDUSDT", base="MID", vs=1.0, tick=8_000_000.0),
                    self._spec(symbol="HIGHUSDT", base="HIGH", vs=2.0, tick=3_000_000.0),
                    self._spec(symbol="LOWUSDT", base="LOW", vs=0.6, tick=30_000_000.0),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                {row["token_address"] for row in rows},
                {"HIGHUSDT", "MIDUSDT"},
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(payload["quote_turnover"]["passed"], 3)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["HIGHUSDT", "MIDUSDT"],
            )
            self.assertNotIn("LOWUSDT", payload["live_bnb_top_buys"]["selected"])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="ZEDUSDT", base="ZED", vs=1.5, tick=4_000_000.0),
                    self._spec(symbol="ALPHAUSDT", base="ALPHA", vs=1.5, tick=9_000_000.0),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["ALPHAUSDT", "ZEDUSDT"],
            )
            self.assertEqual(
                {row["token_address"] for row in rows},
                {"ALPHAUSDT", "ZEDUSDT"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="ZEDUSDT", base="ZED", vs=1.5, tick=4_000_000.0),
                    self._spec(symbol="MMMUSDT", base="MMM", vs=1.5, tick=4_000_000.0),
                    self._spec(symbol="ALPHAUSDT", base="ALPHA", vs=1.5, tick=4_000_000.0),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["ALPHAUSDT", "MMMUSDT"],
            )
            self.assertEqual(
                {row["token_address"] for row in rows},
                {"ALPHAUSDT", "MMMUSDT"},
            )
            self.assertNotIn("ZEDUSDT", {row["token_address"] for row in rows})

    def test_rejected_or_cooled_strongest_does_not_consume_the_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="HIGHUSDT", base="HIGH", vs=9.0, price=5.0, lower=1.0),
                    self._spec(symbol="MIDUSDT", base="MID", vs=1.1),
                    self._spec(symbol="LOWUSDT", base="LOW", vs=0.6),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["MIDUSDT", "LOWUSDT"],
            )
            self.assertEqual({row["token_address"] for row in rows}, {"MIDUSDT", "LOWUSDT"})
            self.assertNotIn("HIGHUSDT", payload["live_bnb_top_buys"]["ranked"])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="HIGHUSDT", base="HIGH", vs=9.0, liq=1.0, tick=6_000_000.0),
                    self._spec(symbol="MIDUSDT", base="MID", vs=1.1),
                    self._spec(symbol="LOWUSDT", base="LOW", vs=0.6),
                ],
            )
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["MIDUSDT", "LOWUSDT"],
            )
            self.assertEqual({row["token_address"] for row in rows}, {"MIDUSDT", "LOWUSDT"})
            self.assertNotIn("HIGHUSDT", payload["live_bnb_top_buys"]["ranked"])

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="HIGHUSDT", base="HIGH", vs=9.0),
                    self._spec(symbol="MIDUSDT", base="MID", vs=1.2),
                    self._spec(symbol="LOWUSDT", base="LOW", vs=0.6),
                ],
            )
            cooldown = TokenCooldown(60)
            cooldown.filter(
                [
                    {
                        "action": "buy",
                        "chain_id": "binance",
                        "token_address": "HIGHUSDT",
                        "ts": _iso(datetime.now(timezone.utc)),
                    }
                ]
            )
            rows, payload = self._tick(
                db, PURPOSE_LIVE_BNB_BOLL_CLASSIC, cooldown=cooldown
            )
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["MIDUSDT", "LOWUSDT"],
            )
            self.assertEqual({row["token_address"] for row in rows}, {"MIDUSDT", "LOWUSDT"})
            self.assertNotIn("HIGHUSDT", payload["live_bnb_top_buys"]["selected"])

    def test_scan_window_still_reaches_a_later_eligible_name(self) -> None:
        specs = [
            self._spec(
                symbol=f"D{index:03d}USDT",
                base=f"D{index:03d}",
                change=-10.0,
                vs=9.0,
                price=5.0,
                lower=1.0,
            )
            for index in range(60)
        ]
        specs.append(self._spec(symbol="KEEPUSDT", base="KEEP", change=-3.0, vs=0.6))
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(db, specs)
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC, hot_limit=None)
            self.assertGreaterEqual(payload["hot_loaded"], 61)
            self.assertEqual(payload["quote_turnover"]["scanned"], payload["hot_loaded"])
            self.assertEqual([row["token_address"] for row in rows], ["KEEPUSDT"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(db, specs)
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC, hot_limit=20)
            self.assertEqual(payload["hot_loaded"], 20)
            self.assertEqual(rows, [])

    def test_two_emit_passes_still_publish_one_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(
                db,
                [
                    self._spec(symbol="HIGHUSDT", base="HIGH", vs=2.0),
                    self._spec(symbol="MIDUSDT", base="MID", vs=1.0),
                    self._spec(symbol="LOWUSDT", base="LOW", vs=0.7),
                ],
            )
            rows, payload = self._tick(
                db,
                PURPOSE_LIVE_BNB_BOLL_CLASSIC,
                strategies=[get_strategy("tech_boll_fade_v1"), get_strategy("tech_boll_fade_v1")],
            )
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["HIGHUSDT", "MIDUSDT"],
            )
            self.assertEqual(
                {row["token_address"] for row in rows},
                {"HIGHUSDT", "MIDUSDT"},
            )
            self.assertEqual(len(rows), 2)
            self.assertEqual(payload["emitted"], 2)
            self.assertNotIn("LOWUSDT", {row["token_address"] for row in rows})
            self.assertLessEqual(payload["live_bnb_top_buys"]["buys_kept"], 2)

    def test_entry_zone_recipe_stays_on_its_purpose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write(db, [self._spec(zone=True, price=0.8, lower=1.0, change=0.0, vs=0.0)])
            rows, _payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_FADE)
            self.assertEqual(len(rows), 1)
            extras = json.loads(rows[0]["raw_json"])["extras"]
            self.assertEqual(extras["and_mode"], "entry_zone")
            self.assertAlmostEqual(extras["take_profit_price"], 1.0)
            self.assertNotIn("exit_shell", extras)
            self.assertNotEqual(extras["take_profit_price"], 0.8 * 1.10)

    def test_paper_purpose_ignores_the_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            _seed(db)
            rows, payload = self._tick(db, PURPOSE_RACE_TECH_BOLL_FADE, hot_limit=20)
            self.assertNotIn("quote_turnover", payload)
            self.assertTrue(any(row["chain_id"] == "solana" for row in rows))
            self.assertNotIn("CFGUSDT", {row["token_address"] for row in rows})
            for row in rows:
                extras = json.loads(row["raw_json"]).get("extras") or {}
                self.assertNotIn("quote_turnover_24h_usdt", extras)
                self.assertNotIn("stop_price", extras)


class UniverseQuoteContractTests(EngineTurnoverTests):
    def test_column_names_match_the_collector(self) -> None:
        self.assertEqual(QUOTE_COLUMN, "quoteVolume_24h")
        self.assertEqual(COLLECTED_AT_COLUMN, "quoteVolume_collected_at")
        self.assertIn(QUOTE_COLUMN.lower(), turnover_gate._QUOTE_COLUMNS)
        self.assertEqual(turnover_gate._TIME_COLUMNS, (COLLECTED_AT_COLUMN.lower(),))
        self.assertNotIn("updated_at", turnover_gate._TIME_COLUMNS)

    def test_old_schema_without_collected_at_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.db"
            self._write_indicator(db)
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE symbols (
                    symbol TEXT PRIMARY KEY,
                    baseAsset TEXT,
                    quoteAsset TEXT,
                    status TEXT,
                    quoteVolume_24h REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO symbols VALUES ('KEEPUSDT', 'KEEP', 'USDT', 'TRADING', 9000000)
                """
            )
            conn.commit()
            conn.close()
            os.utime(db, None)
            rows, payload = self._tick(db, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(rows, [])
            self.assertEqual(
                payload["quote_turnover"]["reasons"].get("turnover_time_missing"),
                1,
            )

    def test_refresh_writes_collected_at_and_reader_ranks_top2(self) -> None:
        collected = datetime.now(timezone.utc)
        info = {
            "symbols": [
                {"symbol": "HIGHUSDT", "baseAsset": "HIGH", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
                {"symbol": "MIDUSDT", "baseAsset": "MID", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
                {"symbol": "LOWUSDT", "baseAsset": "LOW", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
                {"symbol": "BTCUSDT_PERP", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": False},
                {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "BREAK", "isSpotTradingAllowed": True},
                {
                    "symbol": "SOL/USDT:USDT",
                    "baseAsset": "SOL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                },
            ]
        }
        ticker = [
            {"symbol": "HIGHUSDT", "quoteVolume": "4000000", "volume": "999999999"},
            {"symbol": "MIDUSDT", "quoteVolume": "3500000", "volume": "1"},
            {"symbol": "LOWUSDT", "quoteVolume": "2999999.99", "volume": "999999999"},
            {"symbol": "BADUSDT", "quoteVolume": "nan", "volume": "5"},
            {"symbol": "BTCUSDT_PERP", "quoteVolume": "9000000000"},
        ]
        rows = build_snapshot_rows(
            info, ticker, collected_at=collection_timestamp(collected)
        )
        self.assertEqual([row[0] for row in rows], ["HIGHUSDT", "MIDUSDT"])
        self.assertNotIn("BTCUSDT_PERP", [row[0] for row in rows])

        def _fetch(url: str) -> object:
            if url.endswith("/api/v3/exchangeInfo"):
                return info
            if url.endswith("/api/v3/ticker/24hr"):
                return ticker
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = root / "exports" / "binance_spot_universe.db"
            mirror = root / "exports" / "universe.json"
            universe.parent.mkdir(parents=True, exist_ok=True)
            prior = sqlite3.connect(universe)
            prior.execute(
                """
                CREATE TABLE symbols (
                    symbol TEXT PRIMARY KEY,
                    quoteVolume_24h REAL,
                    quoteVolume_collected_at TEXT
                )
                """
            )
            prior.execute(
                "INSERT INTO symbols VALUES ('OLDUSDT', 8000000, '2026-11-06T08:00:00Z')"
            )
            prior.commit()
            prior.close()
            def _boom(_url: str) -> object:
                raise RuntimeError("down")

            failed = refresh_universe(
                universe,
                fetch_json=_boom,
                collected_at=collected,
            )
            self.assertFalse(failed["wrote"])
            still = sqlite3.connect(universe)
            kept = still.execute(
                "SELECT quoteVolume_24h, quoteVolume_collected_at FROM symbols WHERE symbol='OLDUSDT'"
            ).fetchone()
            still.close()
            self.assertEqual(tuple(kept), (8000000, "2026-11-06T08:00:00Z"))

            result = refresh_universe(
                universe,
                fetch_json=_fetch,
                collected_at=collected,
                json_path=mirror,
            )
            self.assertTrue(result["wrote"])
            self.assertEqual(result["collected_at"], collection_timestamp(collected))
            conn = sqlite3.connect(universe)
            stored = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT symbol, quoteVolume_24h, quoteVolume_collected_at FROM symbols"
                )
            }
            conn.close()
            self.assertEqual(
                stored["HIGHUSDT"],
                (4000000.0, collection_timestamp(collected)),
            )
            self.assertNotIn("OLDUSDT", stored)
            self.assertNotIn("BADUSDT", stored)
            body = json.loads(mirror.read_text(encoding="utf-8"))
            self.assertIn(COLLECTED_AT_COLUMN, body[0])
            self.assertIn(QUOTE_COLUMN, body[0])

            decision = root / "dexscreener.db"
            self._write_indicator(
                decision,
                [
                    ("HIGHUSDT", "HIGH", 2.0),
                    ("MIDUSDT", "MID", 1.0),
                    ("LOWUSDT", "LOW", 0.4),
                    ("OLDUSDT", "OLD", 9.0),
                ],
            )
            os.environ["BINANCE_SPOT_UNIVERSE_DB"] = str(universe)
            rows_out, payload = self._tick(decision, PURPOSE_LIVE_BNB_BOLL_CLASSIC)
            self.assertEqual(
                payload["live_bnb_top_buys"]["selected"],
                ["HIGHUSDT", "MIDUSDT"],
            )
            self.assertEqual(
                {row["token_address"] for row in rows_out},
                {"HIGHUSDT", "MIDUSDT"},
            )
            extras = json.loads(rows_out[0]["raw_json"])["extras"]
            self.assertEqual(extras["quote_turnover_24h_source"], SOURCE_UNIVERSE)
            self.assertNotIn("OLDUSDT", {row["token_address"] for row in rows_out})
            self.assertNotIn("LOWUSDT", payload["live_bnb_top_buys"]["selected"])

    def test_second_refresh_moves_quote_and_timestamp_together(self) -> None:
        first_at = datetime.now(timezone.utc) - timedelta(minutes=20)
        second_at = datetime.now(timezone.utc)
        info = {
            "symbols": [
                {"symbol": "HIGHUSDT", "baseAsset": "HIGH", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
                {"symbol": "GONEUSDT", "baseAsset": "GONE", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
            ]
        }
        first_ticker = [
            {"symbol": "HIGHUSDT", "quoteVolume": "4000000"},
            {"symbol": "GONEUSDT", "quoteVolume": "5000000"},
        ]
        second_info = {
            "symbols": [
                {"symbol": "HIGHUSDT", "baseAsset": "HIGH", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
            ]
        }
        second_ticker = [{"symbol": "HIGHUSDT", "quoteVolume": "7000000"}]

        def _fetch_first(url: str) -> object:
            if url.endswith("/api/v3/exchangeInfo"):
                return info
            if url.endswith("/api/v3/ticker/24hr"):
                return first_ticker
            raise AssertionError(url)

        def _fetch_second(url: str) -> object:
            if url.endswith("/api/v3/exchangeInfo"):
                return second_info
            if url.endswith("/api/v3/ticker/24hr"):
                return second_ticker
            raise AssertionError(url)

        def _boom(_url: str) -> object:
            raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as tmp:
            universe = Path(tmp) / "binance_spot_universe.db"
            first = refresh_universe(
                universe, fetch_json=_fetch_first, collected_at=first_at
            )
            self.assertTrue(first["wrote"])
            failed = refresh_universe(
                universe, fetch_json=_boom, collected_at=second_at
            )
            self.assertFalse(failed["wrote"])
            conn = sqlite3.connect(universe)
            after_fail = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT symbol, quoteVolume_24h, quoteVolume_collected_at FROM symbols"
                )
            }
            conn.close()
            first_stamp = collection_timestamp(first_at)
            self.assertEqual(after_fail["HIGHUSDT"], (4000000.0, first_stamp))
            self.assertEqual(after_fail["GONEUSDT"], (5000000.0, first_stamp))
            second = refresh_universe(
                universe, fetch_json=_fetch_second, collected_at=second_at
            )
            self.assertTrue(second["wrote"])
            conn = sqlite3.connect(universe)
            cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(symbols)")
            }
            after = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT symbol, quoteVolume_24h, quoteVolume_collected_at FROM symbols"
                )
            }
            conn.close()
            second_stamp = collection_timestamp(second_at)
            self.assertIn("quoteVolume_collected_at", cols)
            self.assertEqual(after, {"HIGHUSDT": (7000000.0, second_stamp)})
            self.assertNotEqual(after["HIGHUSDT"][1], first_stamp)
            self.assertNotIn((7000000.0, first_stamp), after.values())
            self.assertNotIn((4000000.0, second_stamp), after.values())

    def test_repo_kline_entry_does_not_rebuild_symbols(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertFalse((root / "crawler" / "scripts" / "refresh_binance_spot_universe.py").is_file())
        self.assertFalse((root / "refresh_binance_spot_universe.py").is_file())
        kline = (root / "crawler" / "indicators" / "binance_spot.py").read_text(encoding="utf-8")
        self.assertNotIn("DROP TABLE symbols", kline)
        self.assertNotIn("quoteVolume_collected_at", kline)
        loop = (root / "crawler" / "scripts" / "run_binance_spot_loop.sh").read_text(encoding="utf-8")
        self.assertNotIn("binance_universe_quote", loop)
        self.assertNotIn("refresh_binance_spot_universe", loop)
        script = root / "crawler" / "scripts" / "run_binance_universe_quote_loop.sh"
        env = os.environ.copy()
        env["BINANCE_INDICATORS_FRESH_SEC"] = "3600"
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--work-root",
                "/workspace/dexscreener-crawler",
                "--python",
                "/workspace/dexscreener-crawler/.venv/bin/python",
                "--db",
                "/workspace/dexscreener-crawler/exports/binance_spot_universe.db",
                "--interval",
                "3600",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("interval", proc.stderr)

    def test_floor_boundary_mirrors_and_production_layout(self) -> None:
        self.assertEqual(collector_floor(), min_quote_volume_usdt())
        spot = {
            "symbol": "EDGEUSDT",
            "baseAsset": "EDGE",
            "quoteAsset": "USDT",
            "status": "TRADING",
            "isSpotTradingAllowed": True,
        }
        below = dict(spot, symbol="TINYUSDT", baseAsset="TINY")
        hidden = dict(spot, symbol="NOSPOTUSDT", baseAsset="NOSPOT")
        hidden.pop("isSpotTradingAllowed")
        info = {"symbols": [spot, below, hidden]}
        ticker = [
            {"symbol": "EDGEUSDT", "quoteVolume": "3000000"},
            {"symbol": "TINYUSDT", "quoteVolume": "2999999.99"},
            {"symbol": "NOSPOTUSDT", "quoteVolume": "9000000"},
            {"symbol": "EDGEUSDT", "quoteVolume": "inf"},
        ]
        # Last EDGE row is illegal and must not replace the exact 3M print.
        # ticker_quote_volume keeps the last finite value only if it parses;
        # inf is omitted, so the earlier 3000000 remains.
        built = build_snapshot_rows(info, ticker, collected_at="2026-11-06T09:00:00Z")
        self.assertEqual([row[0] for row in built], ["EDGEUSDT"])
        self.assertEqual(built[0][4], 3_000_000.0)

        def _fetch(url: str) -> object:
            if url.endswith("/api/v3/exchangeInfo"):
                return info
            if url.endswith("/api/v3/ticker/24hr"):
                return [
                    {"symbol": "EDGEUSDT", "quoteVolume": "3000000"},
                    {"symbol": "TINYUSDT", "quoteVolume": "2999999.99"},
                    {"symbol": "GHOSTUSDT", "quoteVolume": None},
                ]
            raise AssertionError(url)

        collected = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            primary = root / "exports" / "binance_spot_universe.db"
            mirror_a = root / "crawler" / "exports" / "binance_spot_universe.db"
            mirror_b = root / "other" / "binance_spot_universe.db"
            report = root / "exports" / "reports" / "universe.json"
            for path in (primary, mirror_a, mirror_b):
                path.parent.mkdir(parents=True, exist_ok=True)
            old = sqlite3.connect(mirror_a)
            old.execute(
                "CREATE TABLE symbols (symbol TEXT, quoteVolume_24h REAL, quoteVolume_collected_at TEXT)"
            )
            old.execute(
                "INSERT INTO symbols VALUES ('OLDUSDT', 8000000, '2026-11-06T08:00:00Z')"
            )
            old.commit()
            old.close()
            mirror_b.write_bytes(mirror_a.read_bytes())

            def _boom(_url: str) -> object:
                raise RuntimeError("down")

            failed = refresh_universe(
                primary,
                fetch_json=_boom,
                collected_at=collected,
                json_path=report,
                mirrors=(mirror_a, mirror_b),
            )
            self.assertFalse(failed["wrote"])
            self.assertFalse(primary.exists())
            self.assertFalse(report.exists())
            kept = sqlite3.connect(mirror_a)
            self.assertEqual(
                kept.execute("SELECT symbol, quoteVolume_collected_at FROM symbols").fetchone(),
                ("OLDUSDT", "2026-11-06T08:00:00Z"),
            )
            kept.close()

            result = refresh_universe(
                primary,
                fetch_json=_fetch,
                collected_at=collected,
                json_path=report,
                mirrors=(mirror_a, mirror_b),
            )
            self.assertTrue(result["wrote"])
            self.assertEqual(result["symbols"], 1)
            stamp = collection_timestamp(collected)

            def _names(path: Path) -> list[tuple[str, float, str]]:
                conn = sqlite3.connect(path)
                found = conn.execute(
                    "SELECT symbol, quoteVolume_24h, quoteVolume_collected_at FROM symbols"
                ).fetchall()
                conn.close()
                return [(row[0], row[1], row[2]) for row in found]

            self.assertEqual(_names(primary), [("EDGEUSDT", 3_000_000.0, stamp)])
            self.assertEqual(_names(mirror_a), _names(primary))
            self.assertEqual(_names(mirror_b), _names(primary))
            body = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(body[0]["symbol"], "EDGEUSDT")
            self.assertEqual(body[0]["quoteVolume_24h"], 3_000_000.0)
            self.assertEqual(body[0]["quoteVolume_collected_at"], stamp)
            self.assertNotIn("TINYUSDT", {row["symbol"] for row in body})

        script = (
            Path(__file__).resolve().parents[2]
            / "crawler"
            / "scripts"
            / "run_binance_universe_quote_loop.sh"
        )
        proc = subprocess.run(
            [
                "bash",
                str(script),
                "--dry-run",
                "--work-root",
                "/workspace/dexscreener-crawler",
                "--python",
                "/workspace/dexscreener-crawler/.venv/bin/python",
                "--db",
                "/workspace/dexscreener-crawler/exports/binance_spot_universe.db",
                "--json",
                "/workspace/dexscreener-crawler/exports/reports/binance_spot_usdt_trading_2026-11-05.json",
                "--mirror",
                "/workspace/dexscreener-crawler/crawler/exports/binance_spot_universe.db",
                "--mirror",
                "/workspace/exports/binance_spot_universe.db",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("work_root=/workspace/dexscreener-crawler", proc.stdout)
        self.assertIn("python=/workspace/dexscreener-crawler/.venv/bin/python", proc.stdout)
        self.assertIn(
            "db=/workspace/dexscreener-crawler/exports/binance_spot_universe.db",
            proc.stdout,
        )
        self.assertIn("mirror=/workspace/exports/binance_spot_universe.db", proc.stdout)
        self.assertNotIn("python=crawler/.venv", proc.stdout)
        missing = subprocess.run(
            ["bash", str(script), "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(missing.returncode, 2)

    def _write_indicator(
        self,
        db: Path,
        names: list[tuple[str, str, float]] | None = None,
    ) -> None:
        if names is None:
            names = [("KEEPUSDT", "KEEP", 1.0)]
        now = _iso(datetime.now(timezone.utc))
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE binance_indicators (
                symbol TEXT PRIMARY KEY,
                base_asset TEXT,
                quote_asset TEXT,
                status TEXT,
                price_usd REAL,
                change_5m REAL,
                volume_strength REAL,
                boll_lower REAL,
                quote_volume_24h REAL,
                tech_updated_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO binance_indicators VALUES (
                ?, ?, 'USDT', 'TRADING', 0.8, -4, ?, 1.0, 90000000, ?, ?
            )
            """,
            [(symbol, base, strength, now, now) for symbol, base, strength in names],
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main()
