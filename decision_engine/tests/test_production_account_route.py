"""Production decision construction copies a route only when one is given."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import PURPOSE_LIVE_CANDIDATE, SignalBus
from decision_engine.core.token_cooldown import TokenCooldown
from decision_engine.run import build_production_engine, load_explicit_account_route
from decision_engine.tests.quiet_macro import write_quiet_macro


class _OneBuy:
    strategy_id = "probe_buy"

    def on_tick(self, _data: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "strategy_id": self.strategy_id,
                "action": "buy",
                "chain_id": "solana",
                "token_address": "Mint",
                "pair_address": "Pair",
                "size_pct": 0.1,
                "liquidity_usd": 1_000_000,
                "reason": "probe",
            }
        ]


class ProductionAccountRouteTests(unittest.TestCase):
    def test_missing_config_is_not_a_route_and_a_bad_file_fails_closed(self) -> None:
        self.assertEqual(load_explicit_account_route(None), (None, None))
        self.assertEqual(load_explicit_account_route("  "), (None, None))
        route, error = load_explicit_account_route("/tmp/does-not-exist-account-route.json")
        self.assertIsNone(route)
        self.assertIn("unreadable", error or "")
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.json"
            empty.write_text("[]", encoding="utf-8")
            route, error = load_explicit_account_route(str(empty))
            self.assertIsNone(route)
            self.assertIn("non-empty object", error or "")
            given = {"account_domain": "binance_cross_margin", "spot_book_full": False}
            path = Path(tmp) / "route.json"
            path.write_text(json.dumps(given), encoding="utf-8")
            loaded, error = load_explicit_account_route(str(path))
            self.assertIsNone(error)
            self.assertEqual(loaded, given)

    def test_production_builder_omits_the_route_unless_the_caller_supplies_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = self._publish(root / "plain.db", None)
            routed_path = root / "route.json"
            given = {
                "account_domain": "binance_cross_margin",
                "spot_book_full": False,
                "execution_mode": "offline_fixture",
            }
            routed_path.write_text(json.dumps(given), encoding="utf-8")
            loaded, error = load_explicit_account_route(str(routed_path))
            self.assertIsNone(error)
            routed = self._publish(root / "routed.db", loaded)
        self.assertNotIn("account_domain", plain)
        self.assertEqual(routed["account_domain"], "binance_cross_margin")
        self.assertIs(routed["spot_book_full"], False)
        self.assertEqual(routed["execution_mode"], "offline_fixture")
        self.assertNotIn("offline_approval", routed)
        self.assertNotIn("max_leverage", routed)

    def _publish(self, db: Path, account_route: dict[str, Any] | None) -> dict[str, Any]:
        adapter = IndicatorDataAdapter(db)
        bus = SignalBus(db, purpose=PURPOSE_LIVE_CANDIDATE)
        try:
            payload = build_production_engine(
                adapter,
                bus,
                [_OneBuy()],
                risk=RiskManager(cooldown_sec=0),
                hot_limit=10,
                token_cooldown=TokenCooldown(0),
                entry_ih_min_hours=None,
                account_route=account_route,
                macro_db=write_quiet_macro(db.with_name(f"{db.stem}-macro.db")),
            ).tick()
        finally:
            bus.close()
            adapter.close()
        self.assertEqual(payload["emitted"], 1)
        conn = sqlite3.connect(db)
        raw = json.loads(conn.execute("SELECT raw_json FROM decision_log").fetchone()[0])
        conn.close()
        return dict(raw.get("extras") or {})


if __name__ == "__main__":
    unittest.main()
