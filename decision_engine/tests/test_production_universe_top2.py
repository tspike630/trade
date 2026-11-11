"""Production buy gates on a real DecisionEngine tick.

Synthetic indicator and ticker rows are labeled by the assertions. The 1h
bars are synthetic Binance-shaped fixtures, not a copy of the production
database. Defaults stay at Top 2 and the 3_000_000 USDT floor. ZAMAUSDT and
BMNRUSDT are outside the fixture universe and must not be emitted.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import decision_engine  # noqa: F401
from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.engine import DecisionEngine
from decision_engine.core.live_bnb_turnover import min_quote_volume_usdt, top_buys_per_tick
from decision_engine.core.protection_halt import ProtectionBook
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import (
    PURPOSE_LIVE_BNB_BOLL_CLASSIC,
    PURPOSE_LIVE_BNB_BOLL_FADE,
    SignalBus,
)
from decision_engine.strategies import get_strategy
from execution.executors.binance_spot_protect import resolve_entry_size

_HOUR = 3_600_000
_SPAN = _HOUR - 1
_UNIVERSE = ("ADAUSDT", "ATOMUSDT", "NEARUSDT", "DOTUSDT", "ALGOUSDT")
_OUTSIDE = ("ZAMAUSDT", "BMNRUSDT")
_HELD_ON_FADE = "ATOMUSDT"
_LOW_TURNOVER = "ALGOUSDT"
_HIGH_Z = "ADAUSDT"
_STRENGTH = {
    "ADAUSDT": 0.99,
    "ATOMUSDT": 0.95,
    "NEARUSDT": 0.90,
    "DOTUSDT": 0.80,
    "ALGOUSDT": 0.99,
    "ZAMAUSDT": 0.99,
    "BMNRUSDT": 0.99,
}


def _bars(symbol: str, close_ms: int, last_quote: float) -> list[tuple]:
    rows = []
    for index in range(21):
        opened = close_ms - _SPAN - (20 - index) * _HOUR
        if index < 19:
            quote = 100.0
        elif index == 19:
            quote = 160.0
        else:
            quote = last_quote
        rows.append((symbol, "1h", opened, opened + _SPAN, quote))
    return rows


def _build(path: Path, now_ms: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE binance_klines (
            symbol TEXT, interval TEXT, open_time_ms INTEGER,
            close_time_ms INTEGER, quote_volume REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE binance_indicators (
            symbol TEXT PRIMARY KEY, base_asset TEXT, quote_asset TEXT, status TEXT,
            price_usd REAL, change_5m REAL, volume_strength REAL,
            boll_lower REAL, boll_mid REAL, kline_low REAL,
            trade_size_ratio REAL, quote_volume_24h REAL,
            xs_breadth_1h REAL, xs_rank_pct_1h REAL
        )
        """
    )
    conn.execute(
        "CREATE TABLE symbols (symbol TEXT, status TEXT, base_asset TEXT, quote_asset TEXT)"
    )
    conn.execute(
        """
        CREATE TABLE ccxt_ticks (
            exchange TEXT, symbol TEXT, ts INTEGER,
            quote_volume_24h REAL, volume_24h REAL
        )
        """
    )
    members = set(_UNIVERSE)
    for symbol in _UNIVERSE + _OUTSIDE:
        base = symbol[:-4]
        price = 100.8
        conn.execute(
            """
            INSERT INTO binance_indicators VALUES (
                ?, ?, 'USDT', 'TRADING', ?, -4.0, ?, ?, 110.0, 100.0, 1.3, ?, 0.75, 0.90
            )
            """,
            (symbol, base, price, _STRENGTH[symbol], price + 1.0, 80_000.0),
        )
        if symbol in members:
            conn.execute(
                "INSERT INTO symbols VALUES (?, 'TRADING', ?, 'USDT')",
                (symbol, base),
            )
        quote = 1_000.0 if symbol == _LOW_TURNOVER else 3_000_000.0
        conn.execute(
            "INSERT INTO ccxt_ticks VALUES ('binance', ?, ?, ?, 1)",
            (f"{base}/USDT", now_ms, quote),
        )
        last = 1000.0 if symbol == _HIGH_Z else 130.0
        conn.executemany(
            "INSERT INTO binance_klines VALUES (?, ?, ?, ?, ?)",
            _bars(symbol, now_ms, last),
        )
    conn.commit()
    conn.close()


def _book(purpose: str) -> ProtectionBook:
    canonical = []
    if purpose == PURPOSE_LIVE_BNB_BOLL_FADE:
        canonical.append(
            {
                "purpose": purpose,
                "symbol": _HELD_ON_FADE,
                "status": "oco_working",
                "entry_order_id": "100",
                "protection": "exchange_oco",
                "oco_order_list_id": "1",
                "oco_above_order_id": "2",
                "oco_below_order_id": "3",
            }
        )
    return ProtectionBook.ready(canonical, canonical)


class ProductionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in (
                "LIVE_BNB_TOP_BUYS",
                "BINANCE_SPOT_MIN_QUOTE_VOLUME",
                "LIVE_BNB_CLUSTER_CAP_ENABLED",
                "BINANCE_SPOT_BOOK_USDT",
                "LIVE_BNB_FACTOR_SOURCE",
                "LIVE_BNB_IGNORE_REPO_UNIVERSE",
                "BINANCE_SPOT_UNIVERSE_DB",
                "BINANCE_SPOT_UNIVERSE_JSON",
            )
        }
        for key in self._saved:
            os.environ.pop(key, None)
        os.environ["BINANCE_SPOT_BOOK_USDT"] = "400"
        os.environ["LIVE_BNB_IGNORE_REPO_UNIVERSE"] = "1"

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_defaults_are_top2_and_three_million(self) -> None:
        self.assertEqual(top_buys_per_tick(), 2)
        self.assertEqual(min_quote_volume_usdt(), 3_000_000.0)

    def _tick(self, purpose: str) -> list[str]:
        now_ms = int(time.time() * 1000)
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "book.db"
            deriv = Path(tmp) / "deriv.db"
            _build(db, now_ms)
            deriv_conn = sqlite3.connect(deriv)
            deriv_conn.execute(
                """
                CREATE TABLE binance_perp_counterparty (
                    symbol TEXT, ts_ms INTEGER, whale_retail_divergence REAL
                )
                """
            )
            deriv_conn.execute(
                "INSERT INTO binance_perp_counterparty VALUES (?, ?, 0.55)",
                (_HIGH_Z, now_ms),
            )
            deriv_conn.commit()
            deriv_conn.close()
            old_deriv = os.environ.get("PUBLIC_DERIVATIVES_DB_PATH")
            os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = str(deriv)
            adapter = IndicatorDataAdapter(db)
            bus = SignalBus(db, purpose=purpose)
            try:
                engine = DecisionEngine(
                    adapter,
                    bus,
                    [get_strategy("tech_boll_fade_v1")],
                    risk=RiskManager(cooldown_sec=0),
                    hot_limit=20,
                    protection_book=_book(purpose),
                    macro_db=None,
                )
                payload = engine.tick()
                symbols = [str(item.get("token_address") or "") for item in bus.emitted]
                self.assertEqual(payload.get("emitted"), len(symbols))
                top = payload.get("live_bnb_top_buys") or {}
                self.assertEqual(top.get("limit"), 2)
                stored = sqlite3.connect(db).execute(
                    "SELECT COUNT(*) FROM decision_volume_z"
                ).fetchone()
                self.assertEqual(int(stored[0]), len(_UNIVERSE) + len(_OUTSIDE))
            finally:
                bus.close()
                adapter.close()
                if old_deriv is None:
                    os.environ.pop("PUBLIC_DERIVATIVES_DB_PATH", None)
                else:
                    os.environ["PUBLIC_DERIVATIVES_DB_PATH"] = old_deriv
        return symbols

    def test_both_arms_keep_universe_top2_floor_and_ledger(self) -> None:
        for purpose in (PURPOSE_LIVE_BNB_BOLL_FADE, PURPOSE_LIVE_BNB_BOLL_CLASSIC):
            emitted = self._tick(purpose)
            self.assertLessEqual(len(emitted), 2)
            self.assertEqual(len(emitted), 2, emitted)
            self.assertTrue(set(emitted).issubset(set(_UNIVERSE) - {_LOW_TURNOVER, _HIGH_Z}))
            self.assertNotIn("ZAMAUSDT", emitted)
            self.assertNotIn("BMNRUSDT", emitted)
            self.assertNotIn(_LOW_TURNOVER, emitted)
            self.assertNotIn(_HIGH_Z, emitted)
            if purpose == PURPOSE_LIVE_BNB_BOLL_FADE:
                self.assertNotIn(_HELD_ON_FADE, emitted)
            else:
                self.assertIn(_HELD_ON_FADE, emitted)
            sized, err, plan = resolve_entry_size(
                {"action": "buy", "size_pct": 0.05},
                {"order_type": "MARKET"},
                environ={"BINANCE_SPOT_BOOK_USDT": "400"},
            )
            self.assertIsNone(err)
            assert plan is not None
            self.assertEqual(plan["source"], "size_pct")
            self.assertEqual(sized["quote_order_qty"], "20")


if __name__ == "__main__":
    unittest.main()
