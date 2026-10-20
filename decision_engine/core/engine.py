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
        token_cooldown: Any = None,
    ):
        self.adapter = adapter
        self.bus = bus
        self.strategies = strategies
        self.risk = risk or RiskManager()
        self.hot_limit = hot_limit
        self.token_cooldown = token_cooldown
        self.tick_count = 0
        resolved = getattr(adapter, "db_path", None)
        if resolved is not None:
            log.info("resolved DB_PATH=%s", resolved)

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

        # Shared buy choke point: every strategy's candidates pass through
        # RiskManager before SignalBus.emit. Liquidity comes from the scanned
        # rows (adapter batch lookup), so tech/combined/jev do not each filter.
        # Also stamp pairCreatedAt (ms) for decision_log extras / token_age.
        self._stamp_buy_liquidity(candidates, hot)
        passed = self.risk.filter(candidates)
        if self.token_cooldown is not None:
            passed = self.token_cooldown.filter(passed)
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

    def _stamp_buy_liquidity(
        self,
        candidates: list[dict[str, Any]],
        hot: list[dict[str, Any]],
    ) -> None:
        """Copy scanned-row liquidity onto buys; stamp pairCreatedAt on all actions.

        Liquidity: Missing row → leave unset (fail closed for the buy floor).
        pairCreatedAt: ms epoch from pairs.pair_created_at when the hot row has
        it. Does not invent values; SignalBus may still look up pairs at write
        time if this stamp is absent. No gate / weight change.
        """
        by_token: dict[tuple[str, str], dict[str, Any]] = {}
        for row in hot:
            key = (
                str(row.get("chain_id") or ""),
                str(row.get("token_address") or ""),
            )
            meta: dict[str, Any] = {"liquidity_usd": row.get("liquidity_usd")}
            if row.get("pair_created_at") is not None:
                meta["pair_created_at"] = row.get("pair_created_at")
            by_token[key] = meta
        for sig in candidates:
            key = (
                str(sig.get("chain_id") or ""),
                str(sig.get("token_address") or ""),
            )
            meta = by_token.get(key)
            if meta is None:
                continue
            if str(sig.get("action") or "").lower() == "buy":
                sig["liquidity_usd"] = meta.get("liquidity_usd")
            # Attribution reads extras.pairCreatedAt (ms). Prefer camelCase once.
            if (
                sig.get("pairCreatedAt") is None
                and sig.get("pair_created_at") is None
                and meta.get("pair_created_at") is not None
            ):
                try:
                    sig["pairCreatedAt"] = int(meta["pair_created_at"])
                except (TypeError, ValueError):
                    pass

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
