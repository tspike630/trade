"""NY cash session and the ruled cash-long exposure caps."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

from decision_engine.core.cityu_institutional_alpha import lseg_us_cash_market_open
from decision_engine.core.regime_v2_policy import cross_margin_emit_clock_gap
from decision_engine.core.token_cooldown import TokenCooldown
from decision_engine.strategies.regime_cross_margin import stamp_cross_margin_classifier_inputs
from execution.margin_cross.strategy_gate import (
    CASH_BASE_POSITION_PCT,
    _exposure_rooms,
    verified_empty_exposure,
)


def _ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    moment = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))
    return int(moment.timestamp() * 1000)


class CashSessionTests(unittest.TestCase):
    def test_weekend_monday_morning_boundary_holiday_and_unknown_year(self) -> None:
        sunday = _ms(2026, 11, 22, 18, 0)
        monday_early = _ms(2026, 11, 23, 9, 29)
        monday_open = _ms(2026, 11, 23, 9, 30)
        monday_close = _ms(2026, 11, 23, 16, 0)
        thanksgiving = _ms(2026, 11, 26, 10, 0)
        unknown_year = _ms(2027, 1, 4, 10, 0)
        self.assertIs(lseg_us_cash_market_open(sunday), False)
        self.assertIs(lseg_us_cash_market_open(monday_early), False)
        self.assertIs(lseg_us_cash_market_open(monday_open), True)
        self.assertIs(lseg_us_cash_market_open(monday_close), False)
        self.assertIs(lseg_us_cash_market_open(thanksgiving), False)
        self.assertIsNone(lseg_us_cash_market_open(unknown_year))

    def test_unknown_session_does_not_stamp_the_flag_open(self) -> None:
        rows = stamp_cross_margin_classifier_inputs(
            [{"binance_symbol": "BTCUSDT"}],
            now_ms=_ms(2027, 1, 4, 10, 0),
            snap=object(),
        )
        self.assertNotIn("lseg_market_open", rows[0])
        clocks = {
            "indicators_ts_ms": 1,
            "obi_top5_source_ts_ms": 1,
            "perp_counterparty_ts_ms": 1,
            "deribit_gamma_ts_ms": 1,
            "volume_z_source_close_ms": 1,
            "news_fetched_at_ms": 1,
            "cot_asof_date": "2026-11-10",
        }
        self.assertEqual(
            cross_margin_emit_clock_gap(clocks, now_ms=1_795_171_200_000),
            "lseg_cross_asset_ts_ms",
        )

    def test_future_clock_is_not_published_and_not_cooled(self) -> None:
        now = 1_700_000_000_000
        clocks = {
            "indicators_ts_ms": now - 1,
            "obi_top5_source_ts_ms": now + 3_600_000,
            "perp_counterparty_ts_ms": now - 1,
            "deribit_gamma_ts_ms": now - 1,
            "volume_z_source_close_ms": now - 1,
            "news_fetched_at_ms": now - 1,
            "cot_asof_date": "2026-11-10",
            "lseg_market_open": False,
        }
        signal = {
            "action": "buy",
            "chain_id": "binance",
            "token_address": "MMTUSDT",
            "strategy_id": "tech_boll_fade_v1",
            "clocks": clocks,
        }
        from decision_engine.core.regime_v2_policy import drop_cross_margin_signals_missing_clocks

        kept = drop_cross_margin_signals_missing_clocks([signal], now_ms=now)
        self.assertEqual(kept, [])
        cooldown = TokenCooldown(60)
        cooldown.commit_published(kept)
        self.assertEqual(cooldown._state, {})

    def test_cash_base_is_the_ruled_fraction_and_caps_are_not_targets(self) -> None:
        net = Decimal("287.73135835")
        base = net * CASH_BASE_POSITION_PCT * Decimal("1") * Decimal("1.20")
        self.assertEqual(base, net * Decimal("0.288"))
        rooms = _exposure_rooms(
            verified_empty_exposure(), net_asset_q=net, is_state4=False
        )
        self.assertIsInstance(rooms, tuple)
        single_room, total_room = rooms
        self.assertEqual(single_room, net * Decimal("0.30"))
        self.assertEqual(total_room, net * Decimal("0.72"))
        self.assertLess(base, single_room)
        self.assertEqual(_exposure_rooms(None, net_asset_q=net, is_state4=False), "exposure_unverified")
        state4 = _exposure_rooms(
            {"verified": True, "gross_notional_quote": "0", "symbol_notional_quote": "0",
             "open_count": 0, "state4_open_count": 1, "cluster_open_count": 0},
            net_asset_q=net,
            is_state4=True,
        )
        self.assertEqual(state4, "state4_exposure_cap")


if __name__ == "__main__":
    unittest.main()
