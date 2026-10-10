"""Tick loop: load indicators → strategies → risk → signal bus."""

from __future__ import annotations

import time
from typing import Any

from logging_setup import get_logger

from decision_engine.core.data_adapter import IndicatorDataAdapter
from decision_engine.core.risk_manager import RiskManager
from decision_engine.core.signal_bus import SignalBus
from decision_engine.strategies.base_strategy import BaseStrategy

log = get_logger("decision_engine.engine")


class DecisionEngine:
    def __init__(
        self,
        adapter: IndicatorDataAdapter,
        bus: SignalBus,
        strategies: list[BaseStrategy],
        *,
        risk: RiskManager | None = None,
        hot_limit: int = 50,
    ):
        self.adapter = adapter
        self.bus = bus
        self.strategies = strategies
        self.risk = risk or RiskManager()
        self.hot_limit = hot_limit
        self.tick_count = 0

    def tick(self) -> dict[str, Any]:
        self.tick_count += 1
        n_ind = self.adapter.count_indicators()
        hot = self.adapter.list_hot_tokens(limit=self.hot_limit)
        log.info(
            "tick#%s indicators_total=%s hot_loaded=%s strategies=%s",
            self.tick_count,
            n_ind,
            len(hot),
            [s.strategy_id for s in self.strategies],
        )
        data = {
            "hot_tokens": hot,
            "indicators_total": n_ind,
            "tick": self.tick_count,
            "adapter": self.adapter,
        }
        candidates: list[dict[str, Any]] = []
        for strat in self.strategies:
            try:
                out = strat.on_tick(data) or []
                candidates.extend(out)
            except Exception as exc:  # noqa: BLE001
                log.exception("strategy %s failed: %s", strat.strategy_id, exc)

        if not candidates:
            log.info("tick#%s 无信号 (no candidates)", self.tick_count)
            return {
                "tick": self.tick_count,
                "hot_loaded": len(hot),
                "indicators_total": n_ind,
                "candidates": 0,
                "emitted": 0,
            }

        passed = self.risk.filter(candidates)
        emitted = self.bus.publish_many(passed)
        log.info(
            "tick#%s candidates=%s emitted=%s",
            self.tick_count,
            len(candidates),
            len(emitted),
        )
        return {
            "tick": self.tick_count,
            "hot_loaded": len(hot),
            "indicators_total": n_ind,
            "candidates": len(candidates),
            "emitted": len(emitted),
        }

    def run(
        self,
        *,
        once: bool = False,
        ticks: int | None = None,
        interval_sec: float = 60.0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if once:
            ticks = 1
        if ticks is not None and ticks <= 0:
            return results

        n = 0
        while True:
            try:
                results.append(self.tick())
            except Exception as exc:  # noqa: BLE001
                log.exception("tick crashed: %s", exc)
                results.append({"tick": self.tick_count, "error": str(exc)})
            n += 1
            if ticks is not None and n >= ticks:
                break
            time.sleep(max(1.0, float(interval_sec)))
        return results
